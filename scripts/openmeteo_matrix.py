#!/usr/bin/env python3
"""Normalize manual dispatch and catch-up into the same per-layer job matrix."""

from __future__ import annotations

import json
import os

from ingest.sources.base import parse_cycle_arg
from ingest.sources.openmeteo import registry


def job_matrix(event: str, layer: str, cycle: str, wait: str, dry_run: bool, enabled: str):
    enabled_layers = enabled.split(",") if enabled else []
    if any(chosen not in registry.LAYERS for chosen in enabled_layers):
        raise ValueError("unknown OPENMETEO_ENABLED_LAYERS model")
    layers = [layer] if event == "workflow_dispatch" else enabled_layers
    jobs = []
    for chosen in layers:
        if chosen not in registry.LAYERS:
            raise ValueError("unknown regional model")
        p = registry.product(chosen)
        if not dry_run and (chosen not in enabled_layers or not p.production_enabled):
            if event == "workflow_dispatch":
                raise ValueError(f"{chosen}: production/consumer/capacity gates are not open")
            continue
        if cycle and parse_cycle_arg(cycle).hour not in p.cycles:
            raise ValueError(f"{chosen}: unregistered cycle")
        minutes = float(wait or 0)
        if not 0 <= minutes <= p.wait_minutes or (minutes and not cycle):
            raise ValueError("wait needs an exact cycle and must stay within the registered window")
        jobs.append(
            {
                "layer": chosen,
                "cycle": cycle,
                "wait_minutes": minutes,
                "dry_run": dry_run,
                "canary": os.environ.get("OPENMETEO_CANARY") == "true",
            }
        )
    return jobs


def main():
    jobs = job_matrix(
        os.environ["EVENT"],
        os.environ.get("LAYER", ""),
        os.environ.get("CYCLE", ""),
        os.environ.get("WAIT", "0"),
        os.environ.get("DRY_RUN") == "true",
        os.environ.get("OPENMETEO_ENABLED_LAYERS", ""),
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as out:
        out.write(f"matrix={json.dumps(jobs, separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
