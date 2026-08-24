"""The compilation's one law, tested as a property: it only ever adds blocked
area. Every case here builds its own sources, so nothing touches the network."""

import numpy as np
import pytest

from ingest.land.build import PROBES, Domain, build_index, index_id
from ingest.land.compile import (
    Conservatism,
    _reduce_block,
    compile_tile,
    dilate,
    rasterise_polygon,
)
from ingest.land.geotiff import GeoRaster
from landkit.grid import CELLS_PER_DEG, lat_cell_metres, lon_cell_metres

NATIVE = 960


def flat_ocean(depth_m=-80.0):
    """A DTM block of open water at a constant depth."""

    def fetch(cache_dir, west, south):
        values = np.full((NATIVE, NATIVE), depth_m, dtype=np.float32)
        return GeoRaster(
            values=values, west=west, north=south + 1, dlon=1 / NATIVE, dlat=1 / NATIVE
        )

    return fetch


def ocean_with_shoal(box, height_m=1.5, depth_m=-80.0):
    """Open water with one rectangle of ground standing above the datum.
    `box` is (west, south, east, north) in degrees."""

    def fetch(cache_dir, west, south):
        values = np.full((NATIVE, NATIVE), depth_m, dtype=np.float32)
        lon = west + (np.arange(NATIVE) + 0.5) / NATIVE
        lat = south + 1 - (np.arange(NATIVE) + 0.5) / NATIVE  # north-up rows
        inside_lon = (lon >= box[0]) & (lon < box[2])
        inside_lat = (lat >= box[1]) & (lat < box[3])
        values[np.ix_(inside_lat, inside_lon)] = height_m
        return GeoRaster(
            values=values, west=west, north=south + 1, dlon=1 / NATIVE, dlat=1 / NATIVE
        )

    return fetch


def square(west, south, size):
    lon = np.array([west, west + size, west + size, west, west])
    lat = np.array([south, south, south + size, south + size, south])
    return lon, lat


# ---------------------------------------------------------------- depth


def test_a_block_reduces_to_its_shallowest_source_cell_never_its_mean():
    elevation = np.full((4, 4), -50.0, dtype=np.float32)
    elevation[0, 0] = 0.5  # one rock among deep water
    shoal, missing = _reduce_block(elevation, 2, safety_contour_m=0.0)
    assert shoal[0, 0], "the shallowest cell must decide, or a rock averages away"
    assert not shoal[1, 1]
    assert not missing.any()


def test_no_data_is_carried_apart_from_deep_rather_than_swallowed_by_the_maximum():
    elevation = np.full((2, 2), np.nan, dtype=np.float32)
    shoal, missing = _reduce_block(elevation, 2, safety_contour_m=0.0)
    assert not shoal[0, 0], "unknown is not shallow"
    assert missing[0, 0], "unknown is also not deep — it is its own answer"


def test_a_deeper_safety_contour_blocks_strictly_more():
    elevation = np.full((2, 2), -3.0, dtype=np.float32)
    assert not _reduce_block(elevation, 2, 0.0)[0][0, 0]
    assert _reduce_block(elevation, 2, 5.0)[0][0, 0]


# ----------------------------------------------------------- coastline


def test_a_square_polygon_fills_and_its_edges_are_marked():
    mask = np.zeros((480, 480), dtype=bool)
    lon, lat = square(-4.6, 48.2, 0.2)
    rasterise_polygon(mask, lon, lat, -5.0, 48.0, CELLS_PER_DEG)
    # Interior
    assert mask[int(0.3 * CELLS_PER_DEG), int(0.5 * CELLS_PER_DEG)]
    # Outside
    assert not mask[int(0.05 * CELLS_PER_DEG), int(0.05 * CELLS_PER_DEG)]
    assert mask.sum() >= (0.2 * CELLS_PER_DEG) ** 2


def test_an_islet_smaller_than_a_cell_is_still_marked():
    """A centre-in-polygon test alone would lose it; the edge pass is why the
    compilation marks every cell an edge passes through as well as filling."""
    mask = np.zeros((480, 480), dtype=bool)
    tiny = 0.2 / CELLS_PER_DEG  # a fifth of a cell across
    lon, lat = square(-4.5 + 0.0007, 48.5 + 0.0007, tiny)
    rasterise_polygon(mask, lon, lat, -5.0, 48.0, CELLS_PER_DEG)
    assert mask.sum() >= 1


