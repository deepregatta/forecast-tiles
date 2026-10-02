"""Account for every object under forecast-runs/ against the pointers.

Each run found by listing is one of: referenced (a pointer names it as some
layer's current or previous run), superseded (complete, named by nothing),
or incomplete (no manifest: still uploading, or abandoned by a failed job).
A reference whose manifest is missing, or whose manifest lists a tile that is
not there, is damage, and the audit reports it first.

Retention only deletes a layer's complete runs older than its retained
previous, and only after its own commit, so an abandoned upload or a run
stranded by a stale publisher stays until this audit deletes it. Deletion is
opt-in and age-gated: a run is removed only when nothing names it, re-checked
against every pointer just before deleting, and its newest object is older
than any job could still be writing (an ingest job is capped at 4 h).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ingest.publish import LATEST_KEY, referenced_run_ids

POINTER_KEYS = (LATEST_KEY,)
DEFAULT_MIN_AGE_HOURS = 24

_RUN_RE = re.compile(r"^forecast-runs/([a-z0-9-]+-\d{8}T\d{2}Z)/")


@dataclass
class RunAudit:
    run_id: str
    objects: int = 0
    bytes: int = 0
    newest: datetime | None = None
    complete: bool = False
    referenced_as: list[str] = field(default_factory=list)  # "latest.json weather current"
    manifest_bytes: int | None = None
    missing_tiles: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.referenced_as:
            return "referenced"
        return "superseded" if self.complete else "incomplete"


@dataclass
class AuditReport:
    runs: dict[str, RunAudit]
    dangling: list[str]  # "latest.json weather current weather-… (no manifest)"
    stray_keys: list[str]  # under forecast-runs/ but not in a run directory

    def bytes_by_state(self) -> dict[str, int]:
        out = {"referenced": 0, "superseded": 0, "incomplete": 0}
        for run in self.runs.values():
            out[run.state] += run.bytes
        return out

    @property
    def damaged(self) -> bool:
        return bool(self.dangling or any(r.missing_tiles for r in self.runs.values()))

    def as_dict(self) -> dict:
        return {
            "bytes_by_state": self.bytes_by_state(),
            "dangling": self.dangling,
            "stray_keys": self.stray_keys,
            "runs": [
                {
                    "run_id": r.run_id,
                    "state": r.state,
                    "objects": r.objects,
                    "bytes": r.bytes,
                    "manifest_bytes": r.manifest_bytes,
                    "newest": r.newest.isoformat() if r.newest else None,
                    "referenced_as": r.referenced_as,
                    "missing_tiles": r.missing_tiles,
                }
                for r in self.runs.values()
            ],
        }


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    return json.loads(raw) if raw is not None else None


def audit_runs(store, pointer_keys: tuple[str, ...] = POINTER_KEYS) -> AuditReport:
    references: dict[str, list[str]] = {}
    for pointer in pointer_keys:
        for layer, entry in (_get_json(store, pointer) or {}).get("layers", {}).items():
            for role, run_id in (
                ("current", entry["run_id"]),
                ("previous", entry.get("previous_run_id")),
            ):
                if run_id:
                    references.setdefault(run_id, []).append(f"{pointer} {layer} {role}")

    runs: dict[str, RunAudit] = {}
    stray: list[str] = []
    keys_by_run: dict[str, set[str]] = {}
    for obj in store.list_objects("forecast-runs/"):
        m = _RUN_RE.match(obj["key"])
        if not m:
            stray.append(obj["key"])
            continue
        run = runs.setdefault(m.group(1), RunAudit(m.group(1)))
        run.objects += 1
        run.bytes += obj["bytes"]
        if obj["modified"] and (run.newest is None or obj["modified"] > run.newest):
            run.newest = obj["modified"]
        keys_by_run.setdefault(run.run_id, set()).add(obj["key"])

    dangling: list[str] = []
    for run_id, roles in sorted(references.items()):
        if run_id not in runs or f"forecast-runs/{run_id}/manifest.json" not in keys_by_run[run_id]:
            dangling += [f"{role} {run_id} (no manifest)" for role in roles]

    for run_id, run in runs.items():
        run.referenced_as = references.get(run_id, [])
        manifest_key = f"forecast-runs/{run_id}/manifest.json"
        run.complete = manifest_key in keys_by_run[run_id]
        if not run.complete:
            continue
        manifest = _get_json(store, manifest_key) or {}
        run.manifest_bytes = manifest.get("totals", {}).get("bytes")
        if run.referenced_as:  # only what clients can reach needs every tile
            template = manifest.get("tiling", {}).get("path_template", "")
            run.missing_tiles = sorted(
                tid
                for tid in manifest.get("tiles", {})
                if f"forecast-runs/{run_id}/" + template.format(tile_id=tid)
                not in keys_by_run[run_id]
            )
    return AuditReport(runs=dict(sorted(runs.items())), dangling=dangling, stray_keys=stray)


def delete_unreferenced(
    store,
    report: AuditReport,
    *,
    now: datetime,
    min_age: timedelta = timedelta(hours=DEFAULT_MIN_AGE_HOURS),
    pointer_keys: tuple[str, ...] = POINTER_KEYS,
) -> tuple[list[str], list[str]]:
    """Delete superseded and incomplete runs whose newest object is older
    than `min_age`. Returns (deleted run ids, ["run_id (reason)"] kept)."""
    deleted: list[str] = []
    kept: list[str] = []
    for run in report.runs.values():
        if run.state == "referenced":
            continue
        if run.newest is None or now - run.newest < min_age:
            kept.append(f"{run.run_id} (written within {min_age.total_seconds() / 3600:g} h)")
            continue
        if run.run_id in referenced_run_ids(store, pointer_keys):
            kept.append(f"{run.run_id} (referenced since the audit)")
            continue
        manifest_key = f"forecast-runs/{run.run_id}/manifest.json"
        keys = store.list_keys(f"forecast-runs/{run.run_id}/")
        for key in sorted(keys, key=lambda k: k == manifest_key):
            store.delete(key)
        deleted.append(run.run_id)
    return deleted, kept
