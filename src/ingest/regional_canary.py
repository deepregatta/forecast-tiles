"""Score confirmed R2 attempt artifacts against the configured canary window.

Metadata creation is not upload availability. The deadline uses the registered
readiness lag and bounded wait plus an explicit ingestion allowance. Missing
artifacts and timing evidence remain unknown; already-published skips alone
cannot establish a delivery time. Bootstrap/backfill cycles are excluded.
"""

from datetime import datetime, timedelta, timezone

from ingest.sources.openmeteo.registry import product


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("canary timestamps need a timezone")
    return result.astimezone(timezone.utc)


def score_canary(attempts, layer, started_at, now, *, ingestion_minutes=10):
    if ingestion_minutes <= 0:
        raise ValueError("ingestion allowance must be positive")
    p = product(layer)
    start, clock = utc(started_at), utc(now)
    end = start + timedelta(days=7)
    hours = (3, 15) if layer == "weather-arome" else (0, 12)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    rows = []
    while day < end + timedelta(days=1):
        for hour in hours:
            cycle = day.replace(hour=hour)
            scheduled = cycle + timedelta(minutes=p.lag_minutes[hour])
            if not start <= scheduled < end:
                continue
            deadline = scheduled + timedelta(minutes=p.wait_minutes + ingestion_minutes)
            matching = [
                a
                for a in attempts
                if a.get("layer") == layer
                and a.get("destination") == "r2"
                and (a.get("cycle") or a.get("requested_cycle"))
                and utc(a.get("cycle") or a["requested_cycle"]) == cycle
            ]
            confirmed = [
                a
                for a in matching
                if a.get("outcome") == "published" and a.get("pointer_commit_confirmed") is True
            ]
            invalid = any(
                a.get("validation", {}).get("ok") is not True
                or a.get("validation", {}).get("failure_count") != 0
                or a.get("source", {}).get("downloads_complete") is not True
                or a.get("exit_code") != 0
                for a in confirmed
            )
            finishes = sorted(
                utc(a["finished_at"])
                for a in confirmed
                if a.get("finished_at") and utc(a["finished_at"]) <= clock
            )
            if invalid:
                status = "invalid_published"
            elif finishes:
                status = "on_time" if finishes[0] <= deadline else "late"
            elif clock < deadline:
                status = "pending"
            elif any(a.get("outcome") not in (None, "already_published") for a in matching):
                status = "failed"
            else:
                status = "unknown"
            rows.append(
                {
                    "cycle": cycle.isoformat(),
                    "scheduled_at": scheduled.isoformat(),
                    "deadline": deadline.isoformat(),
                    "status": status,
                    "first_confirmed_finish": finishes[0].isoformat() if finishes else None,
                    "attempt_outcomes": sorted({a.get("outcome", "unknown") for a in matching}),
                }
            )
        day += timedelta(days=1)
    counts = {
        status: sum(r["status"] == status for r in rows)
        for status in ("on_time", "late", "failed", "unknown", "pending", "invalid_published")
    }
    known = counts["unknown"] == counts["pending"] == 0
    fraction = counts["on_time"] / len(rows) if known and rows else None
    elapsed = clock >= end
    passed = (
        elapsed
        and known
        and fraction is not None
        and fraction >= 0.95
        and not counts["invalid_published"]
    )
    return {
        "layer": layer,
        "started_at": start.isoformat(),
        "observation_ends_at": end.isoformat(),
        "now": clock.isoformat(),
        "deadline_basis": "registered readiness lag + bounded wait + ingestion allowance",
        "ingestion_minutes": ingestion_minutes,
        "expected_cycles": len(rows),
        "counts": counts,
        "on_time_fraction": fraction,
        "observation_complete": elapsed,
        "timeliness_passed": passed,
        "cycles": rows,
    }
