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
    PublishError,
    StorageGuardError,
    build_manifest,
    fnv64,
    publish_run,
    z_res,
)
from ingest.tile import build_tiles
from ingest.validate import validate_cube
from tilekit.codec import decode_tile

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts"


class FakeStore:
    """Dict-backed in-memory object store recording operation order."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, tuple[str, str]] = {}
        self.ops: list[tuple[str, str]] = []

    def put(self, key, data, *, content_type, cache_control):
        self.ops.append(("put", key))
        self.objects[key] = data
        self.meta[key] = (content_type, cache_control)

    def get(self, key):
        self.ops.append(("get", key))
        return self.objects.get(key)

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
