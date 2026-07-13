"""Cube -> gzipped PFT1 tiles (10°x10°, deterministic gzip level 9, mtime 0).

Fully-missing tiles (all-land) are skipped — the pipeline never publishes
them (spec: Tiling)."""

from __future__ import annotations

import gzip

import numpy as np

from ingest.cube import ForecastCube, GridMeta, utcnow_iso
from tilekit.codec import DTYPES, encode_tile
from tilekit.tiles import TILE_DEG, tile_id, tiles_for_grid

GZIP_LEVEL = 9


def tile_index_ranges(grid: GridMeta) -> list[tuple[int, int, slice, slice]]:
    """(tile_lat0, tile_lon0, lat_slice, lon_slice) for tiles intersecting the grid."""
    lats = grid.lats()
    lons = grid.lons()
    out = []
    for t_lat0, t_lon0 in tiles_for_grid(lats[0], lats[-1], lons[0], lons[-1]):
        li = np.where((lats >= t_lat0) & (lats < t_lat0 + TILE_DEG))[0]
        lj = np.where((lons >= t_lon0) & (lons < t_lon0 + TILE_DEG))[0]
        if len(li) and len(lj):
            out.append((t_lat0, t_lon0, slice(li[0], li[-1] + 1), slice(lj[0], lj[-1] + 1)))
    return out


def _has_data(arr: np.ndarray, dtype: str) -> bool:
    if np.issubdtype(arr.dtype, np.floating):
        return bool(np.any(~np.isnan(arr)))
    _, sentinel = DTYPES[dtype]
    return bool(np.any(arr != sentinel))


def build_tiles(cube: ForecastCube, *, generated_at: str | None = None) -> list[tuple[str, bytes]]:
    """Encode every non-empty tile of the cube; returns (tile_id, gzipped PFT1)."""
    generated_at = generated_at or utcnow_iso()
    base_header = {
        "spec": "PFT1",
        "schema_version": 1,
        "layer": cube.layer,
        "model": cube.model,
        "run_id": cube.run_id,
        "cycle": cube.cycle_iso,
        "generated_at": generated_at,
        "dlat": cube.grid.dlat,
        "dlon": cube.grid.dlon,
        "member_count": cube.member_count,
        "time_axes": cube.header_time_axes(),
        "provenance": cube.provenance,
    }
    variables = [v.public() for v in cube.variables]

    tiles: list[tuple[str, bytes]] = []
    for t_lat0, t_lon0, li, lj in tile_index_ranges(cube.grid):
        tile_arrays: dict[str, np.ndarray] = {}
        any_data = False
        for spec in cube.variables:
            arr = np.ascontiguousarray(cube.arrays[spec.name][..., li, lj])
            tile_arrays[spec.name] = arr
            if not any_data and _has_data(arr, spec.dtype):
                any_data = True
        if not any_data:
            continue  # fully-missing (all-land) tile: not published
        first = tile_arrays[cube.variables[0].name]
        lats = cube.grid.lats()[li]
        lons = cube.grid.lons()[lj]
        header = {
            **base_header,
            "tile_id": tile_id(t_lat0, t_lon0),
            "lat0": float(lats[0]),
            "lon0": float(lons[0]),
            "nlat": first.shape[-2],
            "nlon": first.shape[-1],
            "variables": variables,
        }
        buf = encode_tile(header, tile_arrays)
        tiles.append((header["tile_id"], gzip.compress(buf, GZIP_LEVEL, mtime=0)))
    return tiles
