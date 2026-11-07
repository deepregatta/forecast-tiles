"""Shared constants for the TCP1 chart builder: cells, areas, paths, tile maths.

Vendored from tactician `tools/charts/common.py` (main at eaf6d88, 2026-11-07)
and cut into cells (WP10 phase 6): the world is cut into z6 tiles, and each
sea-touching z6 tile is one cell archive with z6-14 over its own area.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "forecast-tiles-charts/1 (+https://github.com/deepregatta/forecast-tiles)"

SPEC = "TCP1"
SCHEMA_VERSION = 1
EXTENT = 4096
NM = 1852.0
CELL_ZOOM = 6
MAX_ZOOM = 14
MAX_LAT = 85.0511287798066


def cache_dir() -> Path:
    """Range-read tile caches, SHOM GeoJSON and local packs live here, never in git."""
    return Path(
        os.environ.get("DEEPREGATTA_CHARTS_CACHE", Path.home() / ".cache/deepregatta/charts")
    )


def land_cache_dir() -> Path:
    """The EMODnet DTM blocks the land index caches (`ingest land --cache-dir`)."""
    return Path(
        os.environ.get("DEEPREGATTA_LAND_CACHE", Path.home() / ".cache/deepregatta/land-cache")
    )


@dataclass(frozen=True)
class Area:
    name: str
    bbox: tuple[float, float, float, float]  # west, south, east, north
    min_zoom: int
    max_zoom: int
    cell: tuple[int, int] | None = None  # (x, y) of its z6 tile, for a cell

    def tiles(self, z: int):
        """The tiles this area builds at zoom z. A cell's are exactly the
        descendants of its z6 tile, so neighbouring cells never share a tile; an
        area's are every tile its box touches."""
        if self.cell is None:
            yield from tiles_in(self.bbox, z)
            return
        k = 1 << (z - CELL_ZOOM)
        cx, cy = self.cell
        for x in range(cx * k, (cx + 1) * k):
            for y in range(cy * k, (cy + 1) * k):
                yield z, x, y


# Stage 1's local area: the English Channel from the Scillies to the Dover Strait.
CHANNEL = Area("channel", (-6.5, 48.3, 2.5, 51.3), 6, 14)
# The world overview: every tile z0-5.
WORLD = Area("world", (-180.0, -MAX_LAT, 180.0, MAX_LAT), 0, 5)
# The golden fixture: one tile per zoom, z6-14, over Cherbourg harbour (z14 tile 8118/5580).
FIXTURE = Area("fixture-cherbourg", (-1.6255, 49.6539, -1.6045, 49.6671), 6, 14)

AREAS = {a.name: a for a in (CHANNEL, WORLD, FIXTURE)}


def cell_id(x: int, y: int) -> str:
    return f"{CELL_ZOOM}-{x}-{y}"


def parse_cell(cell: str) -> tuple[int, int]:
    try:
        z, x, y = (int(p) for p in cell.split("-"))
    except ValueError:
        raise ValueError(f"cell {cell!r} is not {CELL_ZOOM}-X-Y") from None
    n = 1 << CELL_ZOOM
    if z != CELL_ZOOM or not (0 <= x < n and 0 <= y < n) or cell != cell_id(x, y):
        raise ValueError(f"cell {cell!r} is not a z{CELL_ZOOM} tile id")
    return x, y


def cell_area(cell: str) -> Area:
    x, y = parse_cell(cell)
    return Area(cell, tile_bounds(CELL_ZOOM, x, y), CELL_ZOOM, MAX_ZOOM, cell=(x, y))


def cells_over(bbox) -> list[str]:
    """The z6 cells a box touches."""
    return [cell_id(x, y) for _, x, y in sorted(tiles_in(bbox, CELL_ZOOM))]


def lon_to_x(lon: float, z: int) -> int:
    n = 1 << z
    return min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))


def lat_to_y(lat: float, z: int) -> int:
    n = 1 << z
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    r = math.radians(lat)
    return min(n - 1, max(0, int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)))


def tiles_in(bbox, z: int):
    w, s, e, n = bbox
    for x in range(lon_to_x(w, z), lon_to_x(e, z) + 1):
        for y in range(lat_to_y(n, z), lat_to_y(s, z) + 1):
            yield z, x, y


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """west, south, east, north of a tile in degrees."""
    n = 1 << z

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def world_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    """A position in tile units at zoom z: tile (x, y) spans [x, x+1) * EXTENT."""
    n = 1 << z
    r = math.radians(max(-MAX_LAT, min(MAX_LAT, lat)))
    return (lon + 180.0) / 360.0 * n * EXTENT, (
        1.0 - math.asinh(math.tan(r)) / math.pi
    ) / 2.0 * n * EXTENT


def human(n: float) -> str:
    return f"{n / 1e6:.2f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB"


# Europe's and the UK's coasts (the owner's coverage decision, 2026-11-07;
# Tactician docs/offline-charts-plan.md, "Stage 2 coverage"): the 71 z6 cells
# with a European shoreline point, derived from GSHHG by
# scripts/europe_chart_cells.py. No Atlantic islands, Iceland, the Faroes,
# Svalbard or Jan Mayen; no North Africa, Levant or Anatolia; the Black Sea's
# European shore to Sochi; Cyprus in; Kola and the White Sea out. Their
# measured sizes are in docs/chart-pack-format.md ("Published builds").
EUROPE_COAST = (
    "6-30-18",
    "6-30-19",
    "6-30-20",
    "6-30-21",
    "6-30-23",
    "6-30-24",
    "6-30-25",
    "6-31-18",
    "6-31-19",
    "6-31-20",
    "6-31-21",
    "6-31-22",
    "6-31-23",
    "6-31-24",
    "6-31-25",
    "6-32-17",
    "6-32-18",
    "6-32-19",
    "6-32-20",
    "6-32-21",
    "6-32-23",
    "6-32-24",
    "6-33-16",
    "6-33-17",
    "6-33-18",
    "6-33-19",
    "6-33-20",
    "6-33-23",
    "6-33-24",
    "6-34-14",
    "6-34-15",
    "6-34-16",
    "6-34-17",
    "6-34-18",
    "6-34-19",
    "6-34-20",
    "6-34-22",
    "6-34-23",
    "6-34-24",
    "6-34-25",
    "6-35-13",
    "6-35-14",
    "6-35-15",
    "6-35-16",
    "6-35-17",
    "6-35-18",
    "6-35-19",
    "6-35-20",
    "6-35-23",
    "6-35-24",
    "6-35-25",
    "6-36-13",
    "6-36-14",
    "6-36-16",
    "6-36-17",
    "6-36-18",
    "6-36-19",
    "6-36-23",
    "6-36-24",
    "6-36-25",
    "6-37-13",
    "6-37-14",
    "6-37-18",
    "6-37-22",
    "6-37-23",
    "6-37-24",
    "6-37-25",
    "6-38-22",
    "6-38-23",
    "6-38-25",
    "6-39-23",
)

# Named cell sets a run builds. `stage1` is phase 6's: the cells under stage
# 1's Channel area (6.5W-2.5E, 48.3N-51.3N). `europe` is the coast's cells
# and stage 1's inland 6-32-22, which the Channel's race areas reach.
CELL_SETS = {
    "stage1": cells_over(CHANNEL.bbox),
    "europe": sorted(set(EUROPE_COAST) | set(cells_over(CHANNEL.bbox))),
}
