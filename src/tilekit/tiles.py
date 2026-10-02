"""10°x10° geographic tiling. Tile ids name the SW corner (N40W010 = lat
[40,50), lon [-10,0)), half-open on both axes; longitudes normalized to
[-180, 180). The single lat=90 grid row falls outside every band and is
dropped (no sailing at the exact pole).

A regional product may use 3° or 5° tiles (5°: N45W005 = lat [45,50),
lon [-5,0)), named the same way. Both divide the 180°/360° geographic axes.
Every existing layer keeps
10°, and with it its bytes and URLs (docs/open-meteo-bulk-implementation-plan.md
→ Browser budgets and tile layout)."""

from __future__ import annotations

import math

TILE_DEG = 10
SUPPORTED_TILE_DEG = (3, 5, 10)


def tile_id(lat0: float, lon0: float) -> str:
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(int(lat0)):02d}{ew}{abs(int(lon0)):03d}"


def _check(tile_deg: int) -> int:
    if tile_deg not in SUPPORTED_TILE_DEG:
        raise ValueError(f"tile_deg {tile_deg} not in {SUPPORTED_TILE_DEG}")
    return tile_deg


def tile_origin(lat: float, lon: float, tile_deg: int = TILE_DEG) -> tuple[int, int]:
    """SW corner of the tile containing (lat, lon)."""
    d = _check(tile_deg)
    if lon >= 180:
        lon -= 360
    return (
        int(math.floor(lat / d)) * d,
        int(math.floor(lon / d)) * d,
    )


def tiles_for_grid(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float, tile_deg: int = TILE_DEG
) -> list[tuple[int, int]]:
    """SW corners of every tile intersecting the given (normalized-lon) extent."""
    d = _check(tile_deg)
    lat_lo = int(math.floor(max(lat_min, -90) / d)) * d
    lat_hi = int(math.floor(min(lat_max, 89.999) / d)) * d
    lon_lo = int(math.floor(max(lon_min, -180) / d)) * d
    lon_hi = int(math.floor(min(lon_max, 179.999) / d)) * d
    return [
        (lat0, lon0)
        for lat0 in range(lat_lo, lat_hi + 1, d)
        for lon0 in range(lon_lo, lon_hi + 1, d)
    ]
