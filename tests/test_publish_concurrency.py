"""Concurrent publishers sharing one latest.json (plan Phase 0).

Every test interleaves a competing writer at a chosen point and then checks
the two things the race used to break: every committed layer entry survives,
and every run the pointer names still has its manifest."""

import json
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_weather_cube
from test_publish import FakeStore, seed_run

from ingest import cli
from ingest.publish import (
    COMMIT_ATTEMPTS,
    DirStore,
    PointerConflictError,
    PreconditionFailed,
    StalePublishError,
    UncertainWriteError,
    apply_retention,
    commit_layer_entry,
    content_etag,
    publish_run,
)
from ingest.tile import build_tiles
from ingest.validate import validate_cube

DAY = datetime(2026, 7, 13, tzinfo=timezone.utc)


def make_run(layer="weather", hour=6):
    cube = make_weather_cube()
    cube.layer = layer
    cube.cycle = DAY + timedelta(hours=hour)
    report = validate_cube(cube)
    assert report.ok
    return cube, build_tiles(cube, generated_at="2026-07-13T10:00:00Z"), report


def publish(store, layer="weather", hour=6, **kwargs):
    cube, tiles, report = make_run(layer, hour)
    kwargs.setdefault("sleep", lambda s: None)
    return publish_run(store, cube, tiles, report, rng=random.Random(0), **kwargs)


def latest(store) -> dict:
    return json.loads(store.objects["latest.json"])


def assert_references_intact(store):
    for layer, entry in latest(store)["layers"].items():
        for run_id in (entry["run_id"], entry.get("previous_run_id")):
            if run_id:
                key = f"forecast-runs/{run_id}/manifest.json"
                assert key in store.objects, f"{layer} names {run_id}, whose manifest is gone"


def competitor(layer, hour):
    """A before_put hook publishing another run in full, between the
    publisher's read of latest.json and its compare-and-swap."""
    return lambda store, key: publish(store, layer, hour)


# ------------------------------------------------------- different layers


def test_two_layers_committing_at_once_both_survive():
    store = FakeStore()
    publish(store, "weather", 0)
    publish(store, "ensemble", 0)
    # ensemble 06Z commits between weather 06Z's read and its write
    store.before_put.append(competitor("ensemble", 6))
    result = publish(store, "weather", 6)

    layers = latest(store)["layers"]
    assert layers["weather"]["run_id"] == "weather-20260713T06Z"
    assert layers["weather"]["previous_run_id"] == "weather-20260713T00Z"
    assert layers["ensemble"]["run_id"] == "ensemble-20260713T06Z"
    assert layers["ensemble"]["previous_run_id"] == "ensemble-20260713T00Z"
    assert result.commit_attempts == 2
    assert_references_intact(store)


def test_the_loser_rewrites_only_its_own_entry():
    """A retry merges into the document it re-read, never the stale one: the
    winner's entry is byte-for-byte what the winner wrote."""
    store = FakeStore()
    publish(store, "weather", 0)
    store.before_put.append(competitor("waves", 0))
    publish(store, "weather", 6)
    winner = dict(latest(store)["layers"]["waves"])

    store.before_put.append(competitor("currents", 0))
    publish(store, "weather", 12)
    assert latest(store)["layers"]["waves"] == winner
    assert set(latest(store)["layers"]) == {"weather", "waves", "currents"}


def test_first_publishers_racing_to_create_the_pointer():
    store = FakeStore()
    store.before_put.append(competitor("ensemble", 0))
    publish(store, "weather", 0)
    assert set(latest(store)["layers"]) == {"weather", "ensemble"}
    assert_references_intact(store)


def test_creating_the_pointer_uses_if_none_match_then_if_match():
    store = FakeStore()
    calls = []
    original = store.put

    def put(key, data, **kwargs):
        if key == "latest.json":
            calls.append((kwargs.get("if_match"), kwargs.get("if_none_match")))
        return original(key, data, **kwargs)

    store.put = put
    publish(store, "weather", 0)
    etag = content_etag(store.objects["latest.json"])
    publish(store, "weather", 6)
    assert calls == [(None, True), (etag, False)]


