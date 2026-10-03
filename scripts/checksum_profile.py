#!/usr/bin/env python3
"""Offline bounded encode/compress/manifest comparison on identical PFT1 bytes.

Uses native grid spacing, full time axes and synthetic adapter-shaped fields, never
provider downloads. Timing excludes fixture setup and memory tracing. Each hash
backend gets one warmup, then alternating measured runs. Memory is a separate
traced pass; RSS includes interpreter/imports/cube, traced bytes exclude the cube.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest import publish, tile  # noqa: E402
from ingest.cube import ForecastCube, GridMeta, VariableSpec  # noqa: E402
from ingest.sources import gefs, gfs  # noqa: E402
from ingest.validate import ValidationReport  # noqa: E402

STAMP = "2026-10-03T06:00:00Z"


def python_fnv64(data: bytes) -> str:
    """Original implementation, kept here as the comparison oracle."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def fixture(name: str) -> ForecastCube:
    rng = np.random.default_rng(20261003)
    cycle = datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
    if name == "weather":
        grid = GridMeta(40, -10, 0.25, 0.25, 40, 320)
        axes = {"hourly": gfs.HOURLY_AXIS, "h3": gfs.H3_AXIS}
        specs = [v.public() for v in gfs.HOURLY_VARS + gfs.H3_VARS]
        model, members, tile_deg = gfs.MODEL, 1, 10
    elif name == "ensemble":
        grid = GridMeta(40, -10, 0.5, 0.5, 20, 160)
        axes = {"steps": gefs.STEP_AXIS}
        specs = [
            VariableSpec(f"{v}_kt_{kind}", "steps", dtype, scale, per_member=kind == "anom")
            for v in ("wind", "gust")
            for kind, dtype, scale in (("mean", "i16", 0.01), ("anom", "i8", 0.2))
        ]
        model, members, tile_deg = gefs.MODEL, 31, 10
    else:
        cycle = cycle.replace(hour=3)
        grid = GridMeta(40, -10, 0.025, 0.025, 100, 400)
        axes = {"hourly": list(range(52))}
        specs = [
            VariableSpec(v, "hourly", "i16", s)
            for v, s in (("wind_u_kt", 0.01), ("wind_v_kt", 0.01), ("gust_kt", 0.1))
        ]
        model, members, tile_deg = "arome_france", 1, 5
    arrays = {}
    for spec in specs:
        shape = (len(axes[spec.axis]), grid.nlat, grid.nlon)
        if spec.per_member:
            shape = (members, *shape)
        # Low spatial variation plus seeded noise gives compression a bounded
        # weather-like workload; this is not a measured provider entropy model.
        base = np.linspace(100, 500, grid.nlon, dtype=np.int16)[None, None, :]
        if spec.dtype == "i8":
            arr = rng.integers(-30, 31, shape, dtype=np.int8)
        else:
            arr = rng.integers(-30, 31, shape, dtype=np.int16) + base
        if name == "regional":
            # Open-Meteo leaves float32 knot fields for encode_tile to quantize.
            arr = arr.astype(np.float32) * np.float32(spec.scale)
            if spec.name == "gust_kt":
                arr[0] = np.nan  # the maximum diagnostic is absent at +0 h
        arrays[spec.name] = arr
    return ForecastCube(
        layer="weather-arome" if name == "regional" else name,
        model=model,
        cycle=cycle,
        grid=grid,
        time_axes=axes,
        variables=specs,
        arrays=arrays,
        member_count=members,
        tile_deg=tile_deg,
        provenance={"source": "bounded synthetic checksum profile"},
    )


