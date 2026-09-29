"""Hourly CMEMS IBI analysis-forecast surface currents for the IBI region.

The source is the 1/36 degree, hourly-mean, two-dimensional uo/vo dataset for
IBI_ANALYSISFORECAST_PHY_005_001.  It is an ocean-model current field
INCLUDING tide and its hourly cadence resolves the tidal cycle.  It is still
NOT a tidal-stream prediction: there is no SHOM atlas, tidal-coefficient
scaling, or coastal blending in this layer.

Credentials come from COPERNICUSMARINE_SERVICE_USERNAME / _PASSWORD (or the
Copernicus Marine Toolbox credential store).  Published coverage is confined
to the IBI service domain; tiles outside it are absent.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError
from ingest.sources.cmems import AUTH_RETRY_DELAYS_S
from tilekit.codec import quantize

LAYER = "currents-ibi"
MODEL = "cmems_ibi"
PRODUCT_ID = "IBI_ANALYSISFORECAST_PHY_005_001"
DEFAULT_DATASET_ID = "cmems_mod_ibi_phy_anfc_0.027deg-2D_PT1H-m"
DATASET_ID_ENV = "CMEMS_IBI_DATASET_ID"
AXIS_NAME = "steps"

# Vendored from oscar/analysis/src/coachregatta_analysis/environment_fetcher.py
# RegionalModel "IBI" on 2026-08-24.  The provider grid is nominally 1/36
# degree, but its float64 coordinates are their own regular 0.02777863 degree
# lattice, off the 1/36 lines by up to 0.0013 degree (measured 2026-09-24), so
# the tiles keep the provider's coordinates rather than snapping them.
MIN_LAT = 26.0
MAX_LAT = 56.0
MIN_LON = -19.0
MAX_LON = 5.0
NOMINAL_RESOLUTION_DEG = 1 / 36

# The provider publishes 240 hourly forecast values in each daily bulletin.
# The layer trims horizon, never resolution.  0..120 h (from 2026-09-29) keeps
# "the next 3 days" inside the served run at every moment: the day-D bulletin
# finishes its ARCO update at about 10:00-11:40 UTC on day D and is served
# until the next one, so a run is up to about 40 h old when it is replaced,
# and 72 h was 27-55 h of forecast ahead of now.  The four Channel tiles grow
# from 21.6 MB (0..72 h, measured 2026-09-28) to about 36.6 MB.
# docs/ibi-currents.md -> "Horizon: 0-120 h".
NATIVE_FORECAST_HORIZON_H = 239
PUBLISHED_HORIZON_H = 120
STEP_AXIS = axis_offsets((0, PUBLISHED_HORIZON_H, 1))

VARS = [
    VariableSpec("cur_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("cur_v_kt", AXIS_NAME, "i16", 0.01),
]

_RESOLVED_DATASET_ID: str | None = None


def _dataset_variable_names(dataset) -> set[str]:
    names: set[str] = set()
    for version in getattr(dataset, "versions", []):
        for part in getattr(version, "parts", []):
            for service in getattr(part, "services", []):
                names.update(
                    variable.short_name
                    for variable in getattr(service, "variables", [])
                    if getattr(variable, "short_name", None)
                )
    return names


def resolve_dataset_id(copernicusmarine) -> str:
    """Resolve the current hourly 2-D IBI dataset from the live catalogue.

    Copernicus dataset ids can be renamed.  CMEMS_IBI_DATASET_ID is the
    operational escape hatch for a confirmed replacement or a catalogue
    outage; otherwise the preferred id must be present in the current product
    catalogue rather than being trusted blindly.
    """
    override = os.environ.get(DATASET_ID_ENV)
    if override:
        return override
    global _RESOLVED_DATASET_ID
    if _RESOLVED_DATASET_ID:
        return _RESOLVED_DATASET_ID

    try:
        catalogue = copernicusmarine.describe(
            contains=["ibi"],
            disable_progress_bar=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"IBI catalogue resolution failed; set {DATASET_ID_ENV} only to a "
            "verified hourly 2-D analysis-forecast dataset id"
        ) from exc

    candidates: list[str] = []
    for product in getattr(catalogue, "products", []):
        if getattr(product, "product_id", None) != PRODUCT_ID:
            continue
        for dataset in getattr(product, "datasets", []):
            dataset_id = getattr(dataset, "dataset_id", "")
            lowered = dataset_id.lower()
            if not (
                "anfc" in lowered
                and "0.027deg-2d_pt1h" in lowered
                and {"uo", "vo"} <= _dataset_variable_names(dataset)
            ):
                continue
            candidates.append(dataset_id)

    if DEFAULT_DATASET_ID in candidates:
        _RESOLVED_DATASET_ID = DEFAULT_DATASET_ID
        return _RESOLVED_DATASET_ID
    if len(candidates) == 1:
        _RESOLVED_DATASET_ID = candidates[0]
        return _RESOLVED_DATASET_ID
    if not candidates:
        raise RuntimeError(
            f"no hourly 2-D uo/vo dataset found in Copernicus product {PRODUCT_ID}; "
            f"set {DATASET_ID_ENV} only after verifying the replacement"
        )
    raise RuntimeError(
        f"ambiguous hourly 2-D IBI datasets in {PRODUCT_ID}: {sorted(candidates)}; "
        f"set {DATASET_ID_ENV} to the verified choice"
    )


def _catalogue_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def arco_update_state(copernicusmarine, dataset_id: str) -> tuple[datetime | None, datetime | None]:
    """Return (last completed ARCO update, first data instant being rewritten).

    Copernicus rewrites the rolling ARCO store in place after each bulletin.
    While it does, the catalogue's `arco_updating_start_date` names the first
    data instant that "may not be up to date"; the toolbox only warns about it,
    for subset requests, and not at all for open_dataset.  On 2026-09-28 the
    store's zarr metadata was rewritten at 09:54 UTC and its data finished at
    11:36 (`arco_updated_date`), so a read in between can mix two bulletins.
    """
    try:
        catalogue = copernicusmarine.describe(dataset_id=dataset_id, disable_progress_bar=True)
    except Exception as exc:
        if os.environ.get(DATASET_ID_ENV):
            print(
                f"ingest currents-ibi: catalogue unavailable ({type(exc).__name__}); "
                f"ARCO update state not checked under {DATASET_ID_ENV}"
            )
            return None, None
        raise RuntimeError(
            "IBI catalogue unavailable: cannot tell whether the ARCO store is mid-update"
        ) from exc
    for product in getattr(catalogue, "products", []):
        for dataset in getattr(product, "datasets", []):
            if getattr(dataset, "dataset_id", None) != dataset_id:
                continue
            for version in getattr(dataset, "versions", []):
                for part in getattr(version, "parts", []):
                    return (
                        _catalogue_instant(getattr(part, "arco_updated_date", None)),
                        _catalogue_instant(getattr(part, "arco_updating_start_date", None)),
                    )
    raise RuntimeError(f"IBI dataset {dataset_id} has no part in the catalogue")


def _require_settled(state: tuple[datetime | None, datetime | None], cycle: datetime) -> None:
    """Refuse a window the provider is still rewriting."""
    _, updating_from = state
    end = cycle + timedelta(hours=PUBLISHED_HORIZON_H)
    if updating_from is not None and updating_from <= end:
        raise CycleNotAvailableError(
            f"IBI ARCO store is being updated from {updating_from:%Y-%m-%dT%H:%MZ}, inside "
            f"{cycle:%Y%m%dT%H}Z +0..{PUBLISHED_HORIZON_H} h; a later run picks it up"
        )


def _open_dataset_with_auth_retries(
    copernicusmarine,
    *,
    dataset_id: str,
    sleep: Callable[[float], None] = time.sleep,
    **selection,
):
    """Open one IBI subset with the GLO12 layer's coarse auth backoff."""
    for attempt in range(len(AUTH_RETRY_DELAYS_S) + 1):
        try:
            return copernicusmarine.open_dataset(
                dataset_id=dataset_id,
                variables=["uo", "vo"],
                minimum_longitude=MIN_LON,
                maximum_longitude=MAX_LON,
                minimum_latitude=MIN_LAT,
                maximum_latitude=MAX_LAT,
                **selection,
            )
        except copernicusmarine.CouldNotConnectToAuthenticationSystem:
            if attempt == len(AUTH_RETRY_DELAYS_S):
                raise
            delay = AUTH_RETRY_DELAYS_S[attempt]
            print(
                "ingest currents-ibi: Copernicus authentication service unavailable; "
                f"retrying in {delay // 60} minutes "
                f"({attempt + 2}/{len(AUTH_RETRY_DELAYS_S) + 1})"
            )
            sleep(delay)


