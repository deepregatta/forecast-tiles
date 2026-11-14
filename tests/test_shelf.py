"""The staged NW Shelf and Mediterranean current layers (currents-nws, currents-med).

Built exactly as currents-ibi is, offline here with a fake toolbox; and staged:
no publication without --dry-run, no schedule, no dispatcher entry.
"""

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from ingest import cli
from ingest.cube import ForecastCube, GridMeta
from ingest.sources import shelf
from ingest.sources.base import MS_TO_KT, CycleNotAvailableError
from tilekit.codec import decode_tile

ROOT = Path(__file__).resolve().parents[1]
CYCLE = datetime(2026, 11, 13, 0, tzinfo=timezone.utc)
UPDATED = datetime(2026, 11, 13, 10, 7, 41, tzinfo=timezone.utc)


class _Array:
    def __init__(self, values):
        self.values = np.asarray(values)


class _Dataset:
    dims = {}

    def __init__(self, cycle, steps, lats, lons):
        self.times = np.array(
            [np.datetime64(cycle.replace(tzinfo=None) + timedelta(hours=h), "ns") for h in steps]
        )
        shape = (len(steps), len(lats), len(lons))
        self.u = np.full(shape, 1.0, dtype=np.float32)
        self.v = np.full(shape, -0.5, dtype=np.float32)
        self.u[:, 0, 0] = np.nan
        self.v[:, 0, 0] = np.nan
        self.lats, self.lons = lats, lons
        self.closed = False

    def __getitem__(self, name):
        return _Array({"time": self.times, "latitude": self.lats, "longitude": self.lons}[name])

    def sel(self, *, time):
        indices = [int(np.where(self.times == t)[0][0]) for t in np.asarray(time)]
        return {"uo": _Array(self.u[indices]), "vo": _Array(self.v[indices])}

    def close(self):
        self.closed = True


def _grid(spec):
    if spec is shelf.NWS:
        return [46.0, 46.0 + 0.0135135, 46.0 + 2 * 0.0135135], [-5.0, -5.0 + 1 / 33]
    return [38.0, 38.0 + 1 / 24, 38.0 + 2 / 24], [10.0, 10.0 + 1 / 24]


def test_the_products_are_the_catalogue_read_on_2026_11_14():
    assert shelf.NWS.product_id == "NWSHELF_ANALYSISFORECAST_PHY_004_013"
    assert shelf.NWS.default_dataset_id == "cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i"
    assert (shelf.NWS.min_lat, shelf.NWS.max_lat) == (46.0, 62.75)
    assert (shelf.NWS.min_lon, shelf.NWS.max_lon) == (-16.0, 13.0)
    assert shelf.NWS.native_horizon_h == 168
    assert shelf.NWS.temporal_resolution == "hourly instantaneous"
    assert shelf.MED.product_id == "MEDSEA_ANALYSISFORECAST_PHY_006_013"
    assert shelf.MED.default_dataset_id == "cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m"
    assert shelf.MED.native_horizon_h == 239
    assert shelf.MED.temporal_resolution == "hourly mean"
    for spec in (shelf.NWS, shelf.MED):
        assert spec.step_axis == list(range(121))
        assert spec.published_horizon_h <= spec.native_horizon_h


def test_staged_layers_are_not_enabled_for_publication():
    assert shelf.PUBLICATION_ENABLED is False


@pytest.mark.parametrize("layer", shelf.LAYERS)
def test_the_cli_refuses_to_publish_a_staged_layer(layer, monkeypatch, capsys):
    def never(*args, **kwargs):
        raise AssertionError("a staged layer must not reach a store or a provider")

    monkeypatch.setattr(cli, "make_r2_store_from_env", never)
    monkeypatch.setattr(shelf, "resolve", never)
    assert cli.main([layer]) == 1
    assert "staged, not enabled for publication" in capsys.readouterr().out


