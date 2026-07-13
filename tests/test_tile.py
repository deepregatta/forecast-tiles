import gzip

import numpy as np
from conftest import make_ensemble_cube, make_weather_cube

from ingest.tile import build_tiles
from tilekit.codec import decode_tile

GENERATED_AT = "2026-07-13T10:00:00Z"


def decode(gz: bytes):
    return decode_tile(gzip.decompress(gz))


def test_cube_round_trips_through_tiles():
    cube = make_weather_cube()
    tiles = dict(build_tiles(cube, generated_at=GENERATED_AT))
    assert set(tiles) == {"N40W010", "N40E000"}

    for tid, cols in (("N40W010", slice(0, 4)), ("N40E000", slice(4, 8))):
        tile = decode(tiles[tid])
        h = tile.header
        assert h["run_id"] == "weather-20260713T06Z"
        assert h["cycle"] == "2026-07-13T06:00Z"
        assert h["generated_at"] == GENERATED_AT
        assert (h["nlat"], h["nlon"]) == (4, 4)
        assert h["lon0"] == (-10.0 if tid == "N40W010" else 0.0)
        assert h["lat0"] == 40.0
        assert h["time_axes"]["hourly"]["offsets_h"] == [0, 1, 2]
        for name in ("wind_u_kt", "wind_v_kt", "gust_kt", "visibility_m"):
            spec = cube.var(name)
            expected = cube.arrays[name][..., :, cols]
            got = tile.arrays[name]
            assert np.array_equal(np.isnan(got), np.isnan(expected))
            mask = ~np.isnan(expected)
            assert np.max(np.abs(got[mask] - expected[mask])) <= spec.scale / 2 + 1e-6


def test_fully_missing_tile_skipped():
    cube = make_weather_cube(east_all_missing=True)
    tiles = dict(build_tiles(cube, generated_at=GENERATED_AT))
    assert set(tiles) == {"N40W010"}


def test_gzip_deterministic():
    a = build_tiles(make_weather_cube(), generated_at=GENERATED_AT)
    b = build_tiles(make_weather_cube(), generated_at=GENERATED_AT)
    assert [t for t, _ in a] == [t for t, _ in b]
    assert all(ga == gb for (_, ga), (_, gb) in zip(a, b))


def test_per_member_tiles():
    cube = make_ensemble_cube(members=5)
    tiles = dict(build_tiles(cube, generated_at=GENERATED_AT))
    tile = decode(tiles["N40W010"])
    assert tile.header["member_count"] == 5
    assert tile.arrays["wind_kt_anom"].shape == (5, 3, 4, 4)
    expected = cube.arrays["wind_kt_anom"]
    assert np.max(np.abs(tile.arrays["wind_kt_anom"] - expected)) <= 0.1 + 1e-6


def test_prequantized_int_arrays_accepted():
    from tilekit.codec import quantize

    cube = make_weather_cube()
    for name in list(cube.arrays):
        cube.arrays[name] = quantize(cube.arrays[name], cube.var(name).dtype, cube.var(name).scale)
    tiles = dict(build_tiles(cube, generated_at=GENERATED_AT))
    assert set(tiles) == {"N40W010", "N40E000"}
    tile = decode(tiles["N40W010"])
    assert np.isnan(tile.arrays["wind_u_kt"][0, 0, 0])
