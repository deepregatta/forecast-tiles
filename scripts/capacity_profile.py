#!/usr/bin/env python3
"""Read-only whole-bucket inventory and a calculated upload envelope.

Never changes the storage guard or enables models. Does not establish an
observed peak or account-wide free allowance. JSON reports aggregate bytes
and public model IDs, never bucket names, credentials or nonforecast keys.
"""

import argparse
import json
from datetime import datetime, timezone

from ingest.capacity import capacity_profile
from ingest.publish import DirStore, StorageGuardError, make_r2_store_from_env
from ingest.sources.openmeteo.registry import LAYERS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", help="scratch dry-run bucket layout instead of R2")
    parser.add_argument("--layers", default=",".join(LAYERS))
    parser.add_argument("--headroom-bytes", type=int, default=500_000_000)
    args = parser.parse_args()
    store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
    started = datetime.now(timezone.utc).isoformat()
    try:
        profile = capacity_profile(
            store, tuple(args.layers.split(",")), headroom_bytes=args.headroom_bytes
        )
    except StorageGuardError as exc:
        print(f"capacity profile unavailable: {exc}")
        return 2
    print(
        json.dumps(
            {
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                **profile,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
