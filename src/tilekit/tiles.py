"""10°x10° geographic tiling. Tile ids name the SW corner (N40W010 = lat
[40,50), lon [-10,0)), half-open on both axes; longitudes normalized to
[-180, 180). The single lat=90 grid row falls outside every band and is
dropped (no sailing at the exact pole)."""

from __future__ import annotations

import math

TILE_DEG = 10


def tile_id(lat0: float, lon0: float) -> str:
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(int(lat0)):02d}{ew}{abs(int(lon0)):03d}"


def tile_origin(lat: float, lon: float) -> tuple[int, int]:
    """SW corner of the tile containing (lat, lon)."""
    if lon >= 180:
        lon -= 360
    return (
        int(math.floor(lat / TILE_DEG)) * TILE_DEG,
        int(math.floor(lon / TILE_DEG)) * TILE_DEG,
    )


def tiles_for_grid(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float
) -> list[tuple[int, int]]:
    """SW corners of every tile intersecting the given (normalized-lon) extent."""
    lat_lo = int(math.floor(max(lat_min, -90) / TILE_DEG)) * TILE_DEG
    lat_hi = int(math.floor(min(lat_max, 89.999) / TILE_DEG)) * TILE_DEG
    lon_lo = int(math.floor(max(lon_min, -180) / TILE_DEG)) * TILE_DEG
    lon_hi = int(math.floor(min(lon_max, 179.999) / TILE_DEG)) * TILE_DEG
    return [
        (lat0, lon0)
        for lat0 in range(lat_lo, lat_hi + 1, TILE_DEG)
        for lon0 in range(lon_lo, lon_hi + 1, TILE_DEG)
    ]
