"""Check TCP1 archives against docs/chart-pack-format.md, as a reader would.

    uv run ingest-charts check PACK.pmtiles [...]

A reader opens an archive only if the PMTiles header says gzip-compressed MVT
and the metadata says spec "TCP1" with a schema_version it knows (1). This also
checks what the builder promises: only TCP1 layers, only the properties
style-fields.json allows, the zoom ranges for the archive's kind, and the
metadata a reader shows (build id, sources, attribution, not-for-navigation).
An `area` archive stores every tile of its box at z6-14 and a `cell` every
descendant of its z6 tile: a masked tile is the shared land tile, never a gap
(MapLibre Native draws a gap as sea). A cell, and an area with the world folded
in, covers every such tile whole, with its `cover` or the land tile.

Vendored from tactician `tools/charts/check_pack.py` (main at eaf6d88,
2026-11-07), with the `cell` kind added (WP10 phase 6).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import Compression, TileType

from ingest.charts import mvt
from ingest.charts.common import CELL_ZOOM, SCHEMA_VERSION, SPEC, Area, cell_area, cell_id
from ingest.charts.depth import DEPTH_MAX_ZOOM

HERE = Path(__file__).resolve().parent
CHART_LAYERS = {
    "earth",
    "water",
    "places",
    "boundaries",
    "roads",
    "seamark",
    "light",
    "sea_area",
    "depth",
    "cover",
}
ZOOMS = {"world": (0, 5), "area": (0, 14), "cell": (6, 14), "depth": (6, DEPTH_MAX_ZOOM)}
REQUIRED = [
    "spec",
    "schema_version",
    "kind",
    "build_id",
    "built",
    "sources",
    "attribution",
    "not_for_navigation",
    "vector_layers",
]


def whole_tile(layer) -> bool:
    """Whether a layer holds one polygon whose ring reaches past every edge of
    the tile: the `cover` square, or the shared land tile's earth."""
    if len(layer.features) != 1 or layer.features[0].type != mvt.POLYGON:
        return False
    rings = mvt.decode_rings(list(layer.features[0].geometry))
    if len(rings) != 1 or mvt.ring_area(rings[0]) <= 0:
        return False
    xs, ys = {p[0] for p in rings[0]}, {p[1] for p in rings[0]}
    return (
        len(xs) == 2
        and len(ys) == 2
        and min(xs) <= 0
        and min(ys) <= 0
        and max(xs) >= layer.extent
        and max(ys) >= layer.extent
    )


def cell_of(md: dict) -> Area:
    """The cell an archive's `cell` metadata names, refused unless it is one z6 tile."""
    c = md.get("cell") or {}
    try:
        cell = cell_area(cell_id(int(c.get("x", -1)), int(c.get("y", -1))))
    except (TypeError, ValueError):
        raise ValueError(f"cell metadata {c} is not one z{CELL_ZOOM} tile") from None
    if c.get("id") != cell.name or c.get("z") != CELL_ZOOM:
        raise ValueError(f"cell metadata {c} is not one z{CELL_ZOOM} tile")
    return cell


