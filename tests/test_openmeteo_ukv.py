"""UKV primary-source identity, exact cell geometry and vector/mask regressions."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("omfiles")
pytest.importorskip("pyproj")

from ingest.sources.openmeteo import adapter, grids, projected, reader, registry  # noqa: E402
from ingest.sources.openmeteo.reader import ObjectRecord, SourceError  # noqa: E402
from ingest.sources.openmeteo.registry import Grid  # noqa: E402

FIX = Path(__file__).parent / "fixtures/openmeteo/ukv"
CYCLE = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def test_native_identity_covers_full_slices_and_initial_terminal_steps():
    evidence = json.loads((FIX / "identity.json").read_text())
    assert evidence["data_licence"] == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert evidence["gust_kind"] == "instantaneous diagnostic, not max-PT01H"
    rows = evidence["comparisons"]
    assert len(rows) == 7 and all(row["passed"] for row in rows)
    assert all(row["cells"] == 970 * 1042 and row["missing_cells"] == 0 for row in rows)
    assert {row["lead_h"] for row in rows if "gust" in row["bulk_variable"]} == {0, 1, 54}
    assert {row["native_parameter"] for row in rows if "direction" in row["bulk_variable"]} == {
        "wind_direction_on_height_levels"
    }


def test_primary_coordinate_fixture_locates_exact_native_cells():
    """The references came from primary NetCDF axes/CRS, before adapter code.
    A sphere substitution shifts the NW corner by over 4 km and fails here."""
    evidence = json.loads((FIX / "discovery.json").read_text())
    native = registry.UKV_NATIVE
    for point in evidence["reference_coordinates"]:
        lon, lat = point["native_lon_lat"]
        mapping = projected.weights(native, Grid(lat, lon, 40, 1, 1))
        assert mapping.valid[0]
        assert mapping.row[0] + mapping.fy[0] == pytest.approx(point["row"], abs=1e-7)
        assert mapping.col[0] + mapping.fx[0] == pytest.approx(point["column"], abs=1e-7)


def test_cardinal_directions_are_meteorological_from_bearings():
    speed = np.full((1, 1, 5), 10, dtype=np.float32)
    direction = np.array([[[0, 90, 180, 270, 360]]], dtype=np.float32)
    u, v = projected.wind_vectors(speed, direction)
    np.testing.assert_allclose(u, [[[0, -10, 0, 10, 0]]], atol=3e-6)
    np.testing.assert_allclose(v, [[[-10, 0, 10, 0, -10]]], atol=3e-6)


def test_direction_wrap_interpolates_vectors_instead_of_angle_or_speed():
    native = replace(registry.UKV_NATIVE, nlat=2, nlon=2, x0=-1000, y0=-1000)
    target = Grid(54.9, -2.5, 40, 1, 1)  # LAEA centre, halfway between four neighbours
    speed = np.full((1, 2, 2), 10, dtype=np.float32)
    direction = np.array([[[359, 1], [359, 1]]], dtype=np.float32)
    u, v = projected.wind_vectors(speed, direction)
    mapping = projected.weights(native, target)
    assert mapping.remap(u)[0, 0, 0] == pytest.approx(0, abs=2e-6)
    assert mapping.remap(v)[0, 0, 0] == pytest.approx(-9.99847695, abs=2e-6)


def test_geographic_north_is_not_rotated_again_at_oblique_native_corner():
    point = json.loads((FIX / "discovery.json").read_text())["reference_coordinates"][2]
    lon, lat = point["native_lon_lat"]  # NW, far from the projection central meridian
    mapping = projected.weights(registry.UKV_NATIVE, Grid(lat, lon, 40, 1, 1))
    speed = np.broadcast_to(np.float32(10), (1, 970, 1042))
    direction = np.broadcast_to(np.float32(0), speed.shape)
    u, v = projected.wind_vectors(speed, direction)
    assert mapping.remap(u)[0, 0, 0] == pytest.approx(0)
    assert mapping.remap(v)[0, 0, 0] == pytest.approx(-10)


def test_remapping_refuses_missing_neighbours_and_masks_outside_without_extrapolating():
    native = replace(registry.UKV_NATIVE, nlat=2, nlon=2, x0=-1000, y0=-1000)
    values = np.array([[[1, 2], [3, 4]]], dtype=np.float32)
    inside = projected.weights(native, Grid(54.9, -2.5, 40, 1, 1))
    assert inside.remap(values)[0, 0, 0] == pytest.approx(2.5)
    values[0, 0, 1] = np.nan
    assert np.isnan(inside.remap(values)).all()
    outside = projected.weights(native, Grid(55.0, -2.5, 40, 1, 1))
    assert not outside.valid.any() and np.isnan(outside.remap(np.ones((1, 2, 2)))).all()


def test_registered_curved_footprint_matches_weights_and_geometry_changes_fingerprint():
    p = registry.UKV
    mapping = projected.weights(p.source_grid, p.grid)
    np.testing.assert_array_equal(
        grids.load_footprint(p), mapping.valid.reshape(mapping.served_shape)
    )
    assert mapping.valid.sum() == 899361
    assert not mapping.valid.reshape(mapping.served_shape)[0, 0]
    assert projected.fingerprint(replace(p.source_grid, x0=p.source_grid.x0 + 2000), p.grid) != (
        mapping.fingerprint
    )


def crop_product(tmp_path, monkeypatch):
    native = replace(registry.UKV_NATIVE, x0=-124000, y0=-74000, nlat=9, nlon=9)
    served = Grid(54.275, -4.3, 40, 3, 3)
    mapping = projected.weights(native, served)
    sha = grids.save_footprint("crop.v1", mapping.valid.reshape(mapping.served_shape), {}, tmp_path)
    monkeypatch.setattr(grids, "FOOTPRINT_DIR", tmp_path)
    return replace(
        registry.UKV, source_grid=native, grid=served, footprint="crop.v1", footprint_sha256=sha
    )


def decode_crop(p, role):
    return reader.decode(
        FIX / (p.files[role] + ".om"),
        grid=p.source_grid,
        unit=p.direction_unit if role == "direction" else p.source_unit,
        reference=CYCLE,
    )


def test_real_bulk_crop_decodes_remaps_and_preserves_initial_instantaneous_gust(
    tmp_path, monkeypatch
):
    p = crop_product(tmp_path, monkeypatch)
    fields = {role: decode_crop(p, role) for role in p.files}
    assert fields["speed"].values.shape == (55, 9, 9)
    evidence = json.loads((FIX / "identity.json").read_text())
    for role in p.files:
        row = next(
            r
            for r in evidence["comparisons"]
            if r["bulk_variable"] == p.files[role] and r["lead_h"] == 0
        )
        point = next(pt for pt in row["points"] if pt["row"] == 485)
        assert fields[role].values[0, 4, 4] == pytest.approx(point["bulk"], abs=1e-5)
    source = adapter.SourceRun(ObjectRecord("meta", '"meta"', 1), [])
    cube = adapter.to_cube(p, CYCLE, fields, source=source)
    assert np.isfinite(cube.arrays["gust_kt"][0]).all()
    assert cube.var("gust_kt").statistic is None
    assert "instantaneous" in cube.provenance["gust"]
    assert cube.provenance["remapping"]["extrapolation"] is False
    assert adapter.validate(p, cube).ok
    assert cube.time_axes["hourly"] == list(range(55))


def test_projected_decoder_refuses_crs_unit_and_reference_changes(tmp_path, monkeypatch):
    p = crop_product(tmp_path, monkeypatch)
    path = FIX / "wind_direction_10m.om"
    with pytest.raises(SourceError, match="projected CRS"):
        reader.decode(
            path, grid=replace(p.source_grid, bulk_wkt_sha256="wrong"), unit="°", reference=CYCLE
        )
    with pytest.raises(SourceError, match="unit"):
        reader.decode(path, grid=p.source_grid, unit="m/s", reference=CYCLE)
    with pytest.raises(SourceError, match="run"):
        reader.decode(path, grid=p.source_grid, unit="°", reference=CYCLE.replace(hour=0))


def test_native_missing_patch_is_refused_before_remap(tmp_path, monkeypatch):
    p = crop_product(tmp_path, monkeypatch)
    fields = {role: decode_crop(p, role) for role in p.files}
    fields["speed"].values[0, :3] = np.nan
    source = adapter.SourceRun(ObjectRecord("meta", '"meta"', 1), [])
    with pytest.raises(SourceError, match="native hole"):
        adapter.to_cube(p, CYCLE, fields, source=source)


def test_ukv_reduced_canary_cadence(monkeypatch):
    assert registry.product("weather-ukv").cycles == (0, 6, 12, 18)
    monkeypatch.setenv("OPENMETEO_CANARY", "true")
    assert registry.product("weather-ukv").cycles == (0, 12)
