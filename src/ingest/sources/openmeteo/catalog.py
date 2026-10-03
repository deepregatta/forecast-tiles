"""Complete-run metadata, cycle selection and source identity.

A run is ready when `data_run/<domain>/YYYY/MM/DD/HHMMZ/meta.json` lists its
required variables and full registered time axis. The marker can appear early
and be extended: ICON-EU 2026-10-03T00Z first omitted wind fields at 03:24,
then listed them at 03:41. Only `data_run/` is used; the rolling `data/` tree
is rewritten in place and has no immutable runs.

An explicit cycle means exactly that cycle. Without one, the newest complete
cycle among the product's registered hours within `lookback_cycles` is
chosen. Before publication the run's identity is checked again: meta.json's
ETag and each downloaded file's ETag and length must be unchanged, since a
stable completion marker alone does not prove the files were not rewritten.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ingest.sources.base import CycleNotAvailableError
from ingest.sources.openmeteo import reader
from ingest.sources.openmeteo.registry import Product
from ingest.sources.openmeteo.reader import NotFound, ObjectRecord, SourceError


class CycleNotRegistered(SourceError):
    """An explicit --cycle at an hour the product does not publish."""


class RunIncomplete(SourceError):
    """Required variables/times have not all appeared in the run metadata."""


@dataclass(frozen=True)
class RunMeta:
    cycle: datetime
    record: ObjectRecord
    reference_time: datetime
    valid_times: tuple[datetime, ...]
    variables: frozenset[str]
    created_at: str


def run_prefix(product: Product, cycle: datetime) -> str:
    return f"data_run/{product.domain}/{cycle:%Y/%m/%d/%H%M}Z/"


def file_key(product: Product, cycle: datetime, role: str) -> str:
    return run_prefix(product, cycle) + product.files[role] + ".om"


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def lead_times(product: Product, cycle: datetime) -> list[datetime]:
    return [cycle + timedelta(hours=h) for h in product.axes[cycle.hour]]


def fetch_meta(product: Product, cycle: datetime, *, session=None, sleep=time.sleep) -> RunMeta:
    """The run's completion metadata; CycleNotAvailableError until it exists."""
    key = run_prefix(product, cycle) + "meta.json"
    kwargs = {"sleep": sleep} | ({"session": session} if session is not None else {})
    try:
        doc, record = reader.get_json(key, **kwargs)
    except NotFound as exc:
        raise CycleNotAvailableError(
            f"{product.domain} {cycle:%Y-%m-%dT%HZ} not complete (no meta.json)"
        ) from exc
    if product.source_grid is not None:
        reader.validate_wkt(doc.get("crs_wkt", ""), product.source_grid, key)
    return RunMeta(
        cycle=cycle,
        record=record,
        reference_time=datetime.strptime(doc["reference_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ),
        valid_times=tuple(_parse_time(t) for t in doc["valid_times"]),
        variables=frozenset(doc["variables"]),
        created_at=doc.get("created_at", ""),
    )


def require_complete(product: Product, meta: RunMeta, roles: list[str]) -> None:
    """The run carries the registered files and the full registered axis."""
    cycle = meta.cycle
    if meta.reference_time != cycle:
        raise SourceError(
            f"{meta.record.key}: reference_time {meta.reference_time:%Y-%m-%dT%HZ} != {cycle:%Y-%m-%dT%HZ}"
        )
    missing = [product.files[r] for r in roles if product.files[r] not in meta.variables]
    if missing:
        raise RunIncomplete(f"{meta.record.key}: no {missing} in this run")
    absent = sorted(set(lead_times(product, cycle)) - set(meta.valid_times))
    if absent:
        leads = [int((t - cycle).total_seconds() // 3600) for t in absent]
        raise RunIncomplete(
            f"{meta.record.key}: valid_times lack +{leads[:5]} h of the registered "
            f"{len(product.axes[cycle.hour])}-step axis"
        )


def _require_ready(product: Product, meta: RunMeta) -> None:
    roles = ["speed", "direction"] if product.wind_encoding == "speed_direction" else ["u", "v"]
    if product.gust_windows.verified:
        roles.append("gust")
    try:
        require_complete(product, meta, roles)
    except RunIncomplete as exc:
        # Only incomplete inventory/axis is retryable. Wrong reference time,
        # geometry or later file identity still fails immediately.
        raise CycleNotAvailableError(str(exc)) from exc


def resolve(
    product: Product,
    requested: datetime | None = None,
    *,
    now: datetime | None = None,
    session=None,
) -> datetime:
    """The cycle to publish: exactly `requested`, or the newest complete one."""
    if requested is not None:
        if requested.hour not in product.cycles or requested.minute:
            raise CycleNotRegistered(
                f"{product.layer} publishes {product.cycles} UTC cycles, not {requested:%H}Z"
            )
        _require_ready(product, fetch_meta(product, requested, session=session))
        return requested
    now = now or datetime.now(timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    tried = []
    while len(tried) < product.lookback_cycles:
        if t.hour in product.cycles:
            tried.append(t)
            try:
                _require_ready(product, fetch_meta(product, t, session=session))
                return t
            except CycleNotAvailableError:
                pass
        t -= timedelta(hours=1)
    raise CycleNotAvailableError(
        f"no complete {product.domain} run among {[f'{c:%d %HZ}' for c in tried]}"
    )


def source_digest(records: list[ObjectRecord]) -> str:
    """Stable digest of the exact objects a run was built from."""
    h = hashlib.sha256()
    for r in sorted(records, key=lambda r: r.key):
        h.update(f"{r.key}\t{r.etag}\t{r.bytes}\n".encode())
    return h.hexdigest()[:16]


def recheck(meta_record: ObjectRecord, files: list[ObjectRecord], *, session=None) -> None:
    """Refuse a run whose metadata or files changed since they were read."""
    kwargs = {"session": session} if session is not None else {}
    _, now_meta = reader.get_json(meta_record.key, **kwargs)
    if now_meta.etag != meta_record.etag:
        raise SourceError(
            f"{meta_record.key} changed while the run was built ({meta_record.etag} -> {now_meta.etag})"
        )
    for record in files:
        current = reader.head(record.key, **kwargs)
        if (current.etag, current.bytes) != (record.etag, record.bytes):
            raise SourceError(
                f"{record.key} changed while the run was built "
                f"({record.etag}/{record.bytes} -> {current.etag}/{current.bytes})"
            )
