"""Explicit regional-only disable/rollback; never prune or touch root latest.

Stop dispatch and in-flight writers before invoking this operator path. The
expected current run prevents a command from undoing a newer publication.
"""

from __future__ import annotations

import json
import random

from ingest.cube import utcnow_iso
from ingest.publish import (
    APPLICATION_JSON,
    CACHE_MUTABLE,
    COMMIT_ATTEMPTS,
    REGIONAL_KEY,
    PointerConflictError,
    PreconditionFailed,
    PublishError,
    UncertainWriteError,
    _post_publish_check,
    json_bytes,
)
from ingest.sources.openmeteo.registry import is_regional


def change_regional_entry(store, layer: str, expected_current: str, *, restore_previous=False):
    if not is_regional(layer):
        raise PublishError(f"{layer}: operator control is restricted to regional layers")
    for _ in range(COMMIT_ATTEMPTS):
        raw, etag = store.get_with_etag(REGIONAL_KEY)
        if raw is None:
            raise PublishError("no regional catalogue")
        doc = json.loads(raw)
        entry = doc["layers"].get(layer)
        if not entry or entry["run_id"] != expected_current:
            raise PointerConflictError(
                f"{layer}: current run differs from {expected_current}; stop"
            )
        updated = None
        if restore_previous:
            previous = entry.get("previous_run_id")
            if not previous:
                raise PublishError(f"{layer}: no retained previous run to restore")
            data = store.get(f"forecast-runs/{previous}/manifest.json")
            if data is None:
                raise PublishError(f"{previous}: manifest missing")
            manifest = json.loads(data)
            if manifest.get("layer") != layer or manifest.get("run_id") != previous:
                raise PublishError("rollback manifest has the wrong layer or run")
            if not manifest.get("tiles") or not manifest.get("validation", {}).get("checks_passed"):
                raise PublishError("rollback target lacks tiles or passed validation")
            _post_publish_check(store, previous, manifest, random.Random(0))
            updated = {
                "run_id": previous,
                "previous_run_id": None,
                "cycle": manifest["cycle"],
                "member_count": manifest["member_count"],
                "published_at": utcnow_iso(),
            }
            if "cadence_hours" in entry:
                updated["cadence_hours"] = entry["cadence_hours"]
        layers = dict(doc["layers"])
        if updated is None:
            del layers[layer]
        else:
            layers[layer] = updated
        new_doc = {**doc, "layers": layers, "updated_at": utcnow_iso()}
        try:
            store.put(
                REGIONAL_KEY,
                json_bytes(new_doc),
                content_type=APPLICATION_JSON,
                cache_control=CACHE_MUTABLE,
                if_match=etag,
            )
            return updated
        except (PreconditionFailed, UncertainWriteError):
            # Settle an uncertain write; only this layer's desired result counts.
            settled = store.get(REGIONAL_KEY)
            current = json.loads(settled)["layers"].get(layer) if settled else None
            if current == updated:
                return updated
    raise PointerConflictError("regional catalogue kept changing; nothing pruned")
