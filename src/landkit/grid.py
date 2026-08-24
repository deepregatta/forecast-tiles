"""The routing index's cell grid, and the arithmetic that keeps it conservative.

Two decisions live here, and both are versioned parameters of a published
index rather than constants anyone may quietly retune.

**Cell size.** 1/480 degree — about 232 m of latitude, and 149 m of longitude
at 50 N. It is finer than the EMODnet DTM's own 115 m posting is coarse, and
it leaves the Raz de Sein passage roughly ten cells wide, which is the test
this artifact exists to survive.

**The outward buffer.** Every step of the compilation errs toward land. The
buffer is the last of them and the only one with a number the crew can read:
the compiled blocked set is dilated outward so its edge lies at least
`buffer_m` outside the source geometry's edge. Because a longitude cell is
narrower in metres than a latitude cell, and narrower still further north, the
dilation is computed per axis from the *narrowest* cell in the tile — the one
that needs the most cells to cover the same distance.
"""

from __future__ import annotations

import math

#: Index cells per degree. The whole grid is defined by this one integer, so a
#: tile's origin and cell edges are exact in both the producer and the consumer.
CELLS_PER_DEG = 480

#: Metres per degree of latitude on the WGS84 sphere the rest of the fleet
#: uses (tactician's `haversine` radius, 6 371 008.8 m).
EARTH_RADIUS_M = 6_371_008.8
METRES_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0


def cell_deg() -> float:
    return 1.0 / CELLS_PER_DEG


def lat_cell_metres() -> float:
    return METRES_PER_DEG / CELLS_PER_DEG


def lon_cell_metres(lat_deg: float) -> float:
    return METRES_PER_DEG * math.cos(math.radians(lat_deg)) / CELLS_PER_DEG


def dilation_cells(south_lat: float, north_lat: float, buffer_m: float) -> tuple[int, int]:
    """(lon_cells, lat_cells) of outward dilation that guarantee at least
    `buffer_m` everywhere between `south_lat` and `north_lat`.

    The longitude figure is taken at whichever bound is further from the
    equator, because that is where a cell covers the fewest metres and so where
    the most cells are needed to reach the same distance.
    """
    if buffer_m < 0:
        raise ValueError("buffer_m must not be negative")
    worst_lat = max(abs(south_lat), abs(north_lat))
    lon_metres = lon_cell_metres(min(worst_lat, 89.0))
    return (
        math.ceil(buffer_m / lon_metres),
        math.ceil(buffer_m / lat_cell_metres()),
    )
