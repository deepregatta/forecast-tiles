"""Native hash compatibility with the original manifest wire semantics."""

import gzip
import json
import random
from pathlib import Path

import pytest
from conftest import make_ensemble_cube, make_weather_cube

from ingest import publish
from ingest.tile import build_tiles
from ingest.validate import ValidationReport

FIXTURES = Path(__file__).parent / "fixtures"
STAMP = "2026-07-13T10:00:00Z"


def reference(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"a",
        b"foobar",
        b"\0",
        b"a\0b\0c",
        b"\xff" * 4097,
        bytes(range(256)) * 17,
        b"\x80" * 10000,
    ],
)
def test_edge_bytes_match_original(data):
    assert publish.fnv64(data) == reference(data)


def test_seeded_random_bytes_match_original():
    rng = random.Random(20261003)
    for size in (1, 7, 8, 15, 16, 31, 32, 255, 256, 1023, 1024, 65537, 1 << 20):
        data = rng.randbytes(size)
        assert publish.fnv64(data) == reference(data)


@pytest.mark.parametrize("layer", ["weather", "ensemble"])
def test_committed_goldens_match_original_and_expected(layer):
    base = FIXTURES / f"golden-N40W010-{layer}"
    gz = base.with_suffix(".bin.gz").read_bytes()
    raw = gzip.decompress(gz)
    expected = json.loads(base.with_suffix(".expected.json").read_text())
    assert publish.fnv64(raw) == expected["fnv64"] == reference(raw)
    assert publish.fnv64(gz) == reference(gz)


@pytest.mark.parametrize("layer", ["weather", "ensemble", "weather-arome"])
def test_complete_manifest_bytes_match_original(layer, monkeypatch):
    cube = make_ensemble_cube() if layer == "ensemble" else make_weather_cube()
    cube.layer = layer
    report = ValidationReport()
    tiles = build_tiles(cube, generated_at=STAMP)
    native = publish.build_manifest(cube, tiles, report, published_at=STAMP, validated_at=STAMP)
    with monkeypatch.context() as context:
        context.setattr(publish, "fnv64", reference)
        original = publish.build_manifest(
            cube, tiles, report, published_at=STAMP, validated_at=STAMP
        )
    assert publish.json_bytes(native) == publish.json_bytes(original)
    assert tiles == build_tiles(cube, generated_at=STAMP)
