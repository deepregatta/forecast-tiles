"""Compile land polygons and a depth contour into one conservative tile.

Everything here obeys one law, stated once so every step can be checked
against it: **the compilation only ever adds blocked area.** No step removes
any. A route the finished index passes must be passable in the source data; a
route it refuses may or may not have been. That asymmetry is deliberate and it
is why lakes are not subtracted, why a cell is blocked when *any* part of it is
land, and why the dilation is outward only.

The four steps, in order, each conservative on its own:

1. **Depth.** The DTM is read at its own 115 m posting and reduced to index
   cells by taking the *shallowest* source cell in each — never the mean. One
   rock in a 232 m cell makes the cell shallow, which is the answer that keeps
   a boat off it.
2. **Coastline.** Level-1 polygons are rasterised twice over: every cell an
   edge passes through is marked, and interiors are filled by even-odd
   scanline. The edge pass is what catches an islet smaller than a cell, which
   a centre-in-polygon test alone would lose.
3. **Union.** Land, shoal and (by policy) unknown depth are one blocked set.
   The counts are kept apart so the manifest can say what the index is made of.
4. **Buffer.** An outward dilation of the whole set by at least `buffer_m`.

Nothing is Douglas-Peucker simplified. The simplification *is* the raster: a
cell is the tolerance, it is always outward, and integer cell arithmetic gives
the same answer on every platform — which polygon intersection in floating
point does not, and validation-plan section 4 T1 asks for exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ingest.land import emodnet, gshhg
from landkit.grid import CELLS_PER_DEG, cell_deg, dilation_cells

TILE_DEG = 10
#: Source cells per index cell along each axis. An exact integer by
#: construction: the DTM's 1/960 degree posting is twice the index's 1/480.
SOURCE_PER_CELL = emodnet.NATIVE_CELLS_PER_DEG // CELLS_PER_DEG


@dataclass
class Conservatism:
    """The named, versioned parameters that decide how far this index errs."""

    #: Metres the blocked set's edge is pushed outside the source geometry's.
    buffer_m: float = 200.0
    #: Depth below LAT at or above which water is treated as not sailable.
    #: Zero is the contour that dries: at 0 m the ground is exposed at the
    #: lowest astronomical tide, so blocking it can never make a legal route
    #: illegal. Anything deeper is a draft-and-tide judgement about a
    #: particular boat on a particular day, which is a race-package constraint
    #: and not a property of the sea.
    safety_contour_m: float = 0.0
    #: A cell the DTM has no value for cannot be shown to be passable, so it is
    #: blocked. The manifest counts them, because a large count would mean the
    #: index is refusing water for want of data rather than for want of depth.
    nodata_is_blocked: bool = True

    def public(self) -> dict:
        return {
            "buffer_m": self.buffer_m,
            "buffer_rationale": (
                "GSHHG's full-resolution shoreline and the EMODnet DTM's 115 m posting both "
                "carry positional error of this order; the dilation puts the index's edge "
                "outside the source's, never inside it"
            ),
            "safety_contour_m": self.safety_contour_m,
            "safety_contour_rationale": (
                "the contour that dries at Lowest Astronomical Tide. Blocking it cannot make "
                "a legal route illegal; a draft- or tide-relative clearance is a race-package "
                "constraint, not a property of the sea, and is not decided here"
            ),
            "vertical_datum": emodnet.VERTICAL_DATUM,
            "nodata_is_blocked": self.nodata_is_blocked,
            "simplification": (
                "rasterisation to the index cell: a cell is blocked when any source geometry "
                "touches it. No polygon simplification is applied"
            ),
            "simplification_tolerance_deg": cell_deg(),
            "rule": "every step adds blocked area; none removes any",
        }


@dataclass
class TileCompilation:
    tile_id: str
    lon0: int
    lat0: int
    blocked: np.ndarray
    land_cells: int
    shoal_cells: int
    nodata_cells: int
    #: Cells the shoreline calls land and the DTM calls water, and the reverse.
    #: Two independent sources measured against each other: a compilation that
    #: had transposed an axis or flipped a sign would not agree with itself.
    gshhg_only_cells: int
    dtm_only_cells: int
    blocked_before_buffer: int
    dilation: tuple[int, int]
    notes: list[str] = field(default_factory=list)

    @property
    def blocked_cells(self) -> int:
        return int(self.blocked.sum())


def compile_tile(
    lat0: int,
    lon0: int,
    *,
    gshhg_path: Path,
    cache_dir: Path,
    conservatism: Conservatism,
    span_deg: int = TILE_DEG,
    cells_per_deg: int = CELLS_PER_DEG,
    fetch_block=emodnet.fetch_block,
    progress=None,
) -> TileCompilation:
    n = span_deg * cells_per_deg
    land = np.zeros((n, n), dtype=bool)
    shoal = np.zeros((n, n), dtype=bool)
    nodata = np.zeros((n, n), dtype=bool)

    reduce_by = emodnet.NATIVE_CELLS_PER_DEG // cells_per_deg
    for south in range(lat0, lat0 + span_deg):
        for west in range(lon0, lon0 + span_deg):
            if progress is not None:
                progress(west, south)
            raster = fetch_block(cache_dir, west, south)
            elevation = raster.south_up()
            block_shoal, block_nodata = _reduce_block(
                elevation, reduce_by, conservatism.safety_contour_m
            )
            row = (south - lat0) * cells_per_deg
            col = (west - lon0) * cells_per_deg
            shoal[row : row + cells_per_deg, col : col + cells_per_deg] = block_shoal
            nodata[row : row + cells_per_deg, col : col + cells_per_deg] = block_nodata

    polygons = gshhg.land_polygons(gshhg_path, lon0, lat0, lon0 + span_deg, lat0 + span_deg)
    for polygon in polygons:
        rasterise_polygon(land, polygon.lon, polygon.lat, lon0, lat0, cells_per_deg)

    gshhg_only = int((land & ~shoal).sum())
    dtm_only = int((shoal & ~land).sum())
    blocked = land | shoal
    if conservatism.nodata_is_blocked:
        blocked |= nodata
    before = int(blocked.sum())

    lon_cells, lat_cells = dilation_cells(lat0, lat0 + span_deg, conservatism.buffer_m)
    blocked = dilate(blocked, lon_cells=lon_cells, lat_cells=lat_cells)

    from tilekit.tiles import tile_id

    return TileCompilation(
        tile_id=tile_id(lat0, lon0),
        lon0=lon0,
        lat0=lat0,
        blocked=blocked,
        land_cells=int(land.sum()),
        shoal_cells=int(shoal.sum()),
        nodata_cells=int((nodata & ~land & ~shoal).sum()),
        gshhg_only_cells=gshhg_only,
        dtm_only_cells=dtm_only,
        blocked_before_buffer=before,
        dilation=(lon_cells, lat_cells),
        notes=[f"{len(polygons)} GSHHG level-1 polygons reached this tile"],
    )


def _reduce_block(
    elevation: np.ndarray, reduce_by: int, safety_contour_m: float
) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a native-posting block to index cells, shallowest-wins.

    `elevation` is height against LAT, so shallower is larger and the
    conservative reduction is a maximum. NaN is carried separately rather than
    swallowed by the max, because "no data" and "deep" are different answers.
    """
    size = elevation.shape[0] // reduce_by
    grouped = elevation[: size * reduce_by, : size * reduce_by].reshape(
        size, reduce_by, size, reduce_by
    )
    missing = np.isnan(grouped)
    filled = np.where(missing, -np.inf, grouped)
    shallowest = filled.max(axis=(1, 3))
    return shallowest >= -safety_contour_m, missing.any(axis=(1, 3))


