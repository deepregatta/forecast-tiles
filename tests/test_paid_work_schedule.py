"""Scheduled-slot protection stays inside the normal conditional admission."""

import copy
import io
import json
from datetime import datetime, timezone

import pytest

from ingest.paid_work import Guard, Paused


class Store:
    def __init__(self, doc):
        self.doc = copy.deepcopy(doc)
        self.etag = self.puts = 0
        self.before_put = None
        self.uncertain = False

    def get_object(self, **_):
        return {"Body": io.BytesIO(json.dumps(self.doc).encode()), "ETag": str(self.etag)}

    def put_object(self, **kw):
        self.puts += 1
        if self.before_put:
            callback, self.before_put = self.before_put, None
            callback()
        if kw["IfMatch"] != str(self.etag):
            error = RuntimeError("conflict")
            error.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}
            raise error
        self.doc = json.loads(kw["Body"])
        self.etag += 1
        if self.uncertain:
            raise TimeoutError("lost confirmation")


@pytest.fixture
def admission():
    now = [datetime(2026, 10, 12, 8, tzinfo=timezone.utc).timestamp()]
    doc = {
        "version": 1,
        "paused": False,
        "reviewed_at": "2026-10-12T00:00:00Z",
        "period_start": "2026-10-01T00:00:00Z",
        "period_end": "2026-11-01T00:00:00Z",
        "gates": [{"allow_paid_work": True}],
        "max_seconds": 90000,
        "max_starts": 32,
        "single_active": False,
        "channels": {
            "regional": {"daily_starts": 2, "min_interval_seconds": 21600, "max_run_seconds": 9000}
        },
    }
    store = Store(doc)
    return now, store, Guard(store, "scratch", clock=lambda: now[0])


def test_frequency_rejection_charges_nothing(admission):
    now, store, guard = admission
    before = copy.deepcopy(store.doc)
    with pytest.raises(Paused, match="next scheduled"):
        guard.acquire("regional", "late", 9000, protect_next_at=now[0] + 21600 - 1)
    assert store.doc == before and store.puts == 0


def test_exact_frequency_boundary_is_eligible(admission):
    now, store, guard = admission
    token = guard.acquire("regional", "safe", 9000, protect_next_at=now[0] + 21600)
    guard.finish("regional", token)
    assert store.doc["usage"]["starts"] == 1
    now[0] += 21600
    guard.acquire("regional", "scheduled", 9000)
    assert store.doc["usage"]["starts"] == 2


def test_catchup_preserves_same_day_start(admission):
    now, store, guard = admission
    store.doc["channels"]["regional"]["daily_starts"] = 1
    before = copy.deepcopy(store.doc)
    with pytest.raises(Paused, match="next scheduled"):
        guard.acquire("regional", "last-start", 9000, protect_next_at=now[0] + 21600)
    assert store.doc == before and store.puts == 0
    # Tomorrow uses a new daily counter, so the last start today is allowed.
    guard.acquire("regional", "today", 9000, protect_next_at=now[0] + 86400)
    assert store.doc["usage"]["starts"] == 1


def test_reserved_lease_cannot_overlap_next_slot(admission):
    now, store, guard = admission
    store.doc["channels"]["regional"]["min_interval_seconds"] = 900
    before = copy.deepcopy(store.doc)
    with pytest.raises(Paused, match="next scheduled"):
        guard.acquire("regional", "long-lease", 9000, protect_next_at=now[0] + 9000)
    assert store.doc == before and store.puts == 0


@pytest.mark.parametrize("change", ["clock", "policy"])
def test_conflict_rechecks_protection_before_charge(admission, change):
    now, store, guard = admission
    next_at = now[0] + 21600

    def conflict():
        if change == "clock":
            now[0] += 1
        else:
            store.doc["channels"]["regional"]["min_interval_seconds"] += 1
        store.etag += 1

    store.before_put = conflict
    with pytest.raises(Paused, match="next scheduled"):
        guard.acquire("regional", "conflicting", 9000, protect_next_at=next_at)
    assert "usage" not in store.doc and store.puts == 1


def test_uncertain_protected_put_is_never_repeated(admission):
    now, store, guard = admission
    store.uncertain = True
    with pytest.raises(Paused, match="outcome unknown"):
        guard.acquire("regional", "uncertain", 9000, protect_next_at=now[0] + 21600)
    assert store.puts == 1 and store.doc["usage"]["starts"] == 1


@pytest.mark.parametrize("invalid", [False, -1, float("nan"), float("inf")])
def test_invalid_protection_is_closed(admission, invalid):
    _, store, guard = admission
    with pytest.raises(Paused, match="invalid next scheduled"):
        guard.acquire("regional", "invalid", 9000, protect_next_at=invalid)
    assert store.puts == 0 and "usage" not in store.doc
