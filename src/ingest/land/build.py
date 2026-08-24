"""Build a whole routing index: fetch, compile, encode, check, manifest.

This is a **one-shot artifact**, not a cron job. Shorelines and bathymetry
change on the timescale of survey programmes, not forecast cycles, so an index
is rebuilt when a source publishes a new release or when one of its
conservatism parameters is deliberately changed — and each rebuild ships as a
new immutable version rather than editing the one boats already carry.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ingest.land import emodnet, gshhg
from ingest.land.compile import Conservatism, TileCompilation, compile_tile
from ingest.land.publish import build_manifest
from landkit.codec import CELL_ORDER, SCHEMA_VERSION, decode_tile, encode_tile, row_bytes
from landkit.grid import CELLS_PER_DEG, cell_deg
from tilekit.tiles import TILE_DEG, tile_id


@dataclass(frozen=True)
class Domain:
    name: str
    label: str
    #: (lat0, lon0) south-west corners, on the same 10-degree grid the forecast
    #: tiles use, so a race area resolves to tiles once for both.
    tiles: tuple[tuple[int, int], ...]
    span_deg: int = TILE_DEG


DOMAINS: dict[str, Domain] = {
    "nweu": Domain(
        name="nweu",
        label=(
            "North-west European waters, 10 W to 10 E and 40 N to 60 N: Biscay, Brittany, "
            "the Channel and its western approaches, and the southern North Sea"
        ),
        # A whole 2x2 block of the forecast tiling rather than the L-shape the
        # races alone would need. A race area resolves to tiles by its bounding
        # box, and the RORC Channel fleet's own box reaches a degree east of
        # the course into the fourth tile; a domain cut to the courses would
        # have put a coverage edge inside a race the index exists to serve.
        tiles=((40, -10), (40, 0), (50, -10), (50, 0)),
    ),
    # The committed contract fixture: one degree over the Raz de Sein, which
    # carries an islet, a rock chaussee and a legal passage between them. It is
    # a real index of a small domain, not a synthetic one, so the consumer's
    # contract test reads exactly what the producer publishes.
    "raz": Domain(
        name="raz",
        label="Raz de Sein contract fixture: Ile de Sein, the Chaussee, and the passage",
        tiles=((48, -5),),
        span_deg=1,
    ),
}

#: Positions whose answer is not in doubt, checked on every build. The failure
#: this catches is the catastrophic one — a transposed axis, a flipped depth
#: sign, an off-by-a-tile origin — which every other check would pass while the
#: index quietly inverted land and sea.
PROBES: tuple[tuple[str, float, float, bool], ...] = (
    ("mid-Channel, 20 NM north of Cherbourg", -1.60, 50.00, False),
    ("mid-Biscay abyssal plain", -6.00, 45.50, False),
    ("Western Approaches, south-west of the Scillies", -7.50, 49.00, False),
    ("central Brittany, inland of Carhaix", -3.60, 48.30, True),
    ("central Hampshire, inland of Winchester", -1.32, 51.06, True),
    ("central Ireland, inland of Athlone", -8.00, 53.40, True),
)


@dataclass
class BuiltIndex:
    manifest: dict
    tiles: list[tuple[str, bytes]]
    compilations: list[TileCompilation]


def index_id(domain: str, at: datetime) -> str:
    """Version ids read like a forecast run id on purpose: same shape, same
    immutability rule, and an hour stamp so a rebuild is never a mutation."""
    return f"{domain}-{at:%Y%m%dT%H}Z"


def build_index(
    domain: Domain,
    *,
    gshhg_path: Path,
    cache_dir: Path,
    conservatism: Conservatism,
    at: datetime | None = None,
    fetch_block=emodnet.fetch_block,
    progress=None,
) -> BuiltIndex:
    at = at or datetime.now(timezone.utc)
    generated_at = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    version = index_id(domain.name, at)

    compilations: list[TileCompilation] = []
    encoded: list[tuple[str, bytes]] = []
    for lat0, lon0 in domain.tiles:
        if progress is not None:
            progress(f"compiling {tile_id(lat0, lon0)}")
        compilation = compile_tile(
            lat0,
            lon0,
            gshhg_path=gshhg_path,
            cache_dir=cache_dir,
            conservatism=conservatism,
            span_deg=domain.span_deg,
            fetch_block=fetch_block,
        )
        compilations.append(compilation)
        encoded.append(
            (compilation.tile_id, _encode(compilation, version, generated_at, conservatism, domain))
        )

    checks = _check(compilations, encoded, domain, conservatism)
    manifest = build_manifest(
        index_id=version,
        domain=domain.name,
        domain_label=domain.label,
        tiles=encoded,
        coverage={
            "tiles": [tile_id(lat0, lon0) for lat0, lon0 in domain.tiles],
            "min_lon": min(lon0 for _, lon0 in domain.tiles),
            "max_lon": max(lon0 for _, lon0 in domain.tiles) + domain.span_deg,
            "min_lat": min(lat0 for lat0, _ in domain.tiles),
            "max_lat": max(lat0 for lat0, _ in domain.tiles) + domain.span_deg,
            "note": (
                "the published tile list is the coverage. The bounding box is its envelope "
                "and may contain tiles that were not compiled; a query outside a published "
                "tile is unknown, never clear"
            ),
        },
        grid={
            "tile_deg": domain.span_deg,
            "cells_per_deg": CELLS_PER_DEG,
            "dlon": cell_deg(),
            "dlat": cell_deg(),
            "cell_order": CELL_ORDER,
        },
        conservatism=conservatism.public(),
        sources=[
            gshhg.provenance(gshhg_path, generated_at),
            emodnet.provenance(generated_at),
        ],
        composition=_composition(compilations),
        checks=checks,
        published_at=generated_at,
    )
    return BuiltIndex(manifest=manifest, tiles=encoded, compilations=compilations)


def _encode(
    compilation: TileCompilation,
    version: str,
    generated_at: str,
    conservatism: Conservatism,
    domain: Domain,
) -> bytes:
    n = domain.span_deg * CELLS_PER_DEG
    header = {
        "spec": "TLI1",
        "schema_version": SCHEMA_VERSION,
        "index_id": version,
        "domain": domain.name,
        "tile_id": compilation.tile_id,
        "lon0": float(compilation.lon0),
        "lat0": float(compilation.lat0),
        "dlon": cell_deg(),
        "dlat": cell_deg(),
        "nlon": n,
        "nlat": n,
        "row_bytes": row_bytes(n),
        "cell_order": CELL_ORDER,
        "cells": n * n,
        "blocked_cells": compilation.blocked_cells,
        "composition": {
            "land_cells": compilation.land_cells,
            "shoal_cells": compilation.shoal_cells,
            "nodata_cells": compilation.nodata_cells,
            "shoreline_only_cells": compilation.gshhg_only_cells,
            "depth_only_cells": compilation.dtm_only_cells,
            "blocked_before_buffer": compilation.blocked_before_buffer,
        },
        "conservatism": {
            "buffer_m": conservatism.buffer_m,
            "dilation_cells": {"lon": compilation.dilation[0], "lat": compilation.dilation[1]},
            "safety_contour_m": conservatism.safety_contour_m,
            "vertical_datum": emodnet.VERTICAL_DATUM,
        },
        "generated_at": generated_at,
    }
    return gzip.compress(encode_tile(header, compilation.blocked), mtime=0)


def _composition(compilations: list[TileCompilation]) -> dict:
    cells = sum(c.blocked.size for c in compilations)
    land = sum(c.land_cells for c in compilations)
    shoal_only = sum(c.dtm_only_cells for c in compilations)
    shoreline_only = sum(c.gshhg_only_cells for c in compilations)
    return {
        "cells": cells,
        "blocked_cells": sum(c.blocked_cells for c in compilations),
        "blocked_before_buffer": sum(c.blocked_before_buffer for c in compilations),
        "land_cells": land,
        "nodata_cells": sum(c.nodata_cells for c in compilations),
        "depth_only_cells": shoal_only,
        "shoreline_only_cells": shoreline_only,
        "source_agreement": round(1.0 - (shoal_only + shoreline_only) / cells, 6),
        "source_agreement_note": (
            "the fraction of cells on which the shoreline and the depth contour agree, "
            "measured rather than assumed. They are independent surveys; the index is their "
            "union, so a disagreement adds blocked area and never removes any"
        ),
    }


def _check(
    compilations: list[TileCompilation],
    encoded: list[tuple[str, bytes]],
    domain: Domain,
    conservatism: Conservatism,
) -> list[str]:
    checks: list[str] = []
    for compilation, (tile, gz) in zip(compilations, encoded, strict=True):
        decoded = decode_tile(gzip.decompress(gz))
        if decoded.header["tile_id"] != tile:
            raise ValueError(f"{tile}: encoded tile carries id {decoded.header['tile_id']}")
        if not (decoded.blocked == compilation.blocked).all():
            raise ValueError(f"{tile}: does not round-trip through the codec")
        if compilation.blocked_cells < compilation.blocked_before_buffer:
            raise ValueError(
                f"{tile}: the buffer removed blocked cells — the compilation must only add"
            )
    checks.append(f"{len(encoded)} tiles round-trip through the TLI1 codec")
    checks.append("the outward buffer added blocked cells on every tile and removed none")

    for label, lon, lat, expected in PROBES:
        answer = _probe(compilations, domain, lon, lat)
        if answer is None:
            continue
        if answer != expected:
            raise ValueError(
                f"probe {label!r} at {lon},{lat} reads "
                f"{'blocked' if answer else 'clear'}, expected "
                f"{'blocked' if expected else 'clear'}"
            )
        checks.append(f"probe {label!r} reads {'blocked' if expected else 'clear'}")
    return checks


def _probe(
    compilations: list[TileCompilation], domain: Domain, lon: float, lat: float
) -> bool | None:
    for compilation in compilations:
        col = int((lon - compilation.lon0) * CELLS_PER_DEG)
        row = int((lat - compilation.lat0) * CELLS_PER_DEG)
        size = domain.span_deg * CELLS_PER_DEG
        if 0 <= col < size and 0 <= row < size:
            return bool(compilation.blocked[row, col])
    return None
