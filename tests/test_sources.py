"""Pure-logic source tests — no network anywhere."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ingest.sources import base, cmems, ecmwf_open, gefs, gfs, gfswave, ibi
from ingest.sources.base import (
    MS_TO_KT,
    CycleNotAvailableError,
    SourceVar,
    parse_cycle_arg,
    parse_idx,
    resolve_cycle,
    transform_units,
)

CYCLE = datetime(2026, 7, 13, 6, tzinfo=timezone.utc)

SAMPLE_IDX = """\
1:0:d=2026071306:UGRD:10 m above ground:1 hour fcst:
2:120000:d=2026071306:VGRD:10 m above ground:1 hour fcst:
3:240000:d=2026071306:GUST:surface:1 hour fcst:
4:361234:d=2026071306:SWDIR:1 in sequence:1 hour fcst:
"""


def test_parse_idx_ranges():
    rows = parse_idx(SAMPLE_IDX)
    assert rows[0] == ("UGRD", "10 m above ground", 0, 120000)
    assert rows[2] == ("GUST", "surface", 240000, 361234)
    assert rows[3] == ("SWDIR", "1 in sequence", 361234, None)  # last message: open-ended


def test_parse_cycle_arg():
    assert parse_cycle_arg("20260713T06") == CYCLE


def test_resolve_cycle_requested_and_lookback(monkeypatch):
    template = "https://x/{date}/{hh}/final.idx"

    monkeypatch.setattr(base, "head_ok", lambda url: False)
    with pytest.raises(CycleNotAvailableError):
        resolve_cycle(template, CYCLE)

    monkeypatch.setattr(base, "head_ok", lambda url: True)
    assert resolve_cycle(template, CYCLE) == CYCLE

    # latest cycle incomplete -> falls back exactly one cycle, never partial
    now = datetime.now(timezone.utc)
    latest = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    complete = latest - timedelta(hours=6)
    monkeypatch.setattr(
        base,
        "head_ok",
        lambda url: (
            url == template.format(date=complete.strftime("%Y%m%d"), hh=complete.strftime("%H"))
        ),
    )
    assert resolve_cycle(template) == complete


def test_transform_units():
    var_kt = SourceVar("w", ("UGRD", "10 m above ground"), "hourly", "i16", 0.01, to_kt=True)
    var_c = SourceVar("t", ("TMP", "2 m above ground"), "h3", "i16", 0.1, k_to_c=True)
    assert transform_units(np.array([1.0]), var_kt)[0] == pytest.approx(1.943844)
    assert transform_units(np.array([273.15]), var_c)[0] == pytest.approx(0.0)


# --------------------------------------------------------------------- gfs


def test_gfs_variable_selection():
    hourly_names = [v.name for v in gfs.HOURLY_VARS]
    assert hourly_names == ["wind_u_kt", "wind_v_kt", "gust_kt"]
    assert [v.scale for v in gfs.HOURLY_VARS] == [0.01, 0.01, 0.1]
    h3 = {v.name: v for v in gfs.H3_VARS}
    assert set(h3) == {"visibility_m", "cape_jkg", "temp_c", "dew_point_c", "precip_mm"}
    assert h3["visibility_m"].grib == ("VIS", "surface")
    assert h3["visibility_m"].scale == 50
    assert h3["temp_c"].k_to_c and h3["dew_point_c"].k_to_c
    assert h3["precip_mm"].optional  # APCP absent at f000, NaN-filled
    assert all(v.to_kt for v in gfs.HOURLY_VARS)

    # hourly-only steps fetch 3 vars, h3 steps fetch all 8
    assert [v.name for v in gfs.vars_for_step(1)] == hourly_names
    assert len(gfs.vars_for_step(6)) == 8
    assert len(gfs.vars_for_step(123)) == 8  # 3-hourly tail: also an h3 step


def test_gfs_step_url():
    assert gfs.step_url(CYCLE, 7) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260713/06/atmos/gfs.t06z.pgrb2.0p25.f007"
    )


# -------------------------------------------------------------------- gefs


def test_gefs_member_list():
    assert len(gefs.MEMBERS) == 31
    assert gefs.MEMBERS[0] == "gec00"
    assert gefs.MEMBERS[1] == "gep01"
    assert gefs.MEMBERS[-1] == "gep30"


def test_gefs_urls():
    assert gefs.a_url(CYCLE, "gep07", 150).endswith(
        "/gefs.20260713/06/atmos/pgrb2ap5/gep07.t06z.pgrb2a.0p50.f150"
    )
    assert gefs.b_url(CYCLE, "gec00", 6).endswith(
        "/gefs.20260713/06/atmos/pgrb2bp5/gec00.t06z.pgrb2b.0p50.f006"
    )


def test_mean_and_anomaly_encoding_math():
    rng = np.random.default_rng(3)
    speeds = rng.uniform(0, 40, (31, 4, 5, 5)).astype(np.float32)
    speeds[5, 2] += 40.0  # push one member far from the mean

    mean, anom = gefs.mean_and_anomaly(speeds)
    assert mean.shape == (4, 5, 5)
    assert anom.shape == (31, 4, 5, 5)
    np.testing.assert_allclose(mean, speeds.mean(axis=0), atol=1e-4)
    assert anom.max() <= 25.0 and anom.min() >= -25.0  # clipped before quantize

    # members reconstruct as mean + anomaly wherever the clip did not bite
    recon = mean[None] + anom
    unclipped = np.abs(speeds - mean[None]) < 25.0
    np.testing.assert_allclose(recon[unclipped], speeds[unclipped], atol=1e-3)

    # hypot + m/s -> kt member speed definition
    u, v = np.array([3.0]), np.array([4.0])
    assert np.hypot(u, v)[0] * MS_TO_KT == pytest.approx(5 * 1.943844)


# ------------------------------------------------------------------- waves


def test_wave_variables():
    assert [v.name for v in gfswave.VARS] == [
        "hs_m",
        "period_s",
        "dir_deg",
        "wind_wave_h_m",
        "wind_wave_period_s",
        "wind_wave_dir_deg",
        "swell_h_m",
        "swell_period_s",
        "swell_dir_deg",
    ]
    by_name = {v.name: v for v in gfswave.VARS}
    assert by_name["hs_m"].grib == ("HTSGW", "surface")
    assert by_name["swell_h_m"].grib == ("SWELL", "1 in sequence")
    assert by_name["swell_h_m"].scale == 0.01
    assert by_name["swell_dir_deg"].scale == 0.1
    assert gfswave.step_url(CYCLE, 42).endswith(
        "/gfs.20260713/06/wave/gridded/gfswave.t06z.global.0p25.f042.grib2"
    )


# --------------------------------------------------------- currents / ecmwf


def test_cmems_constants():
    assert cmems.DATASET_ID == "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i"
    assert [v.name for v in cmems.VARS] == ["cur_u_kt", "cur_v_kt"]
    assert all(v.scale == 0.01 for v in cmems.VARS)
    assert cmems.resolve(CYCLE) == CYCLE


def test_cmems_retries_authentication_service_outages_only():
    class AuthUnavailable(Exception):
        pass

    class FakeCopernicus:
        CouldNotConnectToAuthenticationSystem = AuthUnavailable

        def __init__(self):
            self.calls = 0

        def open_dataset(self, **kwargs):
            self.calls += 1
            assert kwargs == {"dataset_id": cmems.DATASET_ID, "variables": ["uo", "vo"]}
            if self.calls < 3:
                raise AuthUnavailable
            return "dataset"

    client = FakeCopernicus()
    sleeps = []
    assert cmems._open_dataset_with_auth_retries(client, sleep=sleeps.append) == "dataset"
    assert client.calls == 3
    assert sleeps == list(cmems.AUTH_RETRY_DELAYS_S)


def test_cmems_does_not_retry_invalid_credentials():
    class AuthUnavailable(Exception):
        pass

    class InvalidCredentials(Exception):
        pass

    class FakeCopernicus:
        CouldNotConnectToAuthenticationSystem = AuthUnavailable

        @staticmethod
        def open_dataset(**kwargs):
            raise InvalidCredentials

    with pytest.raises(InvalidCredentials):
        cmems._open_dataset_with_auth_retries(FakeCopernicus(), sleep=lambda _: None)


def _catalog_dataset(dataset_id, variables):
    from types import SimpleNamespace

    service = SimpleNamespace(variables=[SimpleNamespace(short_name=name) for name in variables])
    part = SimpleNamespace(services=[service])
    version = SimpleNamespace(parts=[part])
    return SimpleNamespace(dataset_id=dataset_id, versions=[version])


def test_ibi_resolves_hourly_2d_currents_from_catalog(monkeypatch):
    from types import SimpleNamespace

    datasets = [
        _catalog_dataset("cmems_mod_ibi_phy_anfc_0.027deg-3D_PT1H-m", ["uo", "vo"]),
        _catalog_dataset(ibi.DEFAULT_DATASET_ID, ["uo", "vo", "zos"]),
        _catalog_dataset("cmems_mod_ibi_phy_anfc_0.027deg-2D_PT15M-i", ["uo", "vo"]),
    ]
    catalog = SimpleNamespace(
        products=[SimpleNamespace(product_id=ibi.PRODUCT_ID, datasets=datasets)]
    )

    class FakeCopernicus:
        @staticmethod
        def describe(**kwargs):
            assert kwargs == {"contains": ["ibi"], "disable_progress_bar": True}
            return catalog

    monkeypatch.delenv(ibi.DATASET_ID_ENV, raising=False)
    monkeypatch.setattr(ibi, "_RESOLVED_DATASET_ID", None)
    assert ibi.resolve_dataset_id(FakeCopernicus) == ibi.DEFAULT_DATASET_ID


def test_ibi_dataset_override_bypasses_catalog(monkeypatch):
    class FakeCopernicus:
        @staticmethod
        def describe(**kwargs):
            raise AssertionError("catalogue must not be queried when an override is set")

    monkeypatch.setenv(ibi.DATASET_ID_ENV, "verified-renamed-ibi-dataset")
    assert ibi.resolve_dataset_id(FakeCopernicus) == "verified-renamed-ibi-dataset"


def test_ibi_catalog_failure_is_explicit(monkeypatch):
    class FakeCopernicus:
        @staticmethod
        def describe(**kwargs):
            raise OSError("catalogue down")

    monkeypatch.delenv(ibi.DATASET_ID_ENV, raising=False)
    monkeypatch.setattr(ibi, "_RESOLVED_DATASET_ID", None)
    with pytest.raises(RuntimeError, match="catalogue resolution failed"):
        ibi.resolve_dataset_id(FakeCopernicus)


def test_ibi_retries_authentication_service_outages_only():
    class AuthUnavailable(Exception):
        pass

    class FakeCopernicus:
        CouldNotConnectToAuthenticationSystem = AuthUnavailable

        def __init__(self):
            self.calls = 0

        def open_dataset(self, **kwargs):
            self.calls += 1
            assert kwargs["dataset_id"] == ibi.DEFAULT_DATASET_ID
            assert kwargs["variables"] == ["uo", "vo"]
            assert kwargs["minimum_latitude"] == ibi.MIN_LAT
            assert kwargs["maximum_longitude"] == ibi.MAX_LON
            if self.calls < 3:
                raise AuthUnavailable
            return "dataset"

    client = FakeCopernicus()
    sleeps = []
    assert (
        ibi._open_dataset_with_auth_retries(
            client,
            dataset_id=ibi.DEFAULT_DATASET_ID,
            sleep=sleeps.append,
        )
        == "dataset"
    )
    assert client.calls == 3
    assert sleeps == list(cmems.AUTH_RETRY_DELAYS_S)


class _FakeArray:
    def __init__(self, values):
        self.values = np.asarray(values)


class _FakeIbiDataset:
    dims = {}

    def __init__(self, cycle, steps):
        self.times = np.array(
            [np.datetime64(cycle.replace(tzinfo=None) + timedelta(hours=h), "ns") for h in steps]
        )
        self.u = np.full((len(steps), 2, 3), 1.0, dtype=np.float32)
        self.v = np.full((len(steps), 2, 3), -2.0, dtype=np.float32)
        self.u[:, 0, 0] = np.nan
        self.v[:, 0, 0] = np.nan
        self.closed = False

    def __getitem__(self, name):
        values = {
            "time": self.times,
            "latitude": [40.0, 40.0 + 1 / 36],
            "longitude": [-10.0, -10.0 + 1 / 36, -10.0 + 2 / 36],
        }[name]
        return _FakeArray(values)

    def sel(self, *, time):
        requested = np.asarray(time, dtype="datetime64[ns]")
        if requested.ndim == 0:
            index = int(np.where(self.times == requested)[0][0])
            return {"uo": _FakeArray(self.u[index]), "vo": _FakeArray(self.v[index])}
        indices = [int(np.where(self.times == instant)[0][0]) for instant in requested]
        return {"uo": _FakeArray(self.u[indices]), "vo": _FakeArray(self.v[indices])}

    def close(self):
        self.closed = True


def test_ibi_resolve_derives_latest_complete_daily_cycle(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    ds = _FakeIbiDataset(cycle, range(ibi.NATIVE_FORECAST_HORIZON_H + 1))
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", lambda *args, **kwargs: ds)
    assert ibi.resolve() == cycle
    assert ds.closed


def test_ibi_build_cube_hourly_surface_currents(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    ds = _FakeIbiDataset(cycle, ibi.STEP_AXIS)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", lambda *args, **kwargs: ds)

    cube = ibi.build_cube(cycle)
    assert cube.layer == "currents-ibi"
    assert cube.model == "cmems_ibi"
    assert cube.time_axes == {"steps": list(range(73))}
    assert cube.arrays["cur_u_kt"].shape == (73, 2, 3)
    assert cube.decoded("cur_u_kt")[0, 1, 1] == pytest.approx(MS_TO_KT, abs=0.005)
    assert cube.decoded("cur_v_kt")[0, 1, 1] == pytest.approx(-2 * MS_TO_KT, abs=0.005)
    assert np.isnan(cube.decoded("cur_u_kt")[0, 0, 0])
    assert cube.provenance["dataset_id"] == ibi.DEFAULT_DATASET_ID
    assert "including tide" in cube.provenance["tidal_caveat"]
    assert ds.closed


def test_rtofs_is_marked_skeleton():
    from ingest.sources import rtofs

    assert rtofs.STEP_AXIS == cmems.STEP_AXIS  # same cube shape as the primary path
    with pytest.raises(NotImplementedError, match="skeleton"):
        rtofs.build_cube(CYCLE)


def test_ecmwf_axis_and_gust_probing_order():
    assert ecmwf_open.STEP_AXIS[:2] == [0, 3]
    assert 144 in ecmwf_open.STEP_AXIS and 147 not in ecmwf_open.STEP_AXIS
    assert ecmwf_open.GUST_PARAMS == ("10fg", "10fg3", "10fg6")
    assert ecmwf_open.GUST_VAR.scale == 0.1
