"""Currents (primary): Copernicus Marine GLO12 (GLOBAL_ANALYSISFORECAST_PHY_001_024)
surface uo/vo via the copernicusmarine v2 python API, 6-hourly to 240 h.

Credentials come from COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD (the
copernicusmarine package reads them from the environment). These are ocean
model currents including tides at the sampled instants — NOT tidal stream
predictions; the 6-hourly axis undersamples the tidal cycle (spec § Layers).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT
from tilekit.codec import quantize

LAYER = "currents"
MODEL = "cmems_glo12"
AXIS_NAME = "steps"
DATASET_ID = "cmems_mod_glo_phy_anfc_0.083deg_PT1H-i"

STEP_AXIS = axis_offsets((0, 240, 6))  # 41 steps (Phase 0 lever 2)

VARS = [
    VariableSpec("cur_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("cur_v_kt", AXIS_NAME, "i16", 0.01),
]


def resolve(requested: datetime | None = None) -> datetime:
    """CMEMS GLO12 updates daily; the cube cycle is today's 00Z (data
    availability for the full horizon is verified while subsetting)."""
    if requested is not None:
        return requested
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def build_cube(cycle: datetime) -> ForecastCube:
    import copernicusmarine

    ds = copernicusmarine.open_dataset(
        dataset_id=DATASET_ID,
        variables=["uo", "vo"],
    )
    if "depth" in ds.dims:
        ds = ds.isel(depth=0)  # surface layer

    lats = np.asarray(ds["latitude"].values, dtype=np.float64)
    lons = np.asarray(ds["longitude"].values, dtype=np.float64)
    if lats[1] < lats[0]:
        raise RuntimeError("unexpected descending latitude in GLO12 dataset")
    meta = GridMeta(
        lat0=float(lats[0]),
        lon0=float(lons[0]),
        dlat=float(lats[1] - lats[0]),
        dlon=float(lons[1] - lons[0]),
        nlat=len(lats),
        nlon=len(lons),
    )

    naive_cycle = cycle.astimezone(timezone.utc).replace(tzinfo=None)
    times = [np.datetime64(naive_cycle + timedelta(hours=h)) for h in STEP_AXIS]
    available = set(np.asarray(ds["time"].values, dtype="datetime64[ns]"))
    missing = [t for t in times if np.datetime64(t, "ns") not in available]
    if missing:
        raise RuntimeError(
            f"GLO12 missing {len(missing)}/{len(times)} requested instants "
            f"(first: {missing[0]}) for cycle {cycle:%Y%m%dT%H}Z"
        )

    u_steps, v_steps = [], []
    for t in times:  # one instant at a time keeps peak memory bounded
        snap = ds.sel(time=t)
        u_steps.append(quantize(snap["uo"].values.astype(np.float32) * MS_TO_KT, "i16", 0.01))
        v_steps.append(quantize(snap["vo"].values.astype(np.float32) * MS_TO_KT, "i16", 0.01))

    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: STEP_AXIS},
        variables=list(VARS),
        arrays={"cur_u_kt": np.stack(u_steps), "cur_v_kt": np.stack(v_steps)},
        member_count=1,
        provenance={
            "source": "Copernicus Marine GLOBAL_ANALYSISFORECAST_PHY_001_024",
            "dataset_id": DATASET_ID,
            "attribution": "E.U. Copernicus Marine Service Information; doi:10.48670/moi-00016",
            "tidal_caveat": "instantaneous model currents, not tidal stream predictions",
            "fetched_at": utcnow_iso(),
        },
    )
