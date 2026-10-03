from dataclasses import replace

import numpy as np
import pytest
from conftest import make_ensemble_cube, make_ibi_cube, make_weather_cube

from ingest.cube import Statistic, VariableSpec
from ingest.validate import validate_cube
from tilekit.codec import quantize


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


def test_statistic_windows_must_label_every_step_with_data():
    cube = make_weather_cube()
    idx = next(i for i, v in enumerate(cube.variables) if v.name == "gust_kt")
    gust = cube.variables[idx]

    def with_windows(kind, windows):
        cube.variables[idx] = replace(gust, statistic=Statistic(kind, windows))
        return validate_cube(cube)

    report = with_windows("max", (1, 1, 1))
    assert report.ok and "statistic_windows[gust_kt]" in report.checks_passed
    assert failing(
        with_windows("max", (None, 1, 1)), "statistic_windows[gust_kt]"
    )  # step 0 has data
    assert failing(with_windows("max", (1, 1)), "statistic_windows[gust_kt]")  # wrong length
    assert failing(with_windows("mean", (1, 1, 1)), "statistic_windows[gust_kt]")
    cube.arrays["gust_kt"][0] = np.nan  # no data at step 0: no window needed there
    assert "statistic_windows[gust_kt]" in with_windows("max", (None, 1, 1)).checks_passed


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


def test_ibi_coastal_missing_threshold_is_deliberate():
    from ingest import cli
    from ingest.sources import ibi

    assert cli.MAX_MISSING[ibi.LAYER] == 0.45
    assert validate_cube(
        make_ibi_cube(missing_fraction=0.40),
        max_missing=cli.MAX_MISSING[ibi.LAYER],
        expected_axes={ibi.AXIS_NAME: ibi.STEP_AXIS},
    ).ok
    report = validate_cube(
        make_ibi_cube(missing_fraction=0.50),
        max_missing=cli.MAX_MISSING[ibi.LAYER],
        expected_axes={ibi.AXIS_NAME: ibi.STEP_AXIS},
    )
    assert failing(report, "missing_fraction[cur_u_kt]")


def test_ibi_current_physical_range_is_enforced():
    cube = make_ibi_cube()
    cube.arrays["cur_v_kt"][0, 1, 1] = 20.0
    assert failing(validate_cube(cube, max_missing=0.45), "physical_range[cur_v_kt]")


@pytest.mark.parametrize("quantized", [False, True])
@pytest.mark.parametrize("missing_points", [8, 32])
def test_global_required_slice_cannot_hide_in_cube_average(quantized, missing_points):
    cube = make_weather_cube()
    cube.time_axes["h3"] = list(range(0, 120, 3))
    values = np.full((40, 4, 8), 10_000, np.float32)
    values[20].flat[:missing_points] = np.nan
    assert np.isnan(values).mean() < 0.05
    spec = cube.var("visibility_m")
    cube.arrays[spec.name] = quantize(values, spec.dtype, spec.scale) if quantized else values
    report = validate_cube(cube)
    assert failing(report, "missing_fraction[visibility_m]")
    assert "+60 h" in report.summary()


@pytest.mark.parametrize("whole_member", [False, True])
def test_each_ensemble_member_requires_every_slice(whole_member):
    cube = make_ensemble_cube(members=31)
    cube.arrays["wind_kt_anom"][7, slice(None) if whole_member else 1] = np.nan
    assert np.isnan(cube.arrays["wind_kt_anom"]).mean() < 0.05
    report = validate_cube(cube)
    assert failing(report, "missing_fraction[wind_kt_anom]")
    assert "member 7" in report.summary()


def test_land_mask_does_not_exempt_an_empty_ocean_time_slice():
    cube = make_ibi_cube(missing_fraction=0.40)
    assert validate_cube(cube, max_missing=0.45).ok
    cube.arrays["cur_u_kt"][60] = np.nan
    assert np.isnan(cube.arrays["cur_u_kt"]).mean() < 0.45
    report = validate_cube(cube, max_missing=0.45)
    assert failing(report, "missing_fraction[cur_u_kt]")
    assert "+60 h" in report.summary()


def test_declared_missing_offsets_are_applied_to_global_variables_only_there():
    cube = make_weather_cube()
    cube.arrays["visibility_m"][0] = np.nan
    report = validate_cube(cube, allowed_missing_steps={"visibility_m": [0]})
    assert report.ok, report.summary()
    cube.arrays["visibility_m"][1] = np.nan
    assert failing(
        validate_cube(cube, allowed_missing_steps={"visibility_m": [0]}),
        "missing_fraction[visibility_m]",
    )


def test_footprint_validation_checks_each_member_and_preserves_the_outline():
    cube = make_ensemble_cube(members=31)
    footprint = np.ones((4, 4), dtype=bool)
    footprint[:, 0] = False
    for arr in cube.arrays.values():
        arr[..., ~footprint] = np.nan
    options = {"footprint": footprint, "max_interior_missing": 0.005}
    assert validate_cube(cube, **options).ok
    cube.arrays["wind_kt_anom"][7, 1] = np.nan
    report = validate_cube(cube, **options)
    assert failing(report, "interior_missing[wind_kt_anom]")
    assert "member 7 +3 h" in report.summary()
    assert validate_cube(cube, allowed_missing_steps={"wind_kt_anom": [3]}, **options).ok


def test_an_empty_expected_footprint_is_unusable():
    report = validate_cube(
        make_weather_cube(), footprint=np.zeros((4, 8), dtype=bool), max_interior_missing=0.005
    )
    assert not report.ok


def test_a_fully_masked_required_slice_needs_data_even_with_a_permissive_limit():
    cube = make_weather_cube()
    cube.arrays["visibility_m"][0] = np.nan
    assert failing(validate_cube(cube, max_missing=1), "missing_fraction[visibility_m]")
