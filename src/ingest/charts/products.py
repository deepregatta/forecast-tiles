"""What EMODnet decides for one cell, kept between EMODnet's releases.

A cell's EMODnet product is everything its build takes from the DTM: the sea
distance of every tile z6-14 (the coastal mask and the road rule read it) and
every depth tile z6-12 (the depth companion is cut from them), with the grid's
digest for the cell's input identity. It depends only on the DTM release, this
code and the depth properties the style reads, so it is built once per
release and read back by every later build instead of the DTM's blocks, about
30 whole-degree blocks (some 130 MB) a cell. GitHub's cache could not hold
them for Europe's 73 cells, and evicts what a monthly run has not read for a
week.

Each product is published beside its cell as `charts/{build}/{cell}.emodnet.npz`
and named in the pointer's cell entry (`emodnet`, its `product_key`), so the
publisher reuses and retains it as it does a cell's archive. Readers of the
chart never read it.

An npz of plain arrays (read with `allow_pickle=False`): `distance`, float32 in
`Area.tiles` order z6-14; `depth_ids`, `depth_offsets` and `depth_data`, the
gzipped depth tiles by PMTiles tile id; `meta`, one JSON document.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import time
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

import numpy as np
from pmtiles.tile import zxy_to_tileid

from ingest.charts import mvt
from ingest.charts.common import Area
from ingest.charts.depth import DEPTH_MAX_ZOOM, PRODUCT as EMODNET_PRODUCT, Grid

HERE = Path(__file__).resolve().parent
FORMAT = 1
# The code a product's content follows: a change to any of these files is a
# new product key, so every cell builds its product again.
CODE_FILES = ("common.py", "depth.py", "mvt.py", "products.py")
SUFFIX = ".emodnet.npz"


class ProductError(RuntimeError):
    pass


def product_key(depth_fields) -> str:
    """The key every product of this code, DTM release and depth style shares."""
    h = hashlib.sha256()
    h.update(
        json.dumps(
            {"format": FORMAT, "product": EMODNET_PRODUCT, "depth_fields": depth_fields},
            sort_keys=True,
        ).encode()
    )
    for name in CODE_FILES:
        h.update(name.encode() + b"\0" + (HERE / name).read_bytes() + b"\0")
    return h.hexdigest()


def tiles_of(area: Area) -> list[tuple[int, int, int]]:
    return [t for z in range(area.min_zoom, area.max_zoom + 1) for t in area.tiles(z)]


@dataclass
class Product:
    area: str
    key: str
    grid_sha256: str
    distance: dict  # (z, x, y) -> NM to the open sea
    depth: dict  # tile id -> gzipped depth tile
    blocks: int
    fetched: int

    def save(self, path: Path, area: Area) -> Path:
        tiles = tiles_of(area)
        ids = np.array(sorted(self.depth), dtype=np.uint64)
        data = [self.depth[int(i)] for i in ids]
        offsets = np.zeros(len(data) + 1, dtype=np.uint64)
        offsets[1:] = np.cumsum([len(d) for d in data])
        meta = {
            "format": FORMAT,
            "area": self.area,
            "product_key": self.key,
            "emodnet_product": EMODNET_PRODUCT,
            "grid_sha256": self.grid_sha256,
            "blocks": self.blocks,
            "tiles": len(tiles),
            "depth_tiles": len(data),
        }
        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            distance=np.array([self.distance[t] for t in tiles], dtype=np.float32),
            depth_ids=ids,
            depth_offsets=offsets,
            depth_data=np.frombuffer(b"".join(data), dtype=np.uint8),
            meta=np.array(json.dumps(meta, sort_keys=True)),
        )
        path = Path(path)
        path.write_bytes(buf.getvalue())
        return path

    @classmethod
    def load(cls, path: Path, area: Area, key: str) -> Product:
        with np.load(Path(path), allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            tiles = tiles_of(area)
            if meta.get("format") != FORMAT or meta.get("area") != area.name:
                raise ProductError(f"{path}: not {area.name}'s EMODnet product")
            if meta.get("product_key") != key:
                raise ProductError(
                    f"{path}: product {meta.get('product_key')}, this build reads {key}"
                )
            dist = z["distance"]
            if dist.shape != (len(tiles),):
                raise ProductError(f"{path}: {dist.shape} distances for {len(tiles)} tiles")
            ids, offsets, blob = z["depth_ids"], z["depth_offsets"], z["depth_data"].tobytes()
            if len(offsets) != len(ids) + 1 or int(offsets[-1]) != len(blob):
                raise ProductError(f"{path}: depth tiles do not add up")
            depth = {int(i): blob[int(offsets[k]) : int(offsets[k + 1])] for k, i in enumerate(ids)}
        return cls(
            area=area.name,
            key=key,
            grid_sha256=meta["grid_sha256"],
            distance={t: float(d) for t, d in zip(tiles, dist)},
            depth=depth,
            blocks=meta.get("blocks", 0),
            fetched=0,
        )


_G: dict = {}


def _depth_tile(t):
    z, x, y = t
    layer = _G["grid"].depth_layer(z, x, y, keep=_G["keep"])
    return t, (gzip.compress(mvt.encode([layer]), 6, mtime=0) if len(layer) else None)


def build(area: Area, keep_depth, key: str, *, workers: int = 4, log=print) -> Product:
    """Read the DTM over the area (its blocks cached as the land index caches
    them) and cut every tile's sea distance and depth."""
    t0 = time.time()
    grid = Grid(area.bbox, log=log)
    grid.sea_distance()
    log(
        f"  EMODnet: {grid.blocks} blocks ({grid.fetched} fetched), grid {grid.elev.shape[1]} x {grid.elev.shape[0]}"
    )
    tiles = tiles_of(area)
    distance = {t: grid.tile_sea_distance(*t) for t in tiles}
    _G.clear()
    _G.update(grid=grid, keep=keep_depth)
    want = [t for t in tiles if t[0] <= DEPTH_MAX_ZOOM]
    depth = {}
    if workers > 1:
        with get_context("fork").Pool(workers) as pool:
            done = pool.imap_unordered(_depth_tile, want, chunksize=16)
            depth = {zxy_to_tileid(*t): d for t, d in done if d}
    else:
        depth = {zxy_to_tileid(*t): d for t, d in map(_depth_tile, want) if d}
    log(f"  EMODnet product: {len(depth)} depth tiles of {len(want)} ({time.time() - t0:.0f} s)")
    return Product(
        area=area.name,
        key=key,
        grid_sha256=grid.digest,
        distance=distance,
        depth=depth,
        blocks=grid.blocks,
        fetched=grid.fetched,
    )