# ------------------------------------------------------- same layer, stale


def test_an_older_cycle_is_refused_before_any_upload():
    store = FakeStore()
    publish(store, "weather", 12)
    store.ops.clear()
    with pytest.raises(StalePublishError, match="never moves a layer back"):
        publish(store, "weather", 6)
    assert not [k for op, k in store.ops if op in ("put", "delete")]


def test_a_newer_cycle_committed_mid_publish_makes_the_slow_job_stale():
    """weather 06Z uploads while weather 12Z publishes in full. 06Z must not
    roll the layer back, and must not prune: 12Z's previous stays."""
    store = FakeStore()
    publish(store, "weather", 0)
    store.before_put.append(competitor("weather", 12))
    store.ops.clear()
    with pytest.raises(StalePublishError):
        publish(store, "weather", 6)

    entry = latest(store)["layers"]["weather"]
    assert entry["run_id"] == "weather-20260713T12Z"
    assert entry["previous_run_id"] == "weather-20260713T00Z"
    assert_references_intact(store)
    # the stale job's own run is left for the audit, not deleted by retention
    assert "forecast-runs/weather-20260713T06Z/manifest.json" in store.objects
    deletes_after_hook = [
        k for op, k in store.ops if op == "delete" and "weather-20260713T06Z" in k
    ]
    assert not deletes_after_hook
    assert "status/weather.json" in store.objects
    assert json.loads(store.objects["status/weather.json"])["run_id"] == "weather-20260713T12Z"


def test_same_cycle_republish_keeps_the_previous():
    store = FakeStore()
    publish(store, "weather", 0)
    publish(store, "weather", 6)
    result = publish(store, "weather", 6)  # a --force repair
    assert result.previous_run_id == "weather-20260713T00Z"
    assert latest(store)["layers"]["weather"]["previous_run_id"] == "weather-20260713T00Z"
    assert result.deleted_runs == []
    assert_references_intact(store)


# ------------------------------------------------------- bounded retries


def test_retry_exhaustion_raises_and_prunes_nothing():
    store = FakeStore()
    publish(store, "weather", 0)
    publish(store, "weather", 6)  # previous = 00Z
    seed_run(store, "weather-20260712T18Z")  # older than the previous: normally deleted
    # another layer commits before every one of weather 12Z's attempts
    store.before_put.extend(competitor("ensemble", h) for h in range(0, 6 * COMMIT_ATTEMPTS, 6))
    sleeps = []
    store.ops.clear()
    with pytest.raises(PointerConflictError, match=f"after {COMMIT_ATTEMPTS} attempts"):
        publish(store, "weather", 12, sleep=sleeps.append)

    assert len(sleeps) == COMMIT_ATTEMPTS - 1
    assert all(0 < s <= 12 for s in sleeps)
    assert latest(store)["layers"]["weather"]["run_id"] == "weather-20260713T06Z"
    assert "forecast-runs/weather-20260712T18Z/manifest.json" in store.objects
    weather_deletes = [k for op, k in store.ops if op == "delete" and "/weather-2026" in k]
    assert not weather_deletes, "a publisher that did not commit must not prune"
    assert_references_intact(store)


# ------------------------------------------------------- unknown outcome


def flaky_pointer_put(store, *, apply: bool, error=UncertainWriteError, times=1):
    """Make the next `times` pointer writes fail after (apply=True) or
    instead of (apply=False) being applied."""
    original = store.put
    remaining = [times]

    def put(key, data, **kwargs):
        if key == "latest.json" and remaining[0] > 0:
            remaining[0] -= 1
            if apply:
                original(key, data, **kwargs)
            raise error("connection reset after sending")
        return original(key, data, **kwargs)

    store.put = put


def test_a_write_that_landed_despite_an_error_is_recognized():
    store = FakeStore()
    publish(store, "weather", 0)
    publish(store, "weather", 6)
    seed_run(store, "weather-20260712T18Z")
    flaky_pointer_put(store, apply=True)
    store.ops.clear()
    result = publish(store, "weather", 12)

    assert result.previous_run_id == "weather-20260713T06Z"
    assert result.commit_attempts == 2
    assert result.deleted_runs == ["weather-20260712T18Z", "weather-20260713T00Z"]
    pointer_puts = [k for op, k in store.ops if op == "put" and k == "latest.json"]
    assert len(pointer_puts) == 1, "the landed write is confirmed by reading, not repeated"
    assert_references_intact(store)


