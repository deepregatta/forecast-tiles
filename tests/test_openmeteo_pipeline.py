"""Open-Meteo regional layers from real cropped source files to PFT1 tiles,
offline (docs/open-meteo-bulk-implementation-plan.md, Phases 1 and 2).

tests/fixtures/openmeteo/ holds small crops of real runs written by
`scripts/probe_openmeteo.py fixture` (see each fixture.json for the source
objects and attribution): AROME at the edge of its footprint and over Mont
Blanc, and ICON-EU over Norway with its full 93-step axis."""

import gzip
import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

omfiles = pytest.importorskip("omfiles")

from ingest import cli  # noqa: E402
from ingest.cube import ForecastCube, GridMeta, VariableSpec  # noqa: E402
from ingest.sources.base import MS_TO_KT  # noqa: E402
from ingest.sources.openmeteo import adapter, catalog, grids, reader, registry  # noqa: E402
from ingest.sources.openmeteo.catalog import RunMeta  # noqa: E402
from ingest.sources.openmeteo.reader import ObjectRecord, SourceError  # noqa: E402
from ingest.sources.openmeteo.registry import GustWindows, Grid  # noqa: E402
from ingest.validate import validate_cube  # noqa: E402
from tilekit.codec import decode_tile  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "openmeteo"
VERIFIED_1H = GustWindows(segments=((1, 51, 1),), verified=True, evidence="test")


def fixture_info(name: str) -> dict:
    return json.loads((FIX / name / "fixture.json").read_text())


def crop_product(name: str, base=registry.AROME, **changes):
    info = fixture_info(name)
    rows, cols = info["crop"]["rows"], info["crop"]["cols"]
    grid = Grid(
        lat0=info["crop"]["lat0"],
        lon0=info["crop"]["lon0"],
        cells_per_degree=base.grid.cells_per_degree,
        nlat=rows[1] - rows[0],
        nlon=cols[1] - cols[0],
    )
    fields = {"grid": grid, "footprint": None, "footprint_sha256": None} | changes
    return replace(base, **fields)


def with_footprint(name: str, product, directory: Path, monkeypatch):
    """The crop's own footprint (its static terrain mask), registered like the
    real one: written once, pinned by hash, loaded by name."""
    valid = ~np.isnan(np.load(FIX / name / "HSURF.npy"))
    sha = grids.save_footprint(f"{name}.v1", valid, {}, directory=directory)
    monkeypatch.setattr(grids, "FOOTPRINT_DIR", directory)
    return replace(product, footprint=f"{name}.v1", footprint_sha256=sha), valid


def cycle_of(name: str) -> datetime:
    return datetime.strptime(fixture_info(name)["cycle"], "%Y-%m-%dT%HZ").replace(
        tzinfo=timezone.utc
    )


def decode(name: str, role: str, product):
    return reader.decode(
        FIX / name / f"{product.files[role]}.om",
        grid=product.grid,
        unit="m/s",
        reference=cycle_of(name),
    )


def raw(name: str, role: str, product) -> np.ndarray:
    r = omfiles.OmFileReader(str(FIX / name / f"{product.files[role]}.om"))
    try:
        return r.read_array((slice(None),) * 3)
    finally:
        r.close()


SOURCE = adapter.SourceRun(meta=ObjectRecord("m", '"m"', 1), files=[])


def cube_from_fixture(name, product):
    roles = ["u", "v"] + (["gust"] if adapter.include_gust(product) else [])
    fields = {role: decode(name, role, product) for role in roles}
    return adapter.to_cube(product, cycle_of(name), fields, source=SOURCE)


# ------------------------------------------------------------- decoding


def test_decoding_transposes_lat_lon_time_to_time_lat_lon():
    p = crop_product("arome-edge")
    d = decode("arome-edge", "u", p)
    source = raw("arome-edge", "u", p)  # [lat, lon, time] as Open-Meteo stores it
    assert source.shape == (13, 17, 52) and d.values.shape == (52, 13, 17)
    np.testing.assert_array_equal(d.values, np.moveaxis(source, 2, 0))
    assert d.times[0] == cycle_of("arome-edge") and len(d.times) == 52


