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


def test_gefs_member_by_member_quantization_matches_the_whole_stack():
    """The per-member loop only bounds memory: the bytes are those of
    quantizing the whole stack at once, as before 2026-09-29."""
    from tilekit.codec import quantize

    rng = np.random.default_rng(3)
    speeds = rng.gamma(3.0, 5.0, size=(31, 4, 5, 6)).astype(np.float32)
    speeds[4, 1] += 60.0  # one member far above the rest: anomalies clip at +25 kt
    speeds[:, 2, 0, 0] = np.nan
    mean = speeds.mean(axis=0, dtype=np.float64).astype(np.float32)
    anom = np.clip(speeds - mean[None], -gefs.ANOM_CLIP_KT, gefs.ANOM_CLIP_KT)

    got_mean, got_anom = gefs.quantize_mean_and_anomaly(speeds)
    assert got_mean.dtype == np.int16 and got_anom.dtype == np.int8
    assert np.array_equal(got_mean, quantize(mean, "i16", 0.01))
    assert np.array_equal(got_anom, quantize(anom, "i8", 0.2))
    assert got_anom.max() == round(gefs.ANOM_CLIP_KT / 0.2)
    assert (got_anom[:, 2, 0, 0] == -128).all()  # NaN stays the sentinel


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

    mean_q, anom_q = gefs.quantize_mean_and_anomaly(speeds)
    mean, anom = mean_q * 0.01, anom_q * 0.2
    assert mean.shape == (4, 5, 5)
    assert anom.shape == (31, 4, 5, 5)
    np.testing.assert_allclose(mean, speeds.mean(axis=0), atol=0.005 + 1e-4)
    assert anom.max() <= 25.0 and anom.min() >= -25.0  # clipped before quantize

    # members reconstruct as mean + anomaly, to the quantization steps,
    # wherever the clip did not bite
    recon = mean[None] + anom
    unclipped = np.abs(speeds - speeds.mean(axis=0)[None]) < 25.0
    np.testing.assert_allclose(recon[unclipped], speeds[unclipped], atol=0.105)

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


def _stac(end, updated, updating=None, urls=None):
    """A fetch() serving one STAC item with these properties (ISO strings)."""
    import json

    def fetch(url):
        if urls is not None:
            urls.append(url)
        return json.dumps(
            {
                "type": "Feature",
                "properties": {
                    "end_datetime": end,
                    "admp_updated_data": updated,
                    "admp_updating_start_date": updating,
                },
            }
        ).encode()

    return fetch


GLO12_CYCLE = datetime(2026, 9, 29, tzinfo=timezone.utc)


def test_glo12_is_ready_once_its_stac_item_shows_the_cycle_written():
    # the item as read on 2026-09-29 at 20:05 UTC
    urls = []
    fetch = _stac("2026-10-09T00:00:00Z", "2026-09-29T06:25:55.524Z", urls=urls)
    assert cmems.resolve(GLO12_CYCLE, fetch=fetch) == GLO12_CYCLE
    assert urls == [
        "https://s3.waw3-1.cloudferro.com/mdl-metadata/metadata/"
        "GLOBAL_ANALYSISFORECAST_PHY_001_024/"
        "cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i_202406/dataset.stac.json"
    ]
    state = cmems.glo12_state(fetch=fetch)
    assert state.updated == datetime(2026, 9, 29, 6, 25, 55, 524000, tzinfo=timezone.utc)
    assert state.updating_from is None


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        # 05:45 UTC: yesterday's bulletin is the latest one
        (("2026-10-08T00:00:00Z", "2026-09-28T06:31:02Z"), "not published yet"),
        # the axis already moved, but the data are still being rewritten
        (("2026-10-09T00:00:00Z", "2026-09-28T06:31:02Z", "2026-09-29T00:00:00Z"), "updating"),
        # an update finished the day before and nothing since
        (("2026-10-09T00:00:00Z", "2026-09-28T23:59:00Z"), "before cycle"),
        ((None, None), "not published yet"),
    ],
)
def test_glo12_is_not_ready_before_copernicus_finishes(item, reason):
    with pytest.raises(CycleNotAvailableError, match=reason):
        cmems.resolve(GLO12_CYCLE, fetch=_stac(*item))


def test_an_unreadable_stac_item_is_not_available_yet():
    def down(url):
        raise OSError("connection reset")

    with pytest.raises(CycleNotAvailableError, match="metadata unreadable"):
        cmems.resolve(GLO12_CYCLE, fetch=down)
    with pytest.raises(CycleNotAvailableError, match="metadata unreadable"):
        cmems.resolve(GLO12_CYCLE, fetch=lambda url: b"<html>not json</html>")


