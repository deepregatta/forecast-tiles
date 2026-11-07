#!/usr/bin/env python3
"""Add the chart publisher's `charts` channel to ops/paid-work.json.

One conditional write with exact read-back, preserving all policy and usage
(`ingest.paid_work_review.add_channel`). A dry run unless --apply.
"""

import argparse

from ingest.paid_work import Guard
from ingest.paid_work_review import CHARTS_CHANNEL, add_channel


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    outcome = add_channel(Guard.from_env(), "charts", CHARTS_CHANNEL, apply=args.apply)
    print(
        {
            "present": "charts channel already present with its limits; no write made",
            "dry-run": f"dry run: would add charts {CHARTS_CHANNEL}; no write made",
            "added": f"charts channel added {CHARTS_CHANNEL} with conditional write and exact read-back",
        }[outcome]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
