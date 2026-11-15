#!/usr/bin/env python3
"""Cut the TCA1 golden fixture from SHOM's Golfe normand-breton atlas.

    uv run --extra atlas python scripts/make_atlas_fixture.py SOURCE.nc

A 12 x 16 node window over the Alderney Race (Raz Blanchard), both
coefficients, every hour: `tests/fixtures/tidal-atlas-alderney/alderney.tca1.gz`
and `alderney.expected.json`, the values a reader must decode. The source is
SHOM's (Licence Ouverte 2.0, credited in the header); run once, then pinned in
both repositories' shared-contract locks. Never regenerated to pass a test.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ingest.atlas import codec  # noqa: E402
from ingest.atlas.build import read  # noqa: E402
from ingest.atlas.sources import BY_ID  # noqa: E402

ROWS = slice(270, 282)
COLS = slice(190, 206)
OUT = ROOT / "tests/fixtures/tidal-atlas-alderney"


def main(source: str) -> None:
    header, values = read(Path(source), BY_ID["golfe-normand-breton"])
    cut = values[:, :, ROWS, COLS, :].copy()
    header = dict(header)
    header["grid"] = "alderney-fixture"
    header["title"] = "Alderney Race, cut from the Golfe normand-breton atlas (test fixture)"
    header["lat0"] = round(header["lat0"] + ROWS.start * header["dlat"], 9)
    header["lon0"] = round(header["lon0"] + COLS.start * header["dlon"], 9)
    header["rows"], header["columns"] = cut.shape[2], cut.shape[3]
    data = codec.encode(header, cut)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "alderney.tca1.gz").write_bytes(data)
    probes = []
    for c, s, r, k in [(0, 0, 0, 0), (1, 6, 6, 8), (1, 2, 11, 15), (0, 12, 3, 4), (1, 9, 5, 10)]:
        u, v = (int(x) for x in cut[c, s, r, k])
        probes.append(
            {
                "coefficient": codec.COEFFICIENTS[c],
                "offset_min": header["offsets_min"][s],
                "row": r,
                "column": k,
                "u": u,
                "v": v,
            }
        )
    missing = int((cut[..., 0] == codec.MISSING).sum())
    expected = {
        "file": "alderney.tca1.gz",
        "sha256": hashlib.sha256(data).hexdigest(),
        "header": header,
        "missing_nodes": missing,
        "probes": probes,
        "sum_u": int(cut[..., 0][cut[..., 0] != codec.MISSING].astype("i8").sum()),
        "sum_v": int(cut[..., 1][cut[..., 1] != codec.MISSING].astype("i8").sum()),
    }
    (OUT / "alderney.expected.json").write_text(
        json.dumps(expected, indent=1, ensure_ascii=False) + "\n"
    )
    print(json.dumps({"bytes": len(data), "missing_nodes": missing, "probes": probes}))


if __name__ == "__main__":
    main(sys.argv[1])