def test_rows_ascend_from_the_south_at_a_known_point():
    """Mont Blanc (45.833N 6.865E, 4808 m) is the highest cell of the Alps
    crop, at the latitude/longitude the registered ascending grid gives that
    cell. The crop is source rows 323-343 counted from the first row; were the
    files north-first, those rows would be near 47.1N in central France (the
    2026-10-02 probe found 1250 m there)."""
    p = crop_product("arome-alps")
    assert fixture_info("arome-alps")["crop"]["rows"] == [323, 344]
    terrain = np.load(FIX / "arome-alps" / "HSURF.npy")
    i, j = np.unravel_index(np.nanargmax(terrain), terrain.shape)
    lat, lon = grids.grid_meta(p.grid).lats()[i], grids.grid_meta(p.grid).lons()[j]
    assert abs(lat - 45.8326) <= p.grid.step and abs(lon - 6.8652) <= p.grid.step
    assert terrain[i, j] > 3500


@pytest.mark.parametrize(
    "change, match",
    [
        ({"unit": "km/h"}, "unit 'm/s', expected 'km/h'"),
        ({"reference": datetime(2026, 10, 2, 9, tzinfo=timezone.utc)}, "run 2026-10-02T03Z"),
    ],
)
def test_decoding_refuses_a_file_that_is_not_the_registered_one(change, match):
    p = crop_product("arome-edge")
    kwargs = {"grid": p.grid, "unit": "m/s", "reference": cycle_of("arome-edge")} | change
    with pytest.raises(SourceError, match=match):
        reader.decode(FIX / "arome-edge" / "wind_u_component_10m.om", **kwargs)


def test_changed_geometry_fails_clearly():
    p = crop_product("arome-edge")
    shifted = replace(p.grid, lat0=p.grid.lat0 + p.grid.step)  # one row north
    with pytest.raises(SourceError, match="BBOX"):
        reader.decode(
            FIX / "arome-edge" / "wind_u_component_10m.om",
            grid=shifted,
            unit="m/s",
            reference=cycle_of("arome-edge"),
        )
    wider = replace(p.grid, nlon=p.grid.nlon + 1)
    with pytest.raises(SourceError, match="shape"):
        reader.decode(
            FIX / "arome-edge" / "wind_u_component_10m.om",
            grid=wider,
            unit="m/s",
            reference=cycle_of("arome-edge"),
        )


# ------------------------------------------------------------- cube


def test_gust_is_aligned_by_timestamp_with_a_missing_zero_hour():
    p = crop_product("arome-alps", gust_windows=VERIFIED_1H)
    cube = cube_from_fixture("arome-alps", p)
    gust = cube.arrays["gust_kt"]
    source = raw("arome-alps", "gust", p)  # 51 steps, +1..+51 h
    assert source.shape[2] == 51
    assert np.isnan(gust[0]).all(), "no maximum ends at +0 h"
    np.testing.assert_allclose(gust[1:], np.moveaxis(source, 2, 0) * MS_TO_KT, rtol=1e-6)
    u = raw("arome-alps", "u", p)
    np.testing.assert_allclose(cube.arrays["wind_u_kt"], np.moveaxis(u, 2, 0) * MS_TO_KT, rtol=1e-6)
    spec = cube.var("gust_kt")
    assert spec.statistic.window_h == (None,) + (1,) * 51
    assert cube.time_axes == {"hourly": list(range(52))}
    assert cube.provenance["capabilities"] == ["wind", "gust"]
    report = adapter.validate(p, cube)
    assert report.ok, report.summary()
    assert "gust_ge_wind[gust_kt]" in report.checks_passed


def test_an_unverified_gust_window_gives_a_wind_only_run():
    p = crop_product("arome-alps")
    assert not p.gust_windows.verified
    cube = cube_from_fixture("arome-alps", p)
    assert [v.name for v in cube.variables] == ["wind_u_kt", "wind_v_kt"]
    assert cube.provenance["capabilities"] == ["wind"]
    assert "unverified" in cube.provenance["gust"]
    assert adapter.validate(p, cube).ok


def test_a_missing_wind_step_aborts():
    p = crop_product("arome-alps")
    u = decode("arome-alps", "u", p)
    del u.times[30]
    u.values = np.delete(u.values, 30, axis=0)
    fields = {"u": u, "v": decode("arome-alps", "v", p)}
    with pytest.raises(SourceError, match="wind_u_component_10m: no data at"):
        adapter.to_cube(p, cycle_of("arome-alps"), fields, source=SOURCE)