def rasterise_polygon(
    mask: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    lon0: float,
    lat0: float,
    cells_per_deg: int,
) -> None:
    """Mark every cell the polygon covers or touches, in place."""
    _mark_edges(mask, lon, lat, lon0, lat0, cells_per_deg)
    _fill_interior(mask, lon, lat, lon0, lat0, cells_per_deg)


def _mark_edges(
    mask: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    lon0: float,
    lat0: float,
    cells_per_deg: int,
) -> None:
    """Sample every edge at no more than half a cell and mark what it crosses.

    Sampling alone is not enough. Two samples half a cell apart in each axis
    can sit diagonally across a 2x2 block, and the segment between them can
    clip either of the two cells neither sample occupies. Those elbows are
    marked too, which makes the claim in this module's docstring literally
    true — a cell is blocked when any source geometry touches it — rather than
    true only once the buffer has been applied. An index built with a zero
    buffer has to be conservative on its own.
    """
    nlat, nlon = mask.shape
    step = 1.0 / cells_per_deg
    x0, y0 = lon[:-1], lat[:-1]
    x1, y1 = lon[1:], lat[1:]
    # Closing edge: GSHHG polygons repeat their first point, but not always.
    if lon[0] != lon[-1] or lat[0] != lat[-1]:
        x0 = np.append(x0, lon[-1])
        y0 = np.append(y0, lat[-1])
        x1 = np.append(x1, lon[0])
        y1 = np.append(y1, lat[0])

    dx = x1 - x0
    dy = y1 - y0
    keep = np.abs(dx) <= 180.0  # an antimeridian wrap is not a coastline
    box = (
        (np.minimum(x0, x1) <= lon0 + nlon * step)
        & (np.maximum(x0, x1) >= lon0)
        & (np.minimum(y0, y1) <= lat0 + nlat * step)
        & (np.maximum(y0, y1) >= lat0)
    )
    keep &= box
    if not keep.any():
        return
    x0, y0, dx, dy = x0[keep], y0[keep], dx[keep], dy[keep]

    steps = np.maximum(
        1, np.ceil(np.maximum(np.abs(dx), np.abs(dy)) / (0.5 * step)).astype(np.int64)
    )
    counts = steps + 1
    index = np.repeat(np.arange(len(steps)), counts)
    within = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    t = within / np.repeat(steps, counts)
    px = x0[index] + dx[index] * t
    py = y0[index] + dy[index] * t
    _mark_points(mask, px, py, lon0, lat0, cells_per_deg)

    # The elbows of each within-edge consecutive pair. Restricted to pairs from
    # the same edge: across an edge boundary the two samples are the same point
    # when the edge was kept, and unrelated points when the one between them
    # was filtered out.
    same = index[:-1] == index[1:]
    if same.any():
        _mark_points(mask, px[:-1][same], py[1:][same], lon0, lat0, cells_per_deg)
        _mark_points(mask, px[1:][same], py[:-1][same], lon0, lat0, cells_per_deg)


