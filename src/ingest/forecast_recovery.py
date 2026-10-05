"""Operator-authorized recovery of one failed, still-current root cycle.

Only its duplicate-work identity is removed. Counters, policy, active leases
and unrelated identities remain charged and unchanged. Publication still goes
through the ordinary ingestion entry point and all of its guards.
"""

import copy
import hashlib
from datetime import datetime, timezone

from ingest.capacity import ROOT_LAYERS
from ingest.cube import cycle_iso
from ingest.paid_work import Paused, positive
from ingest.publish import parse_cycle_iso, published_layer


def permit_failed_cycle(store, guard, layer, cycle):
    if layer not in ROOT_LAYERS:
        raise ValueError("recovery requires a registered root layer")
    work_cycle = parse_cycle_iso(cycle)
    identity = hashlib.sha256(f"{layer}:{cycle_iso(work_cycle)}".encode()).hexdigest()
    for _ in range(6):
        current = published_layer(store, layer)
        if current and parse_cycle_iso(current["cycle"]) >= work_cycle:
            raise Paused("cycle is already published or superseded; no recovery permitted")
        run_id = f"{layer}-{work_cycle:%Y%m%dT%HZ}"
        if store.get(f"forecast-runs/{run_id}/manifest.json") is not None:
            raise Paused("complete immutable run requires publication reconciliation")
        doc, etag = guard.read()
        guard.validate(doc)
        now = guard.clock()
        state = doc["usage"]["channels"][layer]
        limits = doc["channels"][layer]
        if identity not in state["seen"]:
            raise Paused("no charged duplicate identity to recover")
        if state.get("active", {}).get("until", 0) > now:
            raise Paused("this producer still has an active lease")
        if doc.get("single_active", False) and any(
            s.get("active", {}).get("until", 0) > now for s in doc["usage"]["channels"].values()
        ):
            raise Paused("another producer is running")
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if now - state["last"] < positive(limits["min_interval_seconds"]):
            raise Paused("recompute frequency limit")
        if state["days"].get(day, 0) >= positive(limits["daily_starts"]):
            raise Paused("daily starts limit")
        if doc["usage"]["starts"] >= positive(doc["max_starts"]) or doc["usage"][
            "seconds"
        ] + 14400 > positive(doc["max_seconds"]):
            raise Paused("cumulative runtime/start limit")
        revised = copy.deepcopy(doc)
        del revised["usage"]["channels"][layer]["seen"][identity]
        try:
            confirmed = guard.write(revised, etag)
        except Paused:
            # One uncertain PUT is settled by an exact authenticated GET;
            # it is never repeated, and concurrent changes remain unknown.
            observed, _ = guard.read()
            if observed != revised:
                raise
            return
        if not confirmed:
            continue  # a known 412; re-read all counters and limits
        observed, _ = guard.read()
        if observed != revised:
            raise Paused("recovery read-back changed; no ingestion authorized")
        return
    raise Paused("recovery contention; counters unchanged")