def test_a_missing_gust_step_after_its_first_lead_aborts():
    p = crop_product("arome-alps", gust_windows=VERIFIED_1H)
    g = decode("arome-alps", "gust", p)
    del g.times[5]
    g.values = np.delete(g.values, 5, axis=0)
    fields = {"u": decode("arome-alps", "u", p), "v": decode("arome-alps", "v", p), "gust": g}
    with pytest.raises(SourceError, match="wind_gusts_10m: no data at"):
        adapter.to_cube(p, cycle_of("arome-alps"), fields, source=SOURCE)


# ------------------------------------------------------------- masks


def test_the_expected_outline_is_accepted_and_kept_missing(tmp_path, monkeypatch):
    p, valid = with_footprint("arome-edge", crop_product("arome-edge"), tmp_path, monkeypatch)
    assert 0 < (~valid).sum() < valid.size
    cube = cube_from_fixture("arome-edge", p)
    report = adapter.validate(p, cube)
    assert report.ok, report.summary()
    assert "interior_missing[wind_u_kt]" in report.checks_passed
    assert "exterior_masked[wind_u_kt]" in report.checks_passed
    assert np.isnan(cube.arrays["wind_u_kt"][:, ~valid]).all()
    assert cube.provenance["mask"]["footprint"] == "arome-edge.v1"
    # the global 5 % rule would have refused this valid run
    assert not validate_cube(cube).ok


def test_an_interior_hole_fails(tmp_path, monkeypatch):
    p, valid = with_footprint("arome-edge", crop_product("arome-edge"), tmp_path, monkeypatch)
    cube = cube_from_fixture("arome-edge", p)
    i, j = np.argwhere(valid)[0]
    cube.arrays["wind_v_kt"][17, i, j] = np.nan  # 1 of 134 interior cells: 0.75 %
    report = adapter.validate(p, cube)
    assert not report.ok
    assert any(
        f.startswith("interior_missing[wind_v_kt]") and "+17 h" in f for f in report.failures
    )


def test_an_all_missing_required_step_fails(tmp_path, monkeypatch):
    p, _ = with_footprint("arome-edge", crop_product("arome-edge"), tmp_path, monkeypatch)
    cube = cube_from_fixture("arome-edge", p)
    cube.arrays["wind_u_kt"][40] = np.nan
    report = adapter.validate(p, cube)
    assert any("interior_missing[wind_u_kt]" in f and "+40 h 100.00%" in f for f in report.failures)


def test_the_zero_hour_gust_gap_is_allowed_only_there(tmp_path, monkeypatch):
    p, _ = with_footprint(
        "arome-edge", crop_product("arome-edge", gust_windows=VERIFIED_1H), tmp_path, monkeypatch
    )
    cube = cube_from_fixture("arome-edge", p)
    assert adapter.validate(p, cube).ok
    cube.arrays["gust_kt"][1] = np.nan
    report = adapter.validate(p, cube)
    assert any("interior_missing[gust_kt]" in f and "+1 h" in f for f in report.failures)


def test_a_changed_footprint_is_refused(tmp_path, monkeypatch):
    p = crop_product("arome-alps")  # every cell valid in this crop
    valid = np.ones((p.grid.nlat, p.grid.nlon), dtype=bool)
    valid[:, -3:] = False  # pretend the registered outline stops three columns short
    sha = grids.save_footprint("narrow.v1", valid, {}, directory=tmp_path)
    monkeypatch.setattr(grids, "FOOTPRINT_DIR", tmp_path)
    p = replace(p, footprint="narrow.v1", footprint_sha256=sha)
    with pytest.raises(SourceError, match="footprint changed"):
        cube_from_fixture("arome-alps", p)


