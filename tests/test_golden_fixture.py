"""The golden fixtures are the cross-language contract: this decoder and the
passage TypeScript decoder must produce identical output for them. The same
files (and expected JSON) are vendored into the passage repo."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest

from tilekit.codec import decode_tile

FIXTURES = Path(__file__).parent / "fixtures"


def fnv64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


@pytest.mark.parametrize("name", ["golden-N40W010-weather", "golden-N40W010-ensemble"])
def test_golden_fixture_decodes_to_expected(name):
    raw = gzip.decompress((FIXTURES / f"{name}.bin.gz").read_bytes())
    expected = json.loads((FIXTURES / f"{name}.expected.json").read_text())

    assert fnv64(raw) == expected["fnv64"], "fixture bytes drifted from expected JSON"

    tile = decode_tile(raw)
    assert tile.header == expected["header"]
    for var_name, exp in expected["arrays"].items():
        got = tile.arrays[var_name]
        exp_arr = np.array(
            [x if x is not None else np.nan for x in np.array(exp, dtype=object).ravel()],
            dtype=np.float64,
        ).reshape(got.shape)
        assert np.array_equal(np.isnan(got), np.isnan(exp_arr)), f"{var_name}: NaN mask differs"
        mask = ~np.isnan(exp_arr)
        assert np.max(np.abs(got[mask] - exp_arr[mask])) <= 1e-3, f"{var_name}: values differ"
