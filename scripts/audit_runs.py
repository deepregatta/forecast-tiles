#!/usr/bin/env python3
"""Audit forecast runs on R2 (or a --dry-run directory) against latest.json.

Reports every run under forecast-runs/ as referenced, superseded or
incomplete with its bytes, and lists damage first: a pointer naming a run
whose manifest is gone, or a referenced manifest listing a tile that is
missing. Read-only unless --delete-unreferenced is given, which removes runs
nothing names whose newest object is older than --min-age-hours (default 24),
re-checking the pointer just before each deletion. See ingest.audit.

Usage:
  uv run scripts/audit_runs.py                      # R2 from R2_* env vars
  uv run scripts/audit_runs.py --dir /tmp/tiles     # a --dry-run layout
  uv run scripts/audit_runs.py --json
  uv run scripts/audit_runs.py --delete-unreferenced --min-age-hours 24

Exits 2 when damage is found, so a scheduled audit would fail visibly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from ingest.audit import DEFAULT_MIN_AGE_HOURS, audit_runs, delete_unreferenced
from ingest.publish import DirStore, make_r2_store_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", help="audit a local --dry-run layout instead of R2")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--delete-unreferenced",
        action="store_true",
        help="delete superseded and incomplete runs older than --min-age-hours",
    )
    parser.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS)
    args = parser.parse_args(argv)
    if args.min_age_hours < 4:
        parser.error("--min-age-hours must be at least 4: an ingest job can run that long")

    store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
    report = audit_runs(store)

    if args.json:
        print(json.dumps(report.as_dict(), indent=1))
    else:
        for line in report.dangling:
            print(f"DAMAGE  {line}")
        for run in report.runs.values():
            if run.missing_tiles:
                print(f"DAMAGE  {run.run_id}: {len(run.missing_tiles)} tiles missing")
        for run in report.runs.values():
            refs = ", ".join(run.referenced_as)
            newest = run.newest.strftime("%Y-%m-%dT%H:%MZ") if run.newest else "-"
            print(
                f"{run.state:<11} {run.run_id:<36} {run.objects:>5} objects "
                f"{run.bytes / 1e6:>9.1f} MB  newest {newest}  {refs}"
            )
        for key in report.stray_keys:
            print(f"stray       {key}")
        totals = report.bytes_by_state()
        print(
            "totals: "
            + ", ".join(f"{state} {n / 1e9:.3f} GB" for state, n in totals.items())
            + f", all {sum(totals.values()) / 1e9:.3f} GB"
        )

    if args.delete_unreferenced:
        deleted, kept = delete_unreferenced(
            store,
            report,
            now=datetime.now(timezone.utc),
            min_age=timedelta(hours=args.min_age_hours),
        )
        print(f"deleted {deleted}")
        if kept:
            print(f"kept {kept}")
    return 2 if report.damaged else 0


if __name__ == "__main__":
    sys.exit(main())
