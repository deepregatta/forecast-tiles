"""The routing-index golden fixture is the cross-repo contract: these bytes and
tactician's `core/land` must decode to the same answers. The same three files
are committed in that repo, and neither vendors the other's implementation."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from landkit.codec import decode_tile
from landkit.grid import CELLS_PER_DEG, lat_cell_metres, lon_cell_metres

FIXTURE = Path(__file__).parent / "fixtures" / "land-index-raz"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts"


def fnv64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


@pytest.fixture(scope="module")
def expected():
    return json.loads((FIXTURE / "expected.json").read_text())


@pytest.fixture(scope="module")
def manifest():
    return json.loads((FIXTURE / "manifest.json").read_text())


@pytest.fixture(scope="module")
def tile(expected):
    return decode_tile(gzip.decompress((FIXTURE / f"{expected['tile_id']}.bin.gz").read_bytes()))


def test_the_fixture_bytes_have_not_drifted_from_the_expectations(expected):
    raw = gzip.decompress((FIXTURE / f"{expected['tile_id']}.bin.gz").read_bytes())
    assert fnv64(raw) == expected["fnv64_uncompressed"]


def test_the_manifest_names_the_tile_and_checksums_it(manifest, expected):
    stored = (FIXTURE / f"{expected['tile_id']}.bin.gz").read_bytes()
    entry = manifest["tiles"][expected["tile_id"]]
    assert entry["bytes"] == len(stored)
    assert entry["fnv64"] == fnv64(stored)
    assert manifest["index_id"] == expected["index_id"]


def test_the_fixture_manifest_matches_its_published_schema(manifest):
    validator = Draft202012Validator(
        json.loads((SCHEMA_DIR / "land-index-manifest.schema.json").read_text())
    )
    assert not sorted(validator.iter_errors(manifest), key=str)


def test_the_fixture_tile_header_matches_its_published_schema(tile):
    validator = Draft202012Validator(
        json.loads((SCHEMA_DIR / "land-index-tile.schema.json").read_text())
    )
    assert not sorted(validator.iter_errors(tile.header), key=str)


def test_the_header_round_trips_and_the_counts_agree(tile, expected):
    assert tile.header == expected["header"]
    assert int(tile.blocked.sum()) == expected["blocked_cells"]
    assert tile.header["blocked_cells"] == expected["blocked_cells"]


def test_the_named_geography_reads_the_way_it_looks(tile, expected):
    """Islet blocked, mainland blocked, passage clear, bay clear. The reason
    this region is the fixture."""
    for probe in expected["probes"]:
        cell = tile.cell_of(probe["lon"], probe["lat"])
        assert cell is not None, probe["label"]
        assert bool(tile.blocked[cell]) == probe["blocked"], probe["label"]


def test_the_raz_passage_is_still_a_passage(tile, expected):
    """The buffer errs toward land, which means it can close a channel. This is
    the number that says it has not closed this one."""
    row = expected["raz_passage_row"]
    line = tile.blocked[row["row"]]
    runs = np.split(np.arange(len(line)), np.flatnonzero(np.diff(line)) + 1)
    widest = max((len(r) for r in runs if not line[r[0]]), default=0)
    assert widest == row["widest_clear_run_cells"]
    assert widest * lon_cell_metres(row["lat"]) > 3_000, "the Raz is wider than three kilometres"


def test_the_fixture_states_its_conservatism_in_metres_and_cells(tile, manifest):
    conservatism = tile.header["conservatism"]
    assert conservatism["buffer_m"] == manifest["conservatism"]["buffer_m"]
    assert conservatism["safety_contour_m"] == 0.0
    assert "LAT" in conservatism["vertical_datum"]
    dilation = conservatism["dilation_cells"]
    assert dilation["lon"] * lon_cell_metres(49.0) >= conservatism["buffer_m"]
    assert dilation["lat"] * lat_cell_metres() >= conservatism["buffer_m"]


def test_the_buffer_only_added_area(tile):
    composition = tile.header["composition"]
    assert tile.header["blocked_cells"] >= composition["blocked_before_buffer"]


def test_both_sources_contributed_and_are_measured_against_each_other(manifest):
    composition = manifest["composition"]
    assert composition["depth_only_cells"] > 0, (
        "the depth contour must add rock the shoreline lacks"
    )
    assert composition["shoreline_only_cells"] > 0, "the shoreline must add detail the DTM smooths"
    assert composition["source_agreement"] > 0.95


def test_the_licences_and_the_navigation_constraint_are_recorded(manifest):
    assert "NOT USE FOR NAVIGATION" in manifest["not_for_navigation"]
    names = {source["name"]: source for source in manifest["sources"]}
    assert names["GSHHG"]["licence"].startswith("LGPL")
    assert "Wessel" in names["GSHHG"]["attribution"]
    assert names["EMODnet Bathymetry"]["licence"] == "CC-BY-4.0"
    assert "EMODnet Bathymetry Consortium" in names["EMODnet Bathymetry"]["attribution"]
    for source in manifest["sources"]:
        assert source["accessed"], f"{source['name']} has no access date"


def test_the_grid_is_the_one_the_consumer_expects(tile, manifest):
    assert manifest["grid"]["cells_per_deg"] == CELLS_PER_DEG
    assert (
        tile.header["nlon"] == tile.header["nlat"] == manifest["grid"]["tile_deg"] * CELLS_PER_DEG
    )
    assert tile.header["cell_order"] == "south_to_north_row_major_lsb_first"
