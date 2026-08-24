"""GSHHG — the coastline half of the routing index.

Global Self-consistent Hierarchical High-resolution Geography, full-resolution
shoreline (`gshhs_f.b`), read straight out of its documented binary form. The
shapefile distribution would need a shapefile reader; the binary one is a
44-byte big-endian header per polygon followed by its points in micro-degrees,
which numpy reads directly.

**Only level 1 is used, and lakes are not subtracted.** Level 1 is land as seen
from the sea. A lake (level 2) is a hole in land that a sea route cannot reach
anyway, so leaving it filled blocks water no route could legally use and keeps
the compilation one-directional: everything here adds blocked area, nothing
removes any.

**Points are stored 0-360.** Longitudes are normalised to [-180, 180) on read,
and an edge that then spans more than 180 degrees is an antimeridian wrap and
is dropped — no race in the pilot domain crosses it, and a wrapped edge drawn
straight across the map would paint a continent-wide line of false land.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

NAME = "GSHHG"
VERSION = "2.3.7"
PRODUCT = "Global Self-consistent Hierarchical High-resolution Geography, full resolution"
#: The University of Hawaii mirror. NOAA's own `latest/` path 404s for the
#: binary distribution, so the mirror is the recorded access point.
SOURCE_URL = "https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-bin-2.3.7.zip"
ARCHIVE_SHA256 = "28600e8f7a08645aab43079326df6504212ec5ccb2b4bcf3b5f4f12ed60e82bc"
MEMBER = "gshhs_f.b"
MEMBER_SHA256 = "af9215d58ebc525b2d09654a89959829f09e6edc457f3666759cded37be4ecf6"
LICENCE = "LGPL-3.0-or-later, with permission to use, copy, modify and distribute given attribution"
ATTRIBUTION = (
    "Wessel, P., and W. H. F. Smith (1996), A global, self-consistent, hierarchical, "
    "high-resolution shoreline database, J. Geophys. Res., 101(B4), 8741-8743"
)
HEADER_STRUCT = struct.Struct(">11i")
LEVEL_LAND = 1


class GshhgError(RuntimeError):
    pass


@dataclass
class LandPolygon:
    """One level-1 polygon's vertices, longitudes already normalised."""

    polygon_id: int
    lon: np.ndarray
    lat: np.ndarray


def ensure_source(cache_dir: Path, *, session=None) -> Path:
    """The unpacked `gshhs_f.b`, downloaded once into `cache_dir` and checked
    against its recorded digest on every use."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    member = cache_dir / MEMBER
    if member.is_file() and _sha256(member) == MEMBER_SHA256:
        return member

    archive = cache_dir / "gshhg-bin-2.3.7.zip"
    if not archive.is_file() or _sha256(archive) != ARCHIVE_SHA256:
        import requests

        get = (session or requests).get
        response = get(SOURCE_URL, timeout=600, stream=True)
        response.raise_for_status()
        with archive.open("wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
        found = _sha256(archive)
        if found != ARCHIVE_SHA256:
            raise GshhgError(
                f"{SOURCE_URL} sha256 {found} != recorded {ARCHIVE_SHA256}; refusing to compile"
            )
    with zipfile.ZipFile(archive) as zf:
        member.write_bytes(zf.read(MEMBER))
    found = _sha256(member)
    if found != MEMBER_SHA256:
        raise GshhgError(f"{MEMBER} sha256 {found} != recorded {MEMBER_SHA256}")
    return member


def land_polygons(
    path: Path, west: float, south: float, east: float, north: float
) -> list[LandPolygon]:
    """Every level-1 polygon whose points reach the box, clipped to nothing —
    the caller rasterises the whole polygon and lets the grid do the clipping,
    because a polygon cut at the box edge would need its cut edge closing and a
    wrongly closed coastline is false water."""
    raw = Path(path).read_bytes()
    polygons: list[LandPolygon] = []
    offset = 0
    while offset < len(raw):
        (
            polygon_id,
            n_points,
            flag,
            _west,
            _east,
            raw_south,
            raw_north,
            _area,
            _area_full,
            _container,
            _ancestor,
        ) = HEADER_STRUCT.unpack_from(raw, offset)
        body = offset + HEADER_STRUCT.size
        offset = body + n_points * 8
        if flag & 255 != LEVEL_LAND:
            continue
        # Latitude is stored signed and never wraps, so it filters cheaply
        # before any point is touched.
        if raw_north * 1e-6 < south or raw_south * 1e-6 > north:
            continue
        points = np.frombuffer(raw, dtype=">i4", count=n_points * 2, offset=body).reshape(-1, 2)
        lon = points[:, 0].astype(np.float64) * 1e-6
        lat = points[:, 1].astype(np.float64) * 1e-6
        lon = np.where(lon >= 180.0, lon - 360.0, lon)
        if not ((lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)).any():
            continue
        polygons.append(LandPolygon(polygon_id=polygon_id, lon=lon, lat=lat))
    return polygons


def provenance(path: Path, accessed: str) -> dict:
    return {
        "name": NAME,
        "product": PRODUCT,
        "version": VERSION,
        "role": "land polygons (level 1 shorelines)",
        "url": SOURCE_URL,
        "accessed": accessed,
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "file": MEMBER,
        "sha256": _sha256(Path(path)),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
