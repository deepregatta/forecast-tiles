"""Regional publication layout: 5° tiles beside the 10° default, and the
separate latest-regional.json pointer counted with the root one."""

import json
import gzip
import random
from pathlib import Path

import numpy as np
import pytest
from conftest import make_weather_cube
from test_publish import FakeStore, seed_run

from ingest.publish import (
    PublishError,
    PreconditionFailed,
    StorageGuardError,
    check_storage_guard,
    publish_run,
    referenced_run_ids,
    commit_layer_entry,
)
from ingest.sources.openmeteo import grids, registry
from ingest.tile import build_tiles, tile_index_ranges
from ingest.validate import validate_cube
from tilekit.tiles import tile_id, tile_origin, tiles_for_grid
from tilekit.codec import decode_tile


@pytest.mark.parametrize("tile_deg, count", [(10, 12), (5, 35)])
def test_every_arome_cell_lands_in_exactly_one_tile(tile_deg, count):
    grid = grids.grid_meta(registry.AROME.grid)
    ranges = tile_index_ranges(grid, tile_deg)
    assert len(ranges) == count
    hits = np.zeros((grid.nlat, grid.nlon), dtype=np.int8)
    lats, lons = grid.lats(), grid.lons()
    for lat0, lon0, li, lj in ranges:
        hits[li, lj] += 1
        assert lats[li.start] >= lat0 and lats[li.stop - 1] < lat0 + tile_deg
        assert lons[lj.start] >= lon0 and lons[lj.stop - 1] < lon0 + tile_deg
    assert (hits == 1).all()


def test_rows_on_a_5_degree_line_start_the_tile_above():
    grid = grids.grid_meta(registry.AROME.grid)
    by_origin = {(a, b): (li, lj) for a, b, li, lj in tile_index_ranges(grid, 5)}
    li, lj = by_origin[(45, 5)]
    assert grid.lats()[li.start] == 45.0 and grid.lons()[lj.start] == 5.0
    assert li.stop - li.start == 200 and lj.stop - lj.start == 200


def test_5_degree_tile_ids_name_the_south_west_corner():
    assert tile_origin(47.3, -2.1, 5) == (45, -5)
    assert tile_id(*tile_origin(47.3, -2.1, 5)) == "N45W005"
    assert tile_origin(47.3, -2.1) == (40, -10)
    with pytest.raises(ValueError):
        tiles_for_grid(0, 1, 0, 1, tile_deg=3)


def test_a_5_degree_cube_splits_into_quarter_tiles():
    cube = make_weather_cube()  # 2.5° cells over 40-50N, 10W-10E
    ten = build_tiles(cube, generated_at="x")
    cube.tile_deg = 5
    five = build_tiles(cube, generated_at="x")
    assert [t for t, _ in ten] == ["N40W010", "N40E000"]
    assert [t for t, _ in five] == [
        "N40W010",
        "N40W005",
        "N40E000",
        "N40E005",
        "N45W010",
        "N45W005",
        "N45E000",
        "N45E005",
    ]


# ------------------------------------------------------------- pointers


def regional_cube(hour=3):
    cube = make_weather_cube()
    cube.layer = "weather-arome"
    cube.cycle = cube.cycle.replace(hour=hour)
    cube.tile_deg, cube.path_label = 5, "grid-0p025"
    return cube


def test_a_regional_layer_publishes_to_its_own_pointer_only():
    store = FakeStore()
    cube = regional_cube()
    publish_run(store, cube, build_tiles(cube), validate_cube(cube), rng=random.Random(0))
    assert "latest.json" not in store.objects
    doc = json.loads(store.objects["latest-regional.json"])
    assert doc["layers"]["weather-arome"]["cadence_hours"] == 6
    manifest = json.loads(store.objects[f"forecast-runs/{cube.run_id}/manifest.json"])
    assert manifest["tiling"]["path_template"] == "weather-arome/grid-0p025/{tile_id}.bin.gz"

    with pytest.raises(PublishError, match="belongs in latest-regional.json"):
        publish_run(store, cube, build_tiles(cube), validate_cube(cube), pointer_key="latest.json")
    root = make_weather_cube()
    with pytest.raises(PublishError, match="belongs in latest.json"):
        publish_run(
            store, root, build_tiles(root), validate_cube(root), pointer_key="latest-regional.json"
        )


