"""`ingest <layer>` — build, validate, tile and publish one forecast layer.

`ingest land` is the odd one out: it compiles the conservative routing index
from shoreline and bathymetry rather than a forecast cycle, and it is a
one-shot artifact rather than a scheduled run. It shares this entry point, the
object stores and the publish discipline, and nothing else.

--dry-run DIR writes the exact R2 layout to a local directory instead of R2
(local verification, browser dev fixtures).

A run whose resolved cycle is already in the target's `latest.json` (or older
than the one there) exits 0 before downloading anything, so a layer can be
triggered as often as its provider might update. --force re-publishes the
cycle `latest.json` already has; it never moves a layer back to an older one.

--wait-minutes N (with --cycle) lets a run start before its provider has
finished the cycle: it re-checks readiness every minute or two until the
cycle is out, then continues as above, and exits 1 if N minutes pass first.
The dispatcher (dispatcher/) starts each layer this way at its provider's
usual publication time.

Regional models from Open-Meteo's bulk files (weather-arome,
weather-icon-eu, weather-ukv: src/ingest/sources/openmeteo, `uv sync --extra openmeteo`)
take their settings from that registry and publish to latest-regional.json.
Until a model's production gate opens they run with --dry-run only."""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import nullcontext
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ingest.cube import ForecastCube, cycle_iso
from ingest.publish import (
    DirStore,
    PublishError,
    StalePublishError,
    check_not_older,
    make_r2_store_from_env,
    max_bucket_bytes_from_env,
    parse_cycle_iso,
    pointer_key_for,
    publish_run,
    published_layer,
)
from ingest.paid_work import Guard, Paused, RuntimeExpired, deadline, enforced
from ingest.regional_attempt import RegionalAttempt
from ingest.sources.base import CycleNotAvailableError, allow_missing_files, parse_cycle_arg
from ingest.sources.openmeteo import registry
from ingest.sources.openmeteo.reader import SourceError
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
    *registry.LAYERS,
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


# Per-layer settings: the tables above for the existing layers, the
# Open-Meteo registry for regional ones. Everything looks them up here, so a
# regional layer never meets a KeyError in a table it is not in.
def max_missing(layer: str) -> float:
    """Allowed missing fraction: overall for a global layer, per step inside
    the footprint for a regional one."""
    if layer in MAX_MISSING:
        return MAX_MISSING[layer]
    return registry.product(layer).max_interior_missing


def poll_seconds(layer: str) -> int:
    if layer in POLL_SECONDS:
        return POLL_SECONDS[layer]
    return registry.product(layer).poll_seconds


