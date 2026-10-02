"""Whole-file GETs from the Open-Meteo bucket and local decoding.

Each required variable is one `.om` file per run (AROME's three are about
57 MB together, ICON-EU's 122 MB), so the reader streams whole objects to a
temporary directory with plain requests: no range reads, no fsspec. It records
every object's key, ETag and length, refuses a body shorter than its
Content-Length, and retries connection errors, 429 and 5xx a bounded number
of times with finite timeouts.

Decoding checks what the file says about itself against the registry before
any value is used: [lat, lon, time] dimensions of the registered grid, a
geographic CRS whose bounding box is the registered grid's, the unit, the
embedded run time, and one timestamp per time index. The array is then
transposed to [time, lat, lon] one latitude band at a time, so the peak is
the output array plus one band.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

from ingest.sources.base import SESSION
from ingest.sources.openmeteo.grids import bbox
from ingest.sources.openmeteo.registry import BUCKET_URL, Grid

TIMEOUT = (10, 300)  # connect, read (s)
ATTEMPTS = 4
RETRY_STATUS = {429, 500, 502, 503, 504}
CHUNK = 1 << 20
BAND_ROWS = 64


class SourceError(RuntimeError):
    """The run's files are not what the registry expects: abort, publish nothing."""


class NotFound(SourceError):
    pass


@dataclass(frozen=True)
class ObjectRecord:
    key: str
    etag: str
    bytes: int

    def public(self) -> dict:
        return {"key": self.key, "etag": self.etag, "bytes": self.bytes}


def url(key: str) -> str:
    return f"{BUCKET_URL}/{key}"


def _retrying(
    what: str,
    attempt_fn: Callable[[], object],
    *,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
):
    for attempt in range(1, attempts + 1):
        try:
            return attempt_fn()
        except NotFound:
            raise
        except (requests.ConnectionError, requests.Timeout, _Transient) as exc:
            if attempt == attempts:
                raise SourceError(f"{what}: {exc} (after {attempts} attempts)") from exc
            delay = 2**attempt
            print(f"ingest: {what}: {exc}; retrying in {delay} s", flush=True)
            sleep(delay)


class _Transient(Exception):
    pass


def _check_status(r: requests.Response, key: str) -> None:
    if r.status_code == 404:
        raise NotFound(f"{key}: 404")
    if r.status_code in RETRY_STATUS:
        raise _Transient(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise SourceError(f"{key}: HTTP {r.status_code}")


def get_json(key: str, *, session=SESSION, sleep=time.sleep) -> tuple[dict, ObjectRecord]:
    def once():
        r = session.get(url(key), timeout=TIMEOUT)
        _check_status(r, key)
        body = r.content
        return r.json(), ObjectRecord(key, r.headers.get("ETag", ""), len(body))

    return _retrying(key, once, sleep=sleep)


def head(key: str, *, session=SESSION, sleep=time.sleep) -> ObjectRecord:
    def once():
        r = session.head(url(key), timeout=TIMEOUT)
        _check_status(r, key)
        return ObjectRecord(
            key, r.headers.get("ETag", ""), int(r.headers.get("Content-Length", -1))
        )

    return _retrying(key, once, sleep=sleep)


def download(key: str, dest: Path, *, session=SESSION, sleep=time.sleep) -> ObjectRecord:
    """Stream one whole object to `dest`; refuse a truncated body."""

    def once():
        with session.get(url(key), stream=True, timeout=TIMEOUT) as r:
            _check_status(r, key)
            expected = int(r.headers.get("Content-Length", -1))
            written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    f.write(chunk)
                    written += len(chunk)
            if expected >= 0 and written != expected:
                raise _Transient(f"truncated: {written} of {expected} bytes")
            return ObjectRecord(key, r.headers.get("ETag", ""), written)

    return _retrying(key, once, sleep=sleep)


# ------------------------------------------------------------------ decode


_BBOX_RE = re.compile(r"BBOX\[([-0-9.]+),([-0-9.]+),([-0-9.]+),([-0-9.]+)\]")


@dataclass
class Decoded:
    values: np.ndarray  # float32 [time, lat, lon], NaN = missing
    times: list[datetime]
    unit: str


def decode(
    path: Path,
    *,
    grid: Grid,
    unit: str,
    reference: datetime,
    band_rows: int = BAND_ROWS,
) -> Decoded:
    import omfiles

    reader = omfiles.OmFileReader(str(path))
    try:
        name = Path(path).name
        shape = tuple(reader.shape)
        if len(shape) != 3 or shape[:2] != (grid.nlat, grid.nlon):
            raise SourceError(f"{name}: shape {shape} is not [{grid.nlat}, {grid.nlon}, time]")

        def child(key: str):
            try:
                found = reader.get_child_by_name(key)
            except Exception as exc:  # omfiles raises for an absent child
                raise SourceError(f"{name}: no {key!r} ({exc})") from exc
            if found is None:
                raise SourceError(f"{name}: no {key!r}")
            return found

        def scalar(key: str):
            return child(key).read_scalar()

        coordinates = str(scalar("coordinates")).split()
        if coordinates != ["lat", "lon", "time"]:
            raise SourceError(f"{name}: coordinates {coordinates}, expected lat lon time")
        wkt = str(scalar("crs_wkt"))
        m = _BBOX_RE.search(wkt.replace(" ", ""))
        if not wkt.startswith("GEOGCRS") or not m:
            raise SourceError(f"{name}: not a geographic grid with a BBOX: {wkt[:60]}…")
        got = tuple(float(x) for x in m.groups())
        if any(abs(a - b) > 1e-6 for a, b in zip(got, bbox(grid))):
            raise SourceError(f"{name}: BBOX {got} != registered grid {bbox(grid)}")
        file_unit = str(scalar("unit"))
        if file_unit != unit:
            raise SourceError(f"{name}: unit {file_unit!r}, expected {unit!r}")
        frt = int(scalar("forecast_reference_time"))
        if frt != int(reference.timestamp()):
            got_ref = datetime.fromtimestamp(frt, timezone.utc)
            raise SourceError(
                f"{name}: run {got_ref:%Y-%m-%dT%HZ}, expected {reference:%Y-%m-%dT%HZ}"
            )
        times = child("time").read_array((slice(None),)).astype(np.int64)
        if times.shape != (shape[2],):
            raise SourceError(f"{name}: {times.size} timestamps for {shape[2]} time indices")

        out = np.empty((shape[2], shape[0], shape[1]), dtype=np.float32)
        for i0 in range(0, shape[0], band_rows):
            i1 = min(i0 + band_rows, shape[0])
            band = reader.read_array((slice(i0, i1), slice(None), slice(None)))
            out[:, i0:i1, :] = np.moveaxis(band, 2, 0)
            del band
    finally:
        reader.close()
    return Decoded(
        values=out,
        times=[datetime.fromtimestamp(int(t), timezone.utc) for t in times],
        unit=file_unit,
    )
