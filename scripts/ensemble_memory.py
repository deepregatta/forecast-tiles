#!/usr/bin/env python3
"""Scaled replay of the ensemble layer's array code, for its peak memory.

Replays the numpy buffers `gefs.build_cube` holds after its downloads: the
float32 wind-speed stack [members, steps, 361, 720] (2.87 GB at full size),
its quantized mean and anomalies, then the gust stack and its pair while the
wind arrays are kept. The grid is cut to 1/SCALE of its rows; every buffer has
the row dimension, so the peak scales back linearly. Downloads, GRIB decode
buffers and interpreter overhead come on top.

A public-repo `ubuntu-latest` runner has 16 GB; the layer runs 4x a day once
the dispatcher publishes every GEFS cycle (Passage docs/grib-export-plan.md,
Phase 5A task 9).

Usage: uv run scripts/ensemble_memory.py [--scale 8]
"""

from __future__ import annotations

import argparse
import sys
import tracemalloc
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest.sources import gefs  # noqa: E402

NLAT, NLON = 361, 720  # GEFS 0.5 deg global


def replay(nlat: int) -> int:
    """Peak traced bytes of the wind then gust phases on an nlat-row grid."""
    shape = (gefs.EXPECTED_MEMBERS, len(gefs.STEP_AXIS), nlat, NLON)
    rng = np.random.default_rng(0)
    tracemalloc.start()
    arrays = {}
    for name in ("wind", "gust"):
        speeds = np.empty(shape, dtype=np.float32)  # _speed_stack's output
        for m in range(shape[0]):
            speeds[m] = rng.gamma(3.0, 5.0, size=shape[1:])
        arrays[f"{name}_kt_mean"], arrays[f"{name}_kt_anom"] = gefs.quantize_mean_and_anomaly(
            speeds
        )
        del speeds
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=int, default=8, help="replay 1/SCALE of the rows")
    args = parser.parse_args()
    nlat = -(-NLAT // args.scale)
    peak = replay(nlat)
    full = peak * NLAT / nlat
    stack = gefs.EXPECTED_MEMBERS * len(gefs.STEP_AXIS) * NLAT * NLON * 4
    print(
        f"ensemble: {gefs.EXPECTED_MEMBERS} members x {len(gefs.STEP_AXIS)} steps, "
        f"replayed {nlat}/{NLAT} rows: peak {peak / 1e9:.2f} GB -> full grid "
        f"{full / 1e9:.1f} GB (float32 speed stack alone {stack / 1e9:.2f} GB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