def skip_when_not_available(layer: str) -> bool:
    """A regional run not yet written is a normal outcome for a catch-up run."""
    return layer in SKIP_WHEN_NOT_AVAILABLE or registry.is_regional(layer)


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
    if registry.is_regional(layer):
        from ingest.sources.openmeteo import catalog

        return catalog.resolve(registry.product(layer), requested)
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
    if registry.is_regional(layer):
        from ingest.sources.openmeteo import adapter

        extra = {"metrics": args.attempt.source_metrics} if args.attempt else {}
        cube, source = adapter.build_cube(registry.product(layer), cycle, **extra)
        args.source_run = source  # rechecked just before publication
        return cube
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
    interval = poll_seconds(layer)
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
    """The layer's pointer entry when it already holds `cycle` or a newer one."""
    entry = published_layer(store, layer, pointer_key_for(layer))
    if not entry:
        return None
    return entry if parse_cycle_iso(entry["cycle"]) >= _utc(cycle) else None


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
            "publish even when latest.json already has this cycle (an older cycle is "
            "still refused); re-publishing a live run id rewrites tiles clients cache "
            "forever, so only repair a run that never served correct tiles"
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
    parser.add_argument(
        "--attempt-report",
        metavar="FILE",
        help="regional only: write attempt evidence outside the forecast layout",
    )
    args = parser.parse_args(argv)
    if args.attempt_report and not registry.is_regional(args.layer):
        parser.error("--attempt-report is only supported for regional layers")
    if (
        args.attempt_report
        and args.dry_run
        and Path(args.attempt_report).resolve().is_relative_to(Path(args.dry_run).resolve())
    ):
        parser.error("--attempt-report must be outside the immutable forecast layout")
    args.attempt = (
        RegionalAttempt(args.attempt_report, args.layer, args.cycle, bool(args.dry_run))
        if args.attempt_report
        else None
    )
    args.paid_guard = None
    args.paid_token = None
    # Match the existing workflow allowance: GLO12 may wait 240 minutes
    # before its 35-46 minute build. Other jobs have at most four hours.
    args.paid_seconds = (
        18000 if args.layer == "currents" else 9000 if registry.is_regional(args.layer) else 14400
    )
    try:
        if not args.dry_run and enforced():
            args.paid_guard = Guard.from_env()
            args.paid_guard.check()
        with deadline(args.paid_seconds) if args.paid_guard else nullcontext():
            code = _run_forecast(args, parser, clock=clock, sleep=sleep)
    except (Paused, RuntimeExpired) as exc:
        _outcome(args, "paused", "spending_control", exc)
        print(f"ingest {args.layer}: paused: {exc}; existing forecasts remain available")
        if args.attempt:
            args.attempt.finish(0)
        return 0
    except BaseException as exc:
        if args.attempt:
            args.attempt.outcome("failed", error=exc)
            code = exc.code if isinstance(exc, SystemExit) and isinstance(exc.code, int) else 1
            args.attempt.finish(code)
        raise
    else:
        if args.attempt:
            args.attempt.finish(code)
        return code
    finally:
        if args.paid_guard and args.paid_token:
            try:
                args.paid_guard.finish(args.layer, args.paid_token)
            except Paused as exc:
                print(f"ingest {args.layer}: lease retained until expiry: {exc}")
        if args.attempt:
            try:
                args.attempt.write()
            except (OSError, ValueError) as exc:
                # Publication may already be committed. A missing report is
                # unknown evidence; do not mislabel that forecast as failed.
                print(
                    f"ingest {args.layer}: attempt report unavailable ({type(exc).__name__})",
                    file=sys.stderr,
                )


def _observe(args, phase: str | None = None, **fields):
    if args.attempt:
        args.attempt.update(phase, **fields)


def _outcome(args, name: str, category: str | None = None, error: BaseException | None = None):
    if args.attempt:
        args.attempt.outcome(name, category, error)