def check(path: Path, fields: dict, quiet: bool = False) -> list[str]:
    errors = []
    path = Path(path)
    with open(path, "rb") as f:
        r = Reader(MmapSource(f))
        h, md = r.header(), r.metadata()
        if h["tile_type"] != TileType.MVT or h["tile_compression"] != Compression.GZIP:
            return [f"{path.name}: not gzip MVT ({h['tile_type']}, {h['tile_compression']})"]
        if md.get("spec") != SPEC or md.get("schema_version") != SCHEMA_VERSION:
            return [
                f"{path.name}: spec {md.get('spec')!r} schema {md.get('schema_version')!r}, "
                f"reader knows {SPEC} {SCHEMA_VERSION}"
            ]
        errors += [f"{path.name}: metadata lacks {k}" for k in REQUIRED if k not in md]
        kind = md.get("kind")
        if kind not in ZOOMS:
            errors.append(f"{path.name}: unknown kind {kind!r}")
            return errors
        cell = None
        if kind == "cell" or (kind == "depth" and md.get("cell") is not None):
            try:
                cell = cell_of(md)
            except ValueError as e:
                return errors + [f"{path.name}: {e}"]
            if [round(v, 9) for v in md.get("bounds", [])] != [round(v, 9) for v in cell.bbox]:
                errors.append(f"{path.name}: bounds {md.get('bounds')} are not cell {cell.name}'s")
        lo, hi = ZOOMS[kind]
        if kind == "area" and md.get("world_overview") is None:
            lo = md["area"]["minzoom"]
        if (h["min_zoom"], h["max_zoom"]) != (lo, hi):
            errors.append(
                f"{path.name}: zooms {h['min_zoom']}-{h['max_zoom']}, {kind} archives are {lo}-{hi}"
            )
        if kind == "cell" and md.get("world_overview") is not None:
            errors.append(f"{path.name}: a cell never folds the world in")
        keep = {name: mvt.keeper(props) for name, props in fields["layers"].items()}
        separate = (md.get("depth") or {}).get("packaging") == "separate archive" or (
            kind in ("cell", "area") and md.get("depth", "absent") is None
        )
        allowed = (
            {"depth"} if kind == "depth" else CHART_LAYERS - ({"depth"} if separate else set())
        )
        tiles = 0
        stored = set()
        covered = set()
        for (z, x, y), data in all_tiles(r.get_bytes):
            tiles += 1
            stored.add((z, x, y))
            for layer in mvt.parse(data).layers:
                if (layer.name == "cover" or layer.name == "earth") and whole_tile(layer):
                    covered.add((z, x, y))
                if layer.name == "cover" and (z < 6 or not whole_tile(layer)):
                    errors.append(
                        f"{path.name}: z{z}/{x}/{y} carries a cover that is not one square "
                        "over the whole tile at z6-14"
                    )
                if layer.name not in allowed:
                    errors.append(f"{path.name}: z{z}/{x}/{y} carries layer {layer.name!r}")
                    continue
                bad = [k for k in layer.keys if not keep[layer.name](k)]
                if bad:
                    errors.append(f"{path.name}: z{z}/{x}/{y} {layer.name} carries {bad}")
                if layer.name == "roads" and z < 12:
                    errors.append(f"{path.name}: z{z}/{x}/{y} carries roads below z12")
                if layer.name == "depth" and z > DEPTH_MAX_ZOOM and separate:
                    errors.append(f"{path.name}: z{z} depth in a separate-depth pack")
            if cell is not None and (
                z < CELL_ZOOM or (x >> (z - CELL_ZOOM), y >> (z - CELL_ZOOM)) != cell.cell
            ):
                errors.append(f"{path.name}: z{z}/{x}/{y} lies outside cell {cell.name}")
            if len(errors) > 20:
                errors.append("... stopped after 20 errors")
                return errors
        if kind in ("area", "cell"):
            a = md["area"]
            box = (
                cell
                if kind == "cell"
                else Area("box", tuple(a["bounds"]), a["minzoom"], a["maxzoom"])
            )
            gaps, bare = {}, {}
            for z in range(a["minzoom"], a["maxzoom"] + 1):
                want = list(box.tiles(z))
                n = sum(1 for t in want if t not in stored)
                if n:
                    gaps[z] = n
                if kind == "cell" or md.get("world_overview") is not None:
                    n = sum(1 for t in want if t in stored and t not in covered)
                    if n:
                        bare[z] = n
            if gaps:
                errors.append(f"{path.name}: tiles of the {kind}'s box not stored, by zoom: {gaps}")
            if bare:
                errors.append(
                    f"{path.name}: tiles of the {kind}'s box without a cover, by zoom: {bare} "
                    "(the style draws the world overview's land under the area's tiles, so it "
                    "would show through their sea)"
                )
        if not quiet:
            print(
                f"{path.name}: {SPEC} schema {md['schema_version']}, {kind}, "
                f"build {md['build_id']}, z{h['min_zoom']}-{h['max_zoom']}, {tiles} tiles, "
                f"{path.stat().st_size / 1e6:.2f} MB: {'ok' if not errors else 'FAILED'}"
            )
    return errors


def style_fields() -> dict:
    return json.loads((HERE / "style-fields.json").read_text())


def main(paths) -> int:
    fields = style_fields()
    errors = []
    for p in paths:
        errors += check(Path(p), fields)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