def _datetime64_to_utc(value: np.datetime64) -> datetime:
    seconds = int(np.datetime64(value, "s").astype(np.int64))
    return datetime.fromtimestamp(seconds, timezone.utc)


def resolve(requested: datetime | None = None) -> datetime:
    """Return an explicit cycle or derive the latest complete daily bulletin.

    The rolling analysis-forecast dataset does not expose an issue-time
    coordinate.  Its documented bulletin contains 240 forecast hours, so the
    issue/cycle is the final available instant minus 239 hours.  build_cube
    independently requires every published step before it will return.
    """
    if requested is not None:
        return requested

    import copernicusmarine

    dataset_id = resolve_dataset_id(copernicusmarine)
    ds = _open_dataset_with_auth_retries(copernicusmarine, dataset_id=dataset_id)
    try:
        times = np.asarray(ds["time"].values, dtype="datetime64[ns]")
        if times.size < NATIVE_FORECAST_HORIZON_H + 1:
            raise RuntimeError(
                f"IBI dataset has only {times.size} instants; cannot identify its "
                f"documented {NATIVE_FORECAST_HORIZON_H + 1}-hour bulletin"
            )
        final = _datetime64_to_utc(times[-1])
    finally:
        close = getattr(ds, "close", None)
        if close:
            close()

    cycle = final - timedelta(hours=NATIVE_FORECAST_HORIZON_H)
    if cycle.minute or cycle.second or cycle.microsecond:
        raise RuntimeError(f"IBI latest complete cycle is not hour-aligned: {cycle.isoformat()}")
    return cycle


