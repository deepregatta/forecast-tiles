"""Currents: Copernicus Marine GLO12 (GLOBAL_ANALYSISFORECAST_PHY_001_024)
surface uo/vo via the copernicusmarine v2 python API, 6-hourly to 240 h.

Credentials come from COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD (the
copernicusmarine package reads them from the environment). These are ocean
model currents including tides at the sampled instants — NOT tidal stream
predictions; the 6-hourly axis undersamples the tidal cycle (spec § Layers).

A cycle is ready once the dataset's public STAC item says Copernicus has
finished writing it (`provider_state`, `require_published`). Until then
`resolve` and `build_cube` raise CycleNotAvailableError, which the CLI passes
to its wait/skip handling. Other failures propagate with cycle context; no
alternate provider is supported for this layer.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError, http
from tilekit.codec import quantize

LAYER = "currents"
MODEL = "cmems_glo12"
AXIS_NAME = "steps"
# per-variable currents dataset, 6-hourly instantaneous surface uo/vo —
# matches the committed 6-hourly axis natively. Env-overridable because the
# CMEMS catalog renames dataset ids from time to time.
DATASET_ID = os.environ.get("CMEMS_DATASET_ID", "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i")

STEP_AXIS = axis_offsets((0, 240, 6))  # 41 steps (Phase 0 lever 2)

PRODUCT_ID = "GLOBAL_ANALYSISFORECAST_PHY_001_024"
# The STAC item is per dataset version; override with the dataset id when the
# catalogue moves to a new version.
DATASET_VERSION = os.environ.get("CMEMS_DATASET_VERSION", "202406")

# Public, credential-free metadata for every Copernicus Marine dataset version.
STAC_BASE = "https://s3.waw3-1.cloudferro.com/mdl-metadata/metadata"

# GLO12's native grid is exactly 1/12° (lat -80 + k/12, lon -180 + k/12); the
# dataset's float32 coordinates only approximate it (measured 2026-09-24:
# within 2.1e-5°). Tiles carry the exact lattice so that its 10° lines, which
# 1/12 divides, start their tiles.
CELLS_PER_DEGREE = 12

# The Copernicus authentication service occasionally has short outages.  The
# toolbox already retries each HTTP request, so these are deliberately coarse
# retries of the authentication/open operation rather than every data read.
AUTH_RETRY_DELAYS_S = (300, 900)

VARS = [
    VariableSpec("cur_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("cur_v_kt", AXIS_NAME, "i16", 0.01),
]


def stac_url(product_id: str, dataset_id: str, version: str) -> str:
    return f"{STAC_BASE}/{product_id}/{dataset_id}_{version}/dataset.stac.json"


def _stac_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True)
class ProviderState:
    """What a dataset's STAC item says about its latest update."""

    end: datetime | None  # last data instant (`end_datetime`)
    updated: datetime | None  # when the last update finished (`admp_updated_data`)
    updating_from: datetime | None  # set while an update runs (`admp_updating_start_date`)


def provider_state(url: str, *, fetch: Callable[[str], bytes] = http) -> ProviderState:
    """Read a STAC item. An unreadable item is "not available yet", so a
    waiting run retries it and a scheduled run skips."""
    try:
        props = json.loads(fetch(url))["properties"]
    except Exception as exc:
        raise CycleNotAvailableError(
            f"Copernicus metadata unreadable ({type(exc).__name__}: {exc}): {url}"
        ) from exc
    return ProviderState(
        end=_stac_instant(props.get("end_datetime")),
        updated=_stac_instant(props.get("admp_updated_data")),
        updating_from=_stac_instant(props.get("admp_updating_start_date")),
    )


def require_published(state: ProviderState, cycle: datetime, last_lead_h: int, name: str) -> None:
    """Raise CycleNotAvailableError unless the item shows the cycle written in
    full: no update running, the data reaching `cycle + last_lead_h`, and the
    last update finished on or after the cycle began. The time axis can move
    long before the data are rewritten (IBI, 2026-09-29: 09:48 against
    11:08), so the running-update flag has to clear first."""
    cycle = cycle.astimezone(timezone.utc)
    if state.updating_from is not None:
        raise CycleNotAvailableError(
            f"{name}: Copernicus is updating the data (from "
            f"{state.updating_from:%Y-%m-%dT%H:%MZ}); cycle {cycle:%Y%m%dT%H}Z not ready"
        )
    last = cycle + timedelta(hours=last_lead_h)
    if state.end is None or state.end < last:
        end = f"{state.end:%Y-%m-%dT%H:%MZ}" if state.end else "unknown"
        raise CycleNotAvailableError(
            f"{name}: data end {end}, before {last:%Y-%m-%dT%H:%MZ}; "
            f"cycle {cycle:%Y%m%dT%H}Z not published yet"
        )
    if state.updated is None or state.updated < cycle:
        updated = f"{state.updated:%Y-%m-%dT%H:%MZ}" if state.updated else "unknown"
        raise CycleNotAvailableError(
            f"{name}: last update finished {updated}, before cycle {cycle:%Y%m%dT%H}Z"
        )


def glo12_state(*, fetch: Callable[[str], bytes] = http) -> ProviderState:
    return provider_state(stac_url(PRODUCT_ID, DATASET_ID, DATASET_VERSION), fetch=fetch)


def resolve(requested: datetime | None = None, *, fetch: Callable[[str], bytes] = http) -> datetime:
    """CMEMS GLO12 updates daily; the cube cycle is today's 00Z, or the
    requested one. Either is returned only once the STAC item shows it
    written to 240 h (GLO12 finished at 06:25 UTC on 2026-09-29)."""
    if requested is None:
        now = datetime.now(timezone.utc)
        requested = now.replace(hour=0, minute=0, second=0, microsecond=0)
    require_published(glo12_state(fetch=fetch), requested, STEP_AXIS[-1], "GLO12")
    return requested


def _open_dataset_with_auth_retries(
    copernicusmarine,
    *,
    sleep: Callable[[float], None] = time.sleep,
):
    for attempt in range(len(AUTH_RETRY_DELAYS_S) + 1):
        try:
            return copernicusmarine.open_dataset(
                dataset_id=DATASET_ID,
                variables=["uo", "vo"],
            )
        except copernicusmarine.CouldNotConnectToAuthenticationSystem:
            if attempt == len(AUTH_RETRY_DELAYS_S):
                raise
            delay = AUTH_RETRY_DELAYS_S[attempt]
            print(
                "ingest currents: Copernicus authentication service unavailable; "
                f"retrying in {delay // 60} minutes "
                f"({attempt + 2}/{len(AUTH_RETRY_DELAYS_S) + 1})"
            )
            sleep(delay)


def build_cube(cycle: datetime, *, fetch: Callable[[str], bytes] = http) -> ForecastCube:
    import copernicusmarine

    state = glo12_state(fetch=fetch)
    require_published(state, cycle, STEP_AXIS[-1], "GLO12")
    ds = _open_dataset_with_auth_retries(copernicusmarine)
    if "depth" in ds.dims:
        ds = ds.isel(depth=0)  # surface layer

    # stored float32: its precision is the tolerance for lying on the lattice
    lats = np.asarray(ds["latitude"].values)
    lons = np.asarray(ds["longitude"].values)
    if lats[1] < lats[0]:
        raise RuntimeError("unexpected descending latitude in GLO12 dataset")
    meta = GridMeta.from_coordinates(lats, lons, cells_per_degree=CELLS_PER_DEGREE)

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
            # when Copernicus finished writing this bulletin (STAC admp_updated_data)
            "provider_updated_at": state.updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetched_at": utcnow_iso(),
        },
    )
