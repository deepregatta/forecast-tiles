"""Regional admission counts physical bytes and reserves fixed upload peaks.

The existing root publisher keeps its historical guard. A regional R2 writer
also requires an operator-measured existing-layer peak (including upload
overlap) and explicit headroom. Neither can be inferred from retained tiles.
No account identifiers or private capacity figures belong in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ingest.publish import StorageGuardError
from ingest.sources.openmeteo import registry


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
