import numpy as np
import pytest

from conftest import make_ensemble_cube, make_weather_cube

from ingest.cube import axis_offsets
from ingest.sources import cmems, ecmwf_open, gefs, gfs, gfswave
from tilekit.codec import quantize


def test_axis_offsets_segments():
    assert axis_offsets((0, 6, 3)) == [0, 3, 6]
    assert axis_offsets((0, 4, 1), (6, 12, 3)) == [0, 1, 2, 3, 4, 6, 9, 12]
    with pytest.raises(ValueError, match="overlap"):
        axis_offsets((0, 6, 3), (6, 12, 3))


def test_committed_layer_axes_match_phase0():
    # weather: hourly 1 h -> 120 h + 3 h -> 240 h (161); h3 3 h -> 240 h (81)
    assert len(gfs.HOURLY_AXIS) == 161
    assert gfs.HOURLY_AXIS[:3] == [0, 1, 2]
    assert gfs.HOURLY_AXIS[119:123] == [119, 120, 123, 126]
    assert gfs.HOURLY_AXIS[-1] == 240
    assert len(gfs.H3_AXIS) == 81
    assert set(gfs.H3_AXIS) <= set(gfs.HOURLY_AXIS)
    # ensemble: 3 h -> 144 h + 6 h -> 384 h (89)
    assert len(gefs.STEP_AXIS) == 89
    assert 144 in gefs.STEP_AXIS and 147 not in gefs.STEP_AXIS and 150 in gefs.STEP_AXIS
    assert gefs.STEP_AXIS[-1] == 384
    # waves: 3 h -> 384 h (129)
    assert len(gfswave.STEP_AXIS) == 129
    # currents: 6 h -> 240 h (41)
    assert len(cmems.STEP_AXIS) == 41
    # ecmwf: 3 h -> 144 h + 6 h -> 240 h (65)
    assert len(ecmwf_open.STEP_AXIS) == 65
    assert ecmwf_open.STEP_AXIS[-1] == 240


def test_run_id_and_cycle_iso():
    cube = make_weather_cube()
    assert cube.run_id == "weather-20260713T06Z"
    assert cube.cycle_iso == "2026-07-13T06:00Z"
    assert cube.horizon_h == 3
    assert cube.header_time_axes()["hourly"] == {
        "base": "2026-07-13T06:00Z",
        "offsets_h": [0, 1, 2],
    }


def test_decoded_dequantizes_ints_and_passes_floats():
    cube = make_weather_cube()
    floats = cube.decoded("wind_u_kt")
    assert np.isnan(floats[0, 0, 0])

    quantized = quantize(cube.arrays["wind_u_kt"], "i16", 0.01)
    cube.arrays["wind_u_kt"] = quantized
    decoded = cube.decoded("wind_u_kt")
    assert decoded.dtype == np.float32
    assert np.isnan(decoded[0, 0, 0])
    mask = ~np.isnan(floats)
    assert np.max(np.abs(decoded[mask] - floats[mask])) <= 0.005 + 1e-6


def test_expected_shape_per_member():
    cube = make_ensemble_cube(members=5)
    assert cube.expected_shape(cube.var("wind_kt_mean")) == (3, 4, 4)
    assert cube.expected_shape(cube.var("wind_kt_anom")) == (5, 3, 4, 4)