def _ragged_coast():
    """A ragged closed coast, not a rectangle: the fill and the edge pass both
    have to work for a shape with concavities."""
    angle = np.linspace(0, 2 * np.pi, 400)
    radius = 0.15 + 0.05 * np.sin(7 * angle)
    return -4.5 + radius * np.cos(angle), 48.5 + radius * np.sin(angle)


def test_every_point_on_the_source_boundary_lands_in_a_marked_cell():
    """The conservatism property for the edge pass, stated directly: walk the
    source polygon's own edges at a fine step and every point must be inside
    the compiled blocked set."""
    mask = np.zeros((480, 480), dtype=bool)
    lon, lat = _ragged_coast()
    rasterise_polygon(mask, lon, lat, -5.0, 48.0, CELLS_PER_DEG)

    t = np.linspace(0, 1, 40)[:, None]
    px = (lon[:-1] + (lon[1:] - lon[:-1]) * t).ravel()
    py = (lat[:-1] + (lat[1:] - lat[:-1]) * t).ravel()
    col = ((px + 5.0) * CELLS_PER_DEG).astype(int)
    row = ((py - 48.0) * CELLS_PER_DEG).astype(int)
    assert mask[row, col].all(), "a point of the source boundary read as clear"


def test_the_interior_of_a_ragged_coast_is_filled():
    """The fill's own guarantee. Samples are drawn inside the discretised
    polygon rather than the smooth curve it approximates — a point on the
    smooth curve can sit outside the chord that stands in for it, and that is a
    property of the sample, not of the index."""
    rng = np.random.default_rng(5)
    mask = np.zeros((480, 480), dtype=bool)
    lon, lat = _ragged_coast()
    rasterise_polygon(mask, lon, lat, -5.0, 48.0, CELLS_PER_DEG)

    t = rng.random(20_000)
    radius = (0.15 + 0.05 * np.sin(7 * t * 2 * np.pi)) * 0.97 * np.sqrt(rng.random(20_000))
    px = -4.5 + radius * np.cos(t * 2 * np.pi)
    py = 48.5 + radius * np.sin(t * 2 * np.pi)
    col = ((px + 5.0) * CELLS_PER_DEG).astype(int)
    row = ((py - 48.0) * CELLS_PER_DEG).astype(int)
    assert mask[row, col].all(), "a point inside the source geometry read as clear"


def test_the_concavities_of_a_ragged_coast_stay_water():
    """The other half of the same claim: the fill must not simply block the
    convex hull. A star's notches are sailable water and must read clear."""
    mask = np.zeros((480, 480), dtype=bool)
    lon, lat = _ragged_coast()
    rasterise_polygon(mask, lon, lat, -5.0, 48.0, CELLS_PER_DEG)
    # Half-way between two lobes, just outside the notch's own radius.
    notch = 3 * np.pi / 14  # where sin(7 theta) is -1, so the coast is at 0.10
    px = -4.5 + 0.145 * np.cos(notch)
    py = 48.5 + 0.145 * np.sin(notch)
    assert not mask[int((py - 48.0) * CELLS_PER_DEG), int((px + 5.0) * CELLS_PER_DEG)]


# -------------------------------------------------------------- buffer


def test_dilation_is_a_superset_and_reaches_the_distance_it_claims():
    mask = np.zeros((100, 100), dtype=bool)
    mask[50, 50] = True
    out = dilate(mask, lon_cells=2, lat_cells=1)
    assert (out | mask == out).all(), "the buffer must never clear a cell"
    assert out[50, 48] and out[50, 52] and out[49, 50] and out[51, 50]
    assert not out[50, 47] and not out[48, 50]


def test_dilation_does_not_wrap_across_the_tile_edge():
    """A tile boundary is a publishing artefact, not a coastline. A blocked
    cell on the west edge must not paint the east edge of the same tile."""
    mask = np.zeros((20, 20), dtype=bool)
    mask[0, 0] = True
    out = dilate(mask, lon_cells=3, lat_cells=3)
    assert not out[-1, -1]
    assert not out[0, -1]
    assert not out[-1, 0]


# --------------------------------------------------------- end to end


