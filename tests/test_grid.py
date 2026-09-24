"""Grid derivation from the providers' real coordinate arrays (recorded by
scripts/record_cmems_coordinates.py) and the tile boundaries it produces."""

import gzip
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import CYCLE

from ingest.cube import ForecastCube, GridMeta, regular_axis
from ingest.sources import cmems
from ingest.tile import build_tiles, tile_index_ranges
from tilekit.codec import decode_tile

COORDS = np.load(Path(__file__).parent / "fixtures" / "cmems-coordinates.npz")
GLO12_LATS = COORDS["glo12_latitude"]
GLO12_LONS = COORDS["glo12_longitude"]
IBI_LATS = COORDS["ibi_latitude"]
IBI_LONS = COORDS["ibi_longitude"]

# the true GLO12 lattice: lat -80 + k/12, lon -180 + k/12
TRUE_GLO12_LATS = (-960 + np.arange(GLO12_LATS.size)) / 12
TRUE_GLO12_LONS = (-2160 + np.arange(GLO12_LONS.size)) / 12


def glo12_grid() -> GridMeta:
    return GridMeta.from_coordinates(
        GLO12_LATS, GLO12_LONS, cells_per_degree=cmems.CELLS_PER_DEGREE
    )


def test_recorded_coordinates_are_the_native_grids():
    # GLO12: float32 approximations of the global 1/12° lattice
    assert (GLO12_LATS.dtype, GLO12_LATS.size, GLO12_LONS.size) == (np.float32, 2041, 4320)
    assert np.abs(GLO12_LATS - TRUE_GLO12_LATS).max() < 3e-5
    assert np.abs(GLO12_LONS - TRUE_GLO12_LONS).max() < 3e-5
    # IBI: float64, exactly regular at 0.02777863°, and not on the 1/36° lines
    for coords in (IBI_LATS, IBI_LONS):
        assert coords.dtype == np.float64
        assert np.abs(np.diff(coords) - 0.02777863).max() < 1e-12
        off_lattice = coords * 36 - np.round(coords * 36)
        assert np.abs(off_lattice).max() * (1 / 36) > 5e-4


def test_glo12_grid_is_the_exact_lattice():
    grid = glo12_grid()
    assert (grid.lat0, grid.lon0, grid.dlat, grid.dlon) == (-80.0, -180.0, 1 / 12, 1 / 12)
    assert (grid.nlat, grid.nlon) == (2041, 4320)
    assert np.abs(grid.lats() - TRUE_GLO12_LATS).max() < 1e-6
    assert np.abs(grid.lons() - TRUE_GLO12_LONS).max() < 1e-6
    # and the stored coordinates to float32 precision
    assert np.abs(grid.lons() - GLO12_LONS).max() <= 2 * np.spacing(np.float32(180))


def test_glo12_neighbour_difference_drifts():
    """The derivation this replaced: the first two float32 longitudes."""
    dlon = float(GLO12_LONS[1]) - float(GLO12_LONS[0])
    assert dlon == 0.0833282470703125
    drifted = GridMeta(
        lat0=-80.0, lon0=-180.0, dlat=1 / 12, dlon=dlon, nlat=GLO12_LATS.size, nlon=GLO12_LONS.size
    )
    assert (drifted.lons() - TRUE_GLO12_LONS).min() < -0.02
    # ... which put the true -10.0 column at the end of N40W020
    _, _, _, lj = next(r for r in tile_index_ranges(drifted) if r[:2] == (40, -20))
    assert TRUE_GLO12_LONS[lj.stop - 1] == -10.0


def test_ibi_grid_reproduces_provider_coordinates():
    grid = GridMeta.from_coordinates(IBI_LATS, IBI_LONS)
    assert grid.dlat == pytest.approx(0.02777863, abs=1e-15)
    assert grid.dlon == pytest.approx(0.02777863, abs=1e-15)
    assert np.abs(grid.lats() - IBI_LATS).max() < 1e-6
    assert np.abs(grid.lons() - IBI_LONS).max() < 1e-6
    # snapping to 1/36 would move provider points by ~0.001°: refused
    with pytest.raises(ValueError, match="1/36° lattice"):
        regular_axis(IBI_LATS, cells_per_degree=36)