def test_the_storage_guard_and_references_count_both_pointers():
    store = FakeStore()
    seed_run(store, "weather-20260712T06Z", tile_bytes=4000)
    seed_run(store, "weather-arome-20260712T03Z", tile_bytes=4000)
    for key, layer, run in (
        ("latest.json", "weather", "weather-20260712T06Z"),
        ("latest-regional.json", "weather-arome", "weather-arome-20260712T03Z"),
    ):
        store.objects[key] = json.dumps(
            {"layers": {layer: {"run_id": run, "previous_run_id": None, "cycle": "x"}}}
        ).encode()
    assert check_storage_guard(store, "ensemble", 1000, 20_000) == {
        "weather-20260712T06Z": 8000,
        "weather-arome-20260712T03Z": 8000,
    }
    with pytest.raises(StorageGuardError):
        check_storage_guard(store, "ensemble", 5000, 20_000)
    assert referenced_run_ids(store) == {"weather-20260712T06Z", "weather-arome-20260712T03Z"}


def test_regional_tiles_are_stable_and_inventory_is_only_in_the_manifest():
    cube = regional_cube()
    cube.provenance = {"source_digest": "abc", "source_objects": ["inventory"], "fetched_at": "one"}
    first = build_tiles(cube)
    cube.provenance["fetched_at"] = "two"
    assert build_tiles(cube) == first
    header = decode_tile(gzip.decompress(first[0][1])).header
    assert header["generated_at"] == cube.cycle.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert header["provenance"] == {"source_digest": "abc"}


@pytest.mark.parametrize("complete", [False, True])
def test_regional_retry_never_overwrites_a_partial_or_complete_run(complete):
    cube = regional_cube()
    tiles, report = build_tiles(cube), validate_cube(cube)
    store = FakeStore()
    if complete:
        publish_run(store, cube, tiles, report)
    else:
        tid, data = tiles[0]
        store.objects[f"forecast-runs/{cube.run_id}/weather-arome/grid-0p025/{tid}.bin.gz"] = data
    before = dict(store.objects)
    with pytest.raises(PreconditionFailed):
        publish_run(store, cube, tiles, report)
    assert store.objects == before
    assert not any(op == "delete" for op, _ in store.ops)


def test_direct_pointer_commit_cannot_bypass_layer_ownership():
    store = FakeStore()
    with pytest.raises(PublishError, match="belongs in latest-regional.json"):
        commit_layer_entry(store, "weather-arome", {}, pointer_key="latest.json")
    assert not store.ops


def test_an_existing_manifest_prevents_even_missing_tile_repairs():
    cube = regional_cube()
    store = FakeStore()
    store.objects[f"forecast-runs/{cube.run_id}/manifest.json"] = b"existing"
    with pytest.raises(PreconditionFailed, match="nothing uploaded"):
        publish_run(store, cube, build_tiles(cube), validate_cube(cube))
    assert not any(op == "put" for op, _ in store.ops)


def test_duplicate_catalogue_ownership_aborts_before_upload():
    cube = regional_cube()
    store = FakeStore()
    store.objects["latest.json"] = b'{"layers":{"weather-arome":{}}}'
    with pytest.raises(PublishError, match="duplicate catalogue ownership"):
        publish_run(store, cube, build_tiles(cube), validate_cube(cube))
    assert not any(op == "put" for op, _ in store.ops)


def test_regional_manifest_and_pointer_match_the_coordinated_schemas():
    import jsonschema

    cube = regional_cube()
    store = FakeStore()
    publish_run(store, cube, build_tiles(cube), validate_cube(cube))
    contracts = Path(__file__).parents[1] / "contracts"
    for key, schema_name in (
        (f"forecast-runs/{cube.run_id}/manifest.json", "forecast-manifest.schema.json"),
        ("latest-regional.json", "forecast-latest-regional.schema.json"),
    ):
        jsonschema.validate(
            json.loads(store.objects[key]), json.loads((contracts / schema_name).read_text())
        )
    manifest = json.loads(store.objects[f"forecast-runs/{cube.run_id}/manifest.json"])
    for tid, sizes in manifest["tiles"].items():
        raw = gzip.decompress(
            store.objects[
                f"forecast-runs/{cube.run_id}/"
                + manifest["tiling"]["path_template"].format(tile_id=tid)
            ]
        )
        assert len(raw) == sizes["uncompressed_bytes"]
        assert sum(a.nbytes for a in decode_tile(raw).arrays.values()) == sizes["decoded_bytes"]
