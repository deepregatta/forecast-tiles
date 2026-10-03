import gzip
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import make_weather_cube

from ingest.publish import (
    CACHE_IMMUTABLE,
    CACHE_MUTABLE,
    CADENCE_HOURS,
    PreconditionFailed,
    PublishError,
    StorageGuardError,
    build_manifest,
    check_storage_guard,
    content_etag,
    fnv64,
    publish_run,
    z_res,
)
from ingest.tile import build_tiles
from ingest.validate import validate_cube
from tilekit.codec import decode_tile

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts"


class FakeStore:
    """Dict-backed in-memory object store recording operation order, with the
    conditional-write semantics of R2 (ETag = MD5 of the body).

    `before_put` hooks run (once each, in order) just before the next
    conditional write is applied, which is how tests interleave a competing
    writer between a publisher's read and its compare-and-swap."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, tuple[str, str]] = {}
        self.ops: list[tuple[str, str]] = []
        self.before_put: list = []
        self._in_hook = False

    def put(self, key, data, *, content_type, cache_control, if_match=None, if_none_match=False):
        conditional = if_match is not None or if_none_match
        if conditional and self.before_put and not self._in_hook:
            hook = self.before_put.pop(0)
            self._in_hook = True  # a competitor's own writes inside the hook run plainly
            try:
                hook(self, key)
            finally:
                self._in_hook = False
        self.ops.append(("put", key))
        if if_none_match and key in self.objects:
            raise PreconditionFailed(f"{key}: exists")
        if if_match is not None and (
            key not in self.objects or content_etag(self.objects[key]) != if_match
        ):
            raise PreconditionFailed(f"{key}: changed")
        self.objects[key] = data
        self.meta[key] = (content_type, cache_control)
        return content_etag(data)

    def get(self, key):
        self.ops.append(("get", key))
        return self.objects.get(key)

    def get_with_etag(self, key):
        self.ops.append(("get", key))
        data = self.objects.get(key)
        return (data, content_etag(data)) if data is not None else (None, None)

    def list_keys(self, prefix):
        self.ops.append(("list", prefix))
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key):
        self.ops.append(("delete", key))
        self.objects.pop(key, None)


def make_run(cycle_day=13):
    cube = make_weather_cube()
    cube.cycle = datetime(2026, 7, cycle_day, 6, tzinfo=timezone.utc)
    report = validate_cube(cube)
    assert report.ok
    tiles = build_tiles(cube, generated_at="2026-07-13T10:00:00Z")
    return cube, tiles, report


def seed_run(store, run_id, n_tiles=2, tile_bytes=100):
    tiles = {}
    for i in range(n_tiles):
        key = f"forecast-runs/{run_id}/weather/z250/N{i:02d}W010.bin.gz"
        data = bytes([i]) * tile_bytes
        store.objects[key] = data
        tiles[f"N{i:02d}W010"] = {"bytes": len(data), "fnv64": fnv64(data)}
    store.objects[f"forecast-runs/{run_id}/manifest.json"] = json.dumps(
        {"totals": {"tile_count": n_tiles, "bytes": n_tiles * tile_bytes}, "tiles": tiles}
    ).encode()


def test_fnv64_known_vectors():
    assert fnv64(b"") == "cbf29ce484222325"
    assert fnv64(b"a") == "af63dc4c8601ec8c"
    assert fnv64(b"foobar") == "85944171f73967e8"


def test_z_res():
    assert z_res(0.25) == "z025"
    assert z_res(0.5) == "z050"
    assert z_res(1 / 12) == "z008"


def test_publish_ordering_manifest_last_latest_after_check():
    cube, tiles, report = make_run()
    store = FakeStore()
    publish_run(store, cube, tiles, report, rng=random.Random(0))

    puts = [k for op, k in store.ops if op == "put"]
    manifest_key = f"forecast-runs/{cube.run_id}/manifest.json"
    tile_puts = [k for k in puts if k.endswith(".bin.gz")]
    assert len(tile_puts) == len(tiles)
    # every tile is uploaded before the manifest; manifest before latest/status
    assert puts.index(manifest_key) > max(puts.index(k) for k in tile_puts)
    assert puts.index("latest.json") > puts.index(manifest_key)
    assert puts.index(f"status/{cube.layer}.json") > puts.index("latest.json")

    # post-publish verification (manifest + tiles re-download) happens between
    # the manifest upload and the latest.json update
    ops = store.ops
    manifest_put = ops.index(("put", manifest_key))
    latest_put = ops.index(("put", "latest.json"))
    verify_gets = [i for i, (op, k) in enumerate(ops) if op == "get" and cube.run_id in k]
    assert verify_gets and all(manifest_put < i < latest_put for i in verify_gets)

    # cache headers / content types
    assert store.meta[tile_puts[0]] == ("application/octet-stream", CACHE_IMMUTABLE)
    assert store.meta[manifest_key] == ("application/json", CACHE_IMMUTABLE)
    assert store.meta["latest.json"] == ("application/json", CACHE_MUTABLE)


def test_storage_guard_aborts_before_any_upload():
    cube, tiles, report = make_run()
    store = FakeStore()
    seed_run(store, "weather-20260712T06Z", tile_bytes=5000)
    store.objects["latest.json"] = json.dumps(
        {
            "schema_version": 1,
            "updated_at": "x",
            "layers": {
                "weather": {
                    "run_id": "weather-20260712T06Z",
                    "previous_run_id": None,
                    "cycle": "2026-07-12T06:00Z",
                    "published_at": "x",
                }
            },
        }
    ).encode()

    with pytest.raises(StorageGuardError):
        publish_run(store, cube, tiles, report, max_bucket_bytes=10_500)
    assert not any(op == "put" for op, _ in store.ops), "guard must fire before any upload"
    # 10 kB retained + small new run fits under a bigger budget
    publish_run(store, cube, tiles, report, max_bucket_bytes=10_000_000, rng=random.Random(0))


def test_retention_deletes_only_older_runs_of_same_layer():
    cube, tiles, report = make_run(cycle_day=13)
    store = FakeStore()
    seed_run(store, "weather-20260711T06Z")  # will be deleted (older than previous)
    seed_run(store, "weather-20260712T06Z")  # current -> becomes previous
    seed_run(store, "weather-ecmwf-20260712T00Z")  # other layer: untouched
    store.objects["latest.json"] = json.dumps(
        {
            "schema_version": 1,
            "updated_at": "x",
            "layers": {
                "weather": {
                    "run_id": "weather-20260712T06Z",
                    "previous_run_id": "weather-20260711T06Z",
                    "cycle": "2026-07-12T06:00Z",
                    "published_at": "x",
                },
                "weather-ecmwf": {
                    "run_id": "weather-ecmwf-20260712T00Z",
                    "previous_run_id": None,
                    "cycle": "2026-07-12T00:00Z",
                    "published_at": "x",
                },
            },
        }
    ).encode()

    result = publish_run(store, cube, tiles, report, rng=random.Random(0))
    assert result.previous_run_id == "weather-20260712T06Z"
    assert result.deleted_runs == ["weather-20260711T06Z"]
    assert not any("weather-20260711T06Z" in k for k in store.objects)
    assert any(k.startswith("forecast-runs/weather-20260712T06Z/") for k in store.objects)
    assert any(k.startswith("forecast-runs/weather-ecmwf-20260712T00Z/") for k in store.objects)

    latest = json.loads(store.objects["latest.json"])
    entry = latest["layers"]["weather"]
    assert entry["run_id"] == "weather-20260713T06Z"
    assert entry["previous_run_id"] == "weather-20260712T06Z"
    assert entry["cycle"] == "2026-07-13T06:00Z"
    assert entry["member_count"] == 1
    assert latest["layers"]["weather-ecmwf"]["run_id"] == "weather-ecmwf-20260712T00Z"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"",
        b"{",
        b"null",
        b"[]",
        b"{}",
        b'{"totals": {}}',
        b'{"totals": {"bytes": null}}',
        b'{"totals": {"bytes": "200"}}',
        b'{"totals": {"bytes": 200.5}}',
        b'{"totals": {"bytes": true}}',
        b'{"totals": {"bytes": -200}}',
        b'{"totals": {"bytes": 0}, "tiles": {}}',
        b'{"totals": {"bytes": 0}, "tiles": {"N40W010": {"bytes": 200}}}',
        b'{"totals": {"bytes": 200}, "tiles": {"N40W010": {"bytes": "200"}}}',
    ],
)
def test_storage_guard_refuses_unknown_retained_size_before_any_mutation(raw):
    cube, tiles, report = make_run()
    store = FakeStore()
    old = "weather-20260712T06Z"
    store.objects["latest.json"] = json.dumps(
        {"layers": {"weather": {"run_id": old, "cycle": "2026-07-12T06:00Z"}}}
    ).encode()
    if raw is not None:
        store.objects[f"forecast-runs/{old}/manifest.json"] = raw
    before = store.objects.copy()

    with pytest.raises(StorageGuardError, match=old):
        publish_run(store, cube, tiles, report)
    assert store.objects == before
    assert not any(op in ("put", "delete", "list") for op, _ in store.ops)


@pytest.mark.parametrize("pointer", ["latest.json", "latest-regional.json"])
@pytest.mark.parametrize("role", ["run_id", "previous_run_id"])
def test_storage_guard_requires_other_layers_current_and_previous_sizes(pointer, role):
    store = FakeStore()
    current, previous = "waves-20260712T06Z", "waves-20260712T00Z"
    seed_run(store, current)
    seed_run(store, previous)
    entry = {"run_id": current, "previous_run_id": previous}
    store.objects[pointer] = json.dumps({"layers": {"waves": entry}}).encode()
    del store.objects[f"forecast-runs/{entry[role]}/manifest.json"]
    with pytest.raises(StorageGuardError, match=entry[role]):
        check_storage_guard(store, "weather", 100, 100_000)


def test_storage_guard_refuses_unreadable_retained_manifest_before_any_mutation():
    cube, tiles, report = make_run()
    store = FakeStore()
    old = "weather-20260712T06Z"
    store.objects["latest.json"] = json.dumps(
        {"layers": {"weather": {"run_id": old, "cycle": "2026-07-12T06:00Z"}}}
    ).encode()
    get = store.get

    def unreadable(key):
        if key == f"forecast-runs/{old}/manifest.json":
            raise OSError("read failed")
        return get(key)

    store.get = unreadable
    with pytest.raises(StorageGuardError, match=old):
        publish_run(store, cube, tiles, report)
    assert not any(op in ("put", "delete", "list") for op, _ in store.ops)


def test_storage_guard_counts_post_retention_sizes_across_both_pointers():
    store = FakeStore()
    expected = {}
    for pointer, layers in (
        ("latest.json", {"weather": (200, 10_000), "waves": (300, 400)}),
        ("latest-regional.json", {"weather-arome": (500, 600)}),
    ):
        entries = {}
        for layer, (size, prev_size) in layers.items():
            current, previous = f"{layer}-20260712T06Z", f"{layer}-20260712T00Z"
            seed_run(store, current, n_tiles=1, tile_bytes=size)
            seed_run(store, previous, n_tiles=1, tile_bytes=prev_size)
            entries[layer] = {"run_id": current, "previous_run_id": previous}
            expected[current] = size
            if layer != "weather":
                expected[previous] = prev_size
        store.objects[pointer] = json.dumps({"layers": entries}).encode()
    # The publishing layer's old previous is excluded even if damaged; it
    # does not survive retention. Upload overlap remains outside this guard.
    del store.objects["forecast-runs/weather-20260712T00Z/manifest.json"]
    assert check_storage_guard(store, "weather", 100, 2100) == expected
    with pytest.raises(StorageGuardError, match="2100 B > 2099 B"):
        check_storage_guard(store, "weather", 100, 2099)
    assert not any(op in ("put", "delete", "list") for op, _ in store.ops)


def test_manifest_fnv64_matches_stored_objects_and_schema():
    jsonschema = pytest.importorskip("jsonschema")
    cube, tiles, report = make_run()
    store = FakeStore()
    publish_run(store, cube, tiles, report, rng=random.Random(0))

    manifest = json.loads(store.objects[f"forecast-runs/{cube.run_id}/manifest.json"])
    schema = json.loads((SCHEMA_DIR / "forecast-manifest.schema.json").read_text())
    jsonschema.validate(manifest, schema)

    assert manifest["totals"]["tile_count"] == len(tiles)
    for tid, entry in manifest["tiles"].items():
        key = f"forecast-runs/{cube.run_id}/" + manifest["tiling"]["path_template"].format(
            tile_id=tid
        )
        stored = store.objects[key]
        assert entry["bytes"] == len(stored)
        assert entry["fnv64"] == fnv64(stored)  # FNV-1a 64 of the gzipped object as stored
        decoded = decode_tile(gzip.decompress(stored))
        assert decoded.header["tile_id"] == tid

    latest = json.loads(store.objects["latest.json"])
    latest_schema = json.loads((SCHEMA_DIR / "forecast-latest.schema.json").read_text())
    jsonschema.validate(latest, latest_schema)
    assert latest["layers"]["weather"]["cadence_hours"] == 6

    status = json.loads(store.objects["status/weather.json"])
    assert status["tile_count"] == len(tiles)
    assert status["checks_passed"] == report.checks_passed


def test_failed_validation_or_empty_tiles_refused():
    cube, tiles, report = make_run()
    report.failures.append("physical_range[wind_u_kt]: boom")
    store = FakeStore()
    with pytest.raises(PublishError, match="failed validation"):
        publish_run(store, cube, tiles, report)
    report.failures.clear()
    with pytest.raises(PublishError, match="zero tiles"):
        publish_run(store, cube, [], report)
    assert not any(op == "put" for op, _ in store.ops)


class CorruptingStore(FakeStore):
    """Serves corrupted tile bytes on re-download to trip the post-publish check."""

    def get(self, key):
        data = super().get(key)
        if data is not None and key.endswith(".bin.gz"):
            return data[:-1] + bytes([data[-1] ^ 0xFF])
        return data


def test_post_publish_check_failure_blocks_latest_update():
    cube, tiles, report = make_run()
    store = CorruptingStore()
    with pytest.raises(PublishError, match="post-publish"):
        publish_run(store, cube, tiles, report, rng=random.Random(0))
    assert "latest.json" not in store.objects, "latest.json must only follow a verified manifest"


def test_build_manifest_shape():
    cube, tiles, report = make_run()
    manifest = build_manifest(
        cube,
        tiles,
        report,
        published_at="2026-07-13T10:00:00Z",
        validated_at="2026-07-13T10:00:00Z",
    )
    assert manifest["run_id"] == cube.run_id
    assert manifest["horizon_h"] == 3
    assert manifest["tiling"]["path_template"] == "weather/z250/{tile_id}.bin.gz"
    assert manifest["totals"]["bytes"] == sum(len(gz) for _, gz in tiles)


def test_every_published_layer_has_a_cadence_the_schema_accepts():
    """cadence_hours follows each provider since the dispatcher switched on
    (Passage docs/grib-export-plan.md, Phase 5B)."""
    schema = json.loads((SCHEMA_DIR / "forecast-latest.schema.json").read_text())
    entry = schema["properties"]["layers"]["additionalProperties"]["properties"]
    assert entry["cadence_hours"] == {**entry["cadence_hours"], "type": "integer", "minimum": 1}
    layers = set(schema["properties"]["layers"]["propertyNames"]["enum"])
    assert set(CADENCE_HOURS) == layers
    assert CADENCE_HOURS == {
        "weather": 6,
        "waves": 6,
        "ensemble": 6,
        "weather-ecmwf": 12,
        "weather-ecmwf-short": 12,
        "currents": 24,
        "currents-ibi": 24,
    }
