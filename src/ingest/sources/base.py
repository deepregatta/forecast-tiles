"""Shared source plumbing: HTTP with retries, GRIB .idx byte-range subsetting,
eccodes decoding with orientation normalization (ascending latitude, longitudes
rolled to [-180, 180)), cycle resolution by .idx presence, and parallel
step collection. Lifted from scripts/size_prototype.py (Phase 0)."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from ingest.cube import GridMeta, VariableSpec
from tilekit.codec import quantize

MS_TO_KT = 1.943844
DOWNLOAD_WORKERS = 16

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "deepregatta-forecast-tiles-ingest"


class CycleNotAvailableError(RuntimeError):
    """The requested (or any recent) provider cycle is not fully published."""


# ---------------------------------------------------------------- download


def http(url: str, *, headers: dict | None = None, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, headers=headers or {}, timeout=120)
            r.raise_for_status()
            return r.content
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def head_ok(url: str) -> bool:
    try:
        return SESSION.head(url, timeout=30).status_code == 200
    except Exception:
        return False


def parse_idx(idx_text: str) -> list[tuple[str, str, int, int | None]]:
    """Return (var, level, start, end_exclusive|None) per GRIB message."""
    rows = []
    lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        parts = ln.split(":")
        start = int(parts[1])
        end = int(lines[i + 1].split(":")[1]) if i + 1 < len(lines) else None
        rows.append((parts[3], parts[4], start, end))
    return rows


def fetch_fields(
    url: str,
    wanted: list[tuple[str, str]],
    optional: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset(),
) -> dict[tuple[str, str], bytes]:
    """Byte-range download of the wanted (var, level) GRIB messages of one file."""
    idx = parse_idx(http(url + ".idx").decode())
    out: dict[tuple[str, str], bytes] = {}
    for var, level in wanted:
        match = next(((s, e) for v, lv, s, e in idx if v == var and lv == level), None)
        if match is None:
            if (var, level) in optional:
                continue  # e.g. APCP has no accumulation at f000
            raise KeyError(f"{var}:{level} not in {url}.idx")
        start, end = match
        range_header = f"bytes={start}-" if end is None else f"bytes={start}-{end - 1}"
        out[(var, level)] = http(url, headers={"Range": range_header})
    return out


# ------------------------------------------------------------------ decode


def decode_field(msg: bytes) -> tuple[np.ndarray, GridMeta]:
    """GRIB message -> (values[lat_asc, lon_from_-180] float32 NaN-masked, grid meta)."""
    import eccodes

    gid = eccodes.codes_new_from_message(msg)
    try:
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        lat1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lon1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        di = eccodes.codes_get(gid, "iDirectionIncrementInDegrees")
        dj = eccodes.codes_get(gid, "jDirectionIncrementInDegrees")
        j_positive = eccodes.codes_get(gid, "jScansPositively")
        eccodes.codes_set(gid, "missingValue", 1.0e20)
        values = eccodes.codes_get_values(gid).reshape(nj, ni).astype(np.float32)
    finally:
        eccodes.codes_release(gid)
    values[values >= 9.0e19] = np.nan

    lat0 = lat1 if j_positive else lat1 - dj * (nj - 1)
    if not j_positive:
        values = values[::-1]
    lons = (lon1 + np.arange(ni) * di + 180.0) % 360.0 - 180.0
    roll = int(np.argmin(lons))  # index of the smallest lon -> becomes column 0
    values = np.roll(values, -roll, axis=1)
    lon0 = float(lons[roll])
    return values, GridMeta(lat0=float(lat0), lon0=lon0, dlat=dj, dlon=di, nlat=nj, nlon=ni)


# ------------------------------------------------------- cycle resolution


def floor_to_cycle(t: datetime, cadence_h: int = 6) -> datetime:
    return t.replace(hour=(t.hour // cadence_h) * cadence_h, minute=0, second=0, microsecond=0)


def resolve_cycle(
    url_template: str,
    requested: datetime | None = None,
    max_lookback_cycles: int = 8,
    cadence_h: int = 6,
) -> datetime:
    """Latest cycle whose final-step .idx exists (never a partial cycle);
    url_template has {date}/{hh} placeholders. A requested cycle is only
    checked, never substituted."""
    if requested is not None:
        url = url_template.format(date=requested.strftime("%Y%m%d"), hh=requested.strftime("%H"))
        if not head_ok(url):
            raise CycleNotAvailableError(
                f"requested cycle {requested:%Y%m%dT%H}Z incomplete: {url}"
            )
        return requested
    t = floor_to_cycle(datetime.now(timezone.utc), cadence_h)
    for _ in range(max_lookback_cycles):
        if head_ok(url_template.format(date=t.strftime("%Y%m%d"), hh=t.strftime("%H"))):
            return t
        t -= timedelta(hours=cadence_h)
    raise CycleNotAvailableError(f"no complete cycle found for {url_template}")


def parse_cycle_arg(arg: str) -> datetime:
    """Parse a --cycle YYYYMMDDTHH argument to an aware UTC datetime."""
    return datetime.strptime(arg, "%Y%m%dT%H").replace(tzinfo=timezone.utc)


# -------------------------------------------------------------- collection


@dataclass(frozen=True)
class SourceVar:
    """Internal per-source variable spec: GRIB identity + unit transforms."""

    name: str
    grib: tuple[str, str]  # (.idx var name, level string)
    axis: str
    dtype: str
    scale: float
    to_kt: bool = False
    k_to_c: bool = False
    optional: bool = False  # missing at some steps is expected (NaN-filled)

    def public(self) -> VariableSpec:
        return VariableSpec(name=self.name, axis=self.axis, dtype=self.dtype, scale=self.scale)


def transform_units(values: np.ndarray, var: SourceVar) -> np.ndarray:
    v = values * MS_TO_KT if var.to_kt else values
    return v - 273.15 if var.k_to_c else v


def quantize_field(values: np.ndarray, var: SourceVar) -> np.ndarray:
    return quantize(transform_units(values, var), var.dtype, var.scale)


def collect_steps(
    url_for_step,
    steps: list[int],
    var_specs: list[SourceVar],
    workers: int = DOWNLOAD_WORKERS,
) -> tuple[dict[str, np.ndarray], GridMeta]:
    """Download+decode+quantize all (step, var) fields -> arrays [time, nlat, nlon].

    Optional vars missing from a step's .idx are filled with the sentinel.
    """
    wanted = [v.grib for v in var_specs]
    optional = {v.grib for v in var_specs if v.optional}

    def one(step: int):
        return step, fetch_fields(url_for_step(step), wanted, optional)

    fields_by_step: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for step, fields in pool.map(one, steps):
            fields_by_step[step] = fields

    meta: GridMeta | None = None
    arrays: dict[str, list[np.ndarray | None]] = {v.name: [] for v in var_specs}
    pending_nan: list[tuple[str, int]] = []
    for step in steps:
        for var in var_specs:
            msg = fields_by_step[step].get(var.grib)
            if msg is None:
                pending_nan.append((var.name, len(arrays[var.name])))
                arrays[var.name].append(None)  # backfill once grid meta is known
                continue
            values, m = decode_field(msg)
            meta = meta or m
            arrays[var.name].append(quantize_field(values, var))
    if meta is None:
        raise RuntimeError("no fields decoded")
    for name, idx_pos in pending_nan:
        nan_field = np.full((meta.nlat, meta.nlon), np.nan, dtype=np.float32)
        var = next(v for v in var_specs if v.name == name)
        arrays[name][idx_pos] = quantize_field(nan_field, var)
    return {k: np.stack(v) for k, v in arrays.items()}, meta


def urls_digest(urls: list[str]) -> str:
    """Stable digest of the source URLs for provenance."""
    h = hashlib.sha256()
    for u in sorted(urls):
        h.update(u.encode())
        h.update(b"\n")
    return h.hexdigest()[:16]
