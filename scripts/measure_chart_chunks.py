"""What a dock fetch moves at each digest chunk size, from the published cells.

    uv run --extra charts python scripts/measure_chart_chunks.py [--origin URL]

A TCP1 client proves every byte it keeps by the whole chunk around it, so a
fetch moves every chunk that holds a tile of its area. This reads the
published pointer and each needed cell's directories (never its tiles), then
counts, per race area and chunk size, the bytes the area's tiles need, the
bytes the fetch would move, and how long the pointer's chunk lists would be.
It follows Tactician `core/charts` `fold`: z6-14 tiles of the area's box from
each cell, z6-12 from its depth companion, the world overview whole.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import requests  # noqa: E402

from ingest.charts.common import cells_over, tiles_in  # noqa: E402
from ingest.charts.sources import RemoteArchive  # noqa: E402

AREAS = {
    # Tactician core/shore/tests/live_charts.rs
    "Solent and the Needles, 20 NM margin": (-2.1, 50.3, -0.6, 51.1),
    "Cherbourg to Saint-Malo, 30 NM margin": (-3.0, 48.4, -1.0, 50.1),
    "RORC Channel race area": (-2.3607, 49.0145, 0.9577, 51.6021),
    "the whole stage 1 box": (-6.5, 48.3, 2.5, 51.3),
}
SIZES = [1 << 20, 512 << 10, 256 << 10, 128 << 10, 64 << 10]
# one digest in the pointer: 16 hex digits, two quotes and a comma
DIGEST_BYTES = 19


def moved(ranges, size: int, total: int, chunk: int) -> int:
    chunks = {0}  # the header and root directory
    for off, ln in ranges:
        chunks.update(range(off // chunk, (off + ln - 1) // chunk + 1))
    return sum(min(chunk, total - c * chunk) for c in chunks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--origin", default="https://forecast.deepregatta.com")
    args = ap.parse_args(argv)
    latest = requests.get(f"{args.origin}/charts/latest.json", timeout=60).json()
    world = latest["world"]["bytes"]
    total_bytes = world + sum(
        c["bytes"] + (c["depth"]["bytes"] if c.get("depth") else 0)
        for c in latest["cells"].values()
    )
    print(
        f"build {latest['build_id']}: {len(latest['cells'])} cells, {total_bytes / 2**20:.1f} MiB"
    )
    archives = {}

    def archive(key):
        if key not in archives:
            archives[key] = RemoteArchive(
                f"published-{key.replace('/', '_')}", f"{args.origin}/{key}"
            )
        return archives[key]

    rows = []
    for label, box in AREAS.items():
        need = {s: world for s in SIZES}
        kept = world
        for cell in cells_over(box):
            entry = latest["cells"].get(cell)
            if entry is None:
                continue
            for obj, top in ((entry, 14), (entry.get("depth"), 12)):
                if obj is None:
                    continue
                want = [t for z in range(6, top + 1) for t in tiles_in(box, z)]
                found = archive(obj["key"]).entries(want)
                # a cell's tiles are stored once however many ids share them
                ranges = sorted(set(found.values()))
                kept += sum(ln for _, ln in ranges)
                for s in SIZES:
                    need[s] += moved(ranges, s, obj["bytes"], s)
        rows.append((label, kept, need))
    print("| Race area | Tiles kept | " + " | ".join(f"{s >> 10} KiB" for s in SIZES) + " |")
    print("|---|---|" + "---|" * len(SIZES))
    for label, kept, need in rows:
        cells = " | ".join(f"{need[s] / 2**20:.1f} ({need[s] / kept:.2f}x)" for s in SIZES)
        print(f"| {label} | {kept / 2**20:.1f} | {cells} |")
    print(json.dumps({f"{s >> 10} KiB": round(total_bytes / s * DIGEST_BYTES) for s in SIZES}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
