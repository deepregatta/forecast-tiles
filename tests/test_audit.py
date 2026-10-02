"""The run audit: referenced / superseded / incomplete, damage, and its
age-gated deletion of runs nothing names."""

import json
import os
import random
from datetime import datetime, timedelta, timezone

from conftest import make_weather_cube

from ingest.audit import audit_runs, delete_unreferenced
from ingest.publish import DirStore, publish_run
from ingest.tile import build_tiles
from ingest.validate import validate_cube

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def publish(store, hour):
    cube = make_weather_cube()
    cube.cycle = datetime(2026, 7, 13, tzinfo=timezone.utc) + timedelta(hours=hour)
    report = validate_cube(cube)
    publish_run(store, cube, build_tiles(cube), report, rng=random.Random(0))
    return cube.run_id


def age(root, run_id, hours):
    stamp = (NOW - timedelta(hours=hours)).timestamp()
    for path in (root / "forecast-runs" / run_id).rglob("*"):
        os.utime(path, (stamp, stamp))


def test_states_bytes_and_a_clean_bill(tmp_path):
    store = DirStore(tmp_path)
    previous, current = publish(store, 0), publish(store, 6)
    stranded = tmp_path / "forecast-runs/weather-20260713T12Z/weather/z250/N40W010.bin.gz"
    stranded.parent.mkdir(parents=True)
    stranded.write_bytes(b"abc")

    report = audit_runs(store)
    states = {r.run_id: r.state for r in report.runs.values()}
    assert states == {
        previous: "referenced",
        current: "referenced",
        "weather-20260713T12Z": "incomplete",
    }
    assert report.runs[current].referenced_as == ["latest.json weather current"]
    assert report.runs[previous].referenced_as == ["latest.json weather previous"]
    assert report.runs[current].bytes == report.runs[current].manifest_bytes + len(
        (tmp_path / f"forecast-runs/{current}/manifest.json").read_bytes()
    )
    assert report.bytes_by_state()["incomplete"] == 3
    assert not report.damaged
    json.dumps(report.as_dict())


def test_dangling_references_and_missing_tiles_are_damage(tmp_path):
    store = DirStore(tmp_path)
    previous, current = publish(store, 0), publish(store, 6)
    (tmp_path / f"forecast-runs/{previous}/manifest.json").unlink()
    (tmp_path / f"forecast-runs/{current}/weather/z250/N40E000.bin.gz").unlink()

    report = audit_runs(store)
    assert report.damaged
    assert report.dangling == [f"latest.json weather previous {previous} (no manifest)"]
    assert report.runs[current].missing_tiles == ["N40E000"]


def test_deletion_is_age_gated_and_spares_referenced_runs(tmp_path):
    store = DirStore(tmp_path)
    previous, current = publish(store, 0), publish(store, 6)
    for run_id, hours in (("weather-20260712T00Z", 30), ("weather-20260713T12Z", 2)):
        tile = tmp_path / f"forecast-runs/{run_id}/weather/z250/N40W010.bin.gz"
        tile.parent.mkdir(parents=True)
        tile.write_bytes(b"x")
        age(tmp_path, run_id, hours)
    for run_id in (previous, current):
        age(tmp_path, run_id, 100)

    deleted, kept = delete_unreferenced(
        store, audit_runs(store), now=NOW, min_age=timedelta(hours=24)
    )
    assert deleted == ["weather-20260712T00Z"]
    assert kept == ["weather-20260713T12Z (written within 24 h)"]
    assert (tmp_path / f"forecast-runs/{previous}/manifest.json").exists()
    assert (tmp_path / f"forecast-runs/{current}/manifest.json").exists()


def test_a_run_referenced_after_the_audit_is_not_deleted(tmp_path):
    store = DirStore(tmp_path)
    publish(store, 0)
    publish(store, 6)
    tile = tmp_path / "forecast-runs/weather-20260712T00Z/weather/z250/N40W010.bin.gz"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"x")
    age(tmp_path, "weather-20260712T00Z", 48)
    report = audit_runs(store)

    doc = json.loads((tmp_path / "latest.json").read_text())
    doc["layers"]["weather"]["previous_run_id"] = "weather-20260712T00Z"  # an operator rollback
    (tmp_path / "latest.json").write_text(json.dumps(doc))
    deleted, kept = delete_unreferenced(store, report, now=NOW)
    assert deleted == []
    assert kept == ["weather-20260712T00Z (referenced since the audit)"]
    assert tile.exists()