@pytest.mark.parametrize("spec", [shelf.NWS, shelf.MED], ids=lambda s: s.layer)
def test_build_cube_reads_hourly_surface_currents_in_knots(spec, monkeypatch):
    lats, lons = _grid(spec)
    ds = _Dataset(CYCLE, spec.step_axis, lats, lons)
    opened = {}

    def fake_open(client, s, **selection):
        opened.update(selection)
        return ds

    monkeypatch.setattr(shelf, "_open", fake_open)
    calls = []

    def state(client, dataset_id, **kwargs):
        calls.append((dataset_id, kwargs["name"], kwargs["override_env"]))
        return (UPDATED, None)

    monkeypatch.setattr(shelf, "arco_update_state", state)
    monkeypatch.setitem(__import__("sys").modules, "copernicusmarine", object())

    cube = shelf.build_cube(spec, CYCLE)
    assert cube.layer == spec.layer and cube.model == spec.model
    assert cube.tile_deg == spec.tile_deg
    assert cube.time_axes == {"steps": list(range(121))}
    assert opened == {"start_datetime": CYCLE, "end_datetime": CYCLE + timedelta(hours=120)}
    assert cube.decoded("cur_u_kt")[0, 1, 1] == pytest.approx(MS_TO_KT, abs=0.005)
    assert cube.decoded("cur_v_kt")[0, 1, 1] == pytest.approx(-0.5 * MS_TO_KT, abs=0.005)
    assert np.isnan(cube.decoded("cur_u_kt")[0, 0, 0])
    assert cube.provenance["attribution"] == spec.attribution
    assert cube.provenance["temporal_resolution"] == spec.temporal_resolution
    assert "not a tidal-stream prediction" in cube.provenance["tidal_caveat"]
    assert cube.provenance["provider_updated_at"] == "2026-11-13T10:07:41Z"
    assert calls == [(spec.default_dataset_id, spec.name, spec.dataset_id_env)] * 2
    assert ds.closed


def test_build_cube_refuses_a_store_being_rewritten(monkeypatch):
    monkeypatch.setattr(shelf, "arco_update_state", lambda *a, **k: (UPDATED, CYCLE))
    monkeypatch.setattr(shelf, "_open", lambda *a, **k: pytest.fail("must not read"))
    monkeypatch.setitem(__import__("sys").modules, "copernicusmarine", object())
    with pytest.raises(CycleNotAvailableError, match="NWS ARCO store is being updated"):
        shelf.build_cube(shelf.NWS, CYCLE)


def test_build_cube_refuses_an_update_during_the_read(monkeypatch):
    states = iter([(UPDATED, None), (UPDATED + timedelta(hours=1), None)])
    monkeypatch.setattr(shelf, "arco_update_state", lambda *a, **k: next(states))
    lats, lons = _grid(shelf.MED)
    monkeypatch.setattr(
        shelf, "_open", lambda *a, **k: _Dataset(CYCLE, shelf.MED.step_axis, lats, lons)
    )
    monkeypatch.setitem(__import__("sys").modules, "copernicusmarine", object())
    with pytest.raises(CycleNotAvailableError, match="MED ARCO store changed"):
        shelf.build_cube(shelf.MED, CYCLE)


def test_build_cube_refuses_a_short_bulletin(monkeypatch):
    monkeypatch.setattr(shelf, "arco_update_state", lambda *a, **k: (UPDATED, None))
    lats, lons = _grid(shelf.NWS)
    monkeypatch.setattr(shelf, "_open", lambda *a, **k: _Dataset(CYCLE, range(100), lats, lons))
    monkeypatch.setitem(__import__("sys").modules, "copernicusmarine", object())
    with pytest.raises(RuntimeError, match="NWS missing 21/121"):
        shelf.build_cube(shelf.NWS, CYCLE)


@pytest.mark.parametrize("spec", [shelf.NWS, shelf.MED], ids=lambda s: s.layer)
def test_an_undated_run_takes_the_bulletin_the_stac_item_ends(spec):
    end = CYCLE + timedelta(hours=spec.native_horizon_h)
    doc = {
        "properties": {
            "end_datetime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "admp_updated_data": "2026-11-13T10:07:41Z",
            "admp_updating_start_date": None,
        }
    }
    assert shelf.resolve(spec, fetch=lambda url: json.dumps(doc).encode()) == CYCLE
    doc["properties"]["admp_updating_start_date"] = "2026-11-14T00:00:00Z"
    with pytest.raises(CycleNotAvailableError, match="updating"):
        shelf.resolve(spec, fetch=lambda url: json.dumps(doc).encode())


