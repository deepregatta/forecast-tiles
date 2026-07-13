"""`ingest <layer>` — build, validate, tile and publish one forecast layer.

--dry-run DIR writes the exact R2 layout to a local directory instead of R2
(local verification, browser dev fixtures)."""

from __future__ import annotations

import argparse
import sys
import time

from ingest.cube import ForecastCube
from ingest.publish import (
    DirStore,
    PublishError,
    make_r2_store_from_env,
    max_bucket_bytes_from_env,
    publish_run,
)
from ingest.sources.base import CycleNotAvailableError, parse_cycle_arg
from ingest.tile import build_tiles
from ingest.validate import validate_cube

LAYERS = ("weather", "weather-ecmwf", "ensemble", "waves", "currents")

# Allowed missing fraction per layer: atmospheric grids are global (only
# quantization-time gaps like APCP@f000 or polar masks), ocean-only layers
# are mostly land/ice-masked.
MAX_MISSING = {
    "weather": 0.05,
    "weather-ecmwf": 0.05,
    "ensemble": 0.05,
    "waves": 0.80,
    "currents": 0.80,
}


def _build(args: argparse.Namespace) -> ForecastCube:
    requested = parse_cycle_arg(args.cycle) if args.cycle else None
    layer = args.layer
    if layer == "weather":
        from ingest.sources import gfs

        return gfs.build_cube(gfs.resolve(requested))
    if layer == "ensemble":
        from ingest.sources import gefs

        return gefs.build_cube(gefs.resolve(requested), allow_member_drift=args.allow_member_drift)
    if layer == "waves":
        from ingest.sources import gfswave

        return gfswave.build_cube(gfswave.resolve(requested))
    if layer == "weather-ecmwf":
        from ingest.sources import ecmwf_open

        return ecmwf_open.build_cube(ecmwf_open.resolve(requested))
    if layer == "currents":
        from ingest.sources import cmems, rtofs

        try:
            return cmems.build_cube(cmems.resolve(requested))
        except Exception as exc:  # CMEMS outage: fall back to RTOFS
            print(f"ingest: CMEMS failed ({type(exc).__name__}: {exc}); trying RTOFS fallback")
            cube = rtofs.build_cube(rtofs.resolve(requested))
            cube.provenance["fallback"] = f"CMEMS unavailable: {type(exc).__name__}: {exc}"
            return cube
    raise ValueError(f"unknown layer {layer}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest", description=__doc__)
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--cycle", help="explicit cycle YYYYMMDDTHH (default: latest complete)")
    parser.add_argument(
        "--dry-run",
        metavar="DIR",
        help="write the run layout to a local directory instead of R2",
    )
    parser.add_argument(
        "--allow-member-drift",
        action="store_true",
        help="ensemble only: proceed even when member count != 31",
    )
    args = parser.parse_args(argv)

    t0 = time.time()
    try:
        cube = _build(args)
    except CycleNotAvailableError as exc:
        if args.layer == "weather-ecmwf":
            # ECMWF publishes late/partially; scheduled runs skip rather than fail
            print(f"ingest {args.layer}: cycle not available yet, skipping ({exc})")
            return 0
        print(f"ingest {args.layer}: no complete cycle available ({exc})")
        return 1

    print(f"ingest {args.layer}: cycle {cube.cycle_iso} -> run {cube.run_id}")

    report = validate_cube(cube, max_missing=MAX_MISSING[args.layer])
    print(report.summary())
    if not report.ok:
        print(f"ingest {args.layer}: validation FAILED, aborting before upload")
        return 1

    tiles = build_tiles(cube)
    total = sum(len(gz) for _, gz in tiles)
    print(f"ingest {args.layer}: {len(tiles)} tiles, {total / 1e6:.1f} MB gz")

    store = DirStore(args.dry_run) if args.dry_run else make_r2_store_from_env()
    try:
        result = publish_run(
            store, cube, tiles, report, max_bucket_bytes=max_bucket_bytes_from_env(), started_at=t0
        )
    except PublishError as exc:
        print(f"ingest {args.layer}: publish FAILED: {exc}")
        return 1

    dest = args.dry_run or "R2"
    print(
        f"ingest {args.layer}: published {result.run_id} to {dest} "
        f"({result.tile_count} tiles, {result.bytes / 1e6:.1f} MB, {result.duration_s}s; "
        f"previous={result.previous_run_id}, deleted={result.deleted_runs})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
