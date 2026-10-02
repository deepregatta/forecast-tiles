"""Regional publication layout: 5° tiles beside the 10° default, and the
separate latest-regional.json pointer counted with the root one."""

import json
import random

import numpy as np
import pytest
from conftest import make_weather_cube
from test_publish import FakeStore, seed_run

from ingest.publish import (
    PublishError,
    StorageGuardError,
    check_storage_guard,
    publish_run,
    referenced_run_ids,
)
from ingest.sources.openmeteo import grids, registry
from ingest.tile import build_tiles, tile_index_ranges
from ingest.validate import validate_cube
from tilekit.tiles import tile_id, tile_origin, tiles_for_grid


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