def _run_forecast(args, parser, *, clock, sleep) -> int:

    if args.layer == "land":
        from ingest.land.cli import run_land

        if args.paid_guard:
            args.paid_token = args.paid_guard.acquire("land", args.domain, args.paid_seconds)
        return run_land(args)

    if args.wait_minutes < 0:
        parser.error("--wait-minutes must be 0 or more")
    regional = registry.product(args.layer) if registry.is_regional(args.layer) else None
    if regional and args.force:
        parser.error("--force is unsupported for immutable regional runs; use a new cycle")
    if regional and not args.dry_run and not regional.production_enabled:
        _outcome(args, "disabled", "configuration")
        print(
            f"ingest {args.layer}: not enabled for publication yet; use --dry-run DIR "
            "(docs/open-meteo-bulk-implementation-plan.md, Phase 3)"
        )
        return 1
    if args.wait_minutes and not args.cycle:
        parser.error("--wait-minutes needs --cycle: a run waits for one named cycle")

    t0 = time.time()
    requested = parse_cycle_arg(args.cycle) if args.cycle else None
    _observe(args, "resolve", requested_cycle=cycle_iso(requested) if requested else None)
    store = DirStore(args.dry_run) if args.dry_run else make_r2_store_from_env()
    if args.attempt:
        previous = published_layer(store, args.layer, pointer_key=pointer_key_for(args.layer))
        _observe(
            args,
            last_success_before={
                name: previous.get(name) for name in ("run_id", "cycle", "published_at")
            }
            if previous
            else None,
        )
    try:
        if args.wait_minutes:
            try:
                cycle = wait_for_cycle(
                    args.layer, requested, args.wait_minutes, clock=clock, sleep=sleep
                )
            except CycleWaitExpired as exc:
                _outcome(args, "source_wait_expired", "upstream_timeout", exc)
                print(f"ingest {args.layer}: {exc}")
                return 1
            t0 = time.time()  # status duration_s covers the job, not the wait
        else:
            cycle = _resolve(args.layer, requested)
        _observe(args, cycle=cycle_iso(cycle))
        published = already_published(store, args.layer, cycle)
        if published and not args.force:
            _outcome(args, "already_published")
            _observe(args, confirmed_current_run_id=published["run_id"])
            print(
                f"ingest {args.layer}: cycle {cycle_iso(cycle)} already published "
                f"(latest {published['run_id']}, published {published['published_at']}); "
                "nothing to do (--force re-publishes the same cycle)"
            )
            return 0
        if published:
            try:
                check_not_older(args.layer, published, cycle_iso(cycle))
            except StalePublishError as exc:
                print(f"ingest {args.layer}: --force refused: {exc}")
                return 1
        if args.paid_guard:
            # Stable provider-cycle identity covers dispatcher + fallback cron,
            # manual runs and --force; a repair needs explicit ledger recovery.
            args.paid_token = args.paid_guard.acquire(
                args.layer, f"{args.layer}:{cycle_iso(cycle)}", args.paid_seconds
            )
        _observe(args, "build")
        cube = _build(args, cycle, requested)
    except SourceError as exc:  # a regional run's files are not what the registry expects
        _outcome(args, "source_rejected", "source_check", exc)
        print(f"ingest {args.layer}: source check failed: {exc}")
        return 1
    except CycleNotAvailableError as exc:
        _outcome(args, "source_unavailable", "upstream_not_available", exc)
        if skip_when_not_available(args.layer):
            print(f"ingest {args.layer}: cycle not available yet, skipping ({exc})")
            return 0
        print(f"ingest {args.layer}: no complete cycle available ({exc})")
        return 1

    _observe(args, "validate", cycle=cube.cycle_iso, run_id=cube.run_id)
    print(f"ingest {args.layer}: cycle {cube.cycle_iso} -> run {cube.run_id}")

    if regional:
        from ingest.sources.openmeteo import adapter

        report = adapter.validate(regional, cube)
    else:
        expected_axes = None
        if args.layer == "currents-ibi":
            from ingest.sources import ibi

            expected_axes = {ibi.AXIS_NAME: ibi.STEP_AXIS}
        report = validate_cube(
            cube,
            max_missing=max_missing(args.layer),
            expected_axes=expected_axes,
        )
    _observe(
        args,
        validation={
            "ok": report.ok,
            "checks_passed": report.checks_passed,
            "failure_count": len(report.failures),
        },
    )
    print(report.summary())
    if not report.ok:
        _outcome(args, "validation_rejected", "validation")
        print(f"ingest {args.layer}: validation FAILED, aborting before upload")
        return 1

    _observe(args, "encode")
    tiles = build_tiles(cube)
    if args.attempt:
        args.attempt.tiles(tiles)
    total = sum(len(gz) for _, gz in tiles)
    print(f"ingest {args.layer}: {len(tiles)} tiles, {total / 1e6:.1f} MB gz")

    if regional:
        from ingest.sources.openmeteo import catalog

        if total > regional.max_run_bytes:
            _outcome(args, "run_cap_refused", "capacity")
            print(
                f"ingest {args.layer}: run is {total} B, over its {regional.max_run_bytes} B "
                "cap; refusing before upload"
            )
            return 1
        source = getattr(args, "source_run", None)
        if source is not None:
            try:
                _observe(args, "source_recheck")
                catalog.recheck(source.meta, source.files)
            except SourceError as exc:
                _outcome(args, "source_changed", "source_identity", exc)
                print(f"ingest {args.layer}: source changed, refusing to publish: {exc}")
                return 1

    _observe(args, "publish")
    try:
        result = publish_run(
            store, cube, tiles, report, max_bucket_bytes=max_bucket_bytes_from_env(), started_at=t0
        )
    except PublishError as exc:
        _outcome(args, "publication_failed", "publication", exc)
        print(f"ingest {args.layer}: publish FAILED: {exc}")
        return 1

    _outcome(args, "scratch_published" if args.dry_run else "published")
    _observe(
        args,
        pointer_commit_confirmed=True,
        committed_run_id=result.run_id,
        commit_attempts=result.commit_attempts,
        previous_run_id=result.previous_run_id,
    )
    dest = args.dry_run or "R2"
    print(
        f"ingest {args.layer}: published {result.run_id} to {dest} "
        f"({result.tile_count} tiles, {result.bytes / 1e6:.1f} MB, {result.duration_s}s; "
        f"previous={result.previous_run_id}, deleted={result.deleted_runs}, "
        f"commit attempts={result.commit_attempts})"
    )
    if result.kept_runs:
        print(f"ingest {args.layer}: retention left {result.kept_runs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