def test_a_retried_write_refused_because_it_had_landed():
    """boto3 retries a timed-out PUT itself; the retry then fails If-Match
    against the write that did land. Reading settles it as a success."""
    store = FakeStore()
    publish(store, "weather", 0)
    flaky_pointer_put(store, apply=True, error=PreconditionFailed)
    result = publish(store, "weather", 6)
    assert result.previous_run_id == "weather-20260713T00Z"
    assert latest(store)["layers"]["weather"]["run_id"] == "weather-20260713T06Z"


def test_a_write_that_did_not_land_is_retried():
    store = FakeStore()
    publish(store, "weather", 0)
    flaky_pointer_put(store, apply=False)
    result = publish(store, "weather", 6)
    assert result.commit_attempts == 2
    assert latest(store)["layers"]["weather"]["previous_run_id"] == "weather-20260713T00Z"


def test_the_last_attempt_is_settled_by_reading_before_giving_up():
    store = FakeStore()
    store.objects["latest.json"] = b'{"schema_version": 1, "updated_at": "x", "layers": {}}'
    flaky_pointer_put(store, apply=True)
    entry = {
        "run_id": "weather-20260713T06Z",
        "cycle": "2026-07-13T06:00Z",
        "member_count": 1,
        "published_at": "2026-07-13T10:00:00Z",
    }
    result = commit_layer_entry(store, "weather", entry, attempts=1, sleep=lambda s: None)
    assert result.recovered and result.previous_run_id is None


def test_unknown_outcomes_every_time_end_in_a_conflict_error():
    store = FakeStore()
    publish(store, "weather", 0)
    flaky_pointer_put(store, apply=False, times=COMMIT_ATTEMPTS)
    with pytest.raises(PointerConflictError, match="outcome unknown"):
        publish(store, "weather", 6)
    assert latest(store)["layers"]["weather"]["run_id"] == "weather-20260713T00Z"


# ------------------------------------------------------- retention


def test_retention_leaves_newer_and_incomplete_runs_found_by_listing():
    store = FakeStore()
    publish(store, "weather", 0)
    publish(store, "weather", 6)
    seed_run(store, "weather-20260712T12Z")  # complete, older: deleted
    store.objects["forecast-runs/weather-20260712T18Z/weather/z250/N40W010.bin.gz"] = b"x"
    seed_run(store, "weather-20260713T18Z")  # a newer cycle another job is publishing
    store.objects["forecast-runs/weather-20260714T00Z/weather/z250/N40W010.bin.gz"] = b"y"

    result = publish(store, "weather", 12)
    assert result.deleted_runs == ["weather-20260712T12Z", "weather-20260713T00Z"]
    assert result.kept_runs == [
        "weather-20260712T18Z (incomplete: no manifest)",
        "weather-20260713T18Z (not older than the retained previous)",
        "weather-20260714T00Z (not older than the retained previous)",
    ]
    assert "forecast-runs/weather-20260712T18Z/weather/z250/N40W010.bin.gz" in store.objects
    assert "forecast-runs/weather-20260713T18Z/manifest.json" in store.objects
    assert "forecast-runs/weather-20260714T00Z/weather/z250/N40W010.bin.gz" in store.objects


def test_retention_rechecks_every_pointer_before_deleting():
    store = FakeStore()
    seed_run(store, "weather-20260712T00Z")
    seed_run(store, "weather-20260712T06Z")
    seed_run(store, "weather-20260712T12Z")
    store.objects["latest-regional.json"] = json.dumps(
        {"layers": {"other": {"run_id": "weather-20260712T00Z", "previous_run_id": None}}}
    ).encode()
    deleted, kept = apply_retention(
        store,
        "weather",
        "weather-20260712T12Z",
        "weather-20260712T06Z",
        pointer_keys=("latest.json", "latest-regional.json"),
    )
    assert deleted == []
    assert kept == ["weather-20260712T00Z (referenced by a pointer)"]


