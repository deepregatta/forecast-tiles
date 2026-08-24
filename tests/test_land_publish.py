"""The routing index's publish protocol: guard first, manifest last, immutable
versions, per-domain retention. Same discipline as a forecast run, and the
tests hold it to the same order."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator
from test_publish import FakeStore

from ingest.land.publish import (
    LATEST_KEY,
    PREFIX,
    STATUS_KEY,
    build_manifest,
    existing_index_ids,
    publish_index,
)
from ingest.publish import CACHE_IMMUTABLE, CACHE_MUTABLE, PublishError, StorageGuardError, fnv64
from landkit.codec import CELL_ORDER, SCHEMA_VERSION, encode_tile, row_bytes

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts"


def make_tile(index_id: str, tile_id: str, lat0: int, lon0: int, n: int = 16) -> bytes:
    mask = np.zeros((n, n), dtype=bool)
    mask[: n // 2, : n // 2] = True
    header = {
        "spec": "TLI1",
        "schema_version": SCHEMA_VERSION,
        "index_id": index_id,
        "domain": index_id.split("-")[0],
        "tile_id": tile_id,
        "lon0": float(lon0),
        "lat0": float(lat0),
        "dlon": 10 / n,
        "dlat": 10 / n,
        "nlon": n,
        "nlat": n,
        "row_bytes": row_bytes(n),
        "cell_order": CELL_ORDER,
        "cells": n * n,
        "blocked_cells": int(mask.sum()),
        "composition": {
            "land_cells": int(mask.sum()),
            "shoal_cells": 0,
            "nodata_cells": 0,
            "shoreline_only_cells": 0,
            "depth_only_cells": 0,
            "blocked_before_buffer": int(mask.sum()),
        },
        "conservatism": {
            "buffer_m": 200.0,
            "dilation_cells": {"lon": 2, "lat": 1},
            "safety_contour_m": 0.0,
            "vertical_datum": "LAT (Lowest Astronomical Tide)",
        },
        "generated_at": "2026-08-24T00:00:00Z",
    }
    return gzip.compress(encode_tile(header, mask), mtime=0)


def make_index(index_id="nweu-20260824T21Z", tiles=(("N40W010", 40, -10), ("N50W010", 50, -10))):
    encoded = [(tile_id, make_tile(index_id, tile_id, lat0, lon0)) for tile_id, lat0, lon0 in tiles]
    manifest = build_manifest(
        index_id=index_id,
        domain=index_id.split("-")[0],
        domain_label="test domain",
        tiles=encoded,
        coverage={
            "tiles": [t[0] for t in tiles],
            "min_lon": -10,
            "max_lon": 0,
            "min_lat": 40,
            "max_lat": 60,
        },
        grid={
            "tile_deg": 10,
            "cells_per_deg": 480,
            "dlon": 1 / 480,
            "dlat": 1 / 480,
            "cell_order": CELL_ORDER,
        },
        conservatism={
            "buffer_m": 200.0,
            "safety_contour_m": 0.0,
            "vertical_datum": "LAT (Lowest Astronomical Tide)",
            "nodata_is_blocked": True,
            "simplification": "rasterisation to the index cell",
            "simplification_tolerance_deg": 1 / 480,
            "rule": "every step adds blocked area; none removes any",
        },
        sources=[
            {
                "name": "GSHHG",
                "product": "shoreline",
                "version": "2.3.7",
                "role": "land",
                "url": "https://example.invalid/gshhg",
                "accessed": "2026-08-24T00:00:00Z",
                "licence": "LGPL-3.0-or-later",
                "attribution": "Wessel & Smith 1996",
            }
        ],
        composition={
            "cells": 512,
            "blocked_cells": 128,
            "blocked_before_buffer": 128,
            "land_cells": 128,
            "nodata_cells": 0,
            "source_agreement": 1.0,
        },
        checks=["tiles round-trip"],
        published_at="2026-08-24T21:00:00Z",
    )
    return manifest, encoded


def test_manifest_and_latest_match_their_published_schemas():
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)

    for key, schema in (
        (f"{PREFIX}/{manifest['index_id']}/manifest.json", "land-index-manifest"),
        (LATEST_KEY, "land-index-latest"),
    ):
        document = json.loads(store.objects[key])
        validator = Draft202012Validator(
            json.loads((SCHEMA_DIR / f"{schema}.schema.json").read_text())
        )
        errors = sorted(validator.iter_errors(document), key=str)
        assert not errors, f"{key}: {[e.message for e in errors]}"


def test_the_manifest_is_written_after_every_tile():
    """Its presence is what marks a version complete, so a boat that reads a
    manifest has, by construction, every tile it names."""
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    puts = [key for op, key in store.ops if op == "put"]
    manifest_at = puts.index(f"{PREFIX}/{manifest['index_id']}/manifest.json")
    for tile_id, _ in tiles:
        assert puts.index(f"{PREFIX}/{manifest['index_id']}/{tile_id}.bin.gz") < manifest_at
    assert puts.index(LATEST_KEY) > manifest_at


def test_tiles_are_immutable_and_the_pointer_is_not():
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    assert store.meta[f"{PREFIX}/{manifest['index_id']}/N40W010.bin.gz"][1] == CACHE_IMMUTABLE
    assert store.meta[LATEST_KEY][1] == CACHE_MUTABLE
    assert store.meta[STATUS_KEY][1] == CACHE_MUTABLE


def test_the_storage_guard_refuses_before_anything_is_uploaded():
    manifest, tiles = make_index()
    store = FakeStore()
    with pytest.raises(StorageGuardError):
        publish_index(store, manifest, tiles, max_bucket_bytes=10)
    assert not [key for op, key in store.ops if op == "put"]


def test_the_storage_guard_counts_forecast_runs_as_well_as_indexes():
    """One bucket, one budget. A guard that only counted its own kind would be
    a guard that lets the bucket fill."""
    manifest, tiles = make_index()
    store = FakeStore()
    store.objects["latest.json"] = json.dumps(
        {"layers": {"weather": {"run_id": "weather-20260824T06Z"}}}
    ).encode()
    store.objects["forecast-runs/weather-20260824T06Z/manifest.json"] = json.dumps(
        {"totals": {"bytes": 900}}
    ).encode()
    with pytest.raises(StorageGuardError, match="weather-20260824T06Z"):
        publish_index(store, manifest, tiles, max_bucket_bytes=1000)


def test_republishing_the_same_version_with_different_content_is_refused():
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    changed = dict(manifest)
    changed["domain_label"] = "something else"
    with pytest.raises(PublishError, match="immutable"):
        publish_index(store, changed, tiles, max_bucket_bytes=10**9)


def test_republishing_identical_content_is_allowed_because_it_changes_nothing():
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    result = publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    assert result.previous_index_id is None


def test_retention_keeps_the_current_and_previous_version_of_this_domain_only():
    store = FakeStore()
    for index_id in ("nweu-20260101T00Z", "nweu-20260201T00Z", "nweu-20260301T00Z"):
        manifest, tiles = make_index(index_id)
        result = publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    assert result.previous_index_id == "nweu-20260201T00Z"
    assert existing_index_ids(store, "nweu") == ["nweu-20260201T00Z", "nweu-20260301T00Z"]

    other, other_tiles = make_index("med-20260101T00Z", (("N30E000", 30, 0),))
    publish_index(store, other, other_tiles, max_bucket_bytes=10**9)
    assert existing_index_ids(store, "nweu") == ["nweu-20260201T00Z", "nweu-20260301T00Z"]
    latest = json.loads(store.objects[LATEST_KEY])
    assert set(latest["domains"]) == {"nweu", "med"}
    assert latest["domains"]["nweu"]["index_id"] == "nweu-20260301T00Z"


def test_the_post_publish_check_catches_a_tile_that_did_not_land_intact():
    manifest, tiles = make_index()
    store = FakeStore()
    original_put = store.put

    def corrupting_put(key, data, **kwargs):
        original_put(key, data[:-1] if key.endswith(".bin.gz") else data, **kwargs)

    store.put = corrupting_put
    with pytest.raises(PublishError, match="bytes/fnv64 mismatch"):
        publish_index(store, manifest, tiles, max_bucket_bytes=10**9)


def test_zero_tiles_is_refused_rather_than_published_as_an_empty_index():
    manifest, _ = make_index()
    with pytest.raises(PublishError, match="zero tiles"):
        publish_index(FakeStore(), manifest, [], max_bucket_bytes=10**9)


def test_every_manifest_tile_entry_matches_the_bytes_that_were_stored():
    manifest, tiles = make_index()
    store = FakeStore()
    publish_index(store, manifest, tiles, max_bucket_bytes=10**9)
    for tile_id, entry in manifest["tiles"].items():
        stored = store.objects[f"{PREFIX}/{manifest['index_id']}/{tile_id}.bin.gz"]
        assert len(stored) == entry["bytes"]
        assert fnv64(stored) == entry["fnv64"]