def build_cube(cycle: datetime) -> ForecastCube:
    import copernicusmarine

    dataset_id = resolve_dataset_id(copernicusmarine)
    end = cycle + timedelta(hours=PUBLISHED_HORIZON_H)
    state = arco_update_state(copernicusmarine, dataset_id)
    _require_settled(state, cycle)
    ds = _open_dataset_with_auth_retries(
        copernicusmarine,
        dataset_id=dataset_id,
        start_datetime=cycle,
        end_datetime=end,
    )
    try:
        if "depth" in ds.dims:
            ds = ds.isel(depth=0)

        lats = np.asarray(ds["latitude"].values)
        lons = np.asarray(ds["longitude"].values)
        if len(lats) < 2 or len(lons) < 2:
            raise RuntimeError("IBI subset did not return a two-dimensional grid")
        if lats[1] <= lats[0] or lons[1] <= lons[0]:
            raise RuntimeError("unexpected descending IBI latitude/longitude grid")
        meta = GridMeta.from_coordinates(lats, lons)

        naive_cycle = cycle.astimezone(timezone.utc).replace(tzinfo=None)
        times = [np.datetime64(naive_cycle + timedelta(hours=h), "ns") for h in STEP_AXIS]
        available = set(np.asarray(ds["time"].values, dtype="datetime64[ns]"))
        missing = [instant for instant in times if instant not in available]
        if missing:
            raise RuntimeError(
                f"IBI missing {len(missing)}/{len(times)} requested hourly instants "
                f"(first: {missing[0]}) for cycle {cycle:%Y%m%dT%H}Z"
            )

        # Pull the selected Dask/Zarr chunks in one graph per variable. Reading
        # one timestamp at a time made 146 separate provider computations and
        # took more than ten minutes in the real size run. The 121-hour axis is
        # bounded (~454 MB float32 per variable), so bulk reads remain within
        # the runner budget while the toolbox can fetch chunks in parallel.
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

    # An update that started or finished while we read may have mixed bulletins.
    if arco_update_state(copernicusmarine, dataset_id) != state:
        raise CycleNotAvailableError(
            f"IBI ARCO store changed while cycle {cycle:%Y%m%dT%H}Z was read; "
            "a later run picks it up"
        )
    provider_updated, _ = state

    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: list(STEP_AXIS)},
        variables=list(VARS),
        arrays={"cur_u_kt": u_steps, "cur_v_kt": v_steps},
        member_count=1,
        provenance={
            "source": "Copernicus Marine IBI_ANALYSISFORECAST_PHY_005_001",
            "dataset_id": dataset_id,
            "attribution": "E.U. Copernicus Marine Service Information; doi:10.48670/moi-00027",
            "domain": {"lat": [MIN_LAT, MAX_LAT], "lon": [MIN_LON, MAX_LON]},
            "temporal_resolution": "hourly mean",
            "tidal_caveat": (
                "ocean-model currents including tide; resolves the tidal cycle; "
                "not a tidal-stream prediction"
            ),
            # when Copernicus finished writing this bulletin to the ARCO store
            "provider_updated_at": (
                provider_updated.strftime("%Y-%m-%dT%H:%M:%SZ") if provider_updated else None
            ),
            "fetched_at": utcnow_iso(),
        },
    )
