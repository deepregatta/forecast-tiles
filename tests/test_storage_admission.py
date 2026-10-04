"""Storage v1 acceptance cases, all on scratch stores; no production writes."""

import json
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_land_publish import make_index
from test_publish import FakeStore, make_run
from test_publish_concurrency import flaky_pointer_put

from ingest.publish import (
    DirStore,
    PreconditionFailed,
    StorageGuardError,
    UncertainWriteError,
    publish_run,
)
from ingest.land.publish import publish_index
from ingest.storage_admission import (
    Admission,
    CapacityDenied,
    KEY,
    MUTABLE_UPLOAD_BYTES,
    ReservedStore,
    account_cost_model,
    encode,
    envelope,
    inventory,
    validate,
)

META = {"content_type": "application/json", "cache_control": "no-store"}


def policy(*, limit=10**8):
    return {
        "version": 1,
        "paused": False,
        "rollout_complete": True,
        "epoch": 0,
        "limit_bytes": limit,
        "headroom_bytes": 10,
        "owners": {
            "forecast-root": {
                "mode": "managed",
                "prefixes": ["forecast-runs/"],
                "ceiling_bytes": 30_000_000,
            },
            "forecast-regional": {
                "mode": "managed",
                "prefixes": ["forecast-runs/weather-arome-"],
                "ceiling_bytes": 30_000_000,
            },
            "land": {"mode": "managed", "prefixes": ["land-index/"], "ceiling_bytes": 30_000_000},
            "prepared": {"mode": "unmanaged", "prefixes": ["prepared/"], "ceiling_bytes": 1000},
            "technical": {
                "mode": "unmanaged",
                "prefixes": ["ops/", "status/", "latest"],
                "ceiling_bytes": 10_000,
            },
            "multipart": {
                "mode": "unmanaged",
                "prefixes": ["multipart-staging/"],
                "ceiling_bytes": 100,
            },
        },
        "baseline_bytes": {
            n: 0
            for n in (
                "forecast-root",
                "forecast-regional",
                "land",
                "prepared",
                "technical",
                "multipart",
            )
        },
        "reservations": {},
        "seen_work": [],
    }


def seed(store, doc=None):
    doc = doc or policy()
    store.put(KEY, encode(doc), **META)
    return Admission(store, conflicts=(PreconditionFailed,), clock=lambda: 100)


def scoped(admission, r):
    return ReservedStore(admission, r, prefix="forecast-runs/test/", mutable_keys=("latest.json",))


def test_exact_boundary_counts_unconverted_allocations_metadata_and_headroom(tmp_path):
    store = DirStore(tmp_path)
    doc = policy()
    initial = envelope(doc, inventory(store, doc["owners"]))
    doc["limit_bytes"] = initial + 100
    a = seed(store, doc)
    a.acquire("forecast-root", "one", 100)
    with pytest.raises(CapacityDenied, match="bucket capacity"):
        a.acquire("land", "different-writer", 1)


def test_distinct_simultaneous_writers_cannot_both_spend_same_headroom(tmp_path):
    barrier = threading.Barrier(2)

    class RacingStore(DirStore):
        def put(self, key, data, **kwargs):
            if key == KEY and kwargs.get("if_match") and not getattr(local, "waited", False):
                local.waited = True
                barrier.wait(timeout=10)
            return super().put(key, data, **kwargs)

    local = threading.local()
    store = RacingStore(tmp_path)
    d = policy()
    d["limit_bytes"] = envelope(d, inventory(store, d["owners"])) + 199
    seed(store, d)
    outcomes = []

    def reserve(writer):
        try:
            Admission(store, conflicts=(PreconditionFailed,)).acquire(writer, writer, 100)
            outcomes.append("admitted")
        except CapacityDenied:
            outcomes.append("denied")

    threads = [threading.Thread(target=reserve, args=(w,)) for w in ("forecast-root", "land")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)
    assert sorted(outcomes) == ["admitted", "denied"]
    assert len(json.loads(store.get(KEY))["reservations"]) == 1


