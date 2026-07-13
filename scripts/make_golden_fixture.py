#!/usr/bin/env python3
"""Generate the shared golden fixtures that both the Python and TypeScript
codecs must decode identically (see the passage repo's forecast-tiles spec).

Writes, for each fixture:
  tests/fixtures/golden-N40W010-{layer}.bin.gz     deterministic gzip (mtime=0)
  tests/fixtures/golden-N40W010-{layer}.expected.json
      { fnv64: <hash of the UNCOMPRESSED PFT1 bytes>, header, arrays }

Values are analytic functions of (member, time, lat-index, lon-index) so the
fixture is reproducible from this script alone. Expected arrays are the
decoded float32 values rounded to 5 decimals; consumers compare with
atol <= 1e-3 (covers float32 vs float64 arithmetic differences).
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tilekit.codec import decode_tile, encode_tile  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def fnv64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def field(shape: tuple[int, ...], base: float, coeffs: tuple[float, ...]) -> np.ndarray:
    """Analytic field: base + sum(coeff_k * index_k), NaN at the all-zero index."""
    grids = np.indices(shape, dtype=np.float64)
    values = base + sum(c * g for c, g in zip(coeffs, grids))
    values[(0,) * len(shape)] = np.nan
    return values.astype(np.float32)


def write_fixture(name: str, header: dict, arrays: dict[str, np.ndarray]) -> None:
    raw = encode_tile(header, arrays)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.bin.gz").write_bytes(gzip.compress(raw, 9, mtime=0))

    decoded = decode_tile(raw)
    expected = {
        "fnv64": fnv64(raw),
        "header": decoded.header,
        "arrays": {
            k: np.where(np.isnan(v), None, np.round(v.astype(np.float64), 5)).tolist()
            for k, v in decoded.arrays.items()
        },
    }
    (OUT_DIR / f"{name}.expected.json").write_text(json.dumps(expected, indent=1) + "\n")
    print(f"{name}: {len(raw)} bytes raw, fnv64={expected['fnv64']}")


def base_header(**overrides) -> dict:
    header = {
        "spec": "PFT1",
        "schema_version": 1,
        "layer": "weather",
        "model": "gfs_0p25",
        "run_id": "weather-20260713T06Z",
        "cycle": "2026-07-13T06:00Z",
        "generated_at": "2026-07-13T10:00:00Z",
        "tile_id": "N40W010",
        "lat0": 40.0,
        "lon0": -10.0,
        "dlat": 0.25,
        "dlon": 0.25,
        "nlat": 8,
        "nlon": 8,
        "member_count": 1,
        "provenance": {"source": "golden fixture (scripts/make_golden_fixture.py)"},
    }
    header.update(overrides)
    return header


def main() -> None:
    weather = base_header(
        time_axes={
            "hourly": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 1, 2, 3]},
            "h3": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 3]},
        },
        variables=[
            {"name": "wind_u_kt", "axis": "hourly", "dtype": "i16", "scale": 0.01},
            {"name": "wind_v_kt", "axis": "hourly", "dtype": "i16", "scale": 0.01},
            {"name": "gust_kt", "axis": "hourly", "dtype": "i16", "scale": 0.1},
            {"name": "visibility_m", "axis": "h3", "dtype": "i16", "scale": 50},
        ],
    )
    write_fixture(
        "golden-N40W010-weather",
        weather,
        {
            "wind_u_kt": field((4, 8, 8), -12.0, (0.7, 1.3, 0.11)),
            "wind_v_kt": field((4, 8, 8), 8.0, (-0.5, 0.2, 0.9)),
            "gust_kt": field((4, 8, 8), 18.0, (1.1, 0.4, 0.3)),
            "visibility_m": field((2, 8, 8), 12000.0, (-800.0, 150.0, 250.0)),
        },
    )

    ensemble = base_header(
        layer="ensemble",
        model="gefs_0p50",
        run_id="ensemble-20260713T06Z",
        dlat=0.5,
        dlon=0.5,
        member_count=5,
        time_axes={"steps": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 3, 6]}},
        variables=[
            {"name": "wind_kt_mean", "axis": "steps", "dtype": "i16", "scale": 0.01},
            {
                "name": "wind_kt_anom",
                "axis": "steps",
                "dtype": "i8",
                "scale": 0.2,
                "per_member": True,
            },
        ],
    )
    write_fixture(
        "golden-N40W010-ensemble",
        ensemble,
        {
            "wind_kt_mean": field((3, 8, 8), 15.0, (0.9, 0.25, 0.15)),
            "wind_kt_anom": field((5, 3, 8, 8), -6.0, (2.8, 0.6, 0.2, 0.1)),
        },
    )


if __name__ == "__main__":
    main()
