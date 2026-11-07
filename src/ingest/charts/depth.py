"""EMODnet DTM 2024: the pack's depth layer and the sea-distance grid behind
the coastal mask.

Reads the whole-degree blocks the land index caches and fetches a missing one
the same way, through `ingest.land.emodnet` (960 x 960 cells, north up,
elevation against LAT, positive above; land is carried too).

Depth is contoured per output tile from a window of the grid, so every tile is
cut from the same cells as its neighbours and nothing is clipped afterwards.
Grown from spikes/chart-pilots/tools/build_depth.py (2026-11-06). Vendored from
tactician `tools/charts/depth.py` (main at eaf6d88, 2026-11-07); its own WCS
copy and tifffile reader gave way to `ingest.land.emodnet`, whose reader
returns the same values on all 400 cached blocks (checked 2026-11-07).
"""

from __future__ import annotations

import hashlib
import math

import contourpy
import numpy as np
import shapely
from scipy import ndimage

from ingest.charts.common import EXTENT, NM, land_cache_dir, tile_bounds, world_px
from ingest.charts.mvt import LINESTRING, POLYGON, LayerWriter, lines_geometry, ring_area
from ingest.land import emodnet

CELLS = emodnet.NATIVE_CELLS_PER_DEG  # per degree: 1/16 arc-minute, about 115 m
ATTRIBUTION = emodnet.ATTRIBUTION
PRODUCT = emodnet.PRODUCT

# Depth bands (metres below LAT) and contour levels, as the pilots' option E draws them.
BANDS = [(0, 2), (2, 5), (5, 10), (10, 20), (20, 50), (50, 1e5)]
CONTOURS = [0, 2, 5, 10, 20, 30, 50, 100]
DEPTH_MAX_ZOOM = 12
BUFFER = 64  # tile units (of 4096) drawn past each tile edge