def _mark_points(
    mask: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    lon0: float,
    lat0: float,
    cells_per_deg: int,
) -> None:
    nlat, nlon = mask.shape
    col = np.floor((lon - lon0) * cells_per_deg).astype(np.int64)
    row = np.floor((lat - lat0) * cells_per_deg).astype(np.int64)
    inside = (col >= 0) & (col < nlon) & (row >= 0) & (row < nlat)
    if inside.any():
        mask[row[inside], col[inside]] = True


def _fill_interior(
    mask: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    lon0: float,
    lat0: float,
    cells_per_deg: int,
) -> None:
    """Even-odd scanline fill at cell centres.

    Crossings west and east of the tile are clamped to just outside it rather
    than dropped. Parity is what decides a fill, so a dropped crossing would
    invert a whole row; a clamped one keeps the count and collapses to a
    zero-width span when it pairs with another clamped crossing.
    """
    nlat, nlon = mask.shape
    step = 1.0 / cells_per_deg
    x0, y0 = lon[:-1], lat[:-1]
    x1, y1 = lon[1:], lat[1:]
    if lon[0] != lon[-1] or lat[0] != lat[-1]:
        x0 = np.append(x0, lon[-1])
        y0 = np.append(y0, lat[-1])
        x1 = np.append(x1, lon[0])
        y1 = np.append(y1, lat[0])

    keep = (np.abs(x1 - x0) <= 180.0) & (y0 != y1)
    lat_top = lat0 + nlat * step
    keep &= (np.minimum(y0, y1) < lat_top) & (np.maximum(y0, y1) > lat0)
    if not keep.any():
        return
    x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]

    y_low = np.minimum(y0, y1)
    y_high = np.maximum(y0, y1)
    # Rows whose centre latitude lies in [y_low, y_high).
    row_lo = np.ceil((y_low - lat0) * cells_per_deg - 0.5).astype(np.int64)
    row_hi = np.ceil((y_high - lat0) * cells_per_deg - 0.5).astype(np.int64) - 1
    np.clip(row_lo, 0, nlat - 1, out=row_lo)
    np.clip(row_hi, -1, nlat - 1, out=row_hi)
    counts = np.maximum(row_hi - row_lo + 1, 0)
    total = int(counts.sum())
    if total == 0:
        return

    index = np.repeat(np.arange(len(counts)), counts)
    offset = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    rows = row_lo[index] + offset
    centre_lat = lat0 + (rows + 0.5) * step
    crossing_x = x0[index] + (x1[index] - x0[index]) * (centre_lat - y0[index]) / (
        y1[index] - y0[index]
    )
    np.clip(crossing_x, lon0 - step, lon0 + nlon * step + step, out=crossing_x)

    order = np.lexsort((crossing_x, rows))
    rows = rows[order]
    crossing_x = crossing_x[order]
    if len(rows) % 2:
        # A closed polygon crosses every scanline an even number of times. An
        # odd count means a vertex landed exactly on a centre latitude and the
        # half-open rule counted it once; the pairing below would then shift a
        # whole row, so the row is left to the edge pass rather than filled wrong.
        rows, crossing_x = _drop_odd_rows(rows, crossing_x)
        if len(rows) == 0:
            return

    starts = crossing_x[0::2]
    ends = crossing_x[1::2]
    span_rows = rows[0::2]
    if not np.array_equal(span_rows, rows[1::2]):
        rows, crossing_x = _drop_odd_rows(rows, crossing_x)
        if len(rows) == 0:
            return
        starts, ends, span_rows = crossing_x[0::2], crossing_x[1::2], rows[0::2]

    col_lo = np.ceil((starts - lon0) * cells_per_deg - 0.5).astype(np.int64)
    col_hi = np.ceil((ends - lon0) * cells_per_deg - 0.5).astype(np.int64) - 1
    np.clip(col_lo, 0, nlon, out=col_lo)
    np.clip(col_hi, -1, nlon - 1, out=col_hi)
    valid = col_hi >= col_lo
    if not valid.any():
        return

    difference = np.zeros((nlat, nlon + 1), dtype=np.int32)
    np.add.at(difference, (span_rows[valid], col_lo[valid]), 1)
    np.add.at(difference, (span_rows[valid], col_hi[valid] + 1), -1)
    mask |= np.cumsum(difference[:, :-1], axis=1) > 0