def test_inventory_counts_orphans_all_prepared_revisions_land_and_technical(tmp_path):
    store = DirStore(tmp_path)
    for key in (
        "forecast-runs/orphan/tile",
        "prepared/runs/old/file.sha1.json",
        "prepared/runs/old/file.sha2.json",
        "land-index/incomplete/tile",
        "ops/paid-work.json",
    ):
        store.put(key, b"x" * 10, **META)
    totals = inventory(store, policy()["owners"])
    assert sum(totals.values()) == 50
    assert totals["prepared"] == 20


@pytest.mark.parametrize(
    "growth", ["prepared/new", "forecast-runs/unexpected/tile", "unknown/prefix"]
)
def test_unexpected_writer_growth_fails_closed(tmp_path, growth):
    store = DirStore(tmp_path)
    a = seed(store)
    store.put(growth, b"x" * 1001, **META)
    with pytest.raises(CapacityDenied, match="growth|allocation"):
        a.acquire("land", "new", 1)


def test_inventory_transport_invalid_size_and_multipart_unknown_never_zero(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    for bad in (
        lambda _: (_ for _ in ()).throw(TimeoutError()),
        lambda _: [{"key": "prepared/x", "bytes": None}],
    ):
        store.list_objects = bad
        with pytest.raises(CapacityDenied, match="inventory unavailable"):
            a.acquire("land", "new", 1)
    store.list_objects = lambda _: []
    store.multipart_bytes = lambda: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(CapacityDenied, match="inventory unavailable"):
        a.acquire("land", "new", 1)


def test_multipart_parts_use_separate_conservative_ceiling(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    store.multipart_bytes = lambda: 101
    with pytest.raises(CapacityDenied, match="unconverted"):
        a.acquire("land", "new", 1)


@pytest.mark.parametrize("landed", [False, True])
def test_lost_reservation_response_never_starts_upload_or_retries(tmp_path, landed):
    store = DirStore(tmp_path)
    a = seed(store)
    original = store.put
    attempts = []

    def put(key, data, **kwargs):
        attempts.append(key)
        if landed:
            original(key, data, **kwargs)
        raise UncertainWriteError("lost response")

    store.put = put
    with pytest.raises(CapacityDenied, match="outcome unknown"):
        a.acquire("land", "new", 100)
    assert attempts == [KEY]
    assert bool(a.read()[0]["reservations"]) == landed
    assert not store.list_keys("land-index/")


def test_expiry_interruption_and_lost_upload_response_keep_full_debit(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    d, _ = a.read()
    d["limit_bytes"] = envelope(d, inventory(store, d["owners"])) + 100
    store.put(KEY, encode(d), **META)
    r = a.acquire("forecast-root", "crash", 100, seconds=1)
    s = scoped(a, r)
    original = store.put

    def put(key, data, **kwargs):
        original(key, data, **kwargs)
        raise UncertainWriteError("lost tile response")

    store.put = put
    with pytest.raises(UncertainWriteError):
        s.put("forecast-runs/test/tile", b"x", **META)
    a.clock = lambda: 102
    with pytest.raises(CapacityDenied, match="expired"):
        s.put("latest.json", b"{}", **META)
    with pytest.raises(CapacityDenied, match="bucket capacity"):
        a.acquire("land", "other", 1)
    assert store.get("latest.json") is None
    d, _ = a.read()
    d["paused"] = True
    original(KEY, encode(d), **META)
    with pytest.raises(CapacityDenied, match="stop/fence proof"):
        a.reconcile(evidence="expired lease alone")


def test_finish_never_refunds_and_reconcile_requires_pause_and_external_fence(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    r = a.acquire("forecast-root", "new", 100)
    scoped(a, r).put("forecast-runs/test/tile", b"x" * 10, **META)
    with pytest.raises(CapacityDenied, match="pause"):
        a.reconcile(evidence="receipt")
    d, _ = a.read()
    d["paused"] = True
    store.put(KEY, encode(d), **META)
    d = a.reconcile(
        stopped_tokens=[r.token], evidence="external writer stopped and in-flight PUT drained"
    )
    assert d["baseline_bytes"]["forecast-root"] == 10
    assert d["reservations"] == {} and d["seen_work"] and d["epoch"] == 1
    with pytest.raises(CapacityDenied, match="stopped"):
        scoped(a, r).put("forecast-runs/test/late", b"x", **META)


def test_completed_reservation_reconciles_without_touching_paid_work(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    store.put("ops/paid-work.json", b'{"usage":{"starts":12,"seconds":3456}}', **META)
    before = store.get("ops/paid-work.json")
    r = a.acquire("land", "done", 100)
    a.finish(r)
    assert a.read()[0]["reservations"][r.token]["bytes"] == 100
    d, _ = a.read()
    d["paused"] = True
    store.put(KEY, encode(d), **META)
    a.reconcile(evidence="all managed writers drained; unconverted writers fenced")
    assert store.get("ops/paid-work.json") == before


def test_reconciliation_lost_response_cannot_be_treated_as_release(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    d, _ = a.read()
    d["paused"] = True
    store.put(KEY, encode(d), **META)
    original = store.put

    def put(key, data, **kwargs):
        original(key, data, **kwargs)
        raise UncertainWriteError("reconciliation response lost")

    store.put = put
    with pytest.raises(CapacityDenied, match="outcome unknown"):
        a.reconcile(evidence="external receipt")
    assert a.read()[0]["paused"] is True


def test_paused_or_unfinished_rollout_unknown_and_malformed_state_no_data_writes(tmp_path):
    store = DirStore(tmp_path)
    a = Admission(store)
    with pytest.raises(CapacityDenied, match="unavailable"):
        a.acquire("land", "x", 1)
    for patch in (
        {"paused": True},
        {"rollout_complete": False},
        {"limit_bytes": True},
        {"version": 2},
    ):
        d = policy()
        d.update(patch)
        store.put(KEY, encode(d), **META)
        with pytest.raises(CapacityDenied):
            a.acquire("land", "x", 1)
    assert store.list_keys("") == [KEY]


def test_scoped_upload_cap_and_operator_pause_checked_before_every_write(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    r = a.acquire("forecast-root", "x", 2)
    s = scoped(a, r)
    with pytest.raises(CapacityDenied, match="scope"):
        s.put("prepared/other", b"x", **META)
    with pytest.raises(CapacityDenied, match="peak"):
        s.put("forecast-runs/test/tile", b"xxx", **META)
    d, _ = a.read()
    d["paused"] = True
    store.put(KEY, encode(d), **META)
    with pytest.raises(CapacityDenied, match="stopped"):
        s.put("latest.json", b"{}", **META)
    assert store.get("latest.json") is None


@pytest.mark.parametrize("kind", ["root", "regional", "land"])
def test_all_publication_paths_reserve_before_tiles_and_finish_without_credit(kind):
    store = FakeStore()
    a = seed(store)
    if kind == "land":
        manifest, tiles = make_index()
        publish_index(store, manifest, tiles, max_bucket_bytes=10**8, capacity_admission=a)
        data_prefix = "land-index/"
        pointer = "land-index/latest.json"
        writer = "land"
    else:
        cube, tiles, report = make_run()
        if kind == "regional":
            cube.layer = "weather-arome"
            from ingest.tile import build_tiles

            tiles = build_tiles(cube)
        publish_run(store, cube, tiles, report, capacity_admission=a)
        data_prefix = "forecast-runs/"
        pointer = "latest-regional.json" if kind == "regional" else "latest.json"
        writer = "forecast-regional" if kind == "regional" else "forecast-root"
    puts = [k for op, k in store.ops if op == "put"]
    assert puts[1] == KEY and puts.index(pointer) > next(
        i for i, k in enumerate(puts) if k.startswith(data_prefix) and k != pointer
    )
    r = next(iter(a.read()[0]["reservations"].values()))
    assert r["writer"] == writer and r["state"] == "finished" and r["bytes"] > MUTABLE_UPLOAD_BYTES
    assert store.get("ops/paid-work.json") is None


@pytest.mark.parametrize("kind", ["root", "regional", "land"])
def test_denied_admission_preserves_existing_objects_and_pointers(kind):
    store = FakeStore()
    a = seed(store)
    d, _ = a.read()
    d["paused"] = True
    store.objects[KEY] = encode(d)
    before = dict(store.objects)
    with pytest.raises(StorageGuardError, match="shared capacity"):
        if kind == "land":
            m, t = make_index()
            publish_index(store, m, t, max_bucket_bytes=10**8, capacity_admission=a)
        else:
            c, t, r = make_run()
            if kind == "regional":
                c.layer = "weather-arome"
                from ingest.tile import build_tiles

                t = build_tiles(c)
            publish_run(store, c, t, r, capacity_admission=a)
    assert store.objects == before


@pytest.mark.parametrize("landed", [False, True])
def test_uncertain_forecast_pointer_retains_charge_and_prunes_only_after_readback(landed):
    store = FakeStore()
    a = seed(store)
    flaky_pointer_put(store, apply=landed, times=6)
    c, t, r = make_run()
    if landed:
        publish_run(store, c, t, r, capacity_admission=a, sleep=lambda _: None)
    else:
        from ingest.publish import PointerConflictError

        with pytest.raises(PointerConflictError):
            publish_run(store, c, t, r, capacity_admission=a, sleep=lambda _: None)
        assert not [k for op, k in store.ops if op == "delete"]
    reservation = next(iter(a.read()[0]["reservations"].values()))
    assert reservation["bytes"] > 0
    assert reservation["state"] == ("finished" if landed else "active")


def test_immutable_root_force_refuses_changed_content_before_writes():
    store = FakeStore()
    c, t, r = make_run()
    publish_run(store, c, t, r)
    before = dict(store.objects)
    with pytest.raises(PreconditionFailed, match="immutable"):
        publish_run(store, c, [(tid, gz + b"changed") for tid, gz in t], r)
    assert store.objects == before


def test_partial_same_id_never_overwrites_an_orphan_tile():
    store = FakeStore()
    c, t, r = make_run()
    tid, _ = t[0]
    key = f"forecast-runs/{c.run_id}/{c.layer}/z250/{tid}.bin.gz"
    store.objects[key] = b"orphan content"
    with pytest.raises(PreconditionFailed, match="immutable"):
        publish_run(store, c, t, r)
    assert store.objects[key] == b"orphan content" and "latest.json" not in store.objects


def test_account_cost_applies_free_tier_once_and_unknown_is_not_zero():
    d = account_cost_model(
        {"forecast": 14_000_000_000, "other": 14_000_000_000},
        class_a=1_000_001,
        class_b=10_000_001,
        usd_to_eur=1,
        tax_multiplier=1.2,
    )
    assert d["storage_usd"] == pytest.approx(0.27)
    assert d["operations_usd"] == pytest.approx(4.86)
    assert d["total_eur_ttc"] > 4
    d = account_cost_model({"known": 10, "unavailable": None}, class_a=0, class_b=0)
    assert (
        d["unknown_buckets"] == ["unavailable"]
        and d["total_usd"] is None
        and d["storage_usd"] is None
    )


def test_contract_example_matches_schema_and_runtime_validation():
    root = Path(__file__).resolve().parents[1] / "contracts"
    example = json.loads((root / "storage-admission-v1.example.json").read_text())
    schema = json.loads((root / "storage-admission-v1.schema.json").read_text())
    Draft202012Validator(schema).validate(example)
    validate(example)
    assert example["paused"] and not example["rollout_complete"]


def test_unconverted_growth_during_upload_stops_pointer_promotion():
    store = FakeStore()
    a = seed(store)
    original = store.put

    def put(key, data, **kwargs):
        result = original(key, data, **kwargs)
        if key.endswith("/manifest.json"):
            store.objects["prepared/unexpected"] = b"x" * 1001
        return result

    store.put = put
    c, t, r = make_run()
    with pytest.raises(StorageGuardError, match="unconverted"):
        publish_run(store, c, t, r, capacity_admission=a)
    assert "latest.json" not in store.objects
    assert next(iter(a.read()[0]["reservations"].values()))["state"] == "active"


def test_land_distinct_domain_pointer_cas_preserves_both():
    store = FakeStore()
    m, t = make_index()

    def competitor(store, key):
        other, tiles = make_index("med-20260824T21Z", (("N30E000", 30, 0),))
        publish_index(store, other, tiles, max_bucket_bytes=10**8)

    store.before_put.append(competitor)
    publish_index(store, m, t, max_bucket_bytes=10**8)
    assert set(json.loads(store.get("land-index/latest.json"))["domains"]) == {"nweu", "med"}


@pytest.mark.parametrize("landed", [False, True])
def test_land_uncertain_pointer_no_retention_without_confirmation(landed):
    store = FakeStore()
    a = seed(store)
    original = store.put

    def put(key, data, **kwargs):
        if key == "land-index/latest.json":
            if landed:
                original(key, data, **kwargs)
            raise UncertainWriteError("lost land pointer response")
        return original(key, data, **kwargs)

    store.put = put
    m, t = make_index()
    if landed:
        publish_index(store, m, t, max_bucket_bytes=10**8, capacity_admission=a)
    else:
        from ingest.publish import PointerConflictError

        with pytest.raises(PointerConflictError):
            publish_index(store, m, t, max_bucket_bytes=10**8, capacity_admission=a)
        assert not [key for op, key in store.ops if op == "delete"]
    assert next(iter(a.read()[0]["reservations"].values()))["state"] == (
        "finished" if landed else "active"
    )


def test_land_retention_preserves_incomplete_and_newer_uncommitted_index():
    store = FakeStore()
    store.objects["land-index/nweu-20260101T00Z/tile"] = b"orphan"
    future, future_tiles = make_index("nweu-20270101T00Z")
    store.objects["land-index/nweu-20270101T00Z/manifest.json"] = encode(future)
    m, t = make_index()
    publish_index(store, m, t, max_bucket_bytes=10**8)
    assert store.get("land-index/nweu-20260101T00Z/tile") == b"orphan"
    assert store.get("land-index/nweu-20270101T00Z/manifest.json") is not None


def test_env_enforcement_is_at_publish_boundary_and_scratch_bypass(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPACITY_ENFORCE", "1")
    store = FakeStore()
    c, t, r = make_run()
    with pytest.raises(StorageGuardError, match="unavailable"):
        publish_run(store, c, t, r)
    assert not store.objects
    publish_run(DirStore(tmp_path), c, t, r)
    monkeypatch.setenv("CAPACITY_ENFORCE", "invalid")
    with pytest.raises(StorageGuardError, match="invalid CAPACITY_ENFORCE"):
        publish_run(store, c, t, r)


def test_portable_acceptance_vectors():
    import copy

    vectors = json.loads(
        (
            Path(__file__).resolve().parents[1] / "contracts/storage-admission-v1.vectors.json"
        ).read_text()
    )
    for case in vectors["cases"]:
        doc = copy.deepcopy(case["document"])
        validate(doc)
        work = "1" * 64

        def apply():
            envelope(doc, case["measured_bytes"])
            doc["seen_work"].append(work)
            doc["reservations"]["new"] = {
                "writer": case["writer"],
                "work": work,
                "bytes": case["request_bytes"],
                "expires_at": 1000,
                "state": "active",
            }
            return envelope(doc, case["measured_bytes"])

        if case["expected"] == "deny":
            with pytest.raises(CapacityDenied):
                apply()
        else:
            assert apply() == case["expected_peak_bytes"], case["name"]


def test_standalone_module_has_no_runtime_repository_imports():
    import ast

    source = Path(__file__).resolve().parents[1] / "src/ingest/storage_admission.py"
    imports = [
        node.module
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(not name.startswith(("ingest", "oscar", "passage")) for name in imports)


def test_policy_ownership_and_mutable_peak_are_enforced_before_put(tmp_path):
    store = DirStore(tmp_path)
    a = seed(store)
    r = a.acquire("land", "wrong-path", 100)
    s = ReservedStore(
        a, r, prefix="prepared/", mutable_keys=("unregistered-pointer", "latest.json")
    )
    with pytest.raises(CapacityDenied, match="ownership"):
        s.put("prepared/file", b"x", **META)
    with pytest.raises(CapacityDenied, match="unclassified"):
        s.put("unregistered-pointer", b"{}", **META)
    with pytest.raises(CapacityDenied, match="conservative allocation"):
        s.put("latest.json", b"x" * 10001, **META)
    assert store.list_keys("") == [KEY]
