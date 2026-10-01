"""weather-ecmwf and weather-ecmwf-short layers: ECMWF open data (IFS 0.25°)
10 m wind, plus 10 m gust when every step has one (degrade to wind-only
otherwise).

ECMWF runs the IFS four times a day, but only the 00Z/12Z cycles reach 240 h.
The 06Z/18Z cycles stop at 144 h (checked 2026-09-29 and 2026-09-30: the 144h
index is there, 147h is not), so they are a separate layer and every update is
published without cutting the 240 h horizon:

    weather-ecmwf        00Z/12Z   3-hourly to 144 h + 6-hourly to 240 h (65 steps)
    weather-ecmwf-short  06Z/18Z   3-hourly to 144 h (49 steps, the first part)

Passage reads, for each forecast time, the newest cycle that covers it.

Gust is a maximum over the interval before each step, and the open-data stream
names it by that interval, which changes along the axis. Read from the .index
files of 2026-09-28T00Z, and the same to 144 h in the messages of 2026-09-30
06Z and 18Z:

    steps     param   GRIB stepRange  window
    3–90      10fg    2-3 … 89-90     1 h  (since the previous post-processing,
                                            which is hourly to +90 h)
    93–144    10fg3   90-93 …         3 h
    150–240   10fg    144-150 …       6 h
    0         10fg    0               none (constant 0 field; left missing)

So all three names are requested together, one message is kept per step, and
its window (endStep − startStep) is published as the variable's `statistic`.
Probing a single name made 93–144 look missing (~28 %), which is why every
run from 2026-07-13 to 2026-09-28 published wind only.

Resolution requires each layer's whole axis (step 240, or 144 for a 06Z/18Z
cycle), so scheduled runs simply skip until the cycle is out (the CLI treats
CycleNotAvailableError as exit 0)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ingest.cube import ForecastCube, GridMeta, Statistic, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError, decode_field
from tilekit.codec import quantize

LAYER = "weather-ecmwf"
SHORT_LAYER = "weather-ecmwf-short"
# one model: both layers are the same IFS, at different cycles
MODEL = "ecmwf_ifs_0p25"
AXIS_NAME = "steps"

STEP_AXIS = axis_offsets((0, 144, 3), (150, 240, 6))  # 65 steps
SHORT_STEP_AXIS = axis_offsets((0, 144, 3))  # 49 steps, the first part of STEP_AXIS
SHORT_CYCLE_HOURS = (6, 18)

# every open-data name of the 10 m gust; each step serves one of them
GUST_PARAMS = ("10fg", "10fg3", "10fg6")
# GRIB2 (discipline, category, number) of the gust, whatever ecCodes calls it
_GUST_CODE = (0, 2, 22)

VARS_WIND = [
    VariableSpec("wind_u_kt", AXIS_NAME, "i16", 0.01),
    VariableSpec("wind_v_kt", AXIS_NAME, "i16", 0.01),
]
GUST_VAR = VariableSpec("gust_kt", AXIS_NAME, "i16", 0.1)


@dataclass(frozen=True)
class Field:
    """One decoded message: kind is 'u' / 'v' / 'gust' (or the raw shortName)."""

    kind: str
    short_name: str
    start: int  # hours; start == end for instantaneous fields
    end: int
    values: np.ndarray

    @property
    def window(self) -> int:
        return self.end - self.start


def _client():
    from ecmwf.opendata import Client

    return Client(source="ecmwf", model="ifs", resol="0p25")


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def resolve(requested: datetime | None = None) -> datetime:
    """Latest IFS cycle that has the full axis published (step 240 present).
    Raises CycleNotAvailableError when nothing suitable is out yet."""
    client = _client()
    try:
        latest = client.latest(type="fc", param="10u", step=STEP_AXIS[-1])
    except Exception as exc:
        raise CycleNotAvailableError(f"ECMWF open data: no full-horizon cycle found ({exc})")
    latest = _utc(latest)
    if requested is not None:
        if requested > latest:
            raise CycleNotAvailableError(
                f"ECMWF cycle {requested:%Y%m%dT%H}Z not published yet (latest {latest:%Y%m%dT%H}Z)"
            )
        return requested
    return latest


def resolve_short(requested: datetime | None = None) -> datetime:
    """Latest 06Z/18Z IFS cycle whose last short-range step (144 h) is
    published, or `requested` once it is. A 00Z/12Z cycle is refused: it
    belongs to the full layer. Raises CycleNotAvailableError when nothing
    suitable is out yet."""
    if requested is not None and requested.hour not in SHORT_CYCLE_HOURS:
        raise ValueError(
            f"{SHORT_LAYER} takes only 06Z and 18Z cycles, not {requested:%Y%m%dT%H}Z "
            f"(00Z and 12Z are {LAYER})"
        )
    client = _client()
    hours = (requested.hour,) if requested is not None else SHORT_CYCLE_HOURS
    found: list[datetime] = []
    errors: list[str] = []
    for hour in hours:
        # with a time, latest() probes that hour of today and of the day before
        try:
            found.append(
                _utc(client.latest(type="fc", param="10u", step=SHORT_STEP_AXIS[-1], time=hour))
            )
        except Exception as exc:
            errors.append(f"{hour:02d}Z: {exc}")
    if not found:
        raise CycleNotAvailableError(
            f"ECMWF open data: no 06Z/18Z cycle with step {SHORT_STEP_AXIS[-1]} found "
            f"({'; '.join(errors)})"
        )
    latest = max(found)
    if requested is not None:
        if requested > latest:
            raise CycleNotAvailableError(
                f"ECMWF cycle {requested:%Y%m%dT%H}Z not published to "
                f"{SHORT_STEP_AXIS[-1]} h yet (latest {latest:%Y%m%dT%H}Z)"
            )
        return requested
    return latest


def parse_messages(raw: bytes) -> tuple[list[Field], GridMeta | None]:
    """Split concatenated GRIB2 messages into decoded fields."""
    import eccodes

    fields: list[Field] = []
    meta: GridMeta | None = None
    offset = 0
    while offset < len(raw):
        gid = eccodes.codes_new_from_message(raw[offset:])
        try:
            length = eccodes.codes_get(gid, "totalLength")
            short = str(eccodes.codes_get(gid, "shortName"))
            code = tuple(
                eccodes.codes_get_long(gid, k)
                for k in ("discipline", "parameterCategory", "parameterNumber")
            )
            start = int(eccodes.codes_get_long(gid, "startStep"))
            end = int(eccodes.codes_get_long(gid, "endStep"))
        finally:
            eccodes.codes_release(gid)
        values, m = decode_field(raw[offset : offset + length])
        meta = meta or m
        if short == "10u":
            kind = "u"
        elif short == "10v":
            kind = "v"
        elif code == _GUST_CODE:
            kind = "gust"
        else:
            kind = short
        fields.append(Field(kind, short, start, end, values))
        offset += length
    return fields, meta


def _retrieve(
    client, cycle: datetime, params: list[str], steps: list[int]
) -> tuple[list[Field], GridMeta | None]:
    """Retrieve the params for the given steps of the cycle. The client skips
    param/step pairs the index doesn't list and raises only when none match."""
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tmp:
        client.retrieve(
            type="fc",
            param=params,
            step=steps,
            date=cycle.strftime("%Y-%m-%d"),
            time=cycle.hour,
            target=tmp.name,
        )
        raw = Path(tmp.name).read_bytes()
    return parse_messages(raw)


