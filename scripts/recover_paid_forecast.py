#!/usr/bin/env python3
"""Permit one reviewed failed root cycle without refunding prior work."""

import argparse

from ingest.forecast_recovery import permit_failed_cycle
from ingest.paid_work import Guard, enforced
from ingest.publish import make_r2_store_from_env
from ingest.sources.base import parse_cycle_arg
from ingest.cube import cycle_iso


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--layer", choices=("weather", "weather-ecmwf"), required=True)
    p.add_argument("--cycle", required=True)
    args = p.parse_args()
    if not enforced():
        p.error("recovery requires the existing spending control to remain enabled")
    cycle = parse_cycle_arg(args.cycle)
    permit_failed_cycle(make_r2_store_from_env(), Guard.from_env(), args.layer, cycle_iso(cycle))
    print("One reviewed cycle permitted; prior charges and all limits preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
