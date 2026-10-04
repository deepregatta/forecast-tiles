"""Operator preparation must not erase unresolved charges or enable writers."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from test_storage_admission import META, policy

from ingest.publish import DirStore
from ingest.storage_admission import Admission, CapacityDenied, KEY, encode

spec = importlib.util.spec_from_file_location(
    "prepare_storage_policy", Path(__file__).parents[1] / "scripts/prepare_storage_policy.py"
)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def proposal():
    doc = policy()
    doc.update(paused=True, rollout_complete=False)
    return doc


def test_initialization_measures_legacy_and_orphan_bytes_without_writes(tmp_path):
    store = DirStore(tmp_path)
    store.put("forecast-runs/orphan/tile", b"orphan", **META)
    store.put("prepared/legacy/chart", b"saved", **META)
    paid = b'{"usage":{"starts":42,"seconds":1000}}'
    store.put("ops/paid-work.json", paid, **META)
    before = store.list_objects("")
    result = planner.prepare(proposal(), store)
    assert result["condition"] == {"if_none_match": "*"}
    assert result["policy"]["baseline_bytes"]["forecast-root"] == 6
    assert result["policy"]["baseline_bytes"]["prepared"] == 5
    assert result["policy"]["baseline_bytes"]["technical"] == len(paid)
    assert result["policy"]["paused"] and not result["policy"]["rollout_complete"]
    assert store.list_objects("") == before
    assert store.get("ops/paid-work.json") == paid
    assert store.get(KEY) is None


def test_update_preserves_active_finished_expired_and_seen_work(tmp_path):
    store = DirStore(tmp_path)
    current = policy()
    current["epoch"] = 7
    store.put(KEY, encode(current), **META)
    admission = Admission(store, clock=lambda: 100)
    active = admission.acquire("forecast-root", "lost-upload", 100, seconds=1)
    finished = admission.acquire("land", "completed", 200, seconds=1)
    admission.finish(finished)
    original, etag = admission.read()
    result = planner.prepare(proposal(), store)
    assert result["condition"] == {"if_match": etag}
    for field in ("epoch", "baseline_bytes", "reservations", "seen_work"):
        assert result["policy"][field] == original[field]
    assert result["policy"]["reservations"][active.token]["bytes"] == 100
    assert result["policy"]["reservations"][finished.token]["bytes"] == 200
    assert admission.read() == (original, etag)


def test_existing_unaccounted_growth_requires_reconciliation(tmp_path):
    store = DirStore(tmp_path)
    store.put(KEY, encode(policy()), **META)
    store.put("forecast-runs/legacy/tile", b"unaccounted", **META)
    with pytest.raises(CapacityDenied, match="unexpected managed"):
        planner.prepare(proposal(), store)


@pytest.mark.parametrize("change", ["mode", "prefixes", "missing-owner"])
def test_existing_owner_conversion_cannot_erase_charges(tmp_path, change):
    store = DirStore(tmp_path)
    store.put(KEY, encode(policy()), **META)
    proposed = proposal()
    if change == "mode":
        proposed["owners"]["prepared"]["mode"] = "managed"
    elif change == "prefixes":
        proposed["owners"]["land"]["prefixes"] = ["other/"]
    else:
        del proposed["owners"]["land"]
        del proposed["baseline_bytes"]["land"]
    with pytest.raises(CapacityDenied, match="ownership changes"):
        planner.prepare(proposed, store)


@pytest.mark.parametrize("kind", ["unknown", "unmanaged", "missing-parts", "unknown-ledger"])
def test_uncertain_inventory_never_initializes_as_zero(tmp_path, kind):
    class Store(DirStore):
        def multipart_bytes(self):
            if kind == "missing-parts":
                raise RuntimeError("unavailable")
            return 0

        def get_with_etag(self, key):
            if kind == "unknown-ledger":
                raise RuntimeError("lost response")
            return super().get_with_etag(key)

    store = Store(tmp_path)
    if kind == "unknown":
        store.put("foreign/data", b"x", **META)
    elif kind == "unmanaged":
        store.put("prepared/legacy", b"x" * 1001, **META)
    with pytest.raises(CapacityDenied):
        planner.prepare(proposal(), store)


def test_accounting_peak_equality_and_one_byte_over(tmp_path):
    store = DirStore(tmp_path)
    doc = proposal()
    # Full unmanaged ceilings, headroom and control allowance survive staging.
    for owner in doc["owners"].values():
        owner["mode"] = "unmanaged"
    doc["limit_bytes"] = 90_000_000 + 1000 + 10_000 + 100 + 10 + 262_144
    planner.prepare(doc, store)
    doc["limit_bytes"] -= 1
    with pytest.raises(CapacityDenied, match="allocations exceed"):
        planner.prepare(doc, store)


def test_output_is_private_exclusive_and_cannot_be_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "ROOT", tmp_path / "public-checkout")
    destination = tmp_path / "private.json"
    doc = {"policy": deepcopy(proposal())}
    planner.write_private(destination, doc)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text()) == doc
    with pytest.raises(FileExistsError):
        planner.write_private(destination, {})
    with pytest.raises(ValueError, match="outside"):
        planner.write_private(planner.ROOT / "private.json", doc)