def test_retention_deletes_tiles_before_the_manifest():
    store = FakeStore()
    seed_run(store, "weather-20260712T00Z")
    store.ops.clear()
    apply_retention(store, "weather", "weather-20260712T12Z", "weather-20260712T06Z")
    deletes = [k for op, k in store.ops if op == "delete"]
    assert deletes[-1] == "forecast-runs/weather-20260712T00Z/manifest.json"
    assert len(deletes) == 3


def test_retention_keeps_runs_newer_than_the_new_run_without_a_previous():
    store = FakeStore()
    seed_run(store, "weather-20260712T00Z")
    seed_run(store, "weather-20260712T18Z")
    deleted, kept = apply_retention(store, "weather", "weather-20260712T06Z", None)
    assert deleted == ["weather-20260712T00Z"]
    assert kept == ["weather-20260712T18Z (not older than the retained previous)"]


# ------------------------------------------------------- real processes' view


class BarrierDirStore(DirStore):
    """Holds every publisher's first pointer read until all have read it, so
    every one of them starts its compare-and-swap from the same document."""

    def __init__(self, root, parties):
        super().__init__(root)
        self.barrier = threading.Barrier(parties, timeout=30)
        self.local = threading.local()

    def get_with_etag(self, key):
        result = super().get_with_etag(key)
        if key == "latest.json" and not getattr(self.local, "waited", False):
            self.local.waited = True
            self.barrier.wait()
        return result


def test_every_layer_committing_at_once_on_a_directory_store(tmp_path):
    layers = ["weather", "weather-ecmwf", "weather-ecmwf-short", "ensemble", "waves", "currents"]
    seed = DirStore(tmp_path)
    for layer in layers:
        publish(seed, layer, 0)
    store = BarrierDirStore(tmp_path, len(layers))
    errors = []

    def run(layer):
        try:
            publish(store, layer, 6)
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(layer,)) for layer in layers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    doc = json.loads((tmp_path / "latest.json").read_text())
    for layer in layers:
        assert doc["layers"][layer]["run_id"] == f"{layer}-20260713T06Z"
        assert doc["layers"][layer]["previous_run_id"] == f"{layer}-20260713T00Z"
        for hour in ("00", "06"):
            assert (tmp_path / f"forecast-runs/{layer}-20260713T{hour}Z/manifest.json").exists()


def test_directory_store_conditional_writes(tmp_path):
    store = DirStore(tmp_path)
    kw = {"content_type": "application/json", "cache_control": "no-cache"}
    etag = store.put("p.json", b"one", **kw, if_none_match=True)
    assert etag == content_etag(b"one") == store.get_with_etag("p.json")[1]
    with pytest.raises(PreconditionFailed):
        store.put("p.json", b"two", **kw, if_none_match=True)
    with pytest.raises(PreconditionFailed):
        store.put("p.json", b"two", **kw, if_match=content_etag(b"other"))
    with pytest.raises(PreconditionFailed):
        store.put("absent.json", b"two", **kw, if_match=etag)
    store.put("p.json", b"two", **kw, if_match=etag)
    assert store.get("p.json") == b"two"
    assert store.get_with_etag("missing") == (None, None)
    assert store.list_keys("") == ["p.json"], "no temporary or lock files in the layout"


# ------------------------------------------------------- CLI


def test_cli_force_refuses_an_older_cycle_before_downloading(tmp_path, monkeypatch, capsys):
    from ingest.sources import gfs

    cube = make_weather_cube()
    built = []

    def build(cycle):
        built.append(cycle)
        cube.cycle = cycle
        return cube

    monkeypatch.setattr(gfs, "resolve", lambda requested=None: requested or cube.cycle)
    monkeypatch.setattr(gfs, "build_cube", build)
    assert cli.main(["weather", "--dry-run", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = cli.main(["weather", "--cycle", "20260712T18", "--force", "--dry-run", str(tmp_path)])
    assert rc == 1
    assert "never moves a layer back" in capsys.readouterr().out
    assert len(built) == 1
