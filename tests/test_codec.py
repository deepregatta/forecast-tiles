import gzip

import numpy as np
import pytest

from tilekit.codec import DTYPES, decode_tile, encode_tile, quantize
from tilekit.tiles import tile_id, tile_origin, tiles_for_grid


def make_header(**overrides):
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
        "nlat": 4,
        "nlon": 4,
        "time_axes": {
            "hourly": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 1, 2]},
            "h3": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 3]},
        },
        "member_count": 1,
        "variables": [],
    }
    header.update(overrides)
    return header


def test_round_trip_with_missing_values():
    rng = np.random.default_rng(7)
    wind_u = rng.uniform(-40, 40, size=(3, 4, 4)).astype(np.float32)
    wind_u[0, 0, 0] = np.nan  # land / missing
    vis = rng.uniform(0, 20000, size=(2, 4, 4)).astype(np.float32)

    header = make_header(
        variables=[
            {"name": "wind_u_kt", "axis": "hourly", "dtype": "i16", "scale": 0.01},
            {"name": "visibility_m", "axis": "h3", "dtype": "i16", "scale": 50},
        ]
    )
    buf = encode_tile(header, {"wind_u_kt": wind_u, "visibility_m": vis})
    tile = decode_tile(buf)

    assert tile.header["tile_id"] == "N40W010"
    assert np.isnan(tile.arrays["wind_u_kt"][0, 0, 0])
    mask = ~np.isnan(wind_u)
    assert np.max(np.abs(tile.arrays["wind_u_kt"][mask] - wind_u[mask])) <= 0.01 / 2 + 1e-6
    assert np.max(np.abs(tile.arrays["visibility_m"] - vis)) <= 50 / 2 + 1e-6


def test_per_member_i8_anomalies():
    members, times, n = 31, 5, 4
    rng = np.random.default_rng(11)
    anomalies = rng.uniform(-24, 24, size=(members, times, n, n)).astype(np.float32)
    anomalies[3, 2, 1, 0] = np.nan
    header = make_header(
        layer="ensemble",
        model="gefs_0p50",
        member_count=members,
        time_axes={"h3": {"base": "2026-07-13T06:00Z", "offsets_h": [0, 3, 6, 9, 12]}},
        variables=[
            {
                "name": "wind_kt_anom",
                "axis": "h3",
                "dtype": "i8",
                "scale": 0.2,
                "per_member": True,
            }
        ],
    )
    tile = decode_tile(encode_tile(header, {"wind_kt_anom": anomalies}))
    got = tile.arrays["wind_kt_anom"]
    assert got.shape == (members, times, n, n)
    assert np.isnan(got[3, 2, 1, 0])
    mask = ~np.isnan(anomalies)
    assert np.max(np.abs(got[mask] - anomalies[mask])) <= 0.2 / 2 + 1e-6


def test_quantize_clamps_and_reserves_sentinel():
    for dtype, (np_dtype, sentinel) in DTYPES.items():
        info = np.iinfo(np_dtype)
        raw = quantize(np.array([1e9, -1e9, np.nan]), dtype, 0.01)
        assert raw[0] == info.max
        assert raw[1] == info.min + 1  # sentinel excluded from data range
        assert raw[2] == sentinel


def test_int_passthrough_matches_prequantized():
    values = np.array([[[1.234, -5.678]]], dtype=np.float32)
    header = make_header(
        nlat=1,
        nlon=2,
        time_axes={"hourly": {"base": "2026-07-13T06:00Z", "offsets_h": [0]}},
        variables=[{"name": "v", "axis": "hourly", "dtype": "i16", "scale": 0.01}],
    )
    from_float = encode_tile(header, {"v": values})
    from_int = encode_tile(header, {"v": quantize(values, "i16", 0.01)})
    assert from_float == from_int


def test_shape_mismatch_rejected():
    header = make_header(variables=[{"name": "v", "axis": "hourly", "dtype": "i16", "scale": 0.01}])
    with pytest.raises(ValueError, match="shape"):
        encode_tile(header, {"v": np.zeros((2, 4, 4), dtype=np.float32)})


def test_header_padding_alignment():
    # vary tile_id length to shift header size across padding boundaries
    for pad_seed in range(4):
        header = make_header(
            run_id="weather-20260713T06Z" + "x" * pad_seed,
            variables=[{"name": "v", "axis": "hourly", "dtype": "i16", "scale": 0.01}],
        )
        values = np.full((3, 4, 4), 7.25, dtype=np.float32)
        tile = decode_tile(encode_tile(header, {"v": values}))
        assert np.allclose(tile.arrays["v"], 7.25, atol=0.005)


def test_gzip_round_trip():
    header = make_header(variables=[{"name": "v", "axis": "hourly", "dtype": "i16", "scale": 0.01}])
    values = np.zeros((3, 4, 4), dtype=np.float32)
    buf = encode_tile(header, {"v": values})
    assert decode_tile(gzip.decompress(gzip.compress(buf))).header == decode_tile(buf).header


def test_tile_ids():
    assert tile_id(40, -10) == "N40W010"
    assert tile_id(-10, 170) == "S10E170"
    assert tile_id(0, 0) == "N00E000"
    assert tile_origin(49.999, -0.001) == (40, -10)
    assert tile_origin(50.0, 0.0) == (50, 0)
    assert tile_origin(45.0, 185.0) == (40, -180)  # 0-360 input normalized


def test_global_tile_count():
    assert len(tiles_for_grid(-90, 90, -180, 180)) == 18 * 36


def test_payload_alignment_after_odd_i8_array():
    # 1x3x3 i8 array (9 bytes, odd) followed by an i16 array: the i16
    # byte_offset must land on a 4-byte boundary
    header = make_header(
        nlat=3,
        nlon=3,
        time_axes={"hourly": {"base": "2026-07-13T06:00Z", "offsets_h": [0]}},
        variables=[
            {"name": "anom", "axis": "hourly", "dtype": "i8", "scale": 0.2},
            {"name": "mean", "axis": "hourly", "dtype": "i16", "scale": 0.01},
        ],
    )
    anom = np.full((1, 3, 3), 1.4, dtype=np.float32)
    mean = np.full((1, 3, 3), 12.34, dtype=np.float32)
    tile = decode_tile(encode_tile(header, {"anom": anom, "mean": mean}))
    mean_var = next(v for v in tile.header["variables"] if v["name"] == "mean")
    assert mean_var["byte_offset"] % 4 == 0
    assert np.allclose(tile.arrays["mean"], 12.34, atol=0.005)
    assert np.allclose(tile.arrays["anom"], 1.4, atol=0.1)
