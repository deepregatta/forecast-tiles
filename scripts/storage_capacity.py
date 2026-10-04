#!/usr/bin/env python3
"""Read-only storage v1 inventory/admission preview (never modifies R2).

Policy is a locally reviewed draft. Output aggregates ownership classes only;
private inventory/billing evidence belongs outside this public repository.
"""

import argparse
import json
from pathlib import Path

from ingest.publish import DirStore, make_r2_store_from_env
from ingest.storage_admission import CapacityDenied, envelope, inventory, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--dir", help="scratch store instead of live read-only R2")
    args = parser.parse_args()
    try:
        doc = json.loads(Path(args.policy).read_text())
        validate(doc)
        store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
        measured = inventory(store, doc["owners"])
        peak = envelope(doc, measured)
    except (CapacityDenied, ValueError, OSError) as exc:
        print(
            json.dumps(
                {"status": "unavailable_or_denied", "physical_bytes": None, "reason": str(exc)}
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "measured",
                "bytes_by_owner": measured,
                "reserved_peak_bytes": peak,
                "limit_bytes": doc["limit_bytes"],
                "production_activated": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
