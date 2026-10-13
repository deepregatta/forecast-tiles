"""A late automatic retry must not consume the next scheduled admission."""

import copy
import io
import json
from datetime import datetime, timezone

import pytest

from ingest import cli
from ingest.paid_work import Guard
from ingest.publish import DirStore


class Store:
    def __init__(self, doc):
        self.doc = copy.deepcopy(doc)
        self.etag = 0
        self.before_put = None
        self.puts = 0

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


def policy():
    return {
        "version": 1,
        "paused": False,
        "period_start": "2026-10-01T00:00:00Z",
        "period_end": "2026-11-01T00:00:00Z",
        "reviewed_at": "2026-10-12T20:00:00Z",
        "gates": [{"provider": "r2-account", "allow_paid_work": True}],
        "max_seconds": 36000,
        "max_starts": 10,
        "single_active": False,
        "channels": {
            "weather-icon-eu": {
                "daily_starts": 2,
                "min_interval_seconds": 21600,
                "max_run_seconds": 9000,
            }
        },
    }


def test_late_catchup_preserves_next_scheduled_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("PAID_WORK_ENFORCE", "1")
    monkeypatch.setenv("REGIONAL_ENABLED_LAYERS", "weather-icon-eu")
    monkeypatch.setenv("OPENMETEO_CANARY", "true")
    now = datetime(2026, 10, 12, 22, 15, tzinfo=timezone.utc).timestamp()
    store = Store(policy())
    guard = Guard(store, "scratch", clock=lambda: now)
    monkeypatch.setattr(Guard, "from_env", lambda: guard)
    monkeypatch.setattr(cli, "make_r2_store_from_env", lambda: DirStore(tmp_path / "bucket"))
    cycle = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "_resolve", lambda *_: cycle)
    monkeypatch.setattr(
        cli, "_build", lambda *_: pytest.fail("late catch-up was charged and built")
    )
    report = tmp_path / "attempt.json"
    assert cli.main(["weather-icon-eu", "--attempt-report", str(report)]) == 0
    assert store.doc == policy() and store.puts == 0
    attempt = json.loads(report.read_text())
    assert attempt["outcome"] == "paused" and attempt["pointer_commit_confirmed"] is None
    assert attempt["cycle"] == "2026-10-12T12:00Z"
    assert attempt["output"] is None


def test_exact_recovery_still_uses_existing_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("PAID_WORK_ENFORCE", "1")
    monkeypatch.setenv("REGIONAL_ENABLED_LAYERS", "weather-icon-eu")
    monkeypatch.setenv("OPENMETEO_CANARY", "true")
    now = datetime(2026, 10, 13, 7, 45, tzinfo=timezone.utc).timestamp()
    store = Store(policy())
    guard = Guard(store, "scratch", clock=lambda: now)
    monkeypatch.setattr(Guard, "from_env", lambda: guard)
    monkeypatch.setattr(cli, "make_r2_store_from_env", lambda: DirStore(tmp_path / "bucket"))
    monkeypatch.setattr(cli, "_resolve", lambda *_: datetime(2026, 10, 13, tzinfo=timezone.utc))

    def admitted(*_):
        raise RuntimeError("normal admission reached")

    monkeypatch.setattr(cli, "_build", admitted)
    with pytest.raises(RuntimeError, match="normal admission reached"):
        cli.main(["weather-icon-eu", "--cycle", "20261013T00"])
    assert store.doc["usage"]["starts"] == 1
    assert store.doc["usage"]["channels"]["weather-icon-eu"]["days"] == {"2026-10-13": 1}


@pytest.mark.parametrize(
    "layer,cycle,expected,canary",
    [
        ("weather-icon-eu", "2026-10-12T12:00:00+00:00", "2026-10-13T03:25:00+00:00", True),
        ("weather-arome", "2026-10-12T15:00:00+00:00", "2026-10-13T05:45:00+00:00", True),
        ("weather-ukv", "2026-10-13T00:00:00+00:00", "2026-10-13T16:15:00+00:00", True),
        ("weather-icon-eu", "2026-10-12T12:00:00+00:00", "2026-10-12T21:25:00+00:00", False),
    ],
)
def test_next_dispatch_uses_registered_profile(monkeypatch, layer, cycle, expected, canary):
    from ingest.regional_schedule import next_dispatch

    monkeypatch.setenv("OPENMETEO_CANARY", "true" if canary else "false")
    assert next_dispatch(layer, datetime.fromisoformat(cycle)).isoformat() == expected
