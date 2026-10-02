#!/usr/bin/env python3
"""Read-only stale check; supply observed source lag from recent attempt reports.

Use --canary for reduced 03/15Z or 00/12Z cadence. Reads latest-regional.json
only, never alters pointers or sends notifications. Exits 0 fresh, 1 stale,
2 unknown. This is a freshness check, not seven-day canary acceptance.
"""

import argparse
import json
from datetime import datetime, timezone

from ingest.publish import DirStore, make_r2_store_from_env, published_layer
from ingest.regional_health import freshness
from ingest.sources.openmeteo.registry import LAYERS, PRODUCTS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--dir", help="scratch layout instead of R2")
    parser.add_argument(
        "--source-lag-minutes",
        type=float,
        required=True,
        help="observed source completion lag; unknown is not zero",
    )
    parser.add_argument("--canary", action="store_true", help="use the reduced canary cadence")
    parser.add_argument("--now", help="explicit UTC timestamp for a reproducible check")
    args = parser.parse_args(argv)
    try:
        checked = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(timezone.utc)
        )
        # Validate inputs before touching the destination.
        freshness(
            PRODUCTS[args.layer], None, checked, args.source_lag_minutes * 60, canary=args.canary
        )
    except (OverflowError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
        entry = published_layer(store, args.layer, "latest-regional.json")
        report = freshness(
            PRODUCTS[args.layer], entry, checked, args.source_lag_minutes * 60, canary=args.canary
        )
    except Exception as exc:
        # A malformed/unavailable pointer must not look like fresh data, nor
        # print destination-specific error text or credentials.
        report = {
            "layer": args.layer,
            "status": "unknown",
            "reason": "pointer_unavailable",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(report, indent=1))
    return {"fresh": 0, "stale": 1, "unknown": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