def select_gust(fields: list[Field], axis: list[int]) -> dict[int, Field]:
    """One gust field per axis step (after the first). Among the messages
    ending at a step, keep the longest window that fits in the gap since the
    previous axis step, so consecutive windows never overlap; if every window
    is longer than the gap, the shortest. Steps without a message are absent."""
    by_end: dict[int, list[Field]] = {}
    for f in fields:
        if f.kind == "gust" and f.window > 0:
            by_end.setdefault(f.end, []).append(f)
    chosen: dict[int, Field] = {}
    for prev, step in zip(axis, axis[1:]):
        candidates = by_end.get(step)
        if not candidates:
            continue
        fitting = [f for f in candidates if f.window <= step - prev]
        chosen[step] = (
            max(fitting, key=lambda f: f.window)
            if fitting
            else min(candidates, key=lambda f: f.window)
        )
    return chosen


def describe_gust(chosen: dict[int, Field]) -> str:
    """Provenance line: which name and window served which steps, e.g.
    '10fg 1 h max +3..+90 h; 10fg3 3 h max +93..+144 h; 10fg 6 h max +150..+240 h'."""
    runs: list[list] = []  # [short_name, window, first, last]
    for step in sorted(chosen):
        f = chosen[step]
        if runs and runs[-1][0] == f.short_name and runs[-1][1] == f.window:
            runs[-1][3] = step
        else:
            runs.append([f.short_name, f.window, step, step])
    return "; ".join(f"{name} {w} h max +{a}..+{b} h" for name, w, a, b in runs)


