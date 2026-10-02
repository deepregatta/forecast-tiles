"""Stale thresholds follow selected opportunities, including UTC rollovers."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingest.regional_health import freshness
from ingest.sources.openmeteo.registry import AROME, ICON_EU, UKV


def at(hour, day=2):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def entry(product, hour=15):
    cycle = at(hour)
    return {"run_id": f"{product.layer}-{cycle:%Y%m%dT%HZ}", "cycle": f"{cycle:%Y-%m-%dT%H:%MZ}"}


def test_canary_waits_for_two_selected_cycles_and_measured_lag():
    run = entry(AROME)
    assert freshness(AROME, run, at(17, 3), 3 * 3600, canary=True)["status"] == "fresh"
    report = freshness(AROME, run, at(18, 3), 3 * 3600, canary=True)
    assert report["status"] == "stale"
    assert report["next_two_cycles"] == [at(3, 3).isoformat(), at(15, 3).isoformat()]
    assert report["stale_at"] == at(18, 3).isoformat()
    # Full cadence has already missed 21Z and 03Z, while canary still gets 24 h.
    assert freshness(AROME, run, at(6, 3), 3 * 3600, canary=False)["status"] == "stale"
    assert freshness(AROME, run, at(6, 3), 3 * 3600, canary=True)["status"] == "fresh"


@pytest.mark.parametrize("product", [ICON_EU, UKV])
def test_midnight_canary_rollover(product):
    report = freshness(product, entry(product, 12), at(17, 3), 5 * 3600, canary=True)
    assert report["next_two_cycles"] == [at(0, 3).isoformat(), at(12, 3).isoformat()]
    assert report["status"] == "stale"


@pytest.mark.parametrize(
    "run", [None, {}, {"cycle": "invalid"}, entry(AROME, 9) | {"run_id": "wrong"}, entry(AROME, 21)]
)
def test_missing_invalid_and_future_pointers_are_unknown(run):
    assert freshness(AROME, run, at(18), 3600, canary=True)["status"] == "unknown"


@pytest.mark.parametrize("lag", [-1, float("nan"), float("inf")])
def test_invalid_lag_cannot_make_data_look_fresh(lag):
    with pytest.raises(ValueError):
        freshness(AROME, entry(AROME), at(18), lag, canary=True)


def test_operator_check_is_read_only_and_unknown_is_nonzero(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "health_script", Path(__file__).parents[1] / "scripts/regional_health.py"
    )
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    root = tmp_path / "latest.json"
    root.write_text('{"private": "must not be read"}')
    pointer = tmp_path / "latest-regional.json"
    pointer.write_text(json.dumps({"layers": {"weather-arome": entry(AROME)}}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    args = ["weather-arome", "--dir", str(tmp_path), "--canary", "--source-lag-minutes", "180"]
    assert health.main(args + ["--now", at(17, 3).isoformat()]) == 0
    assert health.main(args + ["--now", at(18, 3).isoformat()]) == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert "private" not in capsys.readouterr().out
    pointer.write_text("invalid")
    assert health.main(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
