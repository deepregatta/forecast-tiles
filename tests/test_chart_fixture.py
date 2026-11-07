"""The TCP1 golden fixture reads as cherbourg.expected.json says, and passes
the reader's checks. Consumers (tactician `core/charts`) pin these bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("pmtiles")

from pmtiles.reader import MmapSource, Reader, all_tiles  # noqa: E402

from ingest.charts import check, mvt  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chart-pack-cherbourg"
EXPECTED = json.loads((FIXTURE / "cherbourg.expected.json").read_text())


@pytest.mark.parametrize("name", ["cherbourg.pmtiles", "cherbourg-depth.pmtiles"])
def test_the_fixture_reads_as_expected(name):
    path = FIXTURE / name
    want = EXPECTED[name]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == want["sha256"]
    assert path.stat().st_size == want["bytes"]
    with open(path, "rb") as f:
        r = Reader(MmapSource(f))
        h, md = r.header(), r.metadata()
        tiles = {
            f"{z}/{x}/{y}": {layer.name: len(layer.features) for layer in mvt.parse(data).layers}
            for (z, x, y), data in sorted(all_tiles(r.get_bytes))
        }
    assert (h["min_zoom"], h["max_zoom"]) == (
        want["header"]["min_zoom"],
        want["header"]["max_zoom"],
    )
    for key, value in want["metadata"].items():
        if key == "source_ids":
            assert [s["id"] for s in md["sources"]] == value
        else:
            assert md[key] == value
    assert tiles == want["tiles"]
    assert check.check(path, check.style_fields(), quiet=True) == []


def test_the_fixture_names_its_generator_inputs():
    spec = json.loads((FIXTURE / "cherbourg.json").read_text())
    md = EXPECTED["cherbourg.pmtiles"]["metadata"]
    assert spec["build_id"] == md["build_id"] and spec["built"] == md["built"]
    assert spec["depth"] == "separate" and md["depth"]["archive"] == "cherbourg-depth.pmtiles"
