"""EMODnet Bathymetry — the depth half of the routing index.

GEBCO was the alternative and lost on two counts that matter here. EMODnet's
European DTM is posted at 1/16 arc-minute (about 115 m) against GEBCO's 15 arc-
seconds (about 450 m), which is the difference between resolving the Chaussee
de Sein and averaging it away; and EMODnet is referenced to **Lowest
Astronomical Tide**, which is the datum a "does this dry?" question is actually
asked against. GEBCO stays the fallback if a domain outside EMODnet's coverage
is ever compiled, and that would be a different, stated source.

The product carries `DO NOT USE FOR NAVIGATION` in its own metadata. That is
recorded verbatim in every manifest this pipeline writes, and it is the same
sentence the whole artifact is built under: this is a routing-legality index,
not a chart.

Depths arrive as elevation against LAT — positive above, negative below — so
"shallower than the safety contour" is `elevation >= -contour`.
"""

from __future__ import annotations

import time
from pathlib import Path

from ingest.land.geotiff import GeoRaster, read_geotiff

NAME = "EMODnet Bathymetry"
PRODUCT = "EMODnet Digital Bathymetry (DTM) 2024"
COVERAGE = "emodnet:mean"
WCS_URL = "https://ows.emodnet-bathymetry.eu/wcs"
METADATA_URL = "https://sextant.ifremer.fr/record/cf51df64-56f9-4a99-b1aa-36b8d7b743a1"
LICENCE = "CC-BY-4.0"
ATTRIBUTION = "EMODnet Bathymetry Consortium (2024): EMODnet Digital Bathymetry (DTM 2024)"
USE_CONSTRAINT = "DO NOT USE FOR NAVIGATION"
VERTICAL_DATUM = "LAT (Lowest Astronomical Tide)"
#: 1/16 arc-minute, the DTM's own posting. Requested exactly, so the service
#: resamples nothing: a nearest-neighbour downsample would drop the isolated
#: shallow cell that is the entire point of carrying bathymetry.
NATIVE_CELLS_PER_DEG = 960
NATIVE_DEG = 1.0 / NATIVE_CELLS_PER_DEG
#: The DTM's published extent, refused outside rather than silently returning
#: an empty block.
EXTENT = (-70.5, 11.0, 43.0, 90.0)  # west, south, east, north


class EmodnetError(RuntimeError):
    pass


def block_path(cache_dir: Path, west: int, south: int) -> Path:
    ns = f"{'N' if south >= 0 else 'S'}{abs(south):02d}"
    ew = f"{'E' if west >= 0 else 'W'}{abs(west):03d}"
    return Path(cache_dir) / f"emodnet-mean-{ns}{ew}.tif"


def fetch_block(
    cache_dir: Path,
    west: int,
    south: int,
    *,
    session=None,
    attempts: int = 4,
    sleep=time.sleep,
) -> GeoRaster:
    """One whole-degree block at native resolution, cached on disk.

    Whole degrees are exact multiples of the DTM's own posting, so the returned
    grid lands on the source grid with no resampling — which the geometry check
    below asserts rather than assumes.
    """
    if not (EXTENT[0] <= west and west + 1 <= EXTENT[2]):
        raise EmodnetError(f"longitude block {west} is outside the DTM extent {EXTENT}")
    if not (EXTENT[1] <= south and south + 1 <= EXTENT[3]):
        raise EmodnetError(f"latitude block {south} is outside the DTM extent {EXTENT}")

    path = block_path(cache_dir, west, south)
    if path.is_file():
        return _checked(read_geotiff(path.read_bytes()), west, south)

    import requests

    params = {
        "service": "WCS",
        "version": "1.0.0",
        "request": "GetCoverage",
        "coverage": COVERAGE,
        "CRS": "EPSG:4326",
        "BBOX": f"{west},{south},{west + 1},{south + 1}",
        "RESX": repr(NATIVE_DEG),
        "RESY": repr(NATIVE_DEG),
        "FORMAT": "GeoTIFF",
    }
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = (session or requests).get(WCS_URL, params=params, timeout=180)
            response.raise_for_status()
            if not response.content.startswith((b"MM", b"II")):
                raise EmodnetError(
                    f"WCS returned {response.headers.get('content-type')} for "
                    f"{west},{south}: {response.content[:200]!r}"
                )
            raster = _checked(read_geotiff(response.content), west, south)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
            return raster
        except Exception as error:  # noqa: BLE001 - retried, then re-raised by name
            last = error
            if attempt + 1 < attempts:
                sleep(5 * (attempt + 1))
    raise EmodnetError(f"WCS block {west},{south} failed after {attempts} attempts: {last}")


def _checked(raster: GeoRaster, west: int, south: int) -> GeoRaster:
    """A block that is not exactly the degree square at exactly native
    resolution is refused. Everything downstream indexes the source grid by
    integer arithmetic; a half-cell shift would move every rock."""
    if (raster.width, raster.height) != (NATIVE_CELLS_PER_DEG, NATIVE_CELLS_PER_DEG):
        raise EmodnetError(
            f"block {west},{south} came back {raster.width}x{raster.height}, "
            f"expected {NATIVE_CELLS_PER_DEG} square"
        )
    if abs(raster.west - west) > 1e-9 or abs(raster.north - (south + 1)) > 1e-9:
        raise EmodnetError(f"block {west},{south} is georeferenced at {raster.west},{raster.north}")
    if abs(raster.dlon - NATIVE_DEG) > 1e-12 or abs(raster.dlat - NATIVE_DEG) > 1e-12:
        raise EmodnetError(
            f"block {west},{south} has cell {raster.dlon}x{raster.dlat}, expected {NATIVE_DEG}"
        )
    return raster


def provenance(accessed: str) -> dict:
    return {
        "name": NAME,
        "product": PRODUCT,
        "version": "DTM 2024",
        "role": "depth, for the selected safety contour",
        "url": f"{WCS_URL}?coverage={COVERAGE}",
        "metadata_url": METADATA_URL,
        "accessed": accessed,
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "use_constraint": USE_CONSTRAINT,
        "vertical_datum": VERTICAL_DATUM,
        "resolution_deg": NATIVE_DEG,
    }
