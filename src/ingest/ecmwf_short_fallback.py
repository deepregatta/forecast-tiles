"""Name the newest started short IFS cycle for the scheduled fallback.

Choosing the latest *complete* cycle can charge an older 06Z/18Z run just
before its successor becomes available, blocking that successor under the
unchanged frequency limit. The fallback checks this named cycle once; the
dispatcher retains its bounded readiness wait.
"""

from datetime import datetime, timedelta, timezone


def fallback_cycle(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fallback clock requires an explicit timezone")
    now = now.astimezone(timezone.utc)
    if now.hour < 6:
        now -= timedelta(days=1)
        hour = 18
    else:
        hour = 18 if now.hour >= 18 else 6
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


if __name__ == "__main__":
    print(fallback_cycle(datetime.now(timezone.utc)).strftime("%Y%m%dT%H"))
