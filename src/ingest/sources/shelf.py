"""Hourly Copernicus Marine regional surface currents finer than, or beyond, IBI.

Two layers built exactly as `currents-ibi` is (same toolbox, crop, quantization
and PFT1 encoding), each from one regional analysis-forecast product:

- `currents-nws`: NWSHELF_ANALYSISFORECAST_PHY_004_013 (Met Office), the North
  West Shelf at 0.0135 deg x 0.0303 deg (the product's "1.5 km"), hourly
  INSTANTANEOUS values (IBI's are hourly means), a 7-day forecast;
- `currents-med`: MEDSEA_ANALYSISFORECAST_PHY_006_013 (CMCC), the
  Mediterranean at 1/24 deg (about 4.2 km), hourly means, a 10-day forecast.

Both are ocean-model currents INCLUDING tide; hourly sampling resolves the
tidal cycle. Neither is a tidal-stream prediction. Domains, grids, horizons
and dataset ids were read from the live Copernicus catalogue and each
product's page on 2026-11-14 (docs/shelf-currents.md).

**Staged, not published.** `PUBLICATION_ENABLED` is False: the CLI refuses
any run without --dry-run, no workflow has a schedule, and the dispatcher
does not know these layers. Publication is the owner's decision.

Credentials come from COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD or the
Copernicus Marine Toolbox credential store, as for IBI.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError, http
from ingest.sources.cmems import AUTH_RETRY_DELAYS_S, provider_state, require_published, stac_url
from ingest.sources.ibi import _require_settled, arco_update_state
from tilekit.codec import quantize

AXIS_NAME = "steps"

# Neither layer may reach the bucket until the owner approves publication.
PUBLICATION_ENABLED = False

VARS = [
    VariableSpec("cur_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("cur_v_kt", AXIS_NAME, "i16", 0.01),
]


@dataclass(frozen=True)
class ShelfProduct:
    layer: str
    model: str
    name: str
    product_id: str
    default_dataset_id: str
    dataset_id_env: str
    default_version: str
    version_env: str
    doi: str
    # crop box handed to the toolbox, the product's own catalogue extent
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    # the bulletin's last forecast hour after its cycle (catalogue end - cycle)
    native_horizon_h: int
    published_horizon_h: int
    temporal_resolution: str
    tile_deg: int

    @property
    def dataset_id(self) -> str:
        return os.environ.get(self.dataset_id_env) or self.default_dataset_id

    @property
    def dataset_version(self) -> str:
        return os.environ.get(self.version_env) or self.default_version

    @property
    def step_axis(self) -> list[int]:
        return axis_offsets((0, self.published_horizon_h, 1))

    @property
    def attribution(self) -> str:
        return f"Generated using E.U. Copernicus Marine Service Information; doi:{self.doi}"


# Catalogue, 2026-11-14: latitude 46.0 .. 62.7432 step 0.0135135 (1240 rows),
# longitude -16.0 .. 13.0 step 0.0303030 (958 columns); the 13 Nov bulletin
# finished at 10:07 UTC and ends 20 Nov 00:00, 168 h after 13 Nov 00Z.
# 5 deg tiles: a 10 deg tile would be 740 x 330 cells, twice an IBI tile.
NWS = ShelfProduct(
    layer="currents-nws",
    model="cmems_nws",
    name="NWS",
    product_id="NWSHELF_ANALYSISFORECAST_PHY_004_013",
    default_dataset_id="cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i",
    dataset_id_env="CMEMS_NWS_DATASET_ID",
    default_version="202511",
    version_env="CMEMS_NWS_DATASET_VERSION",
    doi="10.48670/moi-00054",
    min_lat=46.0,
    max_lat=62.75,
    min_lon=-16.0,
    max_lon=13.0,
    native_horizon_h=168,
    published_horizon_h=120,
    temporal_resolution="hourly instantaneous",
    tile_deg=5,
)

# Catalogue, 2026-11-14: latitude 30.1875 .. 45.9792 step 1/24 (380 rows),
# longitude -17.2917 .. 36.2917 (1287 columns); the 13 Nov bulletin finished
# at 13:06 UTC and ends 22 Nov 23:00, 239 h after 13 Nov 00Z. The catalogue
# marks dataset version 202511 retired on 2027-01-12: set CMEMS_MED_DATASET_ID
# / _VERSION to the verified successor before then.
MED = ShelfProduct(
    layer="currents-med",
    model="cmems_med",
    name="MED",
    product_id="MEDSEA_ANALYSISFORECAST_PHY_006_013",
    default_dataset_id="cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m",
    dataset_id_env="CMEMS_MED_DATASET_ID",
    default_version="202511",
    version_env="CMEMS_MED_DATASET_VERSION",
    doi="10.48670/mds-00359",
    min_lat=30.1875,
    max_lat=45.98,
    min_lon=-17.2917,
    max_lon=36.2917,
    native_horizon_h=239,
    published_horizon_h=120,
    temporal_resolution="hourly mean",
    tile_deg=10,
)

PRODUCTS = {product.layer: product for product in (NWS, MED)}
LAYERS = tuple(PRODUCTS)


def product(layer: str) -> ShelfProduct:
    return PRODUCTS[layer]


def is_shelf(layer: str) -> bool:
    return layer in PRODUCTS


def _open(
    copernicusmarine,
    spec: ShelfProduct,
    *,
    sleep: Callable[[float], None] = time.sleep,
    **selection,
):
    for attempt in range(len(AUTH_RETRY_DELAYS_S) + 1):
        try:
            return copernicusmarine.open_dataset(
                dataset_id=spec.dataset_id,
                variables=["uo", "vo"],
                minimum_longitude=spec.min_lon,
                maximum_longitude=spec.max_lon,
                minimum_latitude=spec.min_lat,
                maximum_latitude=spec.max_lat,
                **selection,
            )
        except copernicusmarine.CouldNotConnectToAuthenticationSystem:
            if attempt == len(AUTH_RETRY_DELAYS_S):
                raise
            delay = AUTH_RETRY_DELAYS_S[attempt]
            print(
                f"ingest {spec.layer}: Copernicus authentication service unavailable; "
                f"retrying in {delay // 60} minutes "
                f"({attempt + 2}/{len(AUTH_RETRY_DELAYS_S) + 1})"
            )
            sleep(delay)


def resolve(
    spec: ShelfProduct,
    requested: datetime | None = None,
    *,
    fetch: Callable[[str], bytes] = http,
) -> datetime:
    """The cycle to build: an explicit one once the public STAC item shows it
    written to its last native hour, otherwise the bulletin the item's
    `end_datetime` closes (that instant minus the native horizon).

    Not the ARCO store's time axis, as IBI's undated resolve uses: on
    2026-11-14 at 08:54 UTC the NW Shelf store's axis already ran to 21 Nov
    00:00 while its values stopped, finite, at 20 Nov 00:00, the item's end.
    Read from the axis, the cycle came out a day early and the settled check
    refused it."""
    state = provider_state(
        stac_url(spec.product_id, spec.dataset_id, spec.dataset_version), fetch=fetch
    )
    if requested is None:
        if state.end is None:
            raise CycleNotAvailableError(f"{spec.name}: Copernicus metadata names no data end")
        last = state.end.replace(minute=0, second=0, microsecond=0)
        requested = last - timedelta(hours=spec.native_horizon_h)
    require_published(state, requested, spec.native_horizon_h, spec.name)
    return requested


def build_cube(spec: ShelfProduct, cycle: datetime) -> ForecastCube:
    import copernicusmarine

    end = cycle + timedelta(hours=spec.published_horizon_h)
    state = arco_update_state(
        copernicusmarine,
        spec.dataset_id,
        name=spec.name,
        layer=spec.layer,
        override_env=spec.dataset_id_env,
    )
    _require_settled(state, cycle, spec.name)
    ds = _open(copernicusmarine, spec, start_datetime=cycle, end_datetime=end)
    try:
        if "depth" in ds.dims:
            ds = ds.isel(depth=0)
        lats = np.asarray(ds["latitude"].values)
        lons = np.asarray(ds["longitude"].values)
        if len(lats) < 2 or len(lons) < 2:
            raise RuntimeError(f"{spec.name} subset did not return a two-dimensional grid")
        if lats[1] <= lats[0] or lons[1] <= lons[0]:
            raise RuntimeError(f"unexpected descending {spec.name} latitude/longitude grid")
        meta = GridMeta.from_coordinates(lats, lons)

        naive_cycle = cycle.astimezone(timezone.utc).replace(tzinfo=None)
        times = [np.datetime64(naive_cycle + timedelta(hours=h), "ns") for h in spec.step_axis]
        available = set(np.asarray(ds["time"].values, dtype="datetime64[ns]"))
        missing = [instant for instant in times if instant not in available]
        if missing:
            raise RuntimeError(
                f"{spec.name} missing {len(missing)}/{len(times)} requested hourly instants "
                f"(first: {missing[0]}) for cycle {cycle:%Y%m%dT%H}Z"
            )
        # one graph per variable, as for IBI
        selected = ds.sel(time=times)
        u_values = np.asarray(selected["uo"].values, dtype=np.float32)
        u_values *= MS_TO_KT
        u_steps = quantize(u_values, "i16", 0.01)
        del u_values
        v_values = np.asarray(selected["vo"].values, dtype=np.float32)
        v_values *= MS_TO_KT
        v_steps = quantize(v_values, "i16", 0.01)
        del v_values
    finally:
        close = getattr(ds, "close", None)
        if close:
            close()

    after = arco_update_state(
        copernicusmarine,
        spec.dataset_id,
        name=spec.name,
        layer=spec.layer,
        override_env=spec.dataset_id_env,
    )
    if after != state:
        raise CycleNotAvailableError(
            f"{spec.name} ARCO store changed while cycle {cycle:%Y%m%dT%H}Z was read; "
            "a later run picks it up"
        )
    provider_updated, _ = state

    return ForecastCube(
        layer=spec.layer,
        model=spec.model,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: list(spec.step_axis)},
        variables=list(VARS),
        arrays={"cur_u_kt": u_steps, "cur_v_kt": v_steps},
        member_count=1,
        tile_deg=spec.tile_deg,
        provenance={
            "source": f"Copernicus Marine {spec.product_id}",
            "dataset_id": spec.dataset_id,
            "attribution": spec.attribution,
            "domain": {"lat": [spec.min_lat, spec.max_lat], "lon": [spec.min_lon, spec.max_lon]},
            "temporal_resolution": spec.temporal_resolution,
            "tidal_caveat": (
                "ocean-model currents including tide; resolves the tidal cycle; "
                "not a tidal-stream prediction"
            ),
            "provider_updated_at": (
                provider_updated.strftime("%Y-%m-%dT%H:%M:%SZ") if provider_updated else None
            ),
            "fetched_at": utcnow_iso(),
        },
    )
