"""Read-only freshness against selected regional publication opportunities."""

from datetime import datetime, timedelta, timezone
from math import isfinite

from ingest.publish import parse_cycle_iso
from ingest.sources.openmeteo.registry import Product


def freshness(
    product: Product, entry: dict | None, now: datetime, source_lag_s: float, *, canary: bool
) -> dict:
    """Flag stale only once two later selected cycles could have completed.

    Source lag is an explicitly supplied measurement, not inferred from a
    failure/empty report or silently replaced with the model's cadence.
    Missing/invalid pointer evidence stays unknown. No writes or notifications.
    """
    if not isfinite(source_lag_s) or source_lag_s < 0:
        raise ValueError("source lag must be finite and nonnegative")
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    now = now.astimezone(timezone.utc)
    hours = ((3, 15) if product.layer == "weather-arome" else (0, 12)) if canary else product.cycles
    result = {
        "schema_version": 1,
        "layer": product.layer,
        "pointer": "latest-regional.json",
        "checked_at": now.isoformat(),
        "selected_cycles_utc": list(hours),
        "supplied_source_lag_s": source_lag_s,
        "status": "unknown",
        "reason": "no_current_run",
    }
    if not entry:
        return result
    try:
        cycle = parse_cycle_iso(entry["cycle"])
        if (
            cycle.hour not in product.cycles
            or cycle.minute
            or cycle > now
            or entry["run_id"] != f"{product.layer}-{cycle:%Y%m%dT%HZ}"
        ):
            raise ValueError("invalid current run")
    except (KeyError, TypeError, ValueError):
        result["reason"] = "invalid_current_run"
        return result
    opportunities = []
    next_cycle = cycle + timedelta(hours=1)
    while len(opportunities) < 2:
        if next_cycle.hour in hours:
            opportunities.append(next_cycle)
        next_cycle += timedelta(hours=1)
    stale_at = opportunities[1] + timedelta(seconds=source_lag_s)
    result.update(
        status="stale" if now >= stale_at else "fresh",
        reason="two_missed_opportunities" if now >= stale_at else "within_threshold",
        current_run_id=entry["run_id"],
        current_cycle=entry["cycle"],
        next_two_cycles=[c.isoformat() for c in opportunities],
        stale_at=stale_at.isoformat(),
    )
    return result
