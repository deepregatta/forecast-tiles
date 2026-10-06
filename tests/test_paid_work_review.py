"""Review renewal cannot forgive usage or reopen another closed policy gate."""

import io
import json
from copy import deepcopy
from datetime import datetime

import pytest

from ingest.paid_work import Guard, Paused
from ingest.paid_work_review import renew_review

OLD = "2026-10-03T09:03:45+00:00"
NEW = "2026-10-06T21:00:00+00:00"
NOW = datetime.fromisoformat(NEW).timestamp() + 5


class Store:
    def __init__(self):
        self.doc = {
            "version": 1,
            "paused": False,
            "period_start": "2026-10-01T00:00:00+00:00",
            "period_end": "2026-11-01T00:00:00+00:00",
            "reviewed_at": OLD,
            "gates": [{"provider": "r2-account", "allow_paid_work": True}],
            "max_seconds": 999,
            "max_starts": 5,
            "single_active": False,
            "channels": {"regional": {"daily_starts": 2}},
            "usage": {
                "seconds": 100,
                "starts": 1,
                "channels": {
                    "regional": {
                        "seen": {"charged": "token"},
                        "active": {"token": "token", "until": NOW + 10},
                    }
                },
            },
        }
        self.puts = []
        self.error = None

    def get_object(self, **_):
        return {"Body": io.BytesIO(json.dumps(self.doc).encode()), "ETag": '"original"'}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self.error:
            raise self.error
        self.doc = json.loads(kwargs["Body"])


def test_only_timestamp_changes_and_dry_run_has_no_write():
    store = Store()
    original = deepcopy(store.doc)
    guard = Guard(store, "bucket", clock=lambda: NOW)
    assert renew_review(guard, OLD, NEW) is False
    assert not store.puts and store.doc == original
    assert renew_review(guard, OLD, NEW, apply=True) is True
    assert store.puts[0]["IfMatch"] == '"original"'
    original["reviewed_at"] = NEW
    assert store.doc == original  # Includes all charged identities and the active lease.


@pytest.mark.parametrize(
    "change",
    [
        {"paused": True},
        {"period_end": "2026-10-06T20:00:00Z"},
        {"gates": [{"allow_paid_work": False}]},
        {"gates": [{"spend_eur_ttc": 0, "pause_at_eur_ttc": 5}]},
        {"reviewed_at": NEW},
    ],
)
def test_other_closed_gates_or_changed_review_never_write(change):
    store = Store()
    store.doc.update(change)
    with pytest.raises(Paused):
        renew_review(Guard(store, "bucket", clock=lambda: NOW), OLD, NEW, apply=True)
    assert not store.puts


@pytest.mark.parametrize(
    "value", ["2026-10-06T21:00:00", "2026-10-06T21:01:00Z", "2026-10-06T20:30:00Z"]
)
def test_unknown_future_or_stale_review_refused(value):
    store = Store()
    with pytest.raises(Paused):
        renew_review(Guard(store, "bucket", clock=lambda: NOW), OLD, value, apply=True)
    assert not store.puts


@pytest.mark.parametrize("status", [412, 503])
def test_conflict_and_uncertain_put_are_never_repeated(status):
    store = Store()
    store.error = RuntimeError("write failed")
    store.error.response = {"ResponseMetadata": {"HTTPStatusCode": status}}
    with pytest.raises(Paused):
        renew_review(Guard(store, "bucket", clock=lambda: NOW), OLD, NEW, apply=True)
    assert len(store.puts) == 1


def test_changed_authenticated_readback_stops_without_repeating_write():
    store = Store()
    original_put = store.put_object

    def changed_put(**kwargs):
        original_put(**kwargs)
        store.doc["paused"] = True

    store.put_object = changed_put
    with pytest.raises(Paused, match="read-back differs"):
        renew_review(Guard(store, "bucket", clock=lambda: NOW), OLD, NEW, apply=True)
    assert len(store.puts) == 1
    assert store.doc["paused"] is True