def build_short_cube(cycle: datetime) -> ForecastCube:
    """A 06Z/18Z cycle as the weather-ecmwf-short layer: its 144 h axis."""
    return build_cube(cycle, layer=SHORT_LAYER, axis=SHORT_STEP_AXIS)


def build_cube(cycle: datetime, *, layer: str = LAYER, axis: list[int] = STEP_AXIS) -> ForecastCube:
    client = _client()
    try:
        wind, meta = _retrieve(client, cycle, ["10u", "10v"], axis)
    except Exception as exc:
        raise CycleNotAvailableError(
            f"ECMWF cycle {cycle:%Y%m%dT%H}Z wind retrieval failed ({exc})"
        )
    fields = {(f.kind, f.end): f.values for f in wind}

    missing_wind = [s for s in axis if ("u", s) not in fields or ("v", s) not in fields]
    if missing_wind:
        raise CycleNotAvailableError(
            f"ECMWF cycle {cycle:%Y%m%dT%H}Z missing wind at steps {missing_wind[:5]}…"
        )

    # gust is a max over the interval before each step: none at step 0 (NaN-filled)
    gust: dict[int, Field] = {}
    try:
        gust_fields, _ = _retrieve(client, cycle, list(GUST_PARAMS), axis[1:])
        gust = select_gust(gust_fields, axis)
        absent = [s for s in axis[1:] if s not in gust]
        gust_note = (
            describe_gust(gust)
            if not absent
            else f"unavailable — no gust message at steps {absent[:5]}… ({len(absent)} of "
            f"{len(axis) - 1}); wind-only run"
        )
    except Exception as exc:  # nothing matched, or the download failed
        absent = axis[1:]
        gust_note = f"unavailable — retrieval failed ({exc}); wind-only run"
    if absent:
        # a partial series would fail the missing-fraction gate
        gust = {}

    nan = np.full((meta.nlat, meta.nlon), np.nan, dtype=np.float32)

    def stack(by_step: dict[int, np.ndarray], scale: float) -> np.ndarray:
        steps = [by_step.get(s, nan) * MS_TO_KT for s in axis]
        return np.stack([quantize(a, "i16", scale) for a in steps])

    variables = list(VARS_WIND)
    arrays = {
        "wind_u_kt": stack({s: v for (k, s), v in fields.items() if k == "u"}, 0.01),
        "wind_v_kt": stack({s: v for (k, s), v in fields.items() if k == "v"}, 0.01),
    }
    if gust:
        windows = tuple(gust[s].window if s in gust else None for s in axis)
        variables.append(replace(GUST_VAR, statistic=Statistic("max", windows)))
        arrays["gust_kt"] = stack({s: f.values for s, f in gust.items()}, 0.1)

    return ForecastCube(
        layer=layer,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: axis},
        variables=variables,
        arrays=arrays,
        member_count=1,
        provenance={
            "source": "ECMWF open data IFS 0.25deg (CC BY 4.0)",
            "gust": gust_note,
            "fetched_at": utcnow_iso(),
        },
    )
