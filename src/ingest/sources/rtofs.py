"""Currents fallback: NOAA RTOFS global (noaa-nws-rtofs-pds).

FETCH SKELETON ONLY — cycle resolution and file addressing are implemented,
but RTOFS global surface fields live on a curvilinear (tripolar) grid and
must be regridded to a regular lat/lon grid before they can feed a
ForecastCube. That regridding is deliberately not implemented yet; the CMEMS
path (sources/cmems.py) is primary and `ingest currents` records in
provenance whenever this fallback is attempted (spec: manifest provenance
"records RTOFS fallback when CMEMS is unavailable")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ingest.cube import ForecastCube, axis_offsets
from ingest.sources.base import CycleNotAvailableError, head_ok

RTOFS_BASE = "https://noaa-nws-rtofs-pds.s3.amazonaws.com"
LAYER = "currents"
MODEL = "rtofs_global"
AXIS_NAME = "steps"

STEP_AXIS = axis_offsets((0, 240, 6))  # same cube shape as the CMEMS path


def step_url(cycle: datetime, step: int) -> str:
    """2-D surface diagnostics file holding u_velocity/v_velocity."""
    date = cycle.strftime("%Y%m%d")
    return f"{RTOFS_BASE}/rtofs.{date}/rtofs_glo_2ds_f{step:03d}_prog.nc"


def resolve(requested: datetime | None = None) -> datetime:
    """RTOFS runs daily at 00Z; a cycle counts as complete when the final
    forecast file is present."""
    if requested is not None:
        if not head_ok(step_url(requested, STEP_AXIS[-1])):
            raise CycleNotAvailableError(
                f"RTOFS cycle {requested:%Y%m%d}T00Z incomplete: "
                f"{step_url(requested, STEP_AXIS[-1])}"
            )
        return requested
    t = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(3):
        if head_ok(step_url(t, STEP_AXIS[-1])):
            return t
        t -= timedelta(days=1)
    raise CycleNotAvailableError("no complete RTOFS cycle found")


def build_cube(cycle: datetime) -> ForecastCube:
    raise NotImplementedError(
        "RTOFS fallback is a fetch skeleton: surface u/v files are addressable "
        f"(e.g. {step_url(cycle, 0)}) but the curvilinear-to-regular regridding "
        "onto a 1/12 deg lat/lon grid is not implemented yet. Use the CMEMS "
        "primary path."
    )
