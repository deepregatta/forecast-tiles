"""Exact geometry and the versioned expected footprints of regional products.

A footprint is the set of grid cells a product is expected to fill. AROME's
0.025° distribution grid is a rectangle in latitude/longitude, but the model
only covers part of it: 17.18 % of cells are missing at every step. That mask
is recorded here once, from the product's static terrain file and checked
against independent runs, with a SHA-256 the registry pins. It is never
inferred afresh from the run being validated, which would wave through a
failed download as "expected" missing data.

Footprints are stored as packed bits (np.packbits of the valid mask, row 0 =
the southernmost row) in footprints/<name>.npz next to a JSON note of where
they came from.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from ingest.cube import GridMeta
from ingest.sources.openmeteo.registry import Grid, Product

FOOTPRINT_DIR = Path(__file__).resolve().parent / "footprints"


class FootprintError(RuntimeError):
    pass


def grid_meta(grid: Grid) -> GridMeta:
    return GridMeta(
        lat0=grid.lat0,
        lon0=grid.lon0,
        dlat=grid.step,
        dlon=grid.step,
        nlat=grid.nlat,
        nlon=grid.nlon,
    )


def bbox(grid: Grid) -> tuple[float, float, float, float]:
    """(lat_min, lon_min, lat_max, lon_max) of the cell centres, as the files'
    CRS WKT states it."""
    n = grid.cells_per_degree
    return (
        grid.lat0,
        grid.lon0,
        (grid.lat0 * n + grid.nlat - 1) / n,
        (grid.lon0 * n + grid.nlon - 1) / n,
    )


def footprint_sha256(valid: np.ndarray) -> str:
    valid = np.asarray(valid, dtype=bool)
    h = hashlib.sha256(f"{valid.shape[0]}x{valid.shape[1]}\n".encode())
    h.update(np.packbits(valid).tobytes())
    return h.hexdigest()


def save_footprint(name: str, valid: np.ndarray, note: dict, directory: Path | None = None):
    directory = directory or FOOTPRINT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    valid = np.asarray(valid, dtype=bool)
    np.savez_compressed(
        directory / f"{name}.npz",
        shape=np.array(valid.shape, dtype=np.int64),
        valid_packed=np.packbits(valid),
    )
    record = {"name": name, "shape": list(valid.shape), "sha256": footprint_sha256(valid), **note}
    (directory / f"{name}.json").write_text(json.dumps(record, indent=1) + "\n")
    return record["sha256"]


def load_footprint(product: Product, directory: Path | None = None) -> np.ndarray | None:
    """The product's expected valid-cell mask [nlat, nlon], or None when every
    cell carries data. Refuses a file whose shape or hash differs from the
    registry's."""
    if product.footprint is None:
        return None
    path = (directory or FOOTPRINT_DIR) / f"{product.footprint}.npz"
    with np.load(path) as z:
        nlat, nlon = (int(x) for x in z["shape"])
        valid = np.unpackbits(z["valid_packed"], count=nlat * nlon).reshape(nlat, nlon)
    valid = valid.astype(bool)
    if (nlat, nlon) != (product.grid.nlat, product.grid.nlon):
        raise FootprintError(
            f"{path.name}: shape {(nlat, nlon)} != grid {(product.grid.nlat, product.grid.nlon)}"
        )
    digest = footprint_sha256(valid)
    if digest != product.footprint_sha256:
        raise FootprintError(
            f"{path.name}: sha256 {digest} != registered {product.footprint_sha256}"
        )
    return valid
