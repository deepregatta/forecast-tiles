"""Shared synthetic cubes for offline pipeline tests (no network anywhere)."""

from datetime import datetime, timezone

import numpy as np
import pytest

from ingest.cube import ForecastCube, GridMeta, VariableSpec

CYCLE = datetime(2026, 7, 13, 6, tzinfo=timezone.utc)


def make_weather_cube(*, east_all_missing: bool = False, seed: int = 42) -> ForecastCube:
    """Tiny weather cube spanning exactly two 10° tiles (N40W010, N40E000):
    3 hourly steps of wind u/v/gust + 2 3-hourly steps of visibility."""
    rng = np.random.default_rng(seed)
    grid = GridMeta(lat0=40.0, lon0=-10.0, dlat=2.5, dlon=2.5, nlat=4, nlon=8)
    u = rng.uniform(-30, 30, (3, 4, 8)).astype(np.float32)
    v = rng.uniform(-30, 30, (3, 4, 8)).astype(np.float32)
    gust = (np.hypot(u, v) + 2.0).astype(np.float32)
    vis = rng.uniform(0, 50_000, (2, 4, 8)).astype(np.float32)
    u[0, 0, 0] = v[0, 0, 0] = gust[0, 0, 0] = np.nan  # a little land
    arrays = {"wind_u_kt": u, "wind_v_kt": v, "gust_kt": gust, "visibility_m": vis}
    if east_all_missing:
        for arr in arrays.values():
            arr[..., 4:] = np.nan
    return ForecastCube(
        layer="weather",
        model="gfs_0p25",
        cycle=CYCLE,
        grid=grid,
        time_axes={"hourly": [0, 1, 2], "h3": [0, 3]},
        variables=[
            VariableSpec("wind_u_kt", "hourly", "i16", 0.01),
            VariableSpec("wind_v_kt", "hourly", "i16", 0.01),
            VariableSpec("gust_kt", "hourly", "i16", 0.1),
            VariableSpec("visibility_m", "h3", "i16", 50),
        ],
        arrays=arrays,
        member_count=1,
        provenance={"source": "synthetic test cube"},
    )


def make_ensemble_cube(*, members: int = 5, seed: int = 7) -> ForecastCube:
    rng = np.random.default_rng(seed)
    grid = GridMeta(lat0=40.0, lon0=-10.0, dlat=2.5, dlon=2.5, nlat=4, nlon=4)
    mean = rng.uniform(5, 30, (3, 4, 4)).astype(np.float32)
    anom = rng.uniform(-10, 10, (members, 3, 4, 4)).astype(np.float32)
    return ForecastCube(
        layer="ensemble",
        model="gefs_0p50",
        cycle=CYCLE,
        grid=grid,
        time_axes={"steps": [0, 3, 6]},
        variables=[
            VariableSpec("wind_kt_mean", "steps", "i16", 0.01),
            VariableSpec("wind_kt_anom", "steps", "i8", 0.2, per_member=True),
        ],
        arrays={"wind_kt_mean": mean, "wind_kt_anom": anom},
        member_count=members,
        provenance={"source": "synthetic test cube"},
    )


@pytest.fixture
def weather_cube() -> ForecastCube:
    return make_weather_cube()


@pytest.fixture
def ensemble_cube() -> ForecastCube:
    return make_ensemble_cube()