def test_an_explicit_cycle_waits_for_the_stac_item():
    def stac(end, updated, updating=None):
        doc = {
            "properties": {
                "end_datetime": end,
                "admp_updated_data": updated,
                "admp_updating_start_date": updating,
            }
        }
        return lambda url: json.dumps(doc).encode()

    with pytest.raises(CycleNotAvailableError, match="NWS: data end"):
        shelf.resolve(shelf.NWS, CYCLE, fetch=stac("2026-11-19T23:00:00Z", "2026-11-13T10:07Z"))
    ready = stac("2026-11-20T00:00:00Z", "2026-11-13T10:07:41Z")
    assert shelf.resolve(shelf.NWS, CYCLE, fetch=ready) == CYCLE
    urls = []
    shelf.resolve(
        shelf.MED,
        CYCLE,
        fetch=lambda url: (
            urls.append(url) or stac("2026-11-22T23:00:00Z", "2026-11-13T13:06Z")(url)
        ),
    )
    assert urls == [
        "https://s3.waw3-1.cloudferro.com/mdl-metadata/metadata/MEDSEA_ANALYSISFORECAST_PHY_006_013/"
        "cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m_202511/dataset.stac.json"
    ]


def _cube(spec) -> ForecastCube:
    if spec is shelf.NWS:
        grid = GridMeta(lat0=49.0, lon0=-3.0, dlat=0.0135135, dlon=1 / 33, nlat=6, nlon=4)
    else:
        grid = GridMeta(lat0=38.0, lon0=9.0, dlat=1 / 24, dlon=1 / 24, nlat=4, nlon=4)
    shape = (len(spec.step_axis), grid.nlat, grid.nlon)
    u = np.full(shape, 0.8, dtype=np.float32)
    v = np.full(shape, -0.3, dtype=np.float32)
    u[:, 0, :2] = np.nan
    v[:, 0, :2] = np.nan
    return ForecastCube(
        layer=spec.layer,
        model=spec.model,
        cycle=CYCLE,
        grid=grid,
        time_axes={shelf.AXIS_NAME: list(spec.step_axis)},
        variables=list(shelf.VARS),
        arrays={"cur_u_kt": u, "cur_v_kt": v},
        tile_deg=spec.tile_deg,
        provenance={"source": f"synthetic {spec.name} test cube"},
    )


@pytest.mark.parametrize("spec", [shelf.NWS, shelf.MED], ids=lambda s: s.layer)
def test_a_dry_run_writes_contract_valid_root_layer_artifacts(spec, tmp_path, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    cube = _cube(spec)
    monkeypatch.setattr(shelf, "resolve", lambda s, requested=None: cube.cycle)
    monkeypatch.setattr(shelf, "build_cube", lambda s, cycle: cube)
    assert cli.main([spec.layer, "--dry-run", str(tmp_path)]) == 0

    schema = lambda name: json.loads((ROOT / "contracts" / name).read_text())  # noqa: E731
    run_dir = tmp_path / "forecast-runs" / cube.run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    jsonschema.validate(manifest, schema("forecast-manifest.schema.json"))
    assert manifest["tiling"]["tile_deg"] == spec.tile_deg
    latest = json.loads((tmp_path / "latest.json").read_text())
    jsonschema.validate(latest, schema("forecast-latest.schema.json"))
    assert latest["layers"][spec.layer]["cadence_hours"] == 24
    assert latest["layers"][spec.layer]["run_id"] == cube.run_id
    for tile_id in manifest["tiles"]:
        path = run_dir / manifest["tiling"]["path_template"].format(tile_id=tile_id)
        tile = decode_tile(gzip.decompress(path.read_bytes()))
        jsonschema.validate(tile.header, schema("forecast-tile.schema.json"))


def test_no_workflow_schedules_or_publishes_the_staged_layers():
    workflows = ROOT / ".github" / "workflows"
    staged = (workflows / "ingest-currents-shelf.yml").read_text()
    assert "schedule:" not in staged
    assert "workflow_dispatch:" in staged
    assert "R2_" not in staged, "the staged workflow carries no bucket credentials"
    assert "default: true" in staged.split("dry_run:")[1].split("\n\n")[0]
    for other in workflows.glob("*.yml"):
        if other.name == "ingest-currents-shelf.yml":
            continue
        text = other.read_text()
        for layer in shelf.LAYERS:
            assert layer not in text, f"{other.name} names {layer}"
    for source in (ROOT / "dispatcher" / "src").glob("*.ts"):
        for layer in shelf.LAYERS:
            assert layer not in source.read_text(), f"dispatcher {source.name} names {layer}"