class Grid:
    """Elevation (m, positive above LAT) over whole-degree blocks, north up."""

    def __init__(self, bbox, margin: float = 0.25, log=print, cache=None):
        cache = cache or land_cache_dir()
        w, s, e, n = bbox
        self.west = math.floor(w - margin)
        self.east = math.ceil(e + margin)
        self.south = math.floor(s - margin)
        self.north = math.ceil(n + margin)
        rows = []
        digest = hashlib.sha256()
        fetched = 0
        for bs in range(self.north - 1, self.south - 1, -1):
            row = []
            for bw in range(self.west, self.east):
                if not emodnet.block_path(cache, bw, bs).is_file():
                    fetched += 1
                    log(f"  EMODnet block {bw},{bs}: fetching (about 21 s)")
                a = emodnet.fetch_block(cache, bw, bs).values
                digest.update(f"{bw},{bs}:".encode() + a.tobytes())
                row.append(a)
            rows.append(np.hstack(row))
        self.elev = np.vstack(rows)
        self.blocks = (self.east - self.west) * (self.north - self.south)
        self.fetched = fetched
        # what the grid holds, for a cell's input identity
        self.digest = digest.hexdigest()
        self._strided: dict[int, tuple] = {}

    # ---- the coastal mask ----

    def sea_distance(self, coarse: int = 4):
        """Distance to the open sea in NM on a grid `coarse` times coarser.
        Sea is below LAT and connected to a large body of water, so an inland
        hollow below sea level is not sea."""
        h, w = self.elev.shape
        e = self.elev[: h - h % coarse, : w - w % coarse]
        sea = (e < 0).reshape(h // coarse, coarse, w // coarse, coarse).any(axis=(1, 3))
        labels, n = ndimage.label(sea)
        if n:
            sizes = ndimage.sum(sea, labels, index=np.arange(1, n + 1))
            sea = np.isin(labels, 1 + np.flatnonzero(sizes >= 2000))
        lat_mid = (self.north + self.south) / 2
        dy = 1852.0 * 60 / CELLS * coarse  # metres per coarse cell, north-south
        dx = dy * math.cos(math.radians(lat_mid))
        dist = ndimage.distance_transform_edt(~sea, sampling=(dy, dx)) / NM
        self._dist = (dist.astype(np.float32), coarse)
        return self._dist

    def tile_sea_distance(self, z: int, x: int, y: int) -> float:
        """The shortest distance (NM) from any part of the tile to the sea."""
        dist, coarse = self._dist
        w, s, e, n = tile_bounds(z, x, y)
        per = CELLS / coarse
        c0 = int(math.floor((w - self.west) * per))
        c1 = int(math.ceil((e - self.west) * per))
        r0 = int(math.floor((self.north - n) * per))
        r1 = int(math.ceil((self.north - s) * per))
        H, W = dist.shape
        c0, c1, r0, r1 = max(0, c0), min(W, c1), max(0, r0), min(H, r1)
        if c0 >= c1 or r0 >= r1:
            return math.inf
        return float(dist[r0:r1, c0:c1].min())

    # ---- depth tiles ----

    def _grid_for(self, stride: int):
        if stride not in self._strided:
            if stride == 1:
                g = -self.elev
            else:
                h, w = self.elev.shape
                e = self.elev[: h - h % stride, : w - w % stride]
                g = -e.reshape(h // stride, stride, w // stride, stride).mean(axis=(1, 3))
            per = CELLS / stride
            lon = self.west + (np.arange(g.shape[1]) + 0.5) / per
            lat = self.north - (np.arange(g.shape[0]) + 0.5) / per
            self._strided[stride] = (g.astype(np.float64), lon, lat, per)
        return self._strided[stride]

    @staticmethod
    def stride_for(z: int) -> int:
        # about one grid cell per screen pixel of a 512 px tile, never finer than native
        px_deg = 360.0 / (1 << z) / 512
        return max(1, int(px_deg * CELLS))

    def depth_layer(
        self, z: int, x: int, y: int, keep=lambda k: True, simplify: float = 4.0
    ) -> LayerWriter:
        """The `depth` layer of one tile: `band` polygons (kind, min: the band's
        shallow edge in m; the bands are BANDS) and `contour` lines (kind, depth in m; 0 is the drying line). Above
        DEPTH_MAX_ZOOM the z12 grid is used, so z13-14 carry z12's detail."""
        layer = LayerWriter("depth", keep=keep)
        stride = self.stride_for(min(z, DEPTH_MAX_ZOOM))
        g, lon, lat, per = self._grid_for(stride)
        w, s, e, n = tile_bounds(z, x, y)
        pad_deg = (e - w) * (BUFFER / EXTENT) + 2.0 / per
        c0 = max(0, int(math.floor((w - pad_deg - self.west) * per - 0.5)))
        c1 = min(g.shape[1], int(math.ceil((e + pad_deg - self.west) * per + 0.5)) + 1)
        r0 = max(0, int(math.floor((self.north - n - pad_deg * 0.7) * per - 0.5)))
        r1 = min(g.shape[0], int(math.ceil((self.north - s + pad_deg * 0.7) * per + 0.5)) + 1)
        if c1 - c0 < 2 or r1 - r0 < 2:
            return layer
        win = g[r0:r1, c0:c1]
        if not np.isfinite(win).any() or np.nanmax(win) <= 0:
            return layer  # land or no data only
        # positions in this tile's units
        ox, oy = x * EXTENT, y * EXTENT
        px = np.array([world_px(lo, 0.0, z)[0] - ox for lo in lon[c0:c1]])
        py = np.array([world_px(0.0, la, z)[1] - oy for la in lat[r0:r1]])
        win = np.where(np.isfinite(win), win, -999.0)
        gen = contourpy.contour_generator(
            px, py, win, line_type="Separate", fill_type="OuterOffset"
        )
        lo_clip, hi_clip = -BUFFER, EXTENT + BUFFER
        wmin, wmax = float(win.min()), float(win.max())
        for lo, hi in BANDS:
            if wmax < lo or wmin >= hi:
                continue
            points, offsets = gen.filled(lo, hi)
            polys = []
            for pts, off in zip(points, offsets):
                rings = [pts[off[i] : off[i + 1]] for i in range(len(off) - 1)]
                if len(rings[0]) < 4:
                    continue
                polys.append(shapely.Polygon(rings[0], [r for r in rings[1:] if len(r) >= 4]))
            if not polys:
                continue
            parts = []
            for poly in polys:
                g = shapely.simplify(poly, simplify, preserve_topology=True)
                if not g.is_valid:
                    g = shapely.make_valid(g, method="structure", keep_collapsed=False)
                parts += _polygons(shapely.clip_by_rect(g, lo_clip, lo_clip, hi_clip, hi_clip))
            rings = []
            for poly in parts:
                ext = _ints(poly.exterior.coords)
                if len(ext) < 3 or abs(ring_area(ext)) < 2:
                    continue
                rings.append(_oriented(ext, outer=True))
                for hole in poly.interiors:
                    h = _ints(hole.coords)
                    if len(h) >= 3 and abs(ring_area(h)) >= 2:
                        rings.append(_oriented(h, outer=False))
            if rings:
                layer.add(POLYGON, lines_geometry(rings, closed=True), {"kind": "band", "min": lo})
        for level in CONTOURS:
            if not (wmin < level < wmax):
                continue
            lines = []
            for line in gen.lines(level):
                if len(line) < 2:
                    continue
                geom = shapely.simplify(shapely.LineString(line), simplify)
                geom = shapely.clip_by_rect(geom, lo_clip, lo_clip, hi_clip, hi_clip)
                for part in _lines(geom):
                    pts = _ints(part.coords)
                    if len(pts) >= 2 and _length(pts) >= 8:
                        lines.append(pts)
            if lines:
                layer.add(LINESTRING, lines_geometry(lines), {"kind": "contour", "depth": level})
        return layer


def _polygons(g):
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        out = []
        for part in g.geoms:
            out += _polygons(part)
        return out
    return []


def _lines(g):
    if g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    if g.geom_type in ("MultiLineString", "GeometryCollection"):
        out = []
        for part in g.geoms:
            out += _lines(part)
        return out
    return []


def _ints(coords):
    out = []
    for x, y in coords:
        p = (int(round(x)), int(round(y)))
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _oriented(ring, outer: bool):
    # MVT: an exterior ring has positive area in tile coordinates (y down)
    a = ring_area(ring)
    return ring if (a > 0) == outer else ring[::-1]


def _length(pts):
    return sum(math.hypot(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(pts, pts[1:]))