def test_irregular_axes_are_refused():
    with pytest.raises(ValueError, match="not a regular axis"):
        regular_axis(np.array([0.0, 0.25, 0.5, 0.8]))
    with pytest.raises(ValueError, match="not ascending"):
        regular_axis(np.array([0.5, 0.25, 0.0]))
    with pytest.raises(ValueError, match="at least two"):
        regular_axis(np.array([0.5]))
    # float32 noise is tolerated only while the coordinates stay float32
    with pytest.raises(ValueError, match="1/12° lattice"):
        regular_axis(GLO12_LONS.astype(np.float64), cells_per_degree=12)


@pytest.mark.parametrize(
    ("grid", "true_lats", "true_lons"),
    [
        (glo12_grid(), TRUE_GLO12_LATS, TRUE_GLO12_LONS),
        (GridMeta.from_coordinates(IBI_LATS, IBI_LONS), IBI_LATS, IBI_LONS),
    ],
    ids=["glo12", "ibi"],
)
def test_tile_boundaries_follow_true_coordinates(grid, true_lats, true_lons):
    """Each row/column goes to the tile whose half-open range holds its true
    coordinate, and none is left over at a 10° line."""
    ranges = tile_index_ranges(grid)
    assert ranges
    for t_lat0, t_lon0, li, lj in ranges:
        for true, t0, sl in ((true_lats, t_lat0, li), (true_lons, t_lon0, lj)):
            assert t0 <= true[sl.start] and true[sl.stop - 1] < t0 + 10
            assert sl.start == 0 or true[sl.start - 1] < t0
            assert sl.stop == len(true) or true[sl.stop] >= t0 + 10


class _FakeGlo12Dataset:
    """A window of the real float32 GLO12 grid, u = source column in m/s."""

    dims = {}

    def __init__(self, cycle, rows, cols):
        self.coords = {
            "latitude": GLO12_LATS[rows],
            "longitude": GLO12_LONS[cols],
            "time": np.array(
                [
                    np.datetime64(cycle.replace(tzinfo=None) + timedelta(hours=h), "ns")
                    for h in cmems.STEP_AXIS
                ]
            ),
        }
        nlat, nlon = self.coords["latitude"].size, self.coords["longitude"].size
        self.u = np.broadcast_to(np.arange(nlon, dtype=np.float32), (nlat, nlon))

    def __getitem__(self, name):
        return SimpleNamespace(values=self.coords[name])

    def sel(self, *, time):
        return {"uo": SimpleNamespace(values=self.u), "vo": SimpleNamespace(values=-self.u)}


def test_glo12_build_cube_snaps_the_float32_coordinates(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    ds = _FakeGlo12Dataset(cycle, slice(1437, 1443), slice(2037, 2043))
    monkeypatch.setattr(cmems, "_open_dataset_with_auth_retries", lambda *args, **kwargs: ds)
    cube = cmems.build_cube(cycle)
    assert cube.grid == GridMeta(lat0=39.75, lon0=-10.25, dlat=1 / 12, dlon=1 / 12, nlat=6, nlon=6)
    assert cube.resolution_deg == 1 / 12


def test_glo12_tiles_start_on_their_ten_degree_lines():
    # a real-coordinate window around 40°N 10°W; each value is its source column
    rows, cols = slice(1437, 1443), slice(2037, 2043)
    grid = GridMeta.from_coordinates(
        GLO12_LATS[rows], GLO12_LONS[cols], cells_per_degree=cmems.CELLS_PER_DEGREE
    )
    column = np.broadcast_to(np.arange(6, dtype=np.float32), (1, 6, 6)).copy()
    cube = ForecastCube(
        layer=cmems.LAYER,
        model=cmems.MODEL,
        cycle=CYCLE.replace(hour=0),
        grid=grid,
        time_axes={cmems.AXIS_NAME: [0]},
        variables=list(cmems.VARS),
        arrays={"cur_u_kt": column, "cur_v_kt": column.copy()},
    )
    tiles = {
        tid: decode_tile(gzip.decompress(gz))
        for tid, gz in build_tiles(cube, generated_at="2026-09-24T00:00:00Z")
    }
    assert set(tiles) == {"N30W020", "N30W010", "N40W020", "N40W010"}
    header = tiles["N40W010"].header
    assert (header["lat0"], header["lon0"]) == (40.0, -10.0)
    assert (header["dlat"], header["dlon"]) == (1 / 12, 1 / 12)
    assert (header["nlat"], header["nlon"]) == (3, 3)
    # the true -10.0 column (source column 3) opens N40W010
    assert np.allclose(tiles["N40W010"].arrays["cur_u_kt"][0, :, 0], 3)
    west = tiles["N40W020"].header
    assert west["lon0"] + (west["nlon"] - 1) * west["dlon"] == pytest.approx(-10 - 1 / 12)
