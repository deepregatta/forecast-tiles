"""Build TCP1 chart archives: the z0-5 world overview, z6 cells, and local area packs.

Vendored from tactician `tools/charts/build.py` (main at eaf6d88, 2026-11-07),
the stage 1 builder of WP10, and cut into cells for stage 2 (phase 6):

- `world`: z0-5 over the whole world.
- `cell`: one z6 tile, z6-14 over exactly its own descendants, with its depth
  companion (z6-12). Cells never overlap and never read the world build: every
  tile carries the `cover` square the style hides the overview's land under,
  so core can fold any set of cells and the world into one area pack.
- `area`: stage 1's local pack (an area's box, z6-14, with the world folded in
  below z6), and the golden fixture (without it). Nothing here publishes.

Per tile: Protomaps' earth, water, places and boundaries (roads only from z12
near the sea), Seamap's seamark, light and sea_area with SHOM's marks merged in,
and EMODnet depth to z12; properties cut to style-fields.json. Above z10, tiles
farther than the coastal-mask distance from the sea hold one shared land tile,
or keep their content over water the basemap draws. Format:
docs/chart-pack-format.md.

Grown from tactician spikes/chart-pilots/tools (extract_pmtiles.py,
build_marks.py, build_e_tiles.py, build_depth.py; 2026-11-06). Left behind in
tactician: the measuring run (`--measure`, `measure.py`), whose phase 1 tables
are dated evidence there, and the folded-depth packaging it priced.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
import os
import time
import unicodedata
from collections import Counter, defaultdict
from multiprocessing import get_context
from pathlib import Path

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from ingest.charts import mvt, sources
from ingest.charts import shom as shom_mod
from ingest.charts.common import (
    CHANNEL,
    CELL_ZOOM,
    EXTENT,
    SCHEMA_VERSION,
    SPEC,
    WORLD,
    Area,
    human,
    lat_to_y,
    lon_to_x,
    tile_bounds,
)
from ingest.charts.depth import ATTRIBUTION as EMODNET_ATTRIBUTION
from ingest.charts.depth import DEPTH_MAX_ZOOM, PRODUCT as EMODNET_PRODUCT, Grid

HERE = Path(__file__).resolve().parent
STYLE_FIELDS = HERE / "style-fields.json"
BUILDER = "forecast-tiles src/ingest/charts (vendored from tactician tools/charts, 2026-11-07)"
# The files whose content decides what a build writes: a change to any of
# them changes every input identity, so no cell is reused across it.
RECIPE_FILES = ("build.py", "common.py", "depth.py", "mvt.py", "shom.py", "style-fields.json")

BASE_LAYERS = ["earth", "water", "boundaries", "roads", "places"]
SEAMAP_LAYERS = ["sea_area", "seamark", "light"]
LAYER_ORDER = [
    "cover",
    "earth",
    "water",
    "boundaries",
    "roads",
    "depth",
    "sea_area",
    "places",
    "seamark",
    "light",
]
MASK_FROM_ZOOM = 11  # the coastal mask applies above z10
ROADS_FROM_ZOOM = 12
DUPLICATE_M = 80.0  # an OSM aid this close to a SHOM mark is the same mark
CELL = EXTENT // 16  # Seamap's cell_rank cell: 32 px of a 512 px tile
SHOM_LANDMARK_RANK = (
    3  # SHOM's landmarks (mostly churches inland) yield to everything in their cell
)
AID_FAMILIES = {"major_aid", "minor_aid"}
SECTOR_MIN_ZOOM = 8  # Seamap's own `light` layer starts at z8


# Which roads a tile from z12 near the sea carries. Piers always stay: they are
# harbour structure a crew steers by.
def _pier(p):
    return p.get("kind_detail") == "pier"


ROAD_RULES = {
    "all": lambda p: True,
    "no-paths": lambda p: p.get("kind") != "path" or _pier(p),
    "streets": lambda p: (
        (p.get("kind") != "path" and p.get("kind_detail") != "service") or _pier(p)
    ),
    "major": lambda p: p.get("kind") in ("highway", "major_road", "rail") or _pier(p),
}

# Defaults chosen from the 2026-11-07 measurements (tactician docs/offline-charts-plan.md).
DEFAULT_MASK_NM = 2.0
DEFAULT_ROADS_NM = 1.0
DEFAULT_ROADS = "streets"
# A cell's SHOM marks are read this far past its box, so an OSM copy of a mark
# just across a cell edge is still dropped (DUPLICATE_M is about 0.001 deg).
SHOM_MARGIN_DEG = 0.01


def latin(text) -> bool:
    """Whether a name is Latin as the style can draw it: every letter a Latin
    letter and no combining mark left once composed (MapLibre shapes no
    combining marks). Seamap's names carry no script and no English name, so
    a name in another script is dropped and its feature kept: the style ships
    Latin glyphs only (Protomaps labels are Latin by the style's own rule)."""
    if not isinstance(text, str):
        return True
    for ch in unicodedata.normalize("NFC", text):
        cat = unicodedata.category(ch)
        if cat.startswith("M") or (
            cat.startswith("L") and not unicodedata.name(ch, "").startswith("LATIN")
        ):
            return False
    return True


def latin_names(p: dict) -> dict:
    """Seamap's properties with a name the style cannot draw left out."""
    if "name" in p and not latin(p["name"]):
        del p["name"]
    elif isinstance(p.get("name"), str):
        p["name"] = unicodedata.normalize("NFC", p["name"])
    return p


def drop_base(name: str, gtype: int, p: dict) -> bool:
    """Base features no chart layer draws. Ocean polygons only repeat the
    coastline under the depth bands (the sea is the background); their label
    points stay."""
    return name == "water" and gtype == mvt.POLYGON and p.get("kind") == "ocean"


NOT_FOR_NAVIGATION = (
    "Not for navigation. A picture of the race area for the crew, built from open data "
    "(OpenStreetMap, Open Waters Seamap, SHOM Balisage, EMODnet Bathymetry), none of which "
    "is an official chart; EMODnet: DO NOT USE FOR NAVIGATION."
)
LICENCES = {
    "protomaps": ("ODbL-1.0", "© Protomaps © OpenStreetMap contributors"),
    "seamap": (
        "CC-BY-4.0 (tiles; being corrected to ODbL); data ODbL-1.0",
        "© Open Waters: Seamap © OpenStreetMap contributors",
    ),
    "shom": ("Licence Ouverte 2.0", "Balisage © Shom"),
    "emodnet": ("CC-BY-4.0", EMODNET_ATTRIBUTION),
}


def log(*a):
    print(*a, flush=True)


def style_fields() -> dict:
    """The properties the style reads, vendored from tactician
    `tools/charts/style-fields.json` (which its `style_fields.mjs` derives from
    the style's pinned packages); see VENDORED.md."""
    return json.loads(STYLE_FIELDS.read_text())


# ---------------------------------------------------------------- per tile

G: dict = {}  # worker globals, set before the pool forks


def _shom_tile(z, x, y, extent):
    """SHOM features for one tile, in tile units."""
    marks, lines = [], []
    for i in G["shom_index"].get((z, x, y), ()):
        lon, lat, p = G["shom_marks"][i]
        px, py = mvt.tile_point(lon, lat, z, x, y)
        if 0 <= px < EXTENT and 0 <= py < EXTENT:
            s = extent / EXTENT
            marks.append(((int(px * s), int(py * s)), dict(p)))
    pad = EXTENT // 8
    for i in G["sector_index"].get((z, x, y), ()):
        coords, p = G["shom_sectors"][i]
        pts = [mvt.tile_point(lon, lat, z, x, y) for lon, lat in coords]
        if any(-pad <= px <= EXTENT + pad and -pad <= py <= EXTENT + pad for px, py in pts):
            s = extent / EXTENT
            lines.append(([(int(round(px * s)), int(round(py * s))) for px, py in pts], dict(p)))
    return marks, lines


def _rerank(feats, extent):
    """Recompute Seamap's cell_rank where a SHOM mark joined a cell: SHOM first,
    then the cell's own features in their old order. Other cells keep theirs."""
    cell = CELL * extent // EXTENT
    cells = defaultdict(list)
    for f in feats:
        gtype, geom, p, shom = f
        if gtype != mvt.POINT or "family" not in p:
            continue
        pts = mvt.decode_points(geom)
        if not pts:
            continue
        px, py = pts[0]
        cells[(p["family"], px // cell, py // cell)].append(f)
    for fs in cells.values():
        if not any(f[3] for f in fs):
            continue
        fs.sort(key=lambda f: (0 if f[3] else 1, f[2].get("cell_rank") or 0))
        for i, f in enumerate(fs):
            late = f[3] and f[2].get("type") == "landmark"
            f[2]["cell_rank"] = i + SHOM_LANDMARK_RANK if late else i


def process(task):
    """One tile -> (zxy, chart tile, depth-only tile)."""
    z, x, y, pm_data, sm_data, dist = task
    keep = G["keep"]
    opts = G["opts"]
    hidden = G["hidden"]
    roads_ok = z >= ROADS_FROM_ZOOM and dist <= opts["roads_nm"]
    writers = {}
    if opts["cover"]:
        writers["cover"] = cover_layer()
    if pm_data:
        for layer in mvt.parse(pm_data).layers:
            if layer.name not in BASE_LAYERS or (layer.name == "roads" and not roads_ok):
                continue
            name = layer.name
            if name == "roads":
                rule = ROAD_RULES[opts["roads"]]
                w = writers["roads"] = mvt.LayerWriter("roads", layer.extent)
                mvt.copy_layer(layer, w, keep["roads"], drop=lambda gt, p, rule=rule: not rule(p))
                continue
            w = writers[name] = mvt.LayerWriter(name, layer.extent)
            mvt.copy_layer(
                layer, w, keep[name], drop=lambda gt, p, name=name: drop_base(name, gt, p)
            )
    shom_on = G["shom_marks"] is not None
    if sm_data or shom_on:
        layers = {l.name: l for l in mvt.parse(sm_data).layers} if sm_data else {}
        extent = layers["seamark"].extent if "seamark" in layers else EXTENT
        marks, lines = _shom_tile(z, x, y, extent) if shom_on else ([], [])
        if "sea_area" in layers:
            src = layers["sea_area"]
            w = writers["sea_area"] = mvt.LayerWriter("sea_area", src.extent, keep=keep["sea_area"])
            for f in src.features:
                w.add(f.type, list(f.geometry), latin_names(mvt.props(src, f)))
        # seamark: drop the OSM copies of SHOM marks, add SHOM's, rerank
        feats = []
        if "seamark" in layers:
            src = layers["seamark"]
            for f in src.features:
                p = mvt.props(src, f)
                if p.get("osm_id") not in hidden:
                    feats.append([f.type, list(f.geometry), latin_names(p), False])
        for (px, py), p in marks:
            feats.append([mvt.POINT, mvt.point_geometry([(px, py)]), p, True])
        if marks:
            _rerank(feats, extent)
        if feats:
            k = keep["seamark"]
            w = writers["seamark"] = mvt.LayerWriter("seamark", extent)
            for gtype, geom, p, _ in feats:
                w.add(gtype, geom, {kk: v for kk, v in p.items() if k(kk)})
        lfeats = []
        if "light" in layers:
            src = layers["light"]
            for f in src.features:
                p = mvt.props(src, f)
                if p.get("osm_id") not in hidden:
                    lfeats.append((f.type, list(f.geometry), p))
        for pts, p in lines:
            lfeats.append((mvt.LINESTRING, mvt.lines_geometry([pts]), p))
        lfeats = [f for f in lfeats if f[1]]
        if lfeats:
            k = keep["light"]
            w = writers["light"] = mvt.LayerWriter(
                "light", layers["light"].extent if "light" in layers else extent
            )
            for gtype, geom, p in lfeats:
                w.add(gtype, geom, {kk: v for kk, v in p.items() if k(kk)})
    if G["grid"] is not None and z <= DEPTH_MAX_ZOOM:
        d = G["grid"].depth_layer(z, x, y, keep=keep["depth"])
        if len(d):
            writers["depth"] = d

    def enc(names):
        names = [n for n in LAYER_ORDER if n in writers and n in names]
        return gzip.compress(mvt.encode([writers[n] for n in names]), 6, mtime=0) if names else None

    # depth goes to its own archive (the 2026-11-07 packaging decision)
    chart = enc(set(writers) - {"depth"})
    depth_tile = enc({"depth"}) if z <= DEPTH_MAX_ZOOM else None
    return (z, x, y), chart, depth_tile


# ---------------------------------------------------------------- inputs


def build_hidden(seamap_tiles: dict, marks) -> set:
    """OSM ids of Seamap aids within DUPLICATE_M of a SHOM mark, found at z14
    where positions are exact to a metre."""
    cellsz = 0.002
    index = defaultdict(list)
    for lon, lat, _ in marks:
        index[(int(lon // cellsz), int(lat // cellsz))].append((lon, lat))
    hidden = set()
    for (z, x, y), data in seamap_tiles.items():
        if z != 14:
            continue
        t = mvt.parse(data)
        for layer in t.layers:
            if layer.name != "seamark":
                continue
            ext = layer.extent
            w, s, e, n = tile_bounds(z, x, y)
            for f in layer.features:
                if f.type != mvt.POINT:
                    continue
                p = mvt.props(layer, f)
                if p.get("family") not in AID_FAMILIES and p.get("type") != "landmark":
                    continue
                for px, py in mvt.decode_points(list(f.geometry))[:1]:
                    lon = w + (e - w) * px / ext
                    # latitude through Mercator
                    ty = (y + py / ext) / (1 << z)
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty))))
                    cx, cy = int(lon // cellsz), int(lat // cellsz)
                    near = False
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            for slon, slat in index.get((cx + dx, cy + dy), ()):
                                if (
                                    math.hypot(
                                        (slon - lon) * 111_320 * math.cos(math.radians(lat)),
                                        (slat - lat) * 110_540,
                                    )
                                    < DUPLICATE_M
                                ):
                                    near = True
                    if near and p.get("osm_id") is not None:
                        hidden.add(p["osm_id"])
    return hidden


def index_shom(marks, sectors, zooms):
    mi, si = defaultdict(list), defaultdict(list)
    for i, (lon, lat, _) in enumerate(marks):
        for z in zooms:
            mi[(z, lon_to_x(lon, z), lat_to_y(lat, z))].append(i)
    for i, (coords, _) in enumerate(sectors):
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        for z in (z for z in zooms if z >= SECTOR_MIN_ZOOM):
            # a line is offered to every tile its box touches, plus one tile of margin
            x0, x1 = lon_to_x(min(lons), z) - 1, lon_to_x(max(lons), z) + 1
            y0, y1 = lat_to_y(max(lats), z) - 1, lat_to_y(min(lats), z) + 1
            for xx in range(x0, x1 + 1):
                for yy in range(y0, y1 + 1):
                    si[(z, xx, yy)].append(i)
    return mi, si


# ---------------------------------------------------------------- packs


def plan(area, grid, mask_nm):
    """The tiles of an area pack with their sea distance, and the tiles the
    coastal mask leaves out (above z10, farther than mask_nm from the sea)."""
    out, masked = [], []
    for z in range(area.min_zoom, area.max_zoom + 1):
        for t in area.tiles(z):
            d = grid.tile_sea_distance(*t) if grid is not None else 0.0
            (masked if z >= MASK_FROM_ZOOM and d > mask_nm else out).append((t, d))
    return out, masked


# One tile stands in for every masked tile: the whole tile land, its edges
# past the tile's own (renderers clip a tile's drawing to the tile, so the
# coastline layer draws no square). MapLibre Native, and GL JS on a 404, draw
# a missing tile as an empty one, i.e. as sea; PMTiles stores this tile once
# and run-length codes the runs of it.
LAND_PAD = 64
LAND_GEOMETRY = mvt.lines_geometry(
    # clockwise on screen (y down): an exterior ring
    [
        [
            (-LAND_PAD, -LAND_PAD),
            (EXTENT + LAND_PAD, -LAND_PAD),
            (EXTENT + LAND_PAD, EXTENT + LAND_PAD),
            (-LAND_PAD, EXTENT + LAND_PAD),
        ]
    ],
    closed=True,
)


def land_tile() -> bytes:
    w = mvt.LayerWriter("earth", EXTENT)
    w.add(mvt.POLYGON, LAND_GEOMETRY, {"kind": "earth"})
    return gzip.compress(mvt.encode([w]), 6, mtime=0)


def cover_layer() -> mvt.LayerWriter:
    """The `cover` layer of an area tile: one square, the land tile's, with no
    properties. Beyond an area's box the style draws the land of the pack's
    own z0-5 overview, overzoomed, since a renderer draws a tile the pack
    lacks as the sea (MapLibre Native, 2026-11-07). That land is coarse, so
    inside the box each tile paints its cover in the sea colour over it and
    draws its own land on top; tiles clip their drawing to their edges, so
    the change from overview to area falls on a tile edge, with no seam.
    Identical sea tiles stay identical, and PMTiles still stores them once."""
    w = mvt.LayerWriter("cover", EXTENT)
    w.add(mvt.POLYGON, LAND_GEOMETRY, {})
    return w


class LandTest:
    """Whether a masked tile is wholly land in the basemap, by its z10 tile's
    earth polygons (the OSM coastline, simplified at z10). The coastal mask's
    sea is EMODnet below LAT, so a drying bay (Baie du Mont-Saint-Michel, Baie
    des Veys) can lie past the mask while OSM draws it as water: such a tile
    keeps its own content, and only tiles wholly ashore take the land tile.
    Lakes and rivers ashore are not water here: past the mask they go with the
    inland labels."""

    def __init__(self, pm_tiles: dict):
        self.pm_tiles = pm_tiles
        self.earth: dict = {}

    def _earth(self, x10, y10):
        key = (x10, y10)
        if key not in self.earth:
            import shapely
            from shapely.prepared import prep

            data = self.pm_tiles.get((LAND_ZOOM, x10, y10))
            polys = []
            if data:
                for layer in mvt.parse(data).layers:
                    if layer.name != "earth":
                        continue
                    s = EXTENT / layer.extent
                    for f in layer.features:
                        if f.type != mvt.POLYGON:
                            continue
                        shell, holes = None, []
                        for ring in mvt.decode_rings(list(f.geometry)):
                            ring = [(px * s, py * s) for px, py in ring]
                            if len(ring) < 3:
                                continue
                            if mvt.ring_area(ring) > 0:
                                if shell is not None:
                                    polys.append(shapely.Polygon(shell, holes))
                                shell, holes = ring, []
                            elif shell is not None:
                                holes.append(ring)
                        if shell is not None:
                            polys.append(shapely.Polygon(shell, holes))
            union = (
                shapely.union_all(shapely.make_valid(shapely.GeometryCollection(polys)))
                if polys
                else None
            )
            self.earth[key] = prep(union) if union is not None and not union.is_empty else None
        return self.earth[key]

    def wholly_land(self, t) -> bool:
        import shapely

        z, x, y = t
        k = 1 << (z - LAND_ZOOM)
        earth = self._earth(x // k, y // k)
        if earth is None:
            return False
        size = EXTENT / k
        x0, y0 = (x % k) * size, (y % k) * size
        return earth.covers(shapely.box(x0, y0, x0 + size, y0 + size))


LAND_ZOOM = MASK_FROM_ZOOM - 1  # the last zoom the mask keeps whole


def write_pmtiles(path: Path, tiles: dict, metadata: dict, bbox, min_zoom, max_zoom, center_zoom):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # the pmtiles writer gzips its directories and metadata with the current
    # time in the gzip header; a zero time keeps a rebuild byte-identical
    real_compress = gzip.compress
    gzip.compress = lambda data, compresslevel=9, *, mtime=0: real_compress(
        data, compresslevel, mtime=0
    )
    try:
        _write(tmp, tiles, metadata, bbox, min_zoom, max_zoom, center_zoom)
    finally:
        gzip.compress = real_compress
    os.replace(tmp, path)


def _write(tmp, tiles, metadata, bbox, min_zoom, max_zoom, center_zoom):
    with open(tmp, "wb") as f:
        w = Writer(f)
        for tid in sorted(tiles):
            w.write_tile(tid, tiles[tid])
        w.finalize(
            {
                "tile_type": TileType.MVT,
                "tile_compression": Compression.GZIP,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
                "min_lon_e7": int(round(bbox[0] * 1e7)),
                "min_lat_e7": int(round(bbox[1] * 1e7)),
                "max_lon_e7": int(round(bbox[2] * 1e7)),
                "max_lat_e7": int(round(bbox[3] * 1e7)),
                "center_zoom": center_zoom,
                "center_lon_e7": int(round((bbox[0] + bbox[2]) / 2 * 1e7)),
                "center_lat_e7": int(round((bbox[1] + bbox[3]) / 2 * 1e7)),
            },
            metadata,
        )


def vector_layers(*infos) -> list:
    """TileJSON vector_layers over one or more layer_info() results."""
    merged = {}
    for info in infos:
        for name, i in info.items():
            m = merged.setdefault(
                name, {"minzoom": i["minzoom"], "maxzoom": i["maxzoom"], "fields": {}}
            )
            m["minzoom"], m["maxzoom"] = (
                min(m["minzoom"], i["minzoom"]),
                max(m["maxzoom"], i["maxzoom"]),
            )
            for k, t in i["fields"].items():
                m["fields"][k] = "|".join(
                    sorted(set(m["fields"].get(k, "").split("|")) - {""} | set(t.split("|")))
                )
    return [
        {
            "id": n,
            "fields": dict(sorted(merged[n]["fields"].items())),
            "minzoom": merged[n]["minzoom"],
            "maxzoom": merged[n]["maxzoom"],
        }
        for n in LAYER_ORDER
        if n in merged
    ]


def recipe() -> str:
    h = hashlib.sha256()
    for name in RECIPE_FILES:
        h.update(name.encode() + b"\0" + (HERE / name).read_bytes() + b"\0")
    return h.hexdigest()


def inputs_digest(identity: dict, pm_tiles: dict, sm_tiles: dict) -> str:
    """What a build read, as one digest: the recipe, the rules, SHOM's records,
    the EMODnet grid and every Protomaps and Seamap tile it cut. Two builds with
    the same digest write the same tiles, so a publisher can keep the older
    archive instead of uploading a copy (docs/chart-pack-format.md)."""
    h = hashlib.sha256(json.dumps({"recipe": recipe(), **identity}, sort_keys=True).encode())
    for name, tiles in (("protomaps", pm_tiles), ("seamap", sm_tiles)):
        for t in sorted(tiles):
            h.update(f"{name}/{t[0]}/{t[1]}/{t[2]}:".encode() + hashlib.sha256(tiles[t]).digest())
    return h.hexdigest()


def utc_build_id(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%MZ")


def run(
    area: Area,
    *,
    kind: str,
    protomaps: str,
    seamap: str,
    out: Path,
    build_id: str,
    built: str,
    shom_tag: str | None = None,
    world: Path | None = None,
    builder: str = BUILDER,
    mask_nm: float = DEFAULT_MASK_NM,
    roads_nm: float = DEFAULT_ROADS_NM,
    roads: str = DEFAULT_ROADS,
    workers: int = 6,
) -> dict:
    """Build one archive (and a depth companion for an area or a cell) at
    `out`; returns the report also written beside it. `protomaps` (YYYYMMDD)
    and `seamap` (YYYY-MM-DD) name the inputs: Open Waters' archive is read
    only at a date the run names."""
    if kind not in ("world", "area", "cell"):
        raise ValueError(f"unknown kind {kind!r}")
    if (kind == "cell") != (area.cell is not None) or (kind == "world") != (area is WORLD):
        raise ValueError(f"{area.name} is not a {kind}")
    if roads not in ROAD_RULES:
        raise ValueError(f"unknown road rule {roads!r}")
    out = Path(out)
    fields = style_fields()
    keep = {name: mvt.keeper(props) for name, props in fields["layers"].items()}
    is_world = kind == "world"
    cover = kind == "cell" or (kind == "area" and world is not None)
    shom_tag = shom_tag or dt.date.today().isoformat()
    t0 = time.time()
    log(f"{area.name}: {kind}, Protomaps {protomaps}, Seamap {seamap}")
    pm = sources.RemoteArchive(
        f"protomaps-{protomaps}", sources.PROTOMAPS_URL.format(date=protomaps)
    )
    sm = sources.RemoteArchive(f"seamap-{seamap}", sources.SEAMAP_URL.format(date=seamap))

    grid = None
    shom = None
    shom_bbox = area.bbox
    if kind == "cell":
        w, s, e, n = area.bbox
        m = SHOM_MARGIN_DEG
        shom_bbox = (round(w - m, 6), round(s - m, 6), round(e + m, 6), round(n + m, 6))
    if not is_world:
        grid = Grid(area.bbox, log=log)
        grid.sea_distance()
        log(
            f"  EMODnet: {grid.blocks} blocks ({grid.fetched} fetched), grid {grid.elev.shape[1]} x {grid.elev.shape[0]}"
        )
        shom = sources.fetch_shom(shom_bbox, shom_tag, log=log)
    planned, masked = plan(area, grid, mask_nm)
    want = [t for t, _ in planned]
    pm_tiles = pm.fetch(want, log=log)
    # a masked tile over water the basemap draws keeps its content
    land = LandTest(pm_tiles)
    wet = [(t, d) for t, d in masked if not land.wholly_land(t)]
    if wet:
        planned += wet
        want += [t for t, _ in wet]
        pm_tiles.update(pm.fetch([t for t, _ in wet], log=log))
    wet_ids = {t for t, _ in wet}
    dry = [t for t, _ in masked if t not in wet_ids]
    by_zoom = lambda ts: dict(sorted(Counter(t[0] for t in ts).items()))  # noqa: E731
    log(
        f"  {len(planned)} tiles z{area.min_zoom}-{area.max_zoom}; masked above z10, land tile: {by_zoom(dry)}; "
        f"past the mask over basemap water, kept: {by_zoom([t for t, _ in wet])}"
    )
    sm_tiles = sm.fetch(want, log=log)
    log(
        f"  inputs: Protomaps {len(pm_tiles)} tiles {human(sum(map(len, pm_tiles.values())))}, "
        f"Seamap {len(sm_tiles)} tiles {human(sum(map(len, sm_tiles.values())))} ({time.time() - t0:.0f} s)"
    )

    marks, sectors, hidden = None, [], set()
    if shom is not None:
        marks, sectors = shom_mod.convert(shom)
        # SHOM marks inside the box only (a cell's box and its margin); the WFS box can return a little more
        w, s, e, n = shom_bbox
        marks = [m for m in marks if w <= m[0] <= e and s <= m[1] <= n]
        near = {
            (14, lon_to_x(lon, 14) + dx, lat_to_y(lat, 14) + dy)
            for lon, lat, _ in marks
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        }
        hidden = build_hidden({t: sm_tiles[t] for t in near if t in sm_tiles}, marks)
        log(
            f"  SHOM: {len(marks)} marks, {len(sectors)} sector lines; {len(hidden)} OSM copies dropped"
        )
    mi, si = index_shom(marks or [], sectors, range(area.min_zoom, area.max_zoom + 1))

    G.clear()
    G.update(
        keep=keep,
        hidden=hidden,
        grid=grid,
        shom_marks=marks,
        shom_sectors=sectors,
        shom_index=mi,
        sector_index=si,
        opts={"roads_nm": roads_nm, "roads": roads, "cover": cover},
    )
    tasks = ((t[0], t[1], t[2], pm_tiles.get(t), sm_tiles.get(t), d) for t, d in planned)
    chart, depth_tiles = {}, {}
    done = 0
    ctx = get_context("fork")
    with ctx.Pool(workers) as pool:
        for (z, x, y), c, dt_ in pool.imap_unordered(process, tasks, chunksize=16):
            tid = zxy_to_tileid(z, x, y)
            if c:
                chart[tid] = c
            if dt_:
                depth_tiles[tid] = dt_
            done += 1
            if done % 5000 == 0:
                log(f"  {done}/{len(planned)} tiles ({time.time() - t0:.0f} s)")
    if dry:
        tile = land_tile()
        for t in dry:
            chart[zxy_to_tileid(*t)] = tile

    present = layer_info(chart)
    srcs = [
        {
            "id": "protomaps",
            "build": protomaps,
            **pm.describe(),
            "licence": LICENCES["protomaps"][0],
        },
        {"id": "seamap", "date": seamap, **sm.describe(), "licence": LICENCES["seamap"][0]},
    ]
    attribution = ["© OpenStreetMap contributors", "© Protomaps", "© Open Waters: Seamap"]
    shom_meta = shom["_meta"] if shom is not None else None
    # a cell with no SHOM mark in it (outside French waters) names no SHOM source
    if shom is not None and (kind != "cell" or marks):
        srcs.append(
            {
                "id": "shom",
                "service": sources.SHOM_WFS,
                "fetched": shom_meta["fetched"],
                "content_sha256": shom_meta["content_sha256"],
                "marks": len(marks),
                "licence": LICENCES["shom"][0],
            }
        )
        attribution.insert(0, f"Balisage © Shom {shom_meta['fetched'][:4]}")
    if grid is not None:
        srcs.append(
            {
                "id": "emodnet",
                "product": EMODNET_PRODUCT,
                "service": "https://ows.emodnet-bathymetry.eu/wcs",
                "vertical_datum": "LAT",
                "licence": LICENCES["emodnet"][0],
                "use_constraint": "DO NOT USE FOR NAVIGATION",
            }
        )
        attribution.append("EMODnet Bathymetry Consortium (2024)")
    base_meta = {
        "spec": SPEC,
        "schema_version": SCHEMA_VERSION,
        "build_id": build_id,
        "built": built,
        "builder": builder,
        "sources": srcs,
        "attribution": " · ".join(attribution),
        "not_for_navigation": NOT_FOR_NAVIGATION,
        "style_fields": {
            "derived_from": fields["style_sources"],
            "generated_by": fields["generated_by"],
        },
        "type": "baselayer",
        "format": "pbf",
    }
    report = {"area": area.name, "kind": kind, "build_id": build_id}
    rules = {"mask_nm": mask_nm, "roads_nm": roads_nm, "roads": roads}
    identity = {
        "kind": kind,
        "area": area.name,
        "bbox": list(area.bbox),
        "rules": rules,
        "shom": shom_meta["content_sha256"] if shom_meta else None,
        "emodnet": grid.digest if grid else None,
    }
    digest = inputs_digest(identity, pm_tiles, sm_tiles) if kind != "area" else None
    if digest:
        report["inputs_sha256"] = digest
    if not is_world:
        report["coastal_mask"] = {
            "land_tile_by_zoom": by_zoom(dry),
            "kept_over_basemap_water_by_zoom": by_zoom([t for t, _ in wet]),
            "land_tile_bytes": len(land_tile()),
        }
    if is_world:
        meta = {
            **base_meta,
            "name": "Tactician chart: world overview",
            "kind": "world",
            "bounds": list(area.bbox),
            "minzoom": area.min_zoom,
            "maxzoom": area.max_zoom,
            "vector_layers": vector_layers(present),
            "inputs_sha256": digest,
        }
        write_pmtiles(out, chart, meta, area.bbox, area.min_zoom, area.max_zoom, 2)
        report.update(sizes(out, chart))
    else:
        world_tiles = {}
        if world is not None:
            world_tiles = read_tiles(world)
            report["world"] = {
                "path": str(world),
                "tiles": len(world_tiles),
                "bytes": sum(map(len, world_tiles.values())),
            }
        layers_meta = (
            vector_layers(present, layer_info(world_tiles))
            if world_tiles
            else vector_layers(present)
        )
        depth_name = out.stem + "-depth.pmtiles"
        # an inland cell (no EMODnet sea under it) has no depth and no companion
        depth_meta = (
            {
                "source_layer": "depth",
                "max_detail_zoom": DEPTH_MAX_ZOOM,
                "packaging": "separate archive",
                "archive": depth_name,
            }
            if depth_tiles
            else None
        )
        label = f"cell {area.name}" if kind == "cell" else area.name
        meta = {
            **base_meta,
            "name": f"Tactician chart{' cell' if kind == 'cell' else ''}: {area.name}",
            "kind": kind,
            "bounds": list(area.bbox),
            "minzoom": 0 if world_tiles else area.min_zoom,
            "maxzoom": area.max_zoom,
        }
        if kind == "cell":
            x, y = area.cell
            meta["cell"] = {"id": area.name, "z": CELL_ZOOM, "x": x, "y": y}
        meta.update(
            {
                "area": {
                    "bounds": list(area.bbox),
                    "minzoom": area.min_zoom,
                    "maxzoom": area.max_zoom,
                },
                "world_overview": {"minzoom": 0, "maxzoom": WORLD.max_zoom}
                if world_tiles
                else None,
                "coastal_mask": {
                    "above_zoom": MASK_FROM_ZOOM - 1,
                    "sea_distance_nm": mask_nm,
                    "missing_tile": "draw the nearest ancestor",
                },
                "roads": {
                    "from_zoom": ROADS_FROM_ZOOM,
                    "sea_distance_nm": roads_nm,
                    "kinds": roads,
                },
                "depth": depth_meta,
                "vector_layers": layers_meta,
            }
        )
        if digest:
            meta["inputs_sha256"] = digest
        if world_tiles:
            meta["sources"] = meta["sources"] + [
                {"id": "world", "build_id": read_meta(world).get("build_id")}
            ]
        all_tiles = {**world_tiles, **chart}
        write_pmtiles(
            out,
            all_tiles,
            meta,
            area.bbox,
            meta["minzoom"],
            area.max_zoom,
            9 if area in (CHANNEL,) or kind == "cell" else 12,
        )
        report.update(sizes(out, all_tiles, area_tiles=chart))
        if depth_tiles:
            dpath = out.with_name(depth_name)
            dmeta = {
                **base_meta,
                "name": f"Tactician chart depth: {label}"
                if kind == "cell"
                else f"Tactician chart depth: {area.name}",
                "kind": "depth",
                "bounds": list(area.bbox),
                "minzoom": area.min_zoom,
                "maxzoom": DEPTH_MAX_ZOOM,
                "vector_layers": vector_layers(layer_info(depth_tiles)),
            }
            if kind == "cell":
                dmeta["cell"] = meta["cell"]
                dmeta["inputs_sha256"] = digest
            write_pmtiles(dpath, depth_tiles, dmeta, area.bbox, area.min_zoom, DEPTH_MAX_ZOOM, 9)
            report["depth_archive"] = sizes(dpath, depth_tiles)
    report["seconds"] = round(time.time() - t0)
    out.with_suffix(".report.json").write_text(json.dumps(report, indent=1) + "\n")
    log(
        json.dumps(
            {k: v for k, v in report.items() if k not in ("tile_bytes_by_layer", "area")}, indent=1
        )
    )
    return report


def tid_zoom(tid: int) -> int:
    from pmtiles.tile import tileid_to_zxy

    return tileid_to_zxy(tid)[0]


def layer_info(tiles: dict) -> dict:
    """{layer: {"minzoom", "maxzoom", "fields": {key: type}}} over tiles {tid: gz}."""
    from pmtiles.tile import tileid_to_zxy

    out = {}
    seen = set()
    for tid, data in tiles.items():
        z = tileid_to_zxy(tid)[0]
        for layer in mvt.parse(data).layers:
            info = out.setdefault(layer.name, {"minzoom": z, "maxzoom": z, "fields": {}})
            info["minzoom"], info["maxzoom"] = min(info["minzoom"], z), max(info["maxzoom"], z)
            key = (layer.name, hash(data))
            if key in seen:
                continue
            seen.add(key)
            types = defaultdict(set)
            for f in layer.features:
                tags = f.tags
                for i in range(0, len(tags) - 1, 2):
                    v = layer.values[tags[i + 1]]
                    types[layer.keys[tags[i]]].add(
                        "String"
                        if v.HasField("string_value")
                        else "Boolean"
                        if v.HasField("bool_value")
                        else "Number"
                    )
            for k, ts in types.items():
                info["fields"][k] = "|".join(
                    sorted(set(info["fields"].get(k, "").split("|")) - {""} | ts)
                )
    return out


def read_tiles(path: Path) -> dict:
    from pmtiles.reader import MmapSource, Reader, all_tiles

    with open(path, "rb") as f:
        r = Reader(MmapSource(f))
        return {zxy_to_tileid(*t): d for t, d in all_tiles(r.get_bytes)}


def read_meta(path: Path) -> dict:
    from pmtiles.reader import MmapSource, Reader

    with open(path, "rb") as f:
        return Reader(MmapSource(f)).metadata()


def sizes(path: Path, tiles: dict, area_tiles: dict | None = None) -> dict:
    """File size, and tile bytes per zoom and per layer (a layer's share of a
    tile's gzip bytes is its share of the uncompressed bytes)."""
    out = {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "tiles": len(tiles),
        **_breakdown(tiles),
    }
    if area_tiles is not None:
        out["area_tiles"] = len(area_tiles)
        out["area_tile_bytes"] = sum(map(len, area_tiles.values()))
        out["area_stored_bytes"] = sum(
            len(d) for d in {hash(d): d for d in area_tiles.values()}.values()
        )  # identical tiles once
        out["area"] = _breakdown(area_tiles)
    return out


def _breakdown(tiles: dict) -> dict:
    from pmtiles.tile import tileid_to_zxy

    per_zoom = defaultdict(int)
    per_layer = defaultdict(float)
    seen = set()
    for tid, data in tiles.items():
        z = tileid_to_zxy(tid)[0]
        h = hash(data)
        if h in seen:
            continue  # stored once in the archive
        seen.add(h)
        per_zoom[z] += len(data)
        t = mvt.parse(data)
        raw = {l.name: l.ByteSize() for l in t.layers}
        total = sum(raw.values()) or 1
        for n, b in raw.items():
            per_layer[n] += len(data) * b / total
    return {
        "tile_bytes_by_zoom": dict(sorted(per_zoom.items())),
        "tile_bytes_by_layer": {
            k: round(v) for k, v in sorted(per_layer.items(), key=lambda kv: -kv[1])
        },
    }


FIXTURE_DIR = HERE.parents[2] / "tests" / "fixtures" / "chart-pack-cherbourg"


def fixture(out_dir: Path = FIXTURE_DIR) -> Path:
    """The golden fixture: one tile per zoom over Cherbourg, from the inputs
    pinned in cherbourg.json (sources, rules, build id, time and the builder
    label it was frozen with), so regenerating it from the same cached inputs
    gives the same bytes. Writes cherbourg.pmtiles, cherbourg-depth.pmtiles and
    cherbourg.expected.json into out_dir."""
    from pmtiles.reader import MmapSource, Reader, all_tiles

    from ingest.charts.common import FIXTURE

    spec = json.loads((FIXTURE_DIR / "cherbourg.json").read_text())
    if spec["area"] != FIXTURE.name or spec["depth"] != "separate":
        raise ValueError("cherbourg.json does not describe the fixture area with separate depth")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "cherbourg.pmtiles"
    run(
        FIXTURE,
        kind="area",
        protomaps=spec["sources"]["protomaps"],
        seamap=spec["sources"]["seamap"],
        shom_tag=spec["sources"]["shom_tag"],
        out=path,
        build_id=spec["build_id"],
        built=spec["built"],
        builder=spec["builder"],
        mask_nm=spec["mask_nm"],
        roads_nm=spec["roads_nm"],
        roads=spec["roads"],
        workers=1,
    )
    path.with_suffix(".report.json").unlink(missing_ok=True)
    expected = {
        "about": "What a TCP1 reader should find in cherbourg.pmtiles and cherbourg-depth.pmtiles; written by build.py fixture."
    }
    for p in (path, path.with_name(path.stem + "-depth.pmtiles")):
        with open(p, "rb") as f:
            r = Reader(MmapSource(f))
            h, md = r.header(), r.metadata()
            tiles = {}
            for (z, x, y), data in sorted(all_tiles(r.get_bytes)):
                tiles[f"{z}/{x}/{y}"] = {l.name: len(l.features) for l in mvt.parse(data).layers}
        expected[p.name] = {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "bytes": p.stat().st_size,
            "header": {
                "tile_type": "mvt",
                "tile_compression": "gzip",
                "min_zoom": h["min_zoom"],
                "max_zoom": h["max_zoom"],
                "bounds_e7": [h["min_lon_e7"], h["min_lat_e7"], h["max_lon_e7"], h["max_lat_e7"]],
            },
            "metadata": {
                k: md[k]
                for k in (
                    "spec",
                    "schema_version",
                    "kind",
                    "build_id",
                    "built",
                    "attribution",
                    "depth",
                )
                if k in md
            }
            | {"source_ids": [s["id"] for s in md["sources"]]},
            "tiles": tiles,
        }
    (out_dir / "cherbourg.expected.json").write_text(
        json.dumps(expected, indent=1, ensure_ascii=False) + "\n"
    )
    return path
