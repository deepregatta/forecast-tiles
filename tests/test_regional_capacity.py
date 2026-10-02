from dataclasses import replace

import pytest

from ingest.capacity import RegionalAllocation, check_regional_capacity
from ingest.publish import DirStore, StorageGuardError
from ingest.sources.openmeteo import registry


def test_capacity_counts_all_objects_and_reserves_simultaneous_uploads(tmp_path, monkeypatch):
    monkeypatch.setitem(
        registry.PRODUCTS, "weather-arome", replace(registry.AROME, max_run_bytes=100)
    )
    store = DirStore(tmp_path)
    for key in ("land-index/data", "forecast-runs/abandoned/tile", "other-metadata"):
        store.put(key, b"x" * 200, content_type="x", cache_control="x")
    allocation = RegionalAllocation(500, 50, ("weather-arome",))
    result = check_regional_capacity(store, 100, 1500, allocation)
    assert result == {"physical_bytes": 600, "other_bytes": 600, "reserved_peak_bytes": 1450}
    with pytest.raises(StorageGuardError, match="reserved peak"):
        check_regional_capacity(store, 100, 1449, allocation)
    with pytest.raises(StorageGuardError, match="physical plus upload"):
        check_regional_capacity(store, 900, 1500, allocation)


def test_admission_fails_closed_on_unknown_inventory_or_dangling_reference(tmp_path):
    store = DirStore(tmp_path)
    store.put(
        "latest.json",
        b'{"layers":{"weather":{"run_id":"missing"}}}',
        content_type="x",
        cache_control="x",
    )
    with pytest.raises(StorageGuardError, match="damaged"):
        check_regional_capacity(
            store, 1, 8_000_000_000, RegionalAllocation(1, 1, ("weather-arome",))
        )


def test_live_admission_requires_explicit_capacity_and_layer_allowlist(monkeypatch):
    for key in (
        "REGIONAL_EXISTING_PEAK_BYTES",
        "REGIONAL_HEADROOM_BYTES",
        "REGIONAL_ENABLED_LAYERS",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(StorageGuardError, match="unknown"):
        RegionalAllocation.from_env("weather-arome")
    monkeypatch.setenv("REGIONAL_EXISTING_PEAK_BYTES", "6000000000")
    monkeypatch.setenv("REGIONAL_HEADROOM_BYTES", "250000000")
    monkeypatch.setenv("REGIONAL_ENABLED_LAYERS", "weather-arome,weather-icon-eu")
    assert RegionalAllocation.from_env("weather-arome").layers == (
        "weather-arome",
        "weather-icon-eu",
    )
    monkeypatch.setenv("REGIONAL_ENABLED_LAYERS", "weather-arome,unknown")
    with pytest.raises(StorageGuardError, match="unknown enabled"):
        RegionalAllocation.from_env("weather-arome")