def test_mismatched_wind_and_gust_axes_are_reported_not_raised():
    grid = GridMeta(lat0=40, lon0=-10, dlat=1, dlon=1, nlat=2, nlon=2)
    cube = ForecastCube(
        layer="weather",
        model="m",
        cycle=datetime(2026, 10, 2, tzinfo=timezone.utc),
        grid=grid,
        time_axes={"hourly": [0, 1, 2], "gust": [1, 2]},
        variables=[
            VariableSpec("wind_u_kt", "hourly", "i16", 0.01),
            VariableSpec("wind_v_kt", "hourly", "i16", 0.01),
            VariableSpec("gust_kt", "gust", "i16", 0.1),
        ],
        arrays={
            "wind_u_kt": np.ones((3, 2, 2), np.float32),
            "wind_v_kt": np.ones((3, 2, 2), np.float32),
            "gust_kt": np.full((2, 2, 2), 5, np.float32),
        },
    )
    report = validate_cube(cube)
    assert any(
        "gust_ge_wind[gust_kt]: wind and gust are on different axes" in f for f in report.failures
    )


# ------------------------------------------------------------- ICON-EU


def test_icon_eu_steps_switch_to_three_hourly_after_78_h():
    p = crop_product(
        "icon-eu-norway",
        base=registry.ICON_EU,
        gust_windows=replace(registry.ICON_EU.gust_windows, verified=True),
    )
    cube = cube_from_fixture("icon-eu-norway", p)
    axis = cube.time_axes["steps"]
    assert len(axis) == 93 and axis[77:81] == [77, 78, 81, 84]
    gust = cube.arrays["gust_kt"]
    assert np.isnan(gust[0]).all() and not np.isnan(gust[1:]).any()
    source = raw("icon-eu-norway", "gust", p)
    np.testing.assert_allclose(gust[79], source[..., 78] * MS_TO_KT, rtol=1e-6)  # +81 h
    windows = cube.var("gust_kt").statistic.window_h
    assert windows[0] is None and windows[78] == 1 and windows[79] == 3
    report = adapter.validate(p, cube)
    assert report.ok, report.summary()
    assert "interior_missing[gust_kt]" in report.checks_passed


def test_icon_eu_with_any_missing_cell_beyond_the_limit_fails():
    p = crop_product("icon-eu-norway", base=registry.ICON_EU)
    cube = cube_from_fixture("icon-eu-norway", p)
    cube.arrays["wind_u_kt"][90, :2, :2] = np.nan  # 4 of 441 cells at +114 h: 0.9 %
    report = adapter.validate(p, cube)
    assert any("interior_missing[wind_u_kt]" in f and "+114 h" in f for f in report.failures)


# ------------------------------------------------------------- CLI dry run


class FakeBucket:
    """Serves a fixture directory as if it were the run's data_run/ prefix."""

    def __init__(self, name: str):
        self.name = name
        self.info = fixture_info(name)
        self.downloads: list[str] = []
        self.rechecks = 0

    def fetch_meta(self, product, cycle, **kwargs):
        if cycle != cycle_of(self.name):
            from ingest.sources.base import CycleNotAvailableError

            raise CycleNotAvailableError("not in the fixture")
        d = decode(self.name, "u", product)
        return RunMeta(
            cycle=cycle,
            record=ObjectRecord(catalog.run_prefix(product, cycle) + "meta.json", '"meta"', 1),
            reference_time=cycle,
            valid_times=tuple(d.times),
            variables=frozenset(product.files.values()),
            created_at="2026-10-02T05:47:40Z",
        )

    def download(self, key, dest, **kwargs):
        name = key.rsplit("/", 1)[1]
        self.downloads.append(name)
        shutil.copy(FIX / self.name / name, dest)
        rec = self.info["files"][name]
        return ObjectRecord(key, rec["etag"], (FIX / self.name / name).stat().st_size)

    def recheck(self, meta, files, **kwargs):
        self.rechecks += 1


def install(monkeypatch, tmp_path, name, product):
    bucket = FakeBucket(name)
    monkeypatch.setattr(catalog, "fetch_meta", bucket.fetch_meta)
    monkeypatch.setattr(reader, "download", bucket.download)
    monkeypatch.setattr(catalog, "recheck", bucket.recheck)
    monkeypatch.setitem(registry.PRODUCTS, product.layer, product)
    return bucket


