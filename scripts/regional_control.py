#!/usr/bin/env python3
"""Disable a regional entry or restore its validated retained previous run.

Disable dispatch and consumer selection, and stop in-flight writers FIRST.
This command changes only the named regional entry with compare-and-swap;
it never deletes forecast objects or changes latest.json. No normal ingest
can roll back. Try the command with --dir against scratch data first.
"""

import argparse

from ingest.publish import DirStore, PublishError, make_r2_store_from_env
from ingest.regional_control import change_regional_entry
from ingest.sources.openmeteo.registry import LAYERS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("disable", "restore-previous"))
    parser.add_argument("layer", choices=LAYERS)
    parser.add_argument("--expected-current", required=True)
    parser.add_argument("--dir", help="scratch dry-run layout, otherwise R2")
    args = parser.parse_args()
    store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
    try:
        result = change_regional_entry(
            store,
            args.layer,
            args.expected_current,
            restore_previous=args.action == "restore-previous",
        )
    except PublishError as exc:
        print(f"regional control failed: {exc}")
        return 1
    print(f"{args.layer}: {result or 'disabled'}; no objects pruned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
