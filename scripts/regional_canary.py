#!/usr/bin/env python3
"""Read-only seven-day timeliness score from downloaded regional attempt artifacts.

This score does not enable cadence or prove consumer/rollback/root-delivery
checks. Exit 0 means the timing criteria passed, 1 a completed failed window,
and 2 an incomplete/unknown observation. It never turns absent evidence into zero.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ingest.regional_canary import score_canary
from ingest.sources.openmeteo.registry import LAYERS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--attempts", type=Path, required=True)
    parser.add_argument("--now", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--ingestion-minutes", type=int, default=10)
    args = parser.parse_args()
    attempts = [
        json.loads(path.read_text()) for path in args.attempts.rglob("regional-attempt.json")
    ]
    result = score_canary(
        attempts, args.layer, args.started_at, args.now, ingestion_minutes=args.ingestion_minutes
    )
    print(json.dumps(result, indent=2))
    if result["timeliness_passed"]:
        return 0
    if (
        not result["observation_complete"]
        or result["counts"]["unknown"]
        or result["unknown_publications"]
    ):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
