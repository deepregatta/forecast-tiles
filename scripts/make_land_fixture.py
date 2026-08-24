#!/usr/bin/env python3
"""Generate the shared routing-index contract fixture.

One degree over the Raz de Sein — Ile de Sein, the Chaussee rocks and the legal
passage between them. It is a *real* index of a small domain, built by the same
code path as a production one at a fixed timestamp, so the consumer's contract
test reads exactly what the producer publishes rather than something shaped
like it.

Writes:
  tests/fixtures/land-index-raz/manifest.json
  tests/fixtures/land-index-raz/N48W005.bin.gz     deterministic gzip (mtime=0)
  tests/fixtures/land-index-raz/expected.json      what both repos assert

The same three files are committed in tactician (`core/land/fixtures/`). A
fixture that legitimately changes is a recorded decision in both repos, not a
silent regeneration.

Needs the source cache: the GSHHG archive and the one DTM block. Both are
fetched on demand into --cache-dir.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest.land import gshhg  # noqa: E402
from ingest.land.build import DOMAINS, build_index  # noqa: E402
from ingest.land.compile import Conservatism  # noqa: E402
from ingest.land.publish import json_bytes  # noqa: E402
from landkit.codec import decode_tile  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "land-index-raz"
#: Frozen so the fixture is byte-reproducible from this script alone.
BUILT_AT = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)

#: Named positions the fixture is asserted against in both repos. They are the
#: reason this region was chosen: an islet, a rock chaussee, a legal passage
#: between them, and open water either side.
PROBES = [
    ("Ile de Sein, the islet", -4.8553, 48.0364, True),
    ("Pointe du Raz, the mainland", -4.7361, 48.0386, True),
    ("the Raz de Sein passage", -4.7800, 48.0450, False),
    ("open water west of the Chaussee de Sein", -4.9900, 48.1000, False),
    ("Baie de Douarnenez, a bay the fill must not close", -4.4500, 48.1200, False),
    ("the Iroise, open water", -4.9000, 48.5500, False),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=".land-cache")
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    domain = DOMAINS["raz"]
    built = build_index(
        domain,
        gshhg_path=gshhg.ensure_source(cache_dir),
        cache_dir=cache_dir,
        conservatism=Conservatism(),
        at=BUILT_AT,
    )
    tile_id, gz = built.tiles[0]
    decoded = decode_tile(gzip.decompress(gz))

    probes = []
    for label, lon, lat, expected in PROBES:
        cell = decoded.cell_of(lon, lat)
        if cell is None:
            raise SystemExit(f"fixture probe {label!r} is outside the fixture tile")
        found = bool(decoded.blocked[cell])
        if found != expected:
            raise SystemExit(
                f"fixture probe {label!r} reads {'blocked' if found else 'clear'}, "
                f"expected {'blocked' if expected else 'clear'}"
            )
        probes.append({"label": label, "lon": lon, "lat": lat, "blocked": expected})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "manifest.json").write_bytes(json_bytes(built.manifest))
    (OUT_DIR / f"{tile_id}.bin.gz").write_bytes(gz)
    (OUT_DIR / "expected.json").write_bytes(
        json_bytes(
            {
                "what": (
                    "the routing-index contract fixture, decoded. Both the producer's and the "
                    "consumer's test must reproduce every value here from the committed bytes"
                ),
                "index_id": built.manifest["index_id"],
                "tile_id": tile_id,
                "fnv64_uncompressed": _fnv64(gzip.decompress(gz)),
                "header": decoded.header,
                "blocked_cells": int(decoded.blocked.sum()),
                "probes": probes,
                "raz_passage_row": _passage(decoded),
            }
        )
    )
    print(f"wrote {OUT_DIR} ({len(gz)} bytes of tile, {decoded.blocked.sum()} blocked cells)")
    return 0


def _fnv64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _passage(decoded) -> dict:
    """The widest clear run across the Raz at the passage's own latitude.

    A number rather than a picture: it is what says the buffer has not closed a
    channel a boat legally uses, and a consumer asserting it is asserting that
    its own decoder agrees about which cells are clear.
    """
    lat = 48.045
    row = decoded.cell_of(-4.8, lat)[0]
    line = decoded.blocked[row]
    best, run, start, best_start = 0, 0, 0, 0
    for col, blocked in enumerate(line):
        if blocked:
            run, start = 0, col + 1
        else:
            run += 1
            if run > best:
                best, best_start = run, start
    head = decoded.header
    return {
        "lat": lat,
        "row": row,
        "widest_clear_run_cells": best,
        "from_lon": head["lon0"] + best_start * head["dlon"],
        "to_lon": head["lon0"] + (best_start + best) * head["dlon"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
