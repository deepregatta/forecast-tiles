"""Review renewal cannot forgive usage or reopen another closed policy gate."""

import io
import json
from copy import deepcopy
from datetime import datetime

import pytest

from ingest.paid_work import Guard, Paused
from ingest.paid_work_review import renew_review, rollover_october20

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


PERIOD_OLD_REVIEW = "2026-10-19T20:00:00Z"
PERIOD_NEW_REVIEW = "2026-10-20T00:09:00Z"
PERIOD_NOW = datetime.fromisoformat("2026-10-20T00:10:00+00:00").timestamp()


def period_store():
    store = Store()
    store.doc.update(
        period_start="2026-09-20T00:00:00Z",
        period_end="2026-10-20T00:00:00Z",
        reviewed_at=PERIOD_OLD_REVIEW,
        additional_operator_field={"preserve": ["all", "values"]},
    )
    state = store.doc["usage"]["channels"]["regional"]
    state.update(days={"2026-10-19": 2}, last=PERIOD_NOW - 60)
    state["active"]["until"] = PERIOD_NOW + 10
    return store


def test_approved_period_changes_only_three_fields_and_preserves_full_ledger():
    store = period_store()
    original = deepcopy(store.doc)
    guard = Guard(store, "bucket", clock=lambda: PERIOD_NOW)
    assert rollover_october20(guard, PERIOD_OLD_REVIEW, PERIOD_NEW_REVIEW) is False
    assert not store.puts and store.doc == original
    assert rollover_october20(guard, PERIOD_OLD_REVIEW, PERIOD_NEW_REVIEW, apply=True) is True
    assert len(store.puts) == 1 and store.puts[0]["IfMatch"] == '"original"'
    original.update(
        period_start="2026-10-20T00:00:00Z",
        period_end="2026-11-20T00:00:00Z",
        reviewed_at=PERIOD_NEW_REVIEW,
    )
    assert store.doc == original  # Unknown fields, counters, identities and leases included.
    with pytest.raises(Paused, match="period changed"):
        rollover_october20(guard, PERIOD_NEW_REVIEW, "2026-10-20T00:10:00Z", apply=True)
    assert len(store.puts) == 1  # A completed rollover cannot be repeated.


@pytest.mark.parametrize("now", [PERIOD_NOW - 601, PERIOD_NOW + 31 * 86400])
def test_period_cannot_be_applied_early_or_after_approved_window(now):
    store = period_store()
    with pytest.raises(Paused, match="application period"):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: now),
            PERIOD_OLD_REVIEW,
            PERIOD_NEW_REVIEW,
            apply=True,
        )
    assert not store.puts


@pytest.mark.parametrize(
    "change",
    [
        {"paused": True},
        {"version": 2},
        {"period_start": "2026-09-21T00:00:00Z"},
        {"period_end": "2026-10-21T00:00:00Z"},
        {"reviewed_at": PERIOD_NEW_REVIEW},
        {"gates": [{"allow_paid_work": False}]},
        {"gates": [{"spend_eur_ttc": 0, "pause_at_eur_ttc": 5}]},
        {"gates": []},
    ],
)
def test_period_preserves_closed_policies_and_refuses_changed_preconditions(change):
    store = period_store()
    store.doc.update(change)
    original = deepcopy(store.doc)
    with pytest.raises(Paused):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: PERIOD_NOW),
            PERIOD_OLD_REVIEW,
            PERIOD_NEW_REVIEW,
            apply=True,
        )
    assert not store.puts and store.doc == original


@pytest.mark.parametrize(
    "review",
    [
        "2026-10-20T00:09:00",
        "2026-10-20T00:11:00Z",
        "2026-10-19T23:59:00Z",
        "2026-10-20T00:00:00Z",
    ],
)
def test_period_requires_real_started_period_review_within_fifteen_minutes(review):
    store = period_store()
    now = PERIOD_NOW if review != "2026-10-20T00:00:00Z" else PERIOD_NOW + 360
    with pytest.raises(Paused):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: now), PERIOD_OLD_REVIEW, review, apply=True
        )
    assert not store.puts


@pytest.mark.parametrize("status", [412, 503])
def test_period_conflict_or_uncertain_put_never_retried(status):
    store = period_store()
    store.error = RuntimeError("write failed")
    store.error.response = {"ResponseMetadata": {"HTTPStatusCode": status}}
    with pytest.raises(Paused):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: PERIOD_NOW),
            PERIOD_OLD_REVIEW,
            PERIOD_NEW_REVIEW,
            apply=True,
        )
    assert len(store.puts) == 1


def test_period_readback_mutation_stops_without_further_writes():
    store = period_store()
    original_put = store.put_object

    def concurrent_admission(**kwargs):
        original_put(**kwargs)
        store.doc["usage"]["starts"] += 1

    store.put_object = concurrent_admission
    with pytest.raises(Paused, match="read-back differs"):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: PERIOD_NOW),
            PERIOD_OLD_REVIEW,
            PERIOD_NEW_REVIEW,
            apply=True,
        )
    assert len(store.puts) == 1
    assert store.doc["usage"]["starts"] == 2  # Never overwrite the concurrent charge.


def test_period_review_expiring_during_control_read_cannot_write():
    store = period_store()
    clocks = iter([PERIOD_NOW, PERIOD_NOW + 900, PERIOD_NOW + 900])
    with pytest.raises(Paused, match="last 15 minutes"):
        rollover_october20(
            Guard(store, "bucket", clock=lambda: next(clocks)),
            PERIOD_OLD_REVIEW,
            PERIOD_NEW_REVIEW,
            apply=True,
        )
    assert not store.puts
