"""GFS-Wave 0.25° waves layer: significant height, wind-wave and primary-swell
partitions with periods and directions, 3-hourly to 384 h (spec § Layers)."""

from __future__ import annotations

from datetime import datetime

from ingest.cube import ForecastCube, axis_offsets, utcnow_iso
from ingest.sources.base import (
    DOWNLOAD_WORKERS,
    SourceVar,
    collect_steps,
    resolve_cycle,
    urls_digest,
)

GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
LAYER = "waves"
MODEL = "gfswave_0p25"
AXIS_NAME = "steps"

STEP_AXIS = axis_offsets((0, 384, 3))  # 129 steps

VARS = [
    SourceVar("hs_m", ("HTSGW", "surface"), AXIS_NAME, "i16", 0.01),
    SourceVar("period_s", ("PERPW", "surface"), AXIS_NAME, "i16", 0.1),
    SourceVar("dir_deg", ("DIRPW", "surface"), AXIS_NAME, "i16", 0.1),
    SourceVar("wind_wave_h_m", ("WVHGT", "surface"), AXIS_NAME, "i16", 0.01),
    SourceVar("wind_wave_period_s", ("WVPER", "surface"), AXIS_NAME, "i16", 0.1),
    SourceVar("wind_wave_dir_deg", ("WVDIR", "surface"), AXIS_NAME, "i16", 0.1),
    SourceVar("swell_h_m", ("SWELL", "1 in sequence"), AXIS_NAME, "i16", 0.01),
    SourceVar("swell_period_s", ("SWPER", "1 in sequence"), AXIS_NAME, "i16", 0.1),
    SourceVar("swell_dir_deg", ("SWDIR", "1 in sequence"), AXIS_NAME, "i16", 0.1),
]

_IDX_TEMPLATE = GFS_BASE + "/gfs.{date}/{hh}/wave/gridded/gfswave.t{hh}z.global.0p25.f384.grib2.idx"


def step_url(cycle: datetime, step: int) -> str:
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{GFS_BASE}/gfs.{date}/{hh}/wave/gridded/gfswave.t{hh}z.global.0p25.f{step:03d}.grib2"


def resolve(requested: datetime | None = None) -> datetime:
    return resolve_cycle(_IDX_TEMPLATE, requested)


def build_cube(cycle: datetime, workers: int = DOWNLOAD_WORKERS) -> ForecastCube:
    arrays, meta = collect_steps(lambda s: step_url(cycle, s), STEP_AXIS, VARS, workers)
    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: STEP_AXIS},
        variables=[v.public() for v in VARS],
        arrays=arrays,
        member_count=1,
        provenance={
            "source": "NOAA GFS-Wave global 0.25deg (noaa-gfs-bdp-pds)",
            "source_urls_digest": urls_digest([step_url(cycle, s) for s in STEP_AXIS]),
            "fetched_at": utcnow_iso(),
        },
    )