def test_cli_dry_run_writes_a_regional_run_beside_the_root_catalogue(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    p, _ = with_footprint("arome-edge", crop_product("arome-edge"), tmp_path / "fp", monkeypatch)
    bucket = install(monkeypatch, tmp_path, "arome-edge", p)

    rc = cli.main(["weather-arome", "--cycle", "20261002T03", "--dry-run", str(out)])
    assert rc == 0, capsys.readouterr().out
    assert bucket.downloads == ["wind_u_component_10m.om", "wind_v_component_10m.om"]
    assert bucket.rechecks == 1, "source identity is rechecked just before publication"
    assert not (out / "latest.json").exists(), "a regional run never touches the root catalogue"
    regional = json.loads((out / "latest-regional.json").read_text())
    entry = regional["layers"]["weather-arome"]
    assert entry["run_id"] == "weather-arome-20261002T03Z" and entry["cadence_hours"] == 6

    run = out / "forecast-runs" / entry["run_id"]
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["tiling"] == {
        "tile_deg": 5,
        "path_template": "weather-arome/grid-0p025/{tile_id}.bin.gz",
    }
    assert manifest["provenance"]["capabilities"] == ["wind"]
    assert manifest["provenance"]["attribution"].startswith("Météo-France AROME")
    assert [v["name"] for v in manifest["variables"]] == ["wind_u_kt", "wind_v_kt"]
    (tile_id,) = manifest["tiles"]
    assert tile_id == "N35W010"
    tile = decode_tile(
        gzip.decompress(
            (run / manifest["tiling"]["path_template"].format(tile_id=tile_id)).read_bytes()
        )
    )
    assert tile.header["nlat"] == 13 and tile.header["nlon"] == 17
    assert tile.header["lat0"] == pytest.approx(37.5)
    assert len(tile.header["time_axes"]["hourly"]["offsets_h"]) == 52

    capsys.readouterr()
    assert cli.main(["weather-arome", "--cycle", "20261002T03", "--dry-run", str(out)]) == 0
    assert "already published" in capsys.readouterr().out
    assert len(bucket.downloads) == 2, "the second run downloads nothing"


def test_cli_publishes_gust_once_its_window_is_verified(tmp_path, monkeypatch):
    out = tmp_path / "out"
    p = crop_product("arome-alps", gust_windows=VERIFIED_1H)
    install(monkeypatch, tmp_path, "arome-alps", p)
    assert cli.main(["weather-arome", "--cycle", "20261002T03", "--dry-run", str(out)]) == 0
    run = out / "forecast-runs" / "weather-arome-20261002T03Z"
    manifest = json.loads((run / "manifest.json").read_text())
    gust = next(v for v in manifest["variables"] if v["name"] == "gust_kt")
    assert gust["statistic"] == {"kind": "max", "window_h": [None] + [1] * 51}
    assert "gust_ge_wind[gust_kt]" in manifest["validation"]["checks_passed"]


def test_cli_refuses_r2_for_a_regional_layer_until_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("R2_BUCKET", "unused")
    assert cli.main(["weather-arome", "--cycle", "20261002T03"]) == 1
    assert "not enabled for publication yet" in capsys.readouterr().out


def test_cli_rejects_an_unregistered_cycle_hour(tmp_path, monkeypatch, capsys):
    p = crop_product("arome-edge")
    install(monkeypatch, tmp_path, "arome-edge", p)
    from ingest.sources.openmeteo import catalog as real_catalog

    monkeypatch.setattr(catalog, "resolve", real_catalog.resolve)
    rc = cli.main(["weather-arome", "--cycle", "20261002T00", "--dry-run", str(tmp_path / "o")])
    assert rc == 1
    assert "publishes (3, 9, 15, 21) UTC cycles, not 00Z" in capsys.readouterr().out


def test_cli_icon_eu_dry_run(tmp_path, monkeypatch):
    out = tmp_path / "out"
    p = crop_product("icon-eu-norway", base=registry.ICON_EU)
    install(monkeypatch, tmp_path, "icon-eu-norway", p)
    assert cli.main(["weather-icon-eu", "--cycle", "20261002T06", "--dry-run", str(out)]) == 0
    manifest = json.loads(
        (out / "forecast-runs" / "weather-icon-eu-20261002T06Z" / "manifest.json").read_text()
    )
    assert manifest["tiling"]["tile_deg"] == 10
    assert manifest["tiling"]["path_template"] == "weather-icon-eu/grid-0p0625/{tile_id}.bin.gz"
    assert manifest["horizon_h"] == 120
    assert len(manifest["time_axes"]["steps"]["offsets_h"]) == 93
