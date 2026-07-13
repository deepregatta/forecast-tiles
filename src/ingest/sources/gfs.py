"""GFS 0.25° weather layer: wind u/v + gust on the hourly axis, vis/CAPE/
temp/dew-point/precip on the 3-hourly axis (spec § Layers)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from ingest.cube import ForecastCube, axis_offsets, utcnow_iso
from ingest.sources.base import (
    DOWNLOAD_WORKERS,
    SourceVar,
    decode_field,
    fetch_fields,
    quantize_field,
    resolve_cycle,
    urls_digest,
)

GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
LAYER = "weather"
MODEL = "gfs_0p25"

# hourly to 120 h, then 3-hourly to 240 h (161 steps); 3-hourly to 240 h (81 steps)
HOURLY_AXIS = axis_offsets((0, 120, 1), (123, 240, 3))
H3_AXIS = axis_offsets((0, 240, 3))

HOURLY_VARS = [
    SourceVar("wind_u_kt", ("UGRD", "10 m above ground"), "hourly", "i16", 0.01, to_kt=True),
    SourceVar("wind_v_kt", ("VGRD", "10 m above ground"), "hourly", "i16", 0.01, to_kt=True),
    SourceVar("gust_kt", ("GUST", "surface"), "hourly", "i16", 0.1, to_kt=True),
]
H3_VARS = [
    SourceVar("visibility_m", ("VIS", "surface"), "h3", "i16", 50),
    SourceVar("cape_jkg", ("CAPE", "surface"), "h3", "i16", 1),
    SourceVar("temp_c", ("TMP", "2 m above ground"), "h3", "i16", 0.1, k_to_c=True),
    SourceVar("dew_point_c", ("DPT", "2 m above ground"), "h3", "i16", 0.1, k_to_c=True),
    # no accumulated precip at analysis time: NaN-filled at f000
    SourceVar("precip_mm", ("APCP", "surface"), "h3", "i16", 0.1, optional=True),
]

_IDX_TEMPLATE = GFS_BASE + "/gfs.{date}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f240.idx"


def step_url(cycle: datetime, step: int) -> str:
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{GFS_BASE}/gfs.{date}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{step:03d}"


def vars_for_step(step: int) -> list[SourceVar]:
    """Variables to fetch at a forecast step, gated by axis membership (on the
    committed axes every h3 step is also an hourly step)."""
    out: list[SourceVar] = []
    if step in HOURLY_AXIS:
        out += HOURLY_VARS
    if step in H3_AXIS:
        out += H3_VARS
    return out


def resolve(requested: datetime | None = None) -> datetime:
    """Latest complete GFS cycle (f240 .idx present); falls back one cycle
    rather than ever ingesting a partial one."""
    return resolve_cycle(_IDX_TEMPLATE, requested)


def build_cube(cycle: datetime, workers: int = DOWNLOAD_WORKERS) -> ForecastCube:
    steps = sorted(set(HOURLY_AXIS) | set(H3_AXIS))
    all_vars = HOURLY_VARS + H3_VARS

    def one(step: int):
        specs = vars_for_step(step)
        wanted = [v.grib for v in specs]
        optional = {v.grib for v in specs if v.optional}
        return step, fetch_fields(step_url(cycle, step), wanted, optional)

    fields_by_step: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for step, fields in pool.map(one, steps):
            fields_by_step[step] = fields

    meta = None
    stacks: dict[str, list[np.ndarray | None]] = {v.name: [] for v in all_vars}
    pending_nan: list[tuple[SourceVar, int]] = []
    for step in steps:
        for var in vars_for_step(step):
            msg = fields_by_step[step].get(var.grib)
            if msg is None:
                pending_nan.append((var, len(stacks[var.name])))
                stacks[var.name].append(None)
                continue
            values, m = decode_field(msg)
            meta = meta or m
            stacks[var.name].append(quantize_field(values, var))
    if meta is None:
        raise RuntimeError("no GFS fields decoded")
    for var, pos in pending_nan:
        nan_field = np.full((meta.nlat, meta.nlon), np.nan, dtype=np.float32)
        stacks[var.name][pos] = quantize_field(nan_field, var)

    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={"hourly": HOURLY_AXIS, "h3": H3_AXIS},
        variables=[v.public() for v in all_vars],
        arrays={name: np.stack(stack) for name, stack in stacks.items()},
        member_count=1,
        provenance={
            "source": "NOAA GFS 0.25deg (noaa-gfs-bdp-pds)",
            "source_urls_digest": urls_digest([step_url(cycle, s) for s in steps]),
            "fetched_at": utcnow_iso(),
        },
    )