def _drop_odd_rows(rows: np.ndarray, crossing_x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    unique, counts = np.unique(rows, return_counts=True)
    even = np.isin(rows, unique[counts % 2 == 0])
    return rows[even], crossing_x[even]


def dilate(mask: np.ndarray, *, lon_cells: int, lat_cells: int) -> np.ndarray:
    """Outward dilation by a rectangle, as two separable running maxima.

    The mask is padded with clear cells before shifting and cropped after, so a
    tile's own edge never seeds blocked cells on the far side of it. A tile
    boundary is an artefact of the publishing layout, not a coastline.
    """
    out = mask
    if lon_cells > 0:
        out = _dilate_axis(out, lon_cells, axis=1)
    if lat_cells > 0:
        out = _dilate_axis(out, lat_cells, axis=0)
    return out


def _dilate_axis(mask: np.ndarray, cells: int, axis: int) -> np.ndarray:
    pad = [(0, 0), (0, 0)]
    pad[axis] = (cells, cells)
    padded = np.pad(mask, pad, mode="constant", constant_values=False)
    out = padded.copy()
    for shift in range(1, cells + 1):
        out |= np.roll(padded, shift, axis=axis)
        out |= np.roll(padded, -shift, axis=axis)
    if axis == 0:
        return out[cells:-cells]
    return out[:, cells:-cells]
