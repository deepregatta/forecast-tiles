#!/usr/bin/env python3
"""Generate the TCP1 golden fixture (tests/fixtures/chart-pack-cherbourg/).

One tile per zoom, z6-14, over Cherbourg harbour (z14 tile 8118/5580), without
the world overview, with its depth companion: a *real* pack built by the same
code path as every archive this repository publishes, from the inputs pinned
in cherbourg.json, so a consumer's contract test reads what the producer
writes.

Writes:
  tests/fixtures/chart-pack-cherbourg/cherbourg.pmtiles
  tests/fixtures/chart-pack-cherbourg/cherbourg-depth.pmtiles
  tests/fixtures/chart-pack-cherbourg/cherbourg.expected.json   what readers assert

The same three files are vendored in tactician (`core/charts/tests/fixtures/`)
and pinned there. A fixture that legitimately changes is a recorded decision
in both repositories, not a silent regeneration. The bytes were first written
by tactician tools/charts on 2026-11-07; this generator reproduces them byte
for byte from the same cached inputs (checked 2026-11-07).

Needs the sources cached under DEEPREGATTA_CHARTS_CACHE (default
~/.cache/deepregatta/charts): the pinned Protomaps build expires upstream after
a while, so a rebuild without that cache needs newer pins, and the new bytes
are then a deliberate golden change. Pass --out to write elsewhere and compare.

    uv sync --extra charts
    uv run python scripts/make_chart_fixture.py [--out DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ingest.charts.build import FIXTURE_DIR, fixture  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(FIXTURE_DIR))
    args = ap.parse_args()
    print(fixture(Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
