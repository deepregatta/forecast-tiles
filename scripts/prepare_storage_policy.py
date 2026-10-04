#!/usr/bin/env python3
"""Prepare a paused storage policy from authenticated or scratch inventory.

Read-only: no provider writes, activation, reconciliation or paid-work changes.
Output contains physical accounting; keep it outside the public checkout.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from ingest.publish import DirStore, make_r2_store_from_env
from ingest.storage_admission import (
    CONTROL_BYTES,
    CapacityDenied,
    KEY,
    envelope,
    inventory,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]


def prepare(proposal, store):
    """Return exact conditional-write inputs, without performing the write.

    An existing ledger keeps its accounting, epoch and ownership mapping. Changed
    ownership or unexpected growth needs the separately fenced reconciliation path.
    """
    candidate = deepcopy(proposal)
    validate(candidate)
    if (
        not candidate["paused"]
        or candidate["rollout_complete"]
        or candidate["epoch"]
        or candidate["reservations"]
        or candidate["seen_work"]
        or any(candidate["baseline_bytes"].values())
    ):
        raise CapacityDenied("proposal must be an empty, paused allocation template")
    if (
        sum(owner["ceiling_bytes"] for owner in candidate["owners"].values())
        + candidate["headroom_bytes"]
        + CONTROL_BYTES
        > candidate["limit_bytes"]
    ):
        raise CapacityDenied("owner allocations exceed bucket limit")
    try:
        raw, etag = store.get_with_etag(KEY)
    except Exception as exc:
        raise CapacityDenied("existing capacity state unavailable") from exc
    if raw is None:
        if etag is not None:
            raise CapacityDenied("inconsistent missing capacity state")
        condition = {"if_none_match": "*"}
    else:
        if not etag or len(raw) > CONTROL_BYTES:
            raise CapacityDenied("existing capacity ETag unavailable")
        try:
            current = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CapacityDenied("existing capacity state invalid") from exc
        validate(current)
        if set(current["owners"]) != set(candidate["owners"]) or any(
            current["owners"][name][field] != owner[field]
            for name, owner in candidate["owners"].items()
            for field in ("mode", "prefixes")
        ):
            raise CapacityDenied("ownership changes require fenced reconciliation")
        for field in ("epoch", "baseline_bytes", "reservations", "seen_work"):
            candidate[field] = deepcopy(current[field])
        condition = {"if_match": etag}
    measured = inventory(store, candidate["owners"])
    if raw is None:
        candidate["baseline_bytes"] = measured
    validate(candidate)
    envelope(candidate, measured)
    return {"condition": condition, "policy": candidate}


def write_private(path, document):
    path = Path(path)
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("private policy output must be outside the public checkout")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dir", help="scratch storage instead of authenticated R2")
    args = parser.parse_args()
    try:
        proposal = json.loads(args.proposal.read_text())
        store = DirStore(args.dir) if args.dir else make_r2_store_from_env()
        write_private(args.output, prepare(proposal, store))
    except (CapacityDenied, ValueError, OSError) as exc:
        # Do not expose paths, object identities or provider credential errors.
        print(f"Policy preparation denied ({type(exc).__name__}); no provider changes")
        return 2
    print("Paused conditional policy prepared privately; activation still requires approval")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