class _FakeGlo12Dataset:
    dims = {}

    def __init__(self, cycle, steps):
        self.times = np.array(
            [np.datetime64(cycle.replace(tzinfo=None) + timedelta(hours=h), "ns") for h in steps]
        )

    def __getitem__(self, name):
        return _FakeArray(
            {
                "time": self.times,
                "latitude": np.array([40.0, 40 + 1 / 12], dtype=np.float32),
                "longitude": np.array([-10.0, -10 + 1 / 12, -10 + 2 / 12], dtype=np.float32),
            }[name]
        )

    def sel(self, *, time):
        field = np.full((2, 3), 0.5, dtype=np.float32)
        return {"uo": _FakeArray(field), "vo": _FakeArray(-field)}


def test_glo12_build_cube_records_when_copernicus_finished(monkeypatch):
    ds = _FakeGlo12Dataset(GLO12_CYCLE, cmems.STEP_AXIS)
    monkeypatch.setattr(cmems, "_open_dataset_with_auth_retries", lambda *a, **k: ds)
    fetch = _stac("2026-10-09T00:00:00Z", "2026-09-29T06:25:55.524Z")
    cube = cmems.build_cube(GLO12_CYCLE, fetch=fetch)
    assert cube.provenance["provider_updated_at"] == "2026-09-29T06:25:55Z"
    assert cube.arrays["cur_u_kt"].shape == (41, 2, 3)


def test_glo12_build_cube_refuses_before_reading_an_unfinished_cycle(monkeypatch):
    def never_open(*args, **kwargs):
        raise AssertionError("must not open the dataset before the cycle is written")

    monkeypatch.setattr(cmems, "_open_dataset_with_auth_retries", never_open)
    fetch = _stac("2026-10-08T00:00:00Z", "2026-09-28T06:31:02Z")
    with pytest.raises(CycleNotAvailableError):
        cmems.build_cube(GLO12_CYCLE, fetch=fetch)


def test_ibi_explicit_cycle_waits_for_its_stac_item():
    # the item as read on 2026-09-29 at 20:05 UTC: bulletin D ends D+239 h
    cycle = GLO12_CYCLE
    urls = []
    ready = _stac("2026-10-08T23:00:00Z", "2026-09-29T11:08:40.988Z", urls=urls)
    assert ibi.resolve(cycle, fetch=ready) == cycle
    assert urls == [
        "https://s3.waw3-1.cloudferro.com/mdl-metadata/metadata/"
        "IBI_ANALYSISFORECAST_PHY_005_001/"
        "cmems_mod_ibi_phy_anfc_0.027deg-2D_PT1H-m_202411/dataset.stac.json"
    ]
    # 09:48 on 2026-09-29: the new day appended, the update still running
    mid_update = _stac("2026-10-08T23:00:00Z", "2026-09-28T11:36:46Z", "2026-10-08T00:00:00Z")
    with pytest.raises(CycleNotAvailableError, match="updating"):
        ibi.resolve(cycle, fetch=mid_update)
    # before 09:44: the previous bulletin ends a day early
    previous = _stac("2026-10-07T23:00:00Z", "2026-09-28T11:36:46Z")
    with pytest.raises(CycleNotAvailableError, match="not published yet"):
        ibi.resolve(cycle, fetch=previous)


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


UPDATED = datetime(2026, 7, 13, 11, 36, 46, tzinfo=timezone.utc)


def _settled_state(monkeypatch, *states):
    """Stub the catalogue's ARCO update state; successive calls return `states`."""
    calls = []

    def fake(copernicusmarine, dataset_id):
        calls.append(dataset_id)
        return states[min(len(calls), len(states)) - 1]

    monkeypatch.setattr(ibi, "arco_update_state", fake)
    return calls


