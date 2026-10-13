"""Registered next slot for protecting opportunistic regional catch-ups."""

from datetime import datetime, timedelta, timezone

from ingest.sources.openmeteo.registry import product


def next_dispatch(layer: str, cycle: datetime) -> datetime:
    p = product(layer)
    cycle = cycle.astimezone(timezone.utc)
    day = cycle.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(
        candidate + timedelta(minutes=p.lag_minutes[hour])
        for offset in range(2)
        for hour in p.cycles
        if (candidate := (day + timedelta(days=offset)).replace(hour=hour)) > cycle
    )