def test_compile_tile_blocks_the_shoal_and_only_adds_area():
    conservatism = Conservatism()
    compiled = compile_tile(
        48,
        -5,
        gshhg_path=_empty_gshhg(),
        cache_dir=None,
        conservatism=conservatism,
        span_deg=1,
        fetch_block=ocean_with_shoal((-4.7, 48.2, -4.6, 48.3)),
    )
    assert compiled.shoal_cells > 0
    assert compiled.land_cells == 0
    assert compiled.nodata_cells == 0
    assert compiled.blocked_cells >= compiled.blocked_before_buffer
    # The shoal's own box reads blocked; open water two degrees-worth of cells
    # away from the buffer does not.
    assert compiled.blocked[int(0.25 * CELLS_PER_DEG), int(0.35 * CELLS_PER_DEG)]
    assert not compiled.blocked[int(0.8 * CELLS_PER_DEG), int(0.8 * CELLS_PER_DEG)]


def test_compile_tile_buffers_outward_by_at_least_the_named_distance():
    conservatism = Conservatism(buffer_m=200.0)
    compiled = compile_tile(
        48,
        -5,
        gshhg_path=_empty_gshhg(),
        cache_dir=None,
        conservatism=conservatism,
        span_deg=1,
        fetch_block=ocean_with_shoal((-4.7, 48.2, -4.6, 48.3)),
    )
    lon_cells, lat_cells = compiled.dilation
    assert lon_cells * lon_cell_metres(49.0) >= conservatism.buffer_m
    assert lat_cells * lat_cell_metres() >= conservatism.buffer_m
    # The cell immediately west of the shoal's western edge is now blocked.
    row = int(0.25 * CELLS_PER_DEG)
    west_edge = int(0.3 * CELLS_PER_DEG)
    assert compiled.blocked[row, west_edge - 1]


def test_no_data_is_blocked_and_counted_rather_than_treated_as_deep():
    def all_missing(cache_dir, west, south):
        return GeoRaster(
            values=np.full((NATIVE, NATIVE), np.nan, dtype=np.float32),
            west=west,
            north=south + 1,
            dlon=1 / NATIVE,
            dlat=1 / NATIVE,
        )

    compiled = compile_tile(
        48,
        -5,
        gshhg_path=_empty_gshhg(),
        cache_dir=None,
        conservatism=Conservatism(),
        span_deg=1,
        fetch_block=all_missing,
    )
    assert compiled.nodata_cells == CELLS_PER_DEG**2
    assert compiled.blocked.all()

    permissive = compile_tile(
        48,
        -5,
        gshhg_path=_empty_gshhg(),
        cache_dir=None,
        conservatism=Conservatism(nodata_is_blocked=False),
        span_deg=1,
        fetch_block=all_missing,
    )
    assert not permissive.blocked.any()


# ------------------------------------------------------------- probes


def test_the_build_refuses_an_index_whose_probes_read_backwards():
    """The catastrophic failure — a transposed axis, a flipped depth sign —
    passes every structural check while inverting land and sea. The probes are
    what stands between that and a published artifact."""
    ocean = Domain(name="probe", label="probe test", tiles=((50, -10),), span_deg=10)
    land_probes = [p for p in PROBES if p[3] and _in(p, 50, -10)]
    assert land_probes, "this domain must contain at least one land probe to be a test"

    with pytest.raises(ValueError, match="expected blocked"):
        build_index(
            ocean,
            gshhg_path=_empty_gshhg(),
            cache_dir=None,
            conservatism=Conservatism(),
            fetch_block=flat_ocean(),
        )


def test_index_ids_read_like_a_run_id_and_carry_the_hour():
    from datetime import datetime, timezone

    assert index_id("nweu", datetime(2026, 8, 24, 21, tzinfo=timezone.utc)) == "nweu-20260824T21Z"


def _in(probe, lat0, lon0, span=10):
    _, lon, lat, _ = probe
    return lon0 <= lon < lon0 + span and lat0 <= lat < lat0 + span


def _empty_gshhg(tmp=[]):  # noqa: B006 - module-scoped scratch file
    """A GSHHG file with no polygons: the coastline half contributes nothing,
    so a case can test the depth half on its own."""
    import tempfile
    from pathlib import Path

    if not tmp:
        handle = tempfile.NamedTemporaryFile(suffix=".b", delete=False)
        handle.close()
        tmp.append(Path(handle.name))
    return tmp[0]