def test_ibi_build_cube_hourly_surface_currents(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    ds = _FakeIbiDataset(cycle, ibi.STEP_AXIS)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", lambda *args, **kwargs: ds)
    calls = _settled_state(monkeypatch, (UPDATED, None))

    cube = ibi.build_cube(cycle)
    assert cube.layer == "currents-ibi"
    assert cube.model == "cmems_ibi"
    assert cube.time_axes == {"steps": list(range(121))}
    assert cube.arrays["cur_u_kt"].shape == (121, 2, 3)
    assert cube.decoded("cur_u_kt")[0, 1, 1] == pytest.approx(MS_TO_KT, abs=0.005)
    assert cube.decoded("cur_v_kt")[0, 1, 1] == pytest.approx(-2 * MS_TO_KT, abs=0.005)
    assert np.isnan(cube.decoded("cur_u_kt")[0, 0, 0])
    assert cube.provenance["dataset_id"] == ibi.DEFAULT_DATASET_ID
    assert "including tide" in cube.provenance["tidal_caveat"]
    assert "not a tidal-stream prediction" in cube.provenance["tidal_caveat"]
    assert cube.provenance["provider_updated_at"] == "2026-07-13T11:36:46Z"
    assert calls == [ibi.DEFAULT_DATASET_ID] * 2  # before and after the read
    assert ds.closed


def test_ibi_build_cube_refuses_a_window_being_rewritten(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)

    def never_open(*args, **kwargs):
        raise AssertionError("must not read a store that is mid-update")

    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", never_open)
    # the update rewrites from the previous day's hindcast onward: inside 0..120 h
    _settled_state(monkeypatch, (UPDATED, cycle - timedelta(days=1)))
    with pytest.raises(CycleNotAvailableError, match="being updated"):
        ibi.build_cube(cycle)


def _never_open(*args, **kwargs):
    raise AssertionError("must not read the store")


def test_ibi_build_cube_refuses_an_update_whose_named_range_is_past_its_window(monkeypatch):
    # 2026-09-29 09:48 UTC: the new day was appended and named as the only
    # range being updated, while D..D+5 still held the previous bulletin.
    cycle = CYCLE.replace(hour=0)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", _never_open)
    previous_update = cycle - timedelta(hours=12, minutes=24)
    appended_day = cycle + timedelta(hours=ibi.NATIVE_FORECAST_HORIZON_H - 23)
    _settled_state(monkeypatch, (previous_update, appended_day))
    with pytest.raises(CycleNotAvailableError, match="being updated"):
        ibi.build_cube(cycle)


def test_ibi_build_cube_refuses_a_cycle_newer_than_the_last_finished_update(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", _never_open)
    _settled_state(monkeypatch, (cycle - timedelta(hours=12), None))
    with pytest.raises(CycleNotAvailableError, match="before cycle"):
        ibi.build_cube(cycle)


def test_ibi_build_cube_refuses_an_update_during_the_read(monkeypatch):
    cycle = CYCLE.replace(hour=0)
    ds = _FakeIbiDataset(cycle, ibi.STEP_AXIS)
    monkeypatch.setattr(ibi, "resolve_dataset_id", lambda _: ibi.DEFAULT_DATASET_ID)
    monkeypatch.setattr(ibi, "_open_dataset_with_auth_retries", lambda *args, **kwargs: ds)
    _settled_state(monkeypatch, (UPDATED, None), (UPDATED, cycle))
    with pytest.raises(CycleNotAvailableError, match="changed while"):
        ibi.build_cube(cycle)
    assert ds.closed


def _catalog_part(updated, updating_start):
    from types import SimpleNamespace

    part = SimpleNamespace(
        name="default", arco_updated_date=updated, arco_updating_start_date=updating_start
    )
    dataset = SimpleNamespace(
        dataset_id=ibi.DEFAULT_DATASET_ID, versions=[SimpleNamespace(parts=[part])]
    )
    return SimpleNamespace(
        products=[SimpleNamespace(product_id=ibi.PRODUCT_ID, datasets=[dataset])]
    )


def test_ibi_arco_update_state_reads_the_catalogue_part():
    class FakeCopernicus:
        @staticmethod
        def describe(**kwargs):
            assert kwargs == {"dataset_id": ibi.DEFAULT_DATASET_ID, "disable_progress_bar": True}
            return _catalog_part("2026-09-28T11:36:46.577Z", "2026-09-28T00:00:00Z")

    updated, updating_from = ibi.arco_update_state(FakeCopernicus, ibi.DEFAULT_DATASET_ID)
    assert updated == datetime(2026, 9, 28, 11, 36, 46, 577000, tzinfo=timezone.utc)
    assert updating_from == datetime(2026, 9, 28, tzinfo=timezone.utc)

    class Settled:
        @staticmethod
        def describe(**kwargs):
            return _catalog_part("2026-09-28T11:36:46.577Z", None)

    assert ibi.arco_update_state(Settled, ibi.DEFAULT_DATASET_ID)[1] is None


def test_ibi_arco_update_state_needs_the_catalogue_unless_overridden(monkeypatch):
    class Down:
        @staticmethod
        def describe(**kwargs):
            raise OSError("catalogue down")

    monkeypatch.delenv(ibi.DATASET_ID_ENV, raising=False)
    with pytest.raises(RuntimeError, match="mid-update"):
        ibi.arco_update_state(Down, ibi.DEFAULT_DATASET_ID)
    # the operator override exists for catalogue outages; it must keep working
    monkeypatch.setenv(ibi.DATASET_ID_ENV, ibi.DEFAULT_DATASET_ID)
    assert ibi.arco_update_state(Down, ibi.DEFAULT_DATASET_ID) == (None, None)


def test_rtofs_is_marked_skeleton():
    from ingest.sources import rtofs

    assert rtofs.STEP_AXIS == cmems.STEP_AXIS  # same cube shape as the primary path
    with pytest.raises(NotImplementedError, match="skeleton"):
        rtofs.build_cube(CYCLE)


def test_ecmwf_axis_and_gust_params():
    assert ecmwf_open.STEP_AXIS[:2] == [0, 3]
    assert 144 in ecmwf_open.STEP_AXIS and 147 not in ecmwf_open.STEP_AXIS
    assert len(ecmwf_open.STEP_AXIS) == 65
    assert ecmwf_open.GUST_PARAMS == ("10fg", "10fg3", "10fg6")
    assert ecmwf_open.GUST_VAR.scale == 0.1


def _ecmwf_grib(short_name: str, step: int, window: int, value: float) -> bytes:
    """One 3x2 GRIB2 message on the 0.25° grid across 0°, as ECMWF writes it:
    wind instantaneous (template 4.0), gust a max over `window` h (4.8)."""
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, val in {
            "centre": 98,
            "Ni": 3,
            "Nj": 2,
            "latitudeOfFirstGridPointInDegrees": 50.25,
            "longitudeOfFirstGridPointInDegrees": 359.75,
            "latitudeOfLastGridPointInDegrees": 50.0,
            "longitudeOfLastGridPointInDegrees": 0.25,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
        }.items():
            eccodes.codes_set(gid, key, val)
        if window:
            eccodes.codes_set(gid, "productDefinitionTemplateNumber", 8)
            for key, val in {
                "discipline": 0,
                "parameterCategory": 2,
                "parameterNumber": 22,
                "typeOfFirstFixedSurface": 103,
                "scaleFactorOfFirstFixedSurface": 0,
                "scaledValueOfFirstFixedSurface": 10,
                "typeOfStatisticalProcessing": 2,
                "indicatorOfUnitForTimeRange": 1,
                "lengthOfTimeRange": window,
                "forecastTime": step - window,
            }.items():
                eccodes.codes_set(gid, key, val)
        else:
            eccodes.codes_set(gid, "shortName", short_name)
            eccodes.codes_set(gid, "forecastTime", step)
        eccodes.codes_set_values(gid, np.full(6, value))
        return eccodes.codes_get_message(gid)
    finally:
        eccodes.codes_release(gid)


def test_ecmwf_parse_messages_reads_each_gust_window():
    raw = b"".join(
        [
            _ecmwf_grib("10u", 3, 0, 5.0),
            _ecmwf_grib("10fg", 3, 1, 11.0),
            _ecmwf_grib("10fg3", 96, 3, 12.0),
            _ecmwf_grib("10fg", 150, 6, 13.0),
        ]
    )
    fields, meta = ecmwf_open.parse_messages(raw)
    assert [(f.kind, f.start, f.end, f.window) for f in fields] == [
        ("u", 3, 3, 0),
        ("gust", 2, 3, 1),
        ("gust", 93, 96, 3),
        ("gust", 144, 150, 6),
    ]
    # ecCodes names them as ECMWF's own: 10fg (1 h and 6 h) and 10fg3
    assert [f.short_name for f in fields[1:]] == ["10fg", "10fg3", "10fg"]
    assert fields[2].values[0, 0] == pytest.approx(12.0)
    assert (meta.lat0, meta.lon0, meta.nlat, meta.nlon) == (50.0, -0.25, 2, 3)


def _gust(short_name, end, window, value=10.0):
    values = np.full((2, 3), value, dtype=np.float32)
    return ecmwf_open.Field("gust", short_name, end - window, end, values)


# ECMWF open data 2026-09-28T00Z, from its .index files: the gust's name and
# window change along the axis, and step 0 is a constant-zero placeholder
def _live_gust_pattern(axis):
    fields = [ecmwf_open.Field("gust", "10fg", 0, 0, np.zeros((2, 3), np.float32))]
    for step in axis[1:]:
        if step <= 90:
            fields.append(_gust("10fg", step, 1))
        elif step <= 144:
            fields.append(_gust("10fg3", step, 3))
        else:
            fields.append(_gust("10fg", step, 6))
    return fields


def test_ecmwf_select_gust_takes_one_message_per_step_across_names():
    chosen = ecmwf_open.select_gust(_live_gust_pattern(ecmwf_open.STEP_AXIS), ecmwf_open.STEP_AXIS)
    assert sorted(chosen) == ecmwf_open.STEP_AXIS[1:]  # every step, step 0 excluded
    windows = [chosen[s].window for s in ecmwf_open.STEP_AXIS[1:]]
    assert windows == [1] * 30 + [3] * 18 + [6] * 16
    assert chosen[93].short_name == "10fg3" and chosen[150].short_name == "10fg"
    assert ecmwf_open.describe_gust(chosen) == (
        "10fg 1 h max +3..+90 h; 10fg3 3 h max +93..+144 h; 10fg 6 h max +150..+240 h"
    )


def test_ecmwf_select_gust_prefers_the_longest_window_within_the_gap():
    axis = [0, 3, 6, 12]
    fields = [
        _gust("10fg", 3, 1),
        _gust("10fg3", 3, 3),  # fills the 3 h gap: kept
        _gust("10fg6", 6, 6),  # longer than the 3 h gap
        _gust("10fg", 6, 1),  # fits: kept over the overlapping 6 h window
        _gust("10fg6", 12, 6),  # only one: kept
    ]
    chosen = ecmwf_open.select_gust(fields, axis)
    assert {s: f.window for s, f in chosen.items()} == {3: 3, 6: 1, 12: 6}
    only_long = ecmwf_open.select_gust([_gust("10fg6", 3, 6), _gust("10fg", 3, 12)], [0, 3])
    assert only_long[3].window == 6  # nothing fits: the shortest


def _fake_ecmwf(monkeypatch, gust_fields):
    grid = base.GridMeta(lat0=50.0, lon0=-0.25, dlat=0.25, dlon=0.25, nlat=2, nlon=3)
    calls = []

    def retrieve(client, cycle, params, steps):
        calls.append((tuple(params), tuple(steps)))
        if params == ["10u", "10v"]:
            wind = [
                ecmwf_open.Field(k, "10" + k, s, s, np.full((2, 3), 4.0, np.float32))
                for s in steps
                for k in ("u", "v")
            ]
            return wind, grid
        return [f for f in gust_fields if f.end in steps], grid

    monkeypatch.setattr(ecmwf_open, "_client", lambda: object())
    monkeypatch.setattr(ecmwf_open, "_retrieve", retrieve)
    return calls


def test_ecmwf_build_cube_publishes_complete_gust_with_windows(monkeypatch):
    from ingest.validate import validate_cube

    axis = ecmwf_open.STEP_AXIS
    calls = _fake_ecmwf(monkeypatch, _live_gust_pattern(axis))
    cube = ecmwf_open.build_cube(CYCLE)

    assert calls[1] == (ecmwf_open.GUST_PARAMS, tuple(axis[1:]))  # all names, one request
    assert [v.name for v in cube.variables] == ["wind_u_kt", "wind_v_kt", "gust_kt"]
    gust = cube.var("gust_kt")
    assert gust.statistic.kind == "max"
    assert gust.statistic.window_h == (None,) + (1,) * 30 + (3,) * 18 + (6,) * 16
    public = gust.public()["statistic"]
    assert public["window_h"][:2] == [None, 1] and public["window_h"][-1] == 6
    decoded = cube.decoded("gust_kt")
    assert np.isnan(decoded[0]).all()  # no interval ends at step 0
    assert decoded[1:] == pytest.approx(10.0 * MS_TO_KT, abs=0.05)
    assert cube.provenance["gust"].startswith("10fg 1 h max +3..+90 h")

    report = validate_cube(cube, expected_axes={"steps": axis})
    assert report.ok, report.summary()
    assert "statistic_windows[gust_kt]" in report.checks_passed


def test_ecmwf_build_cube_is_wind_only_when_a_step_has_no_gust(monkeypatch):
    # what a single-name request produced before 2026-09-28: no 10fg at 93–144
    sparse = [f for f in _live_gust_pattern(ecmwf_open.STEP_AXIS) if f.short_name == "10fg"]
    _fake_ecmwf(monkeypatch, sparse)
    cube = ecmwf_open.build_cube(CYCLE)
    assert [v.name for v in cube.variables] == ["wind_u_kt", "wind_v_kt"]
    assert "gust_kt" not in cube.arrays
    assert cube.provenance["gust"].startswith("unavailable — no gust message at steps [93, 96")
    assert "(18 of 64)" in cube.provenance["gust"]
