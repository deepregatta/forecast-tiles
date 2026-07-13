#!/usr/bin/env python3
"""Phase 0 go/no-go: measure real PFT1 tile sizes from live forecast cycles.

Downloads sampled forecast steps of one recent cycle per layer (GFS weather,
GEFS ensemble, GFS-Wave, ECMWF open data) via .idx byte-range subsetting from
the AWS open-data mirrors, quantizes + tiles them with the real tilekit codec,
gzips every tile, and extrapolates to the full per-run tile set.

Currents (CMEMS GLO12) are ESTIMATED analytically (no credentials in this
environment): raw int16 size x ocean fraction x the gzip ratio measured on the
GFS wind layer. Marked as an estimate in the output.

Usage: uv run scripts/size_prototype.py [--layers weather,ensemble,waves,ecmwf,currents]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes
import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tilekit.codec import encode_tile, quantize  # noqa: E402
from tilekit.tiles import TILE_DEG, tile_id, tiles_for_grid  # noqa: E402

GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GEFS_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
MS_TO_KT = 1.943844
GZIP_LEVEL = 9
GO_LIMIT_GB = 4.0

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "passage-forecast-tiles-prototype"


# ---------------------------------------------------------------- download


def http(url: str, *, headers: dict | None = None, retries: int = 3) -> bytes:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, headers=headers or {}, timeout=120)
            r.raise_for_status()
            return r.content
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError


def head_ok(url: str) -> bool:
    try:
        return SESSION.head(url, timeout=30).status_code == 200
    except Exception:
        return False


def parse_idx(idx_text: str) -> list[tuple[str, str, int, int | None]]:
    """Return (var, level, start, end_exclusive|None) per GRIB message."""
    rows = []
    lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        parts = ln.split(":")
        start = int(parts[1])
        end = int(lines[i + 1].split(":")[1]) if i + 1 < len(lines) else None
        rows.append((parts[3], parts[4], start, end))
    return rows


def fetch_fields(
    url: str, wanted: list[tuple[str, str]], optional: set[tuple[str, str]] = frozenset()
) -> dict[tuple[str, str], bytes]:
    idx = parse_idx(http(url + ".idx").decode())
    out: dict[tuple[str, str], bytes] = {}
    for var, level in wanted:
        match = next(((s, e) for v, lv, s, e in idx if v == var and lv == level), None)
        if match is None:
            if (var, level) in optional:
                continue  # e.g. APCP has no accumulation at f000
            raise KeyError(f"{var}:{level} not in {url}.idx")
        start, end = match
        range_header = f"bytes={start}-" if end is None else f"bytes={start}-{end - 1}"
        out[(var, level)] = http(url, headers={"Range": range_header})
    return out


# ------------------------------------------------------------------ decode


def decode_field(msg: bytes) -> tuple[np.ndarray, dict]:
    """GRIB message -> (values[lat_asc, lon_from_-180] float32 NaN-masked, grid meta)."""
    gid = eccodes.codes_new_from_message(msg)
    try:
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        lat1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
        lon1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
        di = eccodes.codes_get(gid, "iDirectionIncrementInDegrees")
        dj = eccodes.codes_get(gid, "jDirectionIncrementInDegrees")
        j_positive = eccodes.codes_get(gid, "jScansPositively")
        eccodes.codes_set(gid, "missingValue", 1.0e20)
        values = eccodes.codes_get_values(gid).reshape(nj, ni).astype(np.float32)
    finally:
        eccodes.codes_release(gid)
    values[values >= 9.0e19] = np.nan

    lat0 = lat1 if j_positive else lat1 - dj * (nj - 1)
    if not j_positive:
        values = values[::-1]
    lons = (lon1 + np.arange(ni) * di + 180.0) % 360.0 - 180.0
    roll = int(np.argmin(lons))  # index of the smallest lon -> becomes column 0
    values = np.roll(values, -roll, axis=1)
    lon0 = lons[roll]
    return values, {"lat0": lat0, "lon0": lon0, "dlat": dj, "dlon": di, "nlat": nj, "nlon": ni}


# ------------------------------------------------------------------ tiling


def tile_index_ranges(meta: dict) -> list[tuple[int, int, slice, slice]]:
    """(tile_lat0, tile_lon0, lat_slice, lon_slice) for tiles intersecting the grid."""
    lats = meta["lat0"] + np.arange(meta["nlat"]) * meta["dlat"]
    lons = meta["lon0"] + np.arange(meta["nlon"]) * meta["dlon"]
    out = []
    for t_lat0, t_lon0 in tiles_for_grid(lats[0], lats[-1], lons[0], lons[-1]):
        li = np.where((lats >= t_lat0) & (lats < t_lat0 + TILE_DEG))[0]
        lj = np.where((lons >= t_lon0) & (lons < t_lon0 + TILE_DEG))[0]
        if len(li) and len(lj):
            out.append((t_lat0, t_lon0, slice(li[0], li[-1] + 1), slice(lj[0], lj[-1] + 1)))
    return out


def measure_tiles(
    layer: str,
    model: str,
    meta: dict,
    axis_name: str,
    offsets_h: list[int],
    variables: list[dict],
    arrays: dict[str, np.ndarray],  # quantized ints [member?, time, nlat, nlon]
    member_count: int = 1,
) -> tuple[int, int, int]:
    """Encode+gzip every tile; return (gz_bytes_total, tile_count, nonempty_tile_count)."""
    total = 0
    tiles = nonempty = 0
    base = {
        "spec": "PFT1",
        "schema_version": 1,
        "layer": layer,
        "model": model,
        "run_id": f"{layer}-prototype",
        "cycle": "prototype",
        "generated_at": "prototype",
        "dlat": meta["dlat"],
        "dlon": meta["dlon"],
        "member_count": member_count,
        "time_axes": {axis_name: {"base": "2026-01-01T00:00Z", "offsets_h": offsets_h}},
    }
    for t_lat0, t_lon0, li, lj in tile_index_ranges(meta):
        tile_arrays = {}
        any_data = False
        for var in variables:
            arr = arrays[var["name"]][..., li, lj]
            tile_arrays[var["name"]] = np.ascontiguousarray(arr)
            sentinel = -32768 if var["dtype"] == "i16" else -128
            if not any_data and np.any(arr != sentinel):
                any_data = True
        if not any_data:
            tiles += 1
            continue  # fully-missing tile: pipeline would not publish it
        header = {
            **base,
            "tile_id": tile_id(t_lat0, t_lon0),
            "lat0": float(t_lat0),
            "lon0": float(t_lon0),
            "nlat": tile_arrays[variables[0]["name"]].shape[-2],
            "nlon": tile_arrays[variables[0]["name"]].shape[-1],
            "variables": variables,
        }
        buf = encode_tile(header, tile_arrays)
        total += len(gzip.compress(buf, GZIP_LEVEL))
        tiles += 1
        nonempty += 1
    return total, tiles, nonempty


# ------------------------------------------------------- cycle resolution


def resolve_cycle(url_template: str, max_lookback_cycles: int = 8) -> datetime:
    """Latest cycle whose final .idx exists; url_template has {date}/{hh} placeholders."""
    now = datetime.now(timezone.utc)
    t = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for _ in range(max_lookback_cycles):
        if head_ok(url_template.format(date=t.strftime("%Y%m%d"), hh=t.strftime("%H"))):
            return t
        t -= timedelta(hours=6)
    raise RuntimeError(f"no complete cycle found for {url_template}")


# ------------------------------------------------------------- layers

WEATHER_HOURLY_VARS = [
    {
        "name": "wind_u_kt",
        "grib": ("UGRD", "10 m above ground"),
        "dtype": "i16",
        "scale": 0.01,
        "to_kt": True,
    },
    {
        "name": "wind_v_kt",
        "grib": ("VGRD", "10 m above ground"),
        "dtype": "i16",
        "scale": 0.01,
        "to_kt": True,
    },
    {"name": "gust_kt", "grib": ("GUST", "surface"), "dtype": "i16", "scale": 0.1, "to_kt": True},
]
WEATHER_H3_VARS = [
    {"name": "visibility_m", "grib": ("VIS", "surface"), "dtype": "i16", "scale": 50},
    {"name": "cape_jkg", "grib": ("CAPE", "surface"), "dtype": "i16", "scale": 1},
    {
        "name": "temp_c",
        "grib": ("TMP", "2 m above ground"),
        "dtype": "i16",
        "scale": 0.1,
        "k_to_c": True,
    },
    {
        "name": "dew_point_c",
        "grib": ("DPT", "2 m above ground"),
        "dtype": "i16",
        "scale": 0.1,
        "k_to_c": True,
    },
    {
        "name": "precip_mm",
        "grib": ("APCP", "surface"),
        "dtype": "i16",
        "scale": 0.1,
        "optional": True,
    },
]
WAVE_VARS = [
    {"name": "hs_m", "grib": ("HTSGW", "surface"), "dtype": "i16", "scale": 0.01},
    {"name": "period_s", "grib": ("PERPW", "surface"), "dtype": "i16", "scale": 0.1},
    {"name": "dir_deg", "grib": ("DIRPW", "surface"), "dtype": "i16", "scale": 0.1},
    {"name": "wind_wave_h_m", "grib": ("WVHGT", "surface"), "dtype": "i16", "scale": 0.01},
    {"name": "wind_wave_period_s", "grib": ("WVPER", "surface"), "dtype": "i16", "scale": 0.1},
    {"name": "wind_wave_dir_deg", "grib": ("WVDIR", "surface"), "dtype": "i16", "scale": 0.1},
    {"name": "swell_h_m", "grib": ("SWELL", "1 in sequence"), "dtype": "i16", "scale": 0.01},
    {"name": "swell_period_s", "grib": ("SWPER", "1 in sequence"), "dtype": "i16", "scale": 0.1},
    {"name": "swell_dir_deg", "grib": ("SWDIR", "1 in sequence"), "dtype": "i16", "scale": 0.1},
]


def quantize_field(values: np.ndarray, var: dict) -> np.ndarray:
    v = values * MS_TO_KT if var.get("to_kt") else values
    v = v - 273.15 if var.get("k_to_c") else v
    return quantize(v, var["dtype"], var["scale"])


def collect_steps(
    url_for_step, steps: list[int], var_specs: list[dict], workers: int = 12
) -> tuple[dict[str, np.ndarray], dict]:
    """Download+decode+quantize all (step, var) fields -> arrays [time, nlat, nlon]."""
    wanted = [v["grib"] for v in var_specs]
    optional = {v["grib"] for v in var_specs if v.get("optional")}

    def one(step: int):
        return step, fetch_fields(url_for_step(step), wanted, optional)

    fields_by_step: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for step, fields in pool.map(one, steps):
            fields_by_step[step] = fields

    meta = None
    arrays: dict[str, list[np.ndarray]] = {v["name"]: [] for v in var_specs}
    pending_nan: list[tuple[str, int]] = []
    for step in steps:
        for var in var_specs:
            msg = fields_by_step[step].get(var["grib"])
            if msg is None:
                pending_nan.append((var["name"], len(arrays[var["name"]])))
                arrays[var["name"]].append(None)  # backfill once grid meta is known
                continue
            values, m = decode_field(msg)
            meta = meta or m
            arrays[var["name"]].append(quantize_field(values, var))
    for name, idx_pos in pending_nan:
        nan_field = np.full((meta["nlat"], meta["nlon"]), np.nan, dtype=np.float32)
        var = next(v for v in var_specs if v["name"] == name)
        arrays[name][idx_pos] = quantize_field(nan_field, var)
    return {k: np.stack(v) for k, v in arrays.items()}, meta


def spec_public(var_specs: list[dict], **extra) -> list[dict]:
    keep = ("name", "dtype", "scale")
    return [{**{k: v[k] for k in keep}, "axis": "sampled", **extra} for v in var_specs]


def measure_weather() -> dict:
    t0 = time.time()
    cycle = resolve_cycle(GFS_BASE + "/gfs.{date}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f240.idx")
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")

    def url(s: int) -> str:
        return f"{GFS_BASE}/gfs.{date}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{s:03d}"

    near_hourly = list(range(0, 24))  # adjacent-hour redundancy
    far_3h = list(range(120, 144, 3))  # 3-hourly tail redundancy
    near_h3 = list(range(0, 24, 3))
    far_h3 = list(range(120, 144, 3))

    results = {}
    for label, steps, specs in [
        ("hourly_near", near_hourly, WEATHER_HOURLY_VARS),
        ("hourly_far", far_3h, WEATHER_HOURLY_VARS),
        ("h3_near", near_h3, WEATHER_H3_VARS),
        ("h3_far", far_h3, WEATHER_H3_VARS),
    ]:
        arrays, meta = collect_steps(url, steps, specs)
        gz, tiles, nonempty = measure_tiles(
            "weather", "gfs_0p25", meta, "sampled", steps, spec_public(specs), arrays
        )
        results[label] = {"gz": gz, "steps": len(steps), "tiles": tiles, "nonempty": nonempty}

    # full axes: hourly f000-f120 (121) + 3h f123-f240 (40); h3 0-240 (81 = 41 near + 40 far)
    est = (
        results["hourly_near"]["gz"] / results["hourly_near"]["steps"] * 121
        + results["hourly_far"]["gz"] / results["hourly_far"]["steps"] * 40
        + results["h3_near"]["gz"] / results["h3_near"]["steps"] * 41
        + results["h3_far"]["gz"] / results["h3_far"]["steps"] * 40
    )
    # gzip ratio of the wind/gust group, reused as the currents proxy
    raw_hourly = results["hourly_near"]["steps"] * 721 * 1440 * 2 * len(WEATHER_HOURLY_VARS)
    return {
        "layer": "weather",
        "cycle": cycle.isoformat(),
        "measured": results,
        "estimated_full_gz": est,
        "wind_gz_ratio": results["hourly_near"]["gz"] / raw_hourly,
        "wall_s": time.time() - t0,
    }


def measure_ensemble() -> dict:
    t0 = time.time()
    cycle = resolve_cycle(
        GEFS_BASE + "/gefs.{date}/{hh}/atmos/pgrb2ap5/gep30.t{hh}z.pgrb2a.0p50.f384.idx"
    )
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    members = ["gec00"] + [f"gep{m:02d}" for m in range(1, 31)]
    wanted = [("UGRD", "10 m above ground"), ("VGRD", "10 m above ground")]

    def speeds_for_steps(steps: list[int]) -> tuple[np.ndarray, dict]:
        def one(job):
            member, step = job
            u = f"{GEFS_BASE}/gefs.{date}/{hh}/atmos/pgrb2ap5/{member}.t{hh}z.pgrb2a.0p50.f{step:03d}"
            return member, step, fetch_fields(u, wanted)

        jobs = [(m, s) for m in members for s in steps]
        got: dict[tuple[str, int], dict] = {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            for member, step, fields in pool.map(one, jobs):
                got[(member, step)] = fields

        meta = None
        out = np.empty((len(members), len(steps)), dtype=object)
        for mi, member in enumerate(members):
            for si, step in enumerate(steps):
                u, meta_ = decode_field(got[(member, step)][wanted[0]])
                v, _ = decode_field(got[(member, step)][wanted[1]])
                meta = meta or meta_
                out[mi, si] = np.hypot(u, v) * MS_TO_KT
        speeds = np.stack([np.stack(list(row)) for row in out])  # [member, time, nlat, nlon]
        return speeds.astype(np.float32), meta

    def encode_block(steps: list[int]) -> dict:
        speeds, meta = speeds_for_steps(steps)
        mean = speeds.mean(axis=0)
        anom = np.clip(speeds - mean[None], -25.0, 25.0)
        arrays = {
            "wind_kt_mean": quantize(mean, "i16", 0.01),
            "wind_kt_anom": quantize(anom, "i8", 0.2),
        }
        variables = [
            {"name": "wind_kt_mean", "axis": "sampled", "dtype": "i16", "scale": 0.01},
            {
                "name": "wind_kt_anom",
                "axis": "sampled",
                "dtype": "i8",
                "scale": 0.2,
                "per_member": True,
            },
        ]
        gz, tiles, nonempty = measure_tiles(
            "ensemble",
            "gefs_0p50",
            meta,
            "sampled",
            steps,
            variables,
            arrays,
            member_count=len(members),
        )
        return {"gz": gz, "steps": len(steps), "tiles": tiles, "nonempty": nonempty}

    near = encode_block(list(range(0, 27, 3)))  # 9 steps, 3-hourly portion
    far = encode_block(list(range(246, 300, 6)))  # 9 steps, 6-hourly portion
    # full axis (Phase 0 lever 1 applied): 3h 0-144 (49 steps) + 6h 150-384 (40 steps);
    # gust members assumed = wind size
    wind_est = near["gz"] / near["steps"] * 49 + far["gz"] / far["steps"] * 40
    return {
        "layer": "ensemble",
        "cycle": cycle.isoformat(),
        "member_count": len(members),
        "measured": {"near": near, "far": far},
        "estimated_full_gz": wind_est * 2,
        "note": "gust members assumed same size as wind members (x2)",
        "wall_s": time.time() - t0,
    }


def measure_waves() -> dict:
    t0 = time.time()
    cycle = resolve_cycle(
        GFS_BASE + "/gfs.{date}/{hh}/wave/gridded/gfswave.t{hh}z.global.0p25.f384.grib2.idx"
    )
    date, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")

    def url(s: int) -> str:
        return f"{GFS_BASE}/gfs.{date}/{hh}/wave/gridded/gfswave.t{hh}z.global.0p25.f{s:03d}.grib2"

    blocks = {}
    for label, steps in [("near", list(range(0, 27, 3))), ("far", list(range(180, 207, 3)))]:
        arrays, meta = collect_steps(url, steps, WAVE_VARS)
        gz, tiles, nonempty = measure_tiles(
            "waves", "gfswave_0p25", meta, "sampled", steps, spec_public(WAVE_VARS), arrays
        )
        blocks[label] = {"gz": gz, "steps": len(steps), "tiles": tiles, "nonempty": nonempty}

    per_step = (
        blocks["near"]["gz"] / blocks["near"]["steps"]
        + blocks["far"]["gz"] / blocks["far"]["steps"]
    ) / 2
    return {
        "layer": "waves",
        "cycle": cycle.isoformat(),
        "measured": blocks,
        "estimated_full_gz": per_step * 129,  # 3-hourly 0-384
        "wall_s": time.time() - t0,
    }


def measure_ecmwf() -> dict:
    t0 = time.time()
    from ecmwf.opendata import Client

    client = Client(source="ecmwf", model="ifs", resol="0p25")
    steps = list(range(0, 27, 3))
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tmp:
        result = client.retrieve(type="fc", param=["10u", "10v"], step=steps, target=tmp.name)
        raw = Path(tmp.name).read_bytes()

    # split into messages and group by (param, step)
    fields: dict[tuple[str, int], np.ndarray] = {}
    meta = None
    offset = 0
    while offset < len(raw):
        gid = eccodes.codes_new_from_message(raw[offset:])
        try:
            length = eccodes.codes_get(gid, "totalLength")
            short = eccodes.codes_get(gid, "shortName")
            step = eccodes.codes_get(gid, "step")
        finally:
            eccodes.codes_release(gid)
        values, m = decode_field(raw[offset : offset + length])
        meta = meta or m
        fields[(short, int(step))] = values
        offset += length

    arrays = {
        "wind_u_kt": np.stack(
            [quantize(fields[("10u", s)] * MS_TO_KT, "i16", 0.01) for s in steps]
        ),
        "wind_v_kt": np.stack(
            [quantize(fields[("10v", s)] * MS_TO_KT, "i16", 0.01) for s in steps]
        ),
    }
    variables = [
        {"name": "wind_u_kt", "axis": "sampled", "dtype": "i16", "scale": 0.01},
        {"name": "wind_v_kt", "axis": "sampled", "dtype": "i16", "scale": 0.01},
    ]
    gz, tiles, nonempty = measure_tiles(
        "weather-ecmwf", "ecmwf_ifs_0p25", meta, "sampled", steps, variables, arrays
    )
    # full axis: 3h 0-144 (49 steps) + 6h 150-240 (16 steps)
    return {
        "layer": "weather-ecmwf",
        "cycle": str(result.datetime),
        "measured": {"near": {"gz": gz, "steps": len(steps), "tiles": tiles, "nonempty": nonempty}},
        "estimated_full_gz": gz / len(steps) * 65,
        "wall_s": time.time() - t0,
    }


def estimate_currents(wind_gz_ratio: float) -> dict:
    # GLO12 1/12deg -> 120x120 tiles, 2 vars i16, 6-hourly to 240h (41 steps;
    # Phase 0 lever 2 applied)
    per_tile_raw = 2 * 41 * 120 * 120 * 2
    ocean_fraction = 0.71
    est = 648 * per_tile_raw * ocean_fraction * wind_gz_ratio
    return {
        "layer": "currents",
        "estimated_full_gz": est,
        "note": (
            "ANALYTIC ESTIMATE (no CMEMS credentials locally): raw i16 x ocean fraction "
            f"0.71 x measured GFS wind gzip ratio {wind_gz_ratio:.3f}. "
            "Verify with real GLO12 data in Phase 3 CI."
        ),
        "wall_s": 0.0,
    }


# -------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layers", default="weather,ensemble,waves,ecmwf,currents")
    parser.add_argument("--out", default="size_prototype_results.json")
    args = parser.parse_args()
    layers = args.layers.split(",")

    results: list[dict] = []
    wind_gz_ratio = 0.5  # fallback if weather not measured

    for layer in layers:
        print(f"== measuring {layer} ...", flush=True)
        try:
            if layer == "weather":
                r = measure_weather()
                wind_gz_ratio = r["wind_gz_ratio"]
            elif layer == "ensemble":
                r = measure_ensemble()
            elif layer == "waves":
                r = measure_waves()
            elif layer == "ecmwf":
                r = measure_ecmwf()
            elif layer == "currents":
                r = estimate_currents(wind_gz_ratio)
            else:
                raise ValueError(f"unknown layer {layer}")
        except Exception as exc:  # keep measuring other layers
            r = {"layer": layer, "error": f"{type(exc).__name__}: {exc}"}
        results.append(r)
        gb = r.get("estimated_full_gz", 0) / 1e9
        print(
            f"   {layer}: {gb:.2f} GB gz estimated full run"
            + (f"  ({r['error']})" if "error" in r else ""),
            flush=True,
        )

    total = sum(r.get("estimated_full_gz", 0) for r in results)
    verdict = (
        "GO" if total / 1e9 <= GO_LIMIT_GB and not any("error" in r for r in results) else "NO-GO"
    )
    print("\n=== Phase 0 size verdict ===")
    for r in results:
        line = f"{r['layer']:<14} {r.get('estimated_full_gz', 0) / 1e9:>7.2f} GB"
        if "wall_s" in r:
            line += f"   ({r['wall_s']:.0f}s)"
        if "error" in r:
            line += f"   ERROR: {r['error']}"
        print(line)
    print(
        f"{'TOTAL':<14} {total / 1e9:>7.2f} GB   (x2 retention = {2 * total / 1e9:.2f} GB, guard 8 GB)"
    )
    print(f"VERDICT: {verdict} (limit {GO_LIMIT_GB} GB per generation)")

    Path(args.out).write_bytes(json.dumps(results, indent=2, default=str).encode())
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
