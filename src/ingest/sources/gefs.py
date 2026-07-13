"""GEFS 0.5° ensemble layer: 31 members (gec00 + gep01..gep30), wind speed
encoded as mean (i16) + per-member anomalies (i8, clipped ±25 kt); the same
pair for gust when GUST is present in the pgrb2b files (spec § Layers)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from ingest.cube import ForecastCube, GridMeta, VariableSpec, axis_offsets, utcnow_iso
from ingest.sources.base import (
    MS_TO_KT,
    decode_field,
    fetch_fields,
    head_ok,
    http,
    parse_idx,
    resolve_cycle,
    urls_digest,
)
from tilekit.codec import quantize

GEFS_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
LAYER = "ensemble"
MODEL = "gefs_0p50"
AXIS_NAME = "steps"
EXPECTED_MEMBERS = 31
ANOM_CLIP_KT = 25.0

# 3-hourly to 144 h + 6-hourly to 384 h (89 steps; Phase 0 lever 1)
STEP_AXIS = axis_offsets((0, 144, 3), (150, 384, 6))

MEMBERS = ["gec00"] + [f"gep{m:02d}" for m in range(1, 31)]

WIND_GRIB = [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")]
GUST_GRIB = ("GUST", "surface")

_IDX_TEMPLATE = GEFS_BASE + "/gefs.{date}/{hh}/atmos/pgrb2ap5/gep30.t{hh}z.pgrb2a.0p50.f384.idx"


def a_url(cycle: datetime, member: str, step: int) -> str:
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{GEFS_BASE}/gefs.{date}/{hh}/atmos/pgrb2ap5/{member}.t{hh}z.pgrb2a.0p50.f{step:03d}"


def b_url(cycle: datetime, member: str, step: int) -> str:
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    return f"{GEFS_BASE}/gefs.{date}/{hh}/atmos/pgrb2bp5/{member}.t{hh}z.pgrb2b.0p50.f{step:03d}"


def resolve(requested: datetime | None = None) -> datetime:
    return resolve_cycle(_IDX_TEMPLATE, requested)


def mean_and_anomaly(speeds: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """[member, time, nlat, nlon] speeds -> (ensemble mean, anomalies clipped
    to ±25 kt). Members reconstruct as mean + anomaly."""
    mean = speeds.mean(axis=0, dtype=np.float64).astype(np.float32)
    anom = np.clip(speeds - mean[None], -ANOM_CLIP_KT, ANOM_CLIP_KT)
    return mean, anom


def available_members(cycle: datetime) -> list[str]:
    """Members whose final-step a-file .idx exists."""

    def ok(member: str) -> bool:
        return head_ok(a_url(cycle, member, STEP_AXIS[-1]) + ".idx")

    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(pool.map(ok, MEMBERS))
    return [m for m, f in zip(MEMBERS, flags) if f]


def gust_available(cycle: datetime) -> bool:
    """Probe the pgrb2b .idx of the control member at the first and last
    forecast steps for GUST:surface."""
    for step in (STEP_AXIS[1], STEP_AXIS[-1]):
        try:
            idx = parse_idx(http(b_url(cycle, "gec00", step) + ".idx").decode())
        except Exception:
            return False
        if not any((v, lv) == GUST_GRIB for v, lv, _, _ in idx):
            return False
    return True


def _speed_stack(
    cycle: datetime, members: list[str], url_fn, wanted: list[tuple[str, str]], workers: int
) -> tuple[np.ndarray, GridMeta]:
    """Download+decode wind speed (hypot of u,v — or a single gust field) for
    every (member, step) -> float32 [member, time, nlat, nlon]."""

    def one(job: tuple[int, int]):
        mi, si = job
        return mi, si, fetch_fields(url_fn(cycle, members[mi], STEP_AXIS[si]), wanted)

    jobs = [(mi, si) for mi in range(len(members)) for si in range(len(STEP_AXIS))]
    meta = None
    out: np.ndarray | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for mi, si, fields in pool.map(one, jobs):
            grids = [decode_field(fields[w]) for w in wanted]
            meta = meta or grids[0][1]
            if len(grids) == 2:
                values = np.hypot(grids[0][0], grids[1][0]) * MS_TO_KT
            else:
                values = grids[0][0] * MS_TO_KT
            if out is None:
                out = np.empty(
                    (len(members), len(STEP_AXIS), meta.nlat, meta.nlon), dtype=np.float32
                )
            out[mi, si] = values
    assert out is not None and meta is not None
    return out, meta


def build_cube(
    cycle: datetime, *, allow_member_drift: bool = False, workers: int = 16
) -> ForecastCube:
    members = available_members(cycle)
    if len(members) != EXPECTED_MEMBERS and not allow_member_drift:
        raise RuntimeError(
            f"GEFS cycle {cycle:%Y%m%dT%H}Z has {len(members)}/{EXPECTED_MEMBERS} members "
            "(pass --allow-member-drift to proceed anyway)"
        )
    if not members:
        raise RuntimeError(f"GEFS cycle {cycle:%Y%m%dT%H}Z has no members available")

    with_gust = gust_available(cycle)

    variables: list[VariableSpec] = [
        VariableSpec("wind_kt_mean", AXIS_NAME, "i16", 0.01),
        VariableSpec("wind_kt_anom", AXIS_NAME, "i8", 0.2, per_member=True),
    ]
    arrays: dict[str, np.ndarray] = {}

    speeds, meta = _speed_stack(cycle, members, a_url, WIND_GRIB, workers)
    mean, anom = mean_and_anomaly(speeds)
    del speeds
    arrays["wind_kt_mean"] = quantize(mean, "i16", 0.01)
    arrays["wind_kt_anom"] = quantize(anom, "i8", 0.2)
    del mean, anom

    if with_gust:
        gusts, _ = _speed_stack(cycle, members, b_url, [GUST_GRIB], workers)
        g_mean, g_anom = mean_and_anomaly(gusts)
        del gusts
        variables += [
            VariableSpec("gust_kt_mean", AXIS_NAME, "i16", 0.01),
            VariableSpec("gust_kt_anom", AXIS_NAME, "i8", 0.2, per_member=True),
        ]
        arrays["gust_kt_mean"] = quantize(g_mean, "i16", 0.01)
        arrays["gust_kt_anom"] = quantize(g_anom, "i8", 0.2)
        del g_mean, g_anom

    provenance = {
        "source": "NOAA GEFS 0.5deg (noaa-gefs-pds), pgrb2ap5 wind"
        + (" + pgrb2bp5 gust" if with_gust else ""),
        "members": members,
        "gust": "included" if with_gust else "unavailable in pgrb2b .idx — wind-only run",
        "source_urls_digest": urls_digest(
            [a_url(cycle, m, s) for m in members for s in (STEP_AXIS[0], STEP_AXIS[-1])]
        ),
        "fetched_at": utcnow_iso(),
    }
    return ForecastCube(
        layer=LAYER,
        model=MODEL,
        cycle=cycle,
        grid=meta,
        time_axes={AXIS_NAME: STEP_AXIS},
        variables=variables,
        arrays=arrays,
        member_count=len(members),
        provenance=provenance,
    )
