import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "matrix", Path(__file__).parents[1] / "scripts/openmeteo_matrix.py"
)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_manual_dry_run_and_explicit_canary_activation(monkeypatch):
    configured = matrix.registry.product
    monkeypatch.setattr(
        matrix.registry,
        "product",
        lambda layer: (
            replace(configured(layer), production_enabled=False)
            if layer == "weather-icon-eu"
            else configured(layer)
        ),
    )
    jobs = matrix.job_matrix("workflow_dispatch", "weather-arome", "20261002T03", "90", True, "")
    assert jobs == [
        {
            "layer": "weather-arome",
            "cycle": "20261002T03",
            "wait_minutes": 90,
            "dry_run": True,
            "canary": False,
        }
    ]
    assert matrix.job_matrix("schedule", "", "", "0", False, "") == []
    activated = matrix.job_matrix("schedule", "", "", "0", False, "weather-arome")
    assert len(activated) == 1 and activated[0]["layer"] == "weather-arome"
    assert activated[0]["dry_run"] is False
    assert matrix.job_matrix("schedule", "", "", "0", False, "weather-icon-eu") == []
    with pytest.raises(ValueError, match="gates"):
        matrix.job_matrix("workflow_dispatch", "weather-icon-eu", "", "0", False, "weather-icon-eu")


@pytest.mark.parametrize("cycle,wait", [("20261002T06", "0"), ("20261002T03", "91"), ("", "90")])
def test_dispatch_rejects_wrong_cycles_and_unbounded_wait(cycle, wait):
    with pytest.raises(ValueError):
        matrix.job_matrix("workflow_dispatch", "weather-arome", cycle, wait, True, "")


def test_catchup_and_dispatch_share_job_level_concurrency():
    workflow = (Path(__file__).parents[1] / ".github/workflows/ingest-openmeteo.yml").read_text()
    assert "group: ingest-${{ matrix.layer }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "max-parallel: 1" in workflow


def test_canary_profiles_apply_to_catchup_and_explicit_cycles(monkeypatch):
    monkeypatch.setenv("OPENMETEO_CANARY", "true")
    assert matrix.registry.product("weather-arome").cycles == (3, 15)
    assert matrix.registry.product("weather-icon-eu").cycles == (0, 12)
    assert matrix.registry.product("weather-arome").cadence_hours == 12
    jobs = matrix.job_matrix("workflow_dispatch", "weather-arome", "20261002T03", "0", True, "")
    assert jobs[0]["canary"] is True
    with pytest.raises(ValueError, match="cycle"):
        matrix.job_matrix("workflow_dispatch", "weather-arome", "20261002T09", "0", True, "")
