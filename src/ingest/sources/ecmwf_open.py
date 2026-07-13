"""weather-ecmwf layer: ECMWF open data (IFS 0.25°) 10 m wind, plus 10 m gust
when the parameter is actually published (probed per cycle; degrade to
wind-only otherwise). 3-hourly to 144 h + 6-hourly to 240 h.

Only the 00Z/12Z IFS cycles reach 240 h (06Z/18Z stop at 90 h); resolution
requires the full axis, so scheduled runs simply skip until a full-horizon
cycle is out (the CLI treats CycleNotAvailableError as exit 0)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError, decode_field
from tilekit.codec import quantize

LAYER = "weather-ecmwf"
MODEL = "ecmwf_ifs_0p25"
AXIS_NAME = "steps"

STEP_AXIS = axis_offsets((0, 144, 3), (150, 240, 6))  # 65 steps

# gust parameter candidates, probed in order (availability varies by cycle/era)
GUST_PARAMS = ("10fg", "10fg3", "10fg6")
_GUST_SHORTNAMES = {"10fg", "fg10", "10fg3", "10fg6", "gust", "i10fg", "p10fg"}

VARS_WIND = [
    VariableSpec("wind_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("wind_v_kt", AXIS_NAME, "i16", 0.01),
]
GUST_VAR = VariableSpec("gust_kt", AXIS_NAME, "i16", 0.1)


def _client():
    from ecmwf.opendata import Client

    return Client(source="ecmwf", model="ifs", resol="0p25")


def resolve(requested: datetime | None = None) -> datetime:
    """Latest IFS cycle that has the full axis published (step 240 present).
    Raises CycleNotAvailableError when nothing suitable is out yet."""
    client = _client()
    try:
        latest = client.latest(type="fc", param="10u", step=STEP_AXIS[-1])
    except Exception as exc:
        raise CycleNotAvailableError(f"ECMWF open data: no full-horizon cycle found ({exc})")
    latest = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest
    if requested is not None:
        if requested > latest:
            raise CycleNotAvailableError(
                f"ECMWF cycle {requested:%Y%m%dT%H}Z not published yet (latest {latest:%Y%m%dT%H}Z)"
            )
        return requested
    return latest


def _retrieve_fields(
    client, cycle: datetime, params: list[str], steps: list[int] | None = None
) -> dict[tuple[str, int], object]:
    """Retrieve params for the given steps of the cycle; returns
    {(kind, step): field} where kind is 'u' / 'v' / 'gust'."""
    import eccodes

    fields: dict[tuple[str, int], object] = {}
    meta: GridMeta | None = None
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tmp:
        client.retrieve(
            type="fc",
            param=params,
            step=steps if steps is not None else STEP_AXIS,
            date=cycle.strftime("%Y-%m-%d"),
            time=cycle.hour,
            target=tmp.name,
        )
        raw = Path(tmp.name).read_bytes()

    offset = 0
    while offset < len(raw):
        gid = eccodes.codes_new_from_message(raw[offset:])
        try:
            length = eccodes.codes_get(gid, "totalLength")
            short = str(eccodes.codes_get(gid, "shortName"))
            step_range = str(eccodes.codes_get(gid, "stepRange"))
        finally:
            eccodes.codes_release(gid)
        step = int(step_range.split("-")[-1])  # gust may come as a "start-end" range
        values, m = decode_field(raw[offset : offset + length])
        meta = meta or m
        if short == "10u":
            kind = "u"
        elif short == "10v":
            kind = "v"
        elif short.lower() in _GUST_SHORTNAMES:
            kind = "gust"
        else:
            kind = short
        fields[(kind, step)] = values
        offset += length
    fields["__meta__"] = meta
    return fields


def probe_gust_param(client, cycle: datetime) -> str | None:
    """First gust parameter the index actually serves for this cycle."""
    for param in GUST_PARAMS:
        try:
            with tempfile.NamedTemporaryFile(suffix=".grib2") as tmp:
                client.retrieve(
                    type="fc",
                    param=[param],
                    step=[STEP_AXIS[1]],
                    date=cycle.strftime("%Y-%m-%d"),
                    time=cycle.hour,
                    target=tmp.name,
                )
            return param
        except Exception:
            continue
    return None


def build_cube(cycle: datetime) -> ForecastCube:
    client = _client()
    try:
        fields = _retrieve_fields(client, cycle, ["10u", "10v"])
    except Exception as exc:
        raise CycleNotAvailableError(
            f"ECMWF cycle {cycle:%Y%m%dT%H}Z wind retrieval failed ({exc})"
        )
    meta: GridMeta = fields.pop("__meta__")

    gust_param = probe_gust_param(client, cycle)
    gust_fields: dict = {}
    if gust_param is not None:
        try:
            # gust is a max-over-interval quantity, absent at step 0 (NaN-filled)
            gust_fields = _retrieve_fields(client, cycle, [gust_param], steps=STEP_AXIS[1:])
            gust_fields.pop("__meta__", None)
        except Exception:
            gust_param = None  # gust exists for some steps only: degrade to wind-only
            gust_fields = {}

    def stack(kind: str, scale: float, source: dict) -> np.ndarray:
        nan = np.full((meta.nlat, meta.nlon), np.nan, dtype=np.float32)
        steps_arr = [source.get((kind, s), nan) * MS_TO_KT for s in STEP_AXIS]
        return np.stack([quantize(a, "i16", scale) for a in steps_arr])

    missing_wind = [s for s in STEP_AXIS if ("u", s) not in fields or ("v", s) not in fields]
    if missing_wind:
        raise CycleNotAvailableError(
            f"ECMWF cycle {cycle:%Y%m%dT%H}Z missing wind at steps {missing_wind[:5]}…"
        )

    variables = list(VARS_WIND)
    arrays = {
        "wind_u_kt": stack("u", 0.01, fields),
        "wind_v_kt": stack("v", 0.01, fields),
    }
    if gust_param is not None:
        # gust is a max-over-interval quantity: absent at step 0, NaN-filled there
        variables.append(GUST_VAR)
        arrays["gust_kt"] = stack("gust", 0.1, gust_fields)

    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: STEP_AXIS},
        variables=variables,
        arrays=arrays,
        member_count=1,
        provenance={
            "source": "ECMWF open data IFS 0.25deg (CC BY 4.0)",
            "gust": gust_param or "unavailable — wind-only run",
            "fetched_at": utcnow_iso(),
        },
    )
