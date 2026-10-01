"""`ingest <layer>` — build, validate, tile and publish one forecast layer.

`ingest land` is the odd one out: it compiles the conservative routing index
from shoreline and bathymetry rather than a forecast cycle, and it is a
one-shot artifact rather than a scheduled run. It shares this entry point, the
object stores and the publish discipline, and nothing else.

--dry-run DIR writes the exact R2 layout to a local directory instead of R2
(local verification, browser dev fixtures).

A run whose resolved cycle is already in the target's `latest.json` (or older
than the one there) exits 0 before downloading anything, so a layer can be
triggered as often as its provider might update. --force publishes anyway.

--wait-minutes N (with --cycle) lets a run start before its provider has
finished the cycle: it re-checks readiness every minute or two until the
cycle is out, then continues as above, and exits 1 if N minutes pass first.
The dispatcher (dispatcher/) starts each layer this way at its provider's
usual publication time."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

from ingest.cube import ForecastCube, cycle_iso
from ingest.publish import (
    DirStore,
    PublishError,
    make_r2_store_from_env,
    max_bucket_bytes_from_env,
    publish_run,
    published_layer,
)
from ingest.sources.base import CycleNotAvailableError, allow_missing_files, parse_cycle_arg
from ingest.tile import build_tiles
from ingest.validate import validate_cube

LAYERS = (
    "weather",
    "weather-ecmwf",
    "weather-ecmwf-short",
    "ensemble",
    "waves",
    "currents",
    "currents-ibi",
    "land",
)

# Allowed missing fraction per layer: atmospheric grids are global (only
# quantization-time gaps like APCP@f000 or polar masks), ocean-only layers
# are mostly land/ice-masked.
MAX_MISSING = {
    "weather": 0.05,
    "weather-ecmwf": 0.05,
    "weather-ecmwf-short": 0.05,
    "ensemble": 0.05,
    "waves": 0.80,
    "currents": 0.80,
    # The bounded IBI rectangle includes European and North African land, but
    # is mostly water.  Live 2026-08-24 data measured 0.418 missing; 0.45 keeps
    # the coastal mask normal while catching a materially truncated provider
    # subset (unlike the global-ocean 0.80 allowance).
    "currents-ibi": 0.45,
}


# Layers whose provider publishes late or rewrites in place: "not available
# yet" is a normal outcome for a scheduled run, so it exits 0 and a later
# trigger picks the cycle up. For GLO12 it must not fall back to RTOFS either.
SKIP_WHEN_NOT_AVAILABLE = {"weather-ecmwf", "weather-ecmwf-short", "currents", "currents-ibi"}

# Seconds between readiness checks while --wait-minutes runs. ECMWF answers
# frequent requests with 429s; Copernicus's metadata updates once a minute at
# best, so both are polled more gently than NOAA's S3 buckets.
POLL_SECONDS = {
    "weather": 60,
    "waves": 60,
    "ensemble": 60,
    "weather-ecmwf": 120,
    "weather-ecmwf-short": 120,
    "currents": 120,
    "currents-ibi": 120,
}


# How long a waited-for cycle's other files may still be uploading after its
# readiness file appears (see ingest.sources.base.allow_missing_files).
MISSING_FILES_GRACE_MINUTES = 15


class CycleWaitExpired(RuntimeError):
    """--wait-minutes ran out before the provider published the cycle."""


def _resolve(layer: str, requested: datetime | None) -> datetime:
    """The cycle this run would publish, from provider metadata only."""
    if layer == "weather":
        from ingest.sources import gfs

        return gfs.resolve(requested)
    if layer == "ensemble":
        from ingest.sources import gefs

        return gefs.resolve(requested)
    if layer == "waves":
        from ingest.sources import gfswave

        return gfswave.resolve(requested)
    if layer == "weather-ecmwf":
        from ingest.sources import ecmwf_open

        return ecmwf_open.resolve(requested)
    if layer == "weather-ecmwf-short":
        from ingest.sources import ecmwf_open

        return ecmwf_open.resolve_short(requested)
    if layer == "currents":
        from ingest.sources import cmems

        return cmems.resolve(requested)
    if layer == "currents-ibi":
        from ingest.sources import ibi

        return ibi.resolve(requested)
    raise ValueError(f"unknown layer {layer}")


def _build(args: argparse.Namespace, cycle: datetime, requested: datetime | None) -> ForecastCube:
    layer = args.layer
    if layer == "weather":
        from ingest.sources import gfs

        return gfs.build_cube(cycle)
    if layer == "ensemble":
        from ingest.sources import gefs

        return gefs.build_cube(cycle, allow_member_drift=args.allow_member_drift)
    if layer == "waves":
        from ingest.sources import gfswave

        return gfswave.build_cube(cycle)
    if layer == "weather-ecmwf":
        from ingest.sources import ecmwf_open

        return ecmwf_open.build_cube(cycle)
    if layer == "weather-ecmwf-short":
        from ingest.sources import ecmwf_open

        return ecmwf_open.build_short_cube(cycle)
    if layer == "currents":
        from ingest.sources import cmems, rtofs

        try:
            return cmems.build_cube(cycle)
        except CycleNotAvailableError:
            raise  # GLO12 not written yet: skip or wait, never publish RTOFS for the day
        except Exception as exc:  # CMEMS outage: fall back to RTOFS
            print(f"ingest: CMEMS failed ({type(exc).__name__}: {exc}); trying RTOFS fallback")
            cube = rtofs.build_cube(rtofs.resolve(requested))
            cube.provenance["fallback"] = f"CMEMS unavailable: {type(exc).__name__}: {exc}"
            return cube
    if layer == "currents-ibi":
        from ingest.sources import ibi

        return ibi.build_cube(cycle)
    raise ValueError(f"unknown layer {layer}")


def _utc(cycle: datetime) -> datetime:
    return (
        cycle.replace(tzinfo=timezone.utc)
        if cycle.tzinfo is None
        else cycle.astimezone(timezone.utc)
    )


def wait_for_cycle(
    layer: str,
    requested: datetime,
    wait_minutes: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> datetime:
    """Resolve `requested`, re-checking every POLL_SECONDS while the provider
    reports it not available, for at most `wait_minutes`."""
    deadline = clock() + wait_minutes * 60
    interval = POLL_SECONDS[layer]
    checks = 0
    while True:
        checks += 1
        try:
            cycle = _resolve(layer, requested)
        except CycleNotAvailableError as exc:
            remaining = deadline - clock()
            if remaining <= 0:
                raise CycleWaitExpired(
                    f"cycle {cycle_iso(requested)} not available after {wait_minutes:g} min "
                    f"({checks} checks; last: {exc})"
                ) from exc
            if checks == 1:
                print(
                    f"ingest {layer}: waiting up to {wait_minutes:g} min for cycle "
                    f"{cycle_iso(requested)}, checking every {interval} s ({exc})",
                    flush=True,
                )
            sleep(min(interval, remaining))
            continue
        if checks > 1:
            print(
                f"ingest {layer}: cycle {cycle_iso(cycle)} available after {checks} checks",
                flush=True,
            )
        # its readiness file can land before the rest of the cycle's files
        allow_missing_files(MISSING_FILES_GRACE_MINUTES * 60)
        return cycle


def already_published(store, layer: str, cycle: datetime) -> dict | None:
    """The layer's `latest.json` entry when it already holds `cycle` or a newer one."""
    entry = published_layer(store, layer)
    if not entry:
        return None
    published = datetime.strptime(entry["cycle"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return entry if published >= _utc(cycle) else None


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="ingest", description=__doc__)
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--cycle", help="explicit cycle YYYYMMDDTHH (default: latest complete)")
    parser.add_argument(
        "--wait-minutes",
        type=float,
        default=0,
        metavar="N",
        help=(
            "with --cycle: re-check every minute or two until the provider has published "
            "the cycle, for at most N minutes, then exit 1 (default 0: check once)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        metavar="DIR",
        help="write the run layout to a local directory instead of R2",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "publish even when latest.json already has this cycle or a newer one; "
            "re-publishing a live run id rewrites tiles clients cache forever, so "
            "only repair a run that never served correct tiles"
        ),
    )
    parser.add_argument(
        "--allow-member-drift",
        action="store_true",
        help="ensemble only: proceed even when member count != 31",
    )
    parser.add_argument(
        "--domain",
        default="nweu",
        help="land only: which routing domain to compile (default: nweu)",
    )
    parser.add_argument(
        "--cache-dir",
        default=".land-cache",
        help="land only: where the shoreline archive and DTM blocks are kept between runs",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        help="land only: outward conservatism buffer in metres (default: the recorded 200)",
    )
    parser.add_argument(
        "--safety-contour-m",
        type=float,
        help="land only: depth below LAT treated as unsailable (default: the recorded 0)",
    )
    args = parser.parse_args(argv)

    if args.layer == "land":
        from ingest.land.cli import run_land

        return run_land(args)

    if args.wait_minutes < 0:
        parser.error("--wait-minutes must be 0 or more")
    if args.wait_minutes and not args.cycle:
        parser.error("--wait-minutes needs --cycle: a run waits for one named cycle")

    t0 = time.time()
    requested = parse_cycle_arg(args.cycle) if args.cycle else None
    store = DirStore(args.dry_run) if args.dry_run else make_r2_store_from_env()
    try:
        if args.wait_minutes:
            try:
                cycle = wait_for_cycle(
                    args.layer, requested, args.wait_minutes, clock=clock, sleep=sleep
                )
            except CycleWaitExpired as exc:
                print(f"ingest {args.layer}: {exc}")
                return 1
            t0 = time.time()  # status duration_s covers the job, not the wait
        else:
            cycle = _resolve(args.layer, requested)
        published = None if args.force else already_published(store, args.layer, cycle)
        if published:
            print(
                f"ingest {args.layer}: cycle {cycle_iso(cycle)} already published "
                f"(latest {published['run_id']}, published {published['published_at']}); "
                "nothing to do (--force publishes anyway)"
            )
            return 0
        cube = _build(args, cycle, requested)
    except CycleNotAvailableError as exc:
        if args.layer in SKIP_WHEN_NOT_AVAILABLE:
            print(f"ingest {args.layer}: cycle not available yet, skipping ({exc})")
            return 0
        print(f"ingest {args.layer}: no complete cycle available ({exc})")
        return 1

    print(f"ingest {args.layer}: cycle {cube.cycle_iso} -> run {cube.run_id}")

    expected_axes = None
    if args.layer == "currents-ibi":
        from ingest.sources import ibi

        expected_axes = {ibi.AXIS_NAME: ibi.STEP_AXIS}
    report = validate_cube(
        cube,
        max_missing=MAX_MISSING[args.layer],
        expected_axes=expected_axes,
    )
    print(report.summary())
    if not report.ok:
        print(f"ingest {args.layer}: validation FAILED, aborting before upload")
        return 1

    tiles = build_tiles(cube)
    total = sum(len(gz) for _, gz in tiles)
    print(f"ingest {args.layer}: {len(tiles)} tiles, {total / 1e6:.1f} MB gz")

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