def run(cube: ForecastCube, hasher, *, trace: bool = False) -> tuple[dict, bytes]:
    phases = dict(encode_s=0.0, compress_s=0.0, hash_s=0.0, hash_calls=0, hashed_bytes=0)
    original_encode, original_compress = tile.encode_tile, tile.gzip.compress
    raw_bytes = 0

    def encode(*args, **kwargs):
        nonlocal raw_bytes
        start = time.perf_counter()
        data = original_encode(*args, **kwargs)
        phases["encode_s"] += time.perf_counter() - start
        raw_bytes += len(data)
        return data

    def compress(*args, **kwargs):
        start = time.perf_counter()
        data = original_compress(*args, **kwargs)
        phases["compress_s"] += time.perf_counter() - start
        return data

    def hash_bytes(data):
        start = time.perf_counter()
        result = hasher(data)
        phases["hash_s"] += time.perf_counter() - start
        phases["hash_calls"] += 1
        phases["hashed_bytes"] += len(data)
        return result

    if trace:
        tracemalloc.start()
    with (
        patch.object(tile, "encode_tile", encode),
        patch.object(tile.gzip, "compress", compress),
        patch.object(publish, "fnv64", hash_bytes),
    ):
        start = time.perf_counter()
        tiles = tile.build_tiles(cube, generated_at=STAMP)
        built = time.perf_counter()
        retained_after_tiles = tracemalloc.get_traced_memory()[0] if trace else None
        manifest = publish.build_manifest(
            cube, tiles, ValidationReport(), published_at=STAMP, validated_at=STAMP
        )
        end = time.perf_counter()
    phases.update(build_tiles_s=built - start, manifest_s=end - built, total_s=end - start)
    if trace:
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        phases.update(
            traced_retained_after_tiles_bytes=retained_after_tiles,
            traced_retained_after_manifest_bytes=retained,
            traced_peak_bytes=peak,
            process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        )
    phases.update(
        tile_count=len(tiles),
        compressed_bytes=sum(len(gz) for _, gz in tiles),
        uncompressed_bytes=raw_bytes,
        cube_array_bytes=sum(a.nbytes for a in cube.arrays.values()),
    )
    digest = hashlib.sha256()
    for tid, gz in tiles:
        digest.update(tid.encode())
        digest.update(gz)
    digest.update(publish.json_bytes(manifest))
    return phases, digest.digest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=("weather", "ensemble", "regional"), required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--cpu", type=int, help="pin timing and memory children to a Linux CPU")
    parser.add_argument("--memory-backend", choices=("python", "current"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.cpu is not None:
        os.sched_setaffinity(0, {args.cpu})
    cube = fixture(args.fixture)
    backends = {"python": python_fnv64, "current": publish.fnv64}
    if args.memory_backend:
        measured, digest = run(cube, backends[args.memory_backend], trace=True)
        print(json.dumps({"measured": measured, "tile_and_manifest_sha256": digest.hex()}))
        return
    expected = None
    timings = {name: [] for name in backends}
    for i in range(args.repeats + 1):
        order = list(backends) if i % 2 == 0 else list(reversed(backends))
        for name in order:
            phases, digest = run(cube, backends[name])
            if expected is None:
                expected = digest
            assert digest == expected, "tile/manifest bytes changed"
            if i:
                timings[name].append(phases)
    result = {
        "fixture": args.fixture,
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "fnv_c": version("fnv-c"),
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "warmups_per_backend": 1,
        "repeats": args.repeats,
        "tile_and_manifest_sha256": expected.hex(),
        "samples": timings,
        "median": {
            n: {k: statistics.median(s[k] for s in samples) for k in samples[0]}
            for n, samples in timings.items()
        },
    }
    if args.memory:
        # Separate fresh processes: RSS must not inherit the timing high-water
        # mark. Traced runs are never used for before/after timing claims.
        result["memory"] = {}
        for name in backends:
            child = subprocess.run(
                [sys.executable, __file__, "--fixture", args.fixture, "--memory-backend", name],
                check=True,
                capture_output=True,
                text=True,
            )
            memory = json.loads(child.stdout)
            assert memory["tile_and_manifest_sha256"] == expected.hex()
            result["memory"][name] = memory["measured"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
