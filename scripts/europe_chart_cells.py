"""Derive the `europe` chart cell set from GSHHG's full-resolution shoreline.

    uv run python scripts/europe_chart_cells.py [--gshhg gshhs_f.b] [--why]

The owner's coverage decision (2026-11-07, Tactician docs/offline-charts-plan.md,
"Stage 2 coverage"): Europe's and the UK's coasts only. No Atlantic islands
(the Azores, Madeira, the Canaries, Iceland, the Faroes) or other faraway
territories (Svalbard, Jan Mayen, Rockall); in the Mediterranean, the European
shore and its islands only, with no North Africa, Levant or Anatolia; the
Black Sea's European shore is included. Settled with the owner the same day:
Cyprus is in; Kola and the White Sea (Russia's Arctic coast) are out.

A z6 cell is kept when a level-1 shoreline point inside it is European by the
rules below. A kept cell is the whole z6 tile, so it also carries whatever
non-European shore shares it (Morocco across Gibraltar, Anatolia across the
Aegean). The list this prints is written into `ingest.charts.common` as
literal ids; this script is its provenance, not a build step.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest.charts.common import CELL_ZOOM, cell_id, lat_to_y, lon_to_x  # noqa: E402
from ingest.land.emodnet import EXTENT  # noqa: E402

HEADER = struct.Struct(">11i")
BOX = (-11.0, 34.0, 43.0, 71.5)  # west, south, east, north: everything Europe's coast can reach

# Faraway territories, by box (west, south, east, north).
FARAWAY = {
    "Faroes": (-7.8, 61.3, -6.2, 62.5),
    "Jan Mayen": (-9.2, 70.7, -7.8, 71.3),
}
# The Europe-Asia line through the Dardanelles, the Sea of Marmara and the
# Bosphorus: mainland south of it, between 26.0E and 29.15E, is Anatolia.
MARMARA = [
    (26.0, 39.9),
    (26.18, 40.03),
    (26.7, 40.4),
    (27.5, 40.62),
    (28.5, 40.85),
    (29.0, 41.0),
    (29.15, 41.25),
]
# Italian islands nearer Africa than Europe; every other small island south of
# 37.6N between 6W and 12E is North African (Tunisia's, Algeria's, Morocco's).
ITALIAN_SOUTH = [
    (11.8, 36.6, 12.1, 36.9),
    (12.2, 35.4, 12.95, 35.95),
]  # Pantelleria; Lampedusa, Linosa, Lampione


def polygons(path: Path):
    raw = path.read_bytes()
    off = 0
    while off < len(raw):
        pid, n, flag, *_ = HEADER.unpack_from(raw, off)
        body = off + HEADER.size
        off = body + n * 8
        if flag & 255 != 1:
            continue
        pts = np.frombuffer(raw, dtype=">i4", count=n * 2, offset=body).reshape(-1, 2) * 1e-6
        lon = np.where(pts[:, 0] >= 180.0, pts[:, 0] - 360.0, pts[:, 0])
        yield pid, lon, pts[:, 1]


def inside(box, lon, lat):
    w, s, e, n = box
    return (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)


def marmara_lat(lon):
    return np.interp(lon, [p[0] for p in MARMARA], [p[1] for p in MARMARA])


def classify(kind, lon, lat):
    """Per point: None if European, else the reason it is not."""
    why = np.full(lon.shape, None, dtype=object)
    for name, box in FARAWAY.items():
        why[inside(box, lon, lat)] = name
    if kind == "africa":
        why[:] = "North Africa"
        return why
    if kind == "island":
        south = (lat < 37.6) & (lon > -6.0) & (lon < 12.0)
        italian = np.zeros(lon.shape, bool)
        for box in ITALIAN_SOUTH:
            italian |= inside(box, lon, lat)
        why[south & ~inside((-6.0, 35.8, -2.0, 36.1), lon, lat) & ~italian] = "North African island"
        # Spain's islets off Morocco's coast are in Africa
        why[(lat < 35.5) & (lon > -5.0) & (lon < -2.0)] = "North African island"
        # Turkey's islets: in the Sea of Marmara, and east of Kastellorizo (29.58E, Greek)
        why[inside((26.9, 40.3, 29.7, 41.0), lon, lat)] = "Anatolia"
        why[(lon > 29.7) & (lat < 42.2) & (lon < 41.7)] = "Anatolia"
    else:
        # the Eurasian mainland
        anatolia = (lon > 26.0) & (lon <= 29.15) & (lat < marmara_lat(lon))
        anatolia |= (lon > 29.15) & (lat < 42.2) & (lon < 41.7)
        why[anatolia] = "Anatolia"
    why[(lon > 32.0) & (lat < 37.5)] = "Levant"
    why[(lon > 39.95) & (lat < 43.42) & (lat > 40.5)] = (
        "Caucasus"  # Georgia's coast, south of the Psou
    )
    why[(lon > 31.2) & (lat > 63.5)] = "Russian Arctic"  # Kola and the White Sea
    if kind == "island":
        # Cyprus is in, on the Levant's side of the sea (owner, 2026-11-07)
        why[inside((32.0, 34.4, 34.7, 35.8), lon, lat)] = None
    return why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gshhg", default=str(Path.home() / ".cache/deepregatta/land-cache/gshhs_f.b"))
    ap.add_argument("--why", action="store_true", help="also list the cells left out, and why")
    ap.add_argument(
        "--with",
        dest="extra",
        action="append",
        default=[],
        help="count a left-out reason as European (for example Russian Arctic)",
    )
    args = ap.parse_args(argv)
    kept, out = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for _pid, lon, lat in polygons(Path(args.gshhg)):
        if not inside(BOX, lon, lat).any():
            continue
        kind = (
            "africa" if lat.min() < -20 else "eurasia" if lon.max() - lon.min() > 100 else "island"
        )
        sel = inside(BOX, lon, lat)
        lo, la = lon[sel], lat[sel]
        why = classify(kind, lo, la)
        for x, y, w in zip(lo, la, why):
            c = cell_id(lon_to_x(x, CELL_ZOOM), lat_to_y(y, CELL_ZOOM))
            if w is None or w in args.extra:
                kept[c] += 1
            else:
                out[c][w] += 1
    w, s, e, n = EXTENT
    for c in kept:
        from ingest.charts.common import cell_area

        cw, cs, ce, cn = cell_area(c).bbox
        assert w <= cw and cs >= s and cw < e and cn <= n, f"{c} is outside EMODnet's DTM extent"
        if ce > e:
            print(
                f"  {c} reaches {ce:.2f}E, past EMODnet's {e}E: no depth there (land: the Caucasus, the Urals' side)"
            )
    cells = sorted(kept, key=lambda c: tuple(int(p) for p in c.split("-")))
    print(f"{len(cells)} cells")
    print(", ".join(cells))
    if args.why:
        for c in sorted(set(out) - set(kept), key=lambda c: tuple(int(p) for p in c.split("-"))):
            print(f"  left out {c}: {dict(out[c])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
