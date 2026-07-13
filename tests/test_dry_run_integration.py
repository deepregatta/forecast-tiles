"""End-to-end offline dry run: synthetic cube -> validate -> tiles -> DirStore
publish -> decode what was written and validate every JSON artifact against
the vendored contract schemas (contracts/, copied from the passage repo)."""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from conftest import make_weather_cube

from ingest import cli
from ingest.publish import DirStore, fnv64, publish_run
from ingest.tile import build_tiles
from ingest.validate import validate_cube
from tilekit.codec import decode_tile

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def test_dry_run_layout_decodes_and_validates(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    cube = make_weather_cube()
    report = validate_cube(cube)
    assert report.ok, report.summary()
    tiles = build_tiles(cube, generated_at="2026-07-13T10:00:00Z")
    assert len(tiles) == 2

    store = DirStore(tmp_path)
    result = publish_run(store, cube, tiles, report)
    run_dir = tmp_path / "forecast-runs" / result.run_id

    manifest = json.loads((run_dir / "manifest.json").read_text())
    jsonschema.validate(manifest, load_schema("forecast-manifest.schema.json"))
    latest = json.loads((tmp_path / "latest.json").read_text())
    jsonschema.validate(latest, load_schema("forecast-latest.schema.json"))
    assert latest["layers"]["weather"]["run_id"] == result.run_id
    status = json.loads((tmp_path / "status" / "weather.json").read_text())
    assert status["tile_count"] == 2

    tile_schema = load_schema("forecast-tile.schema.json")
    for tid, entry in manifest["tiles"].items():
        path = run_dir / manifest["tiling"]["path_template"].format(tile_id=tid)
        gz = path.read_bytes()
        assert len(gz) == entry["bytes"] and fnv64(gz) == entry["fnv64"]
        tile = decode_tile(gzip.decompress(gz))
        jsonschema.validate(tile.header, tile_schema)
        cols = slice(0, 4) if tid == "N40W010" else slice(4, 8)
        for name in tile.arrays:
            expected = cube.arrays[name][..., :, cols]
            mask = ~np.isnan(expected)
            scale = cube.var(name).scale
            assert np.array_equal(np.isnan(tile.arrays[name]), np.isnan(expected))
            assert np.max(np.abs(tile.arrays[name][mask] - expected[mask])) <= scale / 2 + 1e-6


def test_dry_run_retention_across_three_cycles(tmp_path):
    store = DirStore(tmp_path)
    runs = []
    for day in (11, 12, 13):
        cube = make_weather_cube()
        cube.cycle = datetime(2026, 7, day, 6, tzinfo=timezone.utc)
        report = validate_cube(cube)
        tiles = build_tiles(cube, generated_at="2026-07-13T10:00:00Z")
        result = publish_run(store, cube, tiles, report)
        runs.append(result.run_id)

    run_root = tmp_path / "forecast-runs"
    assert not (run_root / runs[0]).exists(), "run older than the previous must be deleted"
    assert (run_root / runs[1] / "manifest.json").exists()
    assert (run_root / runs[2] / "manifest.json").exists()
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["layers"]["weather"]["run_id"] == runs[2]
    assert latest["layers"]["weather"]["previous_run_id"] == runs[1]


def test_cli_dry_run_wiring(tmp_path, monkeypatch):
    """`ingest weather --dry-run DIR` with the GFS source stubbed out."""
    from ingest.sources import gfs

    cube = make_weather_cube()
    monkeypatch.setattr(gfs, "resolve", lambda requested=None: cube.cycle)
    monkeypatch.setattr(gfs, "build_cube", lambda cycle: cube)

    rc = cli.main(["weather", "--dry-run", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "latest.json").exists()
    assert (tmp_path / "forecast-runs" / cube.run_id / "manifest.json").exists()


def test_cli_aborts_on_validation_failure(tmp_path, monkeypatch, capsys):
    from ingest.sources import gfs

    cube = make_weather_cube()
    cube.arrays["wind_u_kt"][:] = 999.0  # implausible
    monkeypatch.setattr(gfs, "resolve", lambda requested=None: cube.cycle)
    monkeypatch.setattr(gfs, "build_cube", lambda cycle: cube)

    rc = cli.main(["weather", "--dry-run", str(tmp_path)])
    assert rc == 1
    assert not (tmp_path / "latest.json").exists()
    assert "validation FAILED" in capsys.readouterr().out
