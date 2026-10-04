"""Shared reservation integration and the existing regional peak calculation.

Every publisher keeps its retained-manifest guard and can activate shared v1
admission separately after the participating-writer rollout. A regional R2 writer
also requires an operator-measured existing-layer peak (including upload
overlap) and explicit headroom. Neither can be inferred from retained tiles.
No account identifiers or private capacity figures belong in this module.
"""

from __future__ import annotations

import os
import math
from contextlib import contextmanager
from dataclasses import dataclass

from ingest.publish import StorageGuardError
from ingest.sources.openmeteo import registry


@contextmanager
def reserved_publication(
    store, writer, work_id, upload_bytes, *, prefix, mutable_keys, admission=None
):
    """One boundary for every root/regional/land caller, including manual/force.

    Scratch output bypasses production policy unless an admission is injected.
    Production activation remains explicit and independent of paid-work.
    """
    from ingest.publish import DirStore, PreconditionFailed
    from ingest.storage_admission import Admission, CapacityDenied, ReservedStore, enforced

    try:
        enabled = False if isinstance(store, DirStore) and admission is None else enforced()
        if admission is None and not enabled:
            yield store
            return
        admission = admission or Admission(store, conflicts=(PreconditionFailed,))
        reservation = admission.acquire(writer, work_id, upload_bytes)
        scoped = ReservedStore(admission, reservation, prefix=prefix, mutable_keys=mutable_keys)
        yield scoped
        admission.finish(reservation)
    except CapacityDenied as exc:
        raise StorageGuardError(f"shared capacity: {exc}") from exc


ROOT_LAYERS = (
    "weather",
    "weather-ecmwf",
    "weather-ecmwf-short",
    "ensemble",
    "waves",
    "currents",
    "currents-ibi",
)


def capacity_profile(store, layers=registry.LAYERS, *, headroom_bytes=500_000_000):
    """Read-only measurements plus a calculated simultaneous-upload envelope.

    This is NOT an observed historical peak. Size each root layer from the
    larger current/previous run, reserve three such runs for concurrent upload,
    then add 10% for day-to-day size variation. Include every nonreferenced
    object and three capped runs for each proposed regional layer. Unknown or
    damaged references fail closed. Account-wide usage is outside this report.
    """
    from ingest.audit import audit_runs

    if headroom_bytes <= 0 or not layers or len(set(layers)) != len(layers):
        raise StorageGuardError("capacity profile needs positive headroom and distinct models")
    if any(not registry.is_regional(layer) for layer in layers):
        raise StorageGuardError("unknown regional profile model")
    report = audit_runs(store)
    if report.damaged:
        raise StorageGuardError("capacity profile: damaged references; reconcile first")
    root = {layer: [] for layer in ROOT_LAYERS}
    for run in report.runs.values():
        for role in run.referenced_as:
            pointer, layer, _ = role.split()
            if pointer == "latest.json" and layer in root:
                root[layer].append(run)
    if any(not runs for runs in root.values()):
        raise StorageGuardError("capacity profile: all seven root layers must be measured")
    physical = sum(obj["bytes"] for obj in store.list_objects(""))
    referenced = sum(run.bytes for run in report.runs.values() if run.referenced_as)
    other = max(0, physical - referenced)
    largest = {layer: max(run.bytes for run in runs) for layer, runs in root.items()}
    root_three_runs = 3 * sum(largest.values())
    # Integer arithmetic: ceil(10% variation), then round the root profile up
    # to whole MB so it can be copied without accidentally rounding down.
    existing_peak = math.ceil((root_three_runs * 110 + 99) // 100 / 1_000_000) * 1_000_000
    regional_caps = {layer: registry.product(layer).max_run_bytes for layer in layers}
    reserved = existing_peak + 3 * sum(regional_caps.values()) + other + headroom_bytes
    return {
        "basis": "snapshot run sizes; calculated overlap, not observed peak",
        "physical_bytes": physical,
        "referenced_bytes": referenced,
        "other_bytes": other,
        "root_largest_run_bytes": largest,
        "root_three_run_bytes": root_three_runs,
        "root_variation_percent": 10,
        "proposed_existing_peak_bytes": existing_peak,
        "regional_run_caps": regional_caps,
        "headroom_bytes": headroom_bytes,
        "reserved_peak_bytes": reserved,
        "proposed_guard_bytes": math.ceil(reserved / 1_000_000_000) * 1_000_000_000,
    }


@dataclass(frozen=True)
class RegionalAllocation:
    existing_peak_bytes: int
    headroom_bytes: int
    layers: tuple[str, ...]

    @classmethod
    def from_env(cls, publishing_layer: str) -> RegionalAllocation:
        try:
            peak = int(os.environ["REGIONAL_EXISTING_PEAK_BYTES"])
            headroom = int(os.environ["REGIONAL_HEADROOM_BYTES"])
            layers = tuple(os.environ["REGIONAL_ENABLED_LAYERS"].split(","))
        except (KeyError, ValueError) as exc:
            raise StorageGuardError(
                "regional capacity unknown: configure REGIONAL_EXISTING_PEAK_BYTES, "
                "REGIONAL_HEADROOM_BYTES and REGIONAL_ENABLED_LAYERS from a live inventory"
            ) from exc
        if peak <= 0 or headroom <= 0 or publishing_layer not in layers:
            raise StorageGuardError(
                "regional capacity needs positive peak/headroom and an enabled layer"
            )
        if len(set(layers)) != len(layers) or any(
            not registry.is_regional(layer) for layer in layers
        ):
            raise StorageGuardError("regional capacity has duplicate or unknown enabled layers")
        return cls(peak, headroom, layers)


def check_regional_capacity(store, new_run_bytes: int, limit: int, allocation: RegionalAllocation):
    """Fail closed on damage, unknown inventory or either physical/reserved peak.

    Count all bucket objects (routing data, metadata and abandoned uploads too).
    Reserve three capped runs for each admitted regional model simultaneously.
    The fixed root peak already covers its own simultaneous ingestion jobs.
    """
    from ingest.audit import audit_runs

    report = audit_runs(store)
    if report.damaged:
        raise StorageGuardError("regional capacity: referenced runs are damaged; reconcile first")
    try:
        physical = sum(obj["bytes"] for obj in store.list_objects(""))
    except Exception as exc:
        raise StorageGuardError(
            "regional physical inventory unavailable; refusing admission"
        ) from exc
    regional_cap = sum(registry.product(layer).max_run_bytes for layer in allocation.layers)
    # Referenced runs are covered by the fixed existing/regional reservations.
    # Everything else remains an additional allocation until actually deleted:
    # routing data, pointers, stray keys and complete/partial abandoned uploads.
    referenced = sum(run.bytes for run in report.runs.values() if run.referenced_as)
    other_bytes = max(0, physical - referenced)
    existing_referenced = sum(
        run.bytes
        for run in report.runs.values()
        if any(role.startswith("latest.json ") for role in run.referenced_as)
    )
    if allocation.existing_peak_bytes < existing_referenced:
        raise StorageGuardError(
            "regional capacity: measured existing peak is below current references"
        )
    reserved = (
        allocation.existing_peak_bytes + 3 * regional_cap + other_bytes + allocation.headroom_bytes
    )
    physical_peak = physical + new_run_bytes + allocation.headroom_bytes
    if max(reserved, physical_peak) > limit:
        raise StorageGuardError(
            f"regional capacity: reserved peak {reserved} B / physical plus upload and headroom "
            f"{physical_peak} B exceeds {limit} B; refusing before upload"
        )
    return {"physical_bytes": physical, "other_bytes": other_bytes, "reserved_peak_bytes": reserved}
