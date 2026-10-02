import json

import pytest
from test_publish import FakeStore
from test_regional_layout import regional_cube

from ingest.publish import PointerConflictError, PublishError, publish_run
from ingest.regional_control import change_regional_entry
from ingest.tile import build_tiles
from ingest.validate import validate_cube


def seed():
    store = FakeStore()
    for hour in (3, 9):
        cube = regional_cube(hour)
        publish_run(store, cube, build_tiles(cube), validate_cube(cube))
    store.objects["latest.json"] = b'{"layers": {}}'
    return store, cube.run_id


def test_rollback_restores_previous_without_touching_root_or_pruning():
    store, current = seed()
    before = dict(store.objects)
    entry = change_regional_entry(store, "weather-arome", current, restore_previous=True)
    assert entry["run_id"] == "weather-arome-20260713T03Z"
    assert entry["previous_run_id"] is None, "a faulty run is not a fallback"
    assert store.objects["latest.json"] == before["latest.json"]
    assert all(store.objects[k] == v for k, v in before.items() if k != "latest-regional.json")


def test_disable_merges_an_unrelated_concurrent_regional_publication():
    store, current = seed()

    def competitor(s, key):
        doc = json.loads(s.objects[key])
        doc["layers"]["weather-icon-eu"] = {"run_id": "icon-new"}
        s.objects[key] = json.dumps(doc).encode()

    store.before_put.append(competitor)
    change_regional_entry(store, "weather-arome", current)
    assert json.loads(store.objects["latest-regional.json"])["layers"] == {
        "weather-icon-eu": {"run_id": "icon-new"}
    }


def test_disable_refuses_a_newer_same_layer_commit():
    store, current = seed()

    def competitor(s, key):
        doc = json.loads(s.objects[key])
        doc["layers"]["weather-arome"]["run_id"] = "newer"
        s.objects[key] = json.dumps(doc).encode()

    store.before_put.append(competitor)
    with pytest.raises(PointerConflictError, match="current run differs"):
        change_regional_entry(store, "weather-arome", current)
    assert (
        json.loads(store.objects["latest-regional.json"])["layers"]["weather-arome"]["run_id"]
        == "newer"
    )


def test_rollback_refuses_corrupt_previous_and_root_layer():
    store, current = seed()
    key = next(k for k in store.objects if "T03Z/" in k and k.endswith(".bin.gz"))
    store.objects[key] = b"corrupt"
    before = store.objects["latest-regional.json"]
    with pytest.raises(PublishError, match="mismatch"):
        change_regional_entry(store, "weather-arome", current, restore_previous=True)
    assert store.objects["latest-regional.json"] == before
    with pytest.raises(PublishError, match="restricted"):
        change_regional_entry(store, "weather", current)
