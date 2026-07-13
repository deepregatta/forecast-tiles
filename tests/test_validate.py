import numpy as np
from conftest import make_ensemble_cube, make_weather_cube

from ingest.cube import VariableSpec
from ingest.validate import validate_cube


def failing(report, prefix):
    return [f for f in report.failures if f.startswith(prefix)]


def test_valid_cube_passes():
    report = validate_cube(make_weather_cube())
    assert report.ok, report.summary()
    assert "gust_ge_wind[gust_kt]" in report.checks_passed
    assert any(c.startswith("physical_range") for c in report.checks_passed)


def test_physical_range_violation_fails():
    cube = make_weather_cube()
    cube.arrays["wind_u_kt"][1] = 500.0  # > 150 kt
    report = validate_cube(cube)
    assert not report.ok
    assert failing(report, "physical_range[wind_u_kt]")


def test_gust_below_wind_fails():
    cube = make_weather_cube()
    cube.arrays["gust_kt"] = np.maximum(
        np.hypot(cube.arrays["wind_u_kt"], cube.arrays["wind_v_kt"]) - 10.0, 0.0
    ).astype(np.float32)
    report = validate_cube(cube)
    assert failing(report, "gust_ge_wind[gust_kt]")


def test_missing_fraction_threshold():
    cube = make_weather_cube()
    cube.arrays["visibility_m"][..., ::2] = np.nan  # 50 % missing
    assert failing(validate_cube(cube), "missing_fraction[visibility_m]")
    assert validate_cube(cube, max_missing=0.6).ok


def test_non_monotonic_axis_fails():
    cube = make_weather_cube()
    cube.time_axes["hourly"] = [0, 2, 1]
    report = validate_cube(cube)
    assert failing(report, "axis_monotonic[hourly]")


def test_step_coverage_shape_mismatch_fails():
    cube = make_weather_cube()
    cube.arrays["wind_u_kt"] = cube.arrays["wind_u_kt"][:2]  # drop a step
    assert failing(validate_cube(cube), "step_coverage[wind_u_kt]")


def test_missing_array_fails():
    cube = make_weather_cube()
    del cube.arrays["gust_kt"]
    assert failing(validate_cube(cube), "present[gust_kt]")


def test_expected_axes_mismatch_fails():
    cube = make_weather_cube()
    report = validate_cube(cube, expected_axes={"hourly": [0, 1, 2, 3]})
    assert failing(report, "axis_complete[hourly]")
    assert validate_cube(cube, expected_axes={"hourly": [0, 1, 2]}).ok


def test_member_count_mismatch_fails():
    cube = make_ensemble_cube(members=5)
    cube.member_count = 31  # per-member arrays only have 5
    report = validate_cube(cube)
    assert failing(report, "step_coverage[wind_kt_anom]")


def test_ensemble_gust_mean_check():
    cube = make_ensemble_cube()
    gust_mean = cube.arrays["wind_kt_mean"] + 3.0
    cube.variables.append(VariableSpec("gust_kt_mean", "steps", "i16", 0.01))
    cube.arrays["gust_kt_mean"] = gust_mean
    assert "gust_ge_wind[gust_kt_mean]" in validate_cube(cube).checks_passed

    cube.arrays["gust_kt_mean"] = cube.arrays["wind_kt_mean"] - 5.0
    assert failing(validate_cube(cube), "gust_ge_wind[gust_kt_mean]")


def test_anomaly_out_of_clip_range_fails():
    cube = make_ensemble_cube()
    cube.arrays["wind_kt_anom"][0] = 30.0  # beyond the ±25 kt clip
    assert failing(validate_cube(cube), "physical_range[wind_kt_anom]")
