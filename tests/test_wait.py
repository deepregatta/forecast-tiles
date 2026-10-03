"""`ingest --wait-minutes`: a dispatched run waits for its provider's cycle.

The clock and sleep are injected, so these tests take no time and touch no
network; the currents tests also cover GLO12 readiness and outage handling."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import make_ibi_cube, make_weather_cube

from ingest import cli
from ingest.sources.base import CycleNotAvailableError


class FakeClock:
    """Monotonic clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _gfs_ready_after(monkeypatch, cube, failures: int, built: list):
    """GFS resolve reports the cycle missing `failures` times, then ready."""
    from ingest.sources import gfs

    calls = []

    def resolve(requested=None):
        calls.append(requested)
        if len(calls) <= failures:
            raise CycleNotAvailableError(f"f240 .idx missing (check {len(calls)})")
        return requested

    def build(cycle):
        built.append(cycle)
        return cube

    monkeypatch.setattr(gfs, "resolve", resolve)
    monkeypatch.setattr(gfs, "build_cube", build)
    return calls


def _wait_args(tmp_path, minutes="90", layer="weather", cycle="20260713T06"):
    return [layer, "--cycle", cycle, "--wait-minutes", minutes, "--dry-run", str(tmp_path)]


def test_wait_publishes_at_once_when_the_cycle_is_ready(tmp_path, monkeypatch):
    cube, built, fake = make_weather_cube(), [], FakeClock()
    calls = _gfs_ready_after(monkeypatch, cube, 0, built)
    rc = cli.main(_wait_args(tmp_path), clock=fake.clock, sleep=fake.sleep)
    assert rc == 0
    assert len(calls) == 1 and fake.sleeps == []
    assert built == [cube.cycle]
    assert (tmp_path / "forecast-runs" / cube.run_id / "manifest.json").exists()


def test_wait_polls_every_minute_until_the_cycle_is_ready(tmp_path, monkeypatch, capsys):
    cube, built, fake = make_weather_cube(), [], FakeClock()
    calls = _gfs_ready_after(monkeypatch, cube, 2, built)
    rc = cli.main(_wait_args(tmp_path), clock=fake.clock, sleep=fake.sleep)
    assert rc == 0
    assert len(calls) == 3
    assert fake.sleeps == [60, 60]
    assert built == [cube.cycle]
    out = capsys.readouterr().out
    assert "waiting up to 90 min for cycle 2026-07-13T06:00Z, checking every 60 s" in out
    assert "available after 3 checks" in out


def test_wait_exits_1_when_the_deadline_passes(tmp_path, monkeypatch, capsys):
    cube, built, fake = make_weather_cube(), [], FakeClock()
    calls = _gfs_ready_after(monkeypatch, cube, 10_000, built)
    rc = cli.main(_wait_args(tmp_path, minutes="3"), clock=fake.clock, sleep=fake.sleep)
    assert rc == 1
    assert fake.sleeps == [60, 60, 60]
    assert len(calls) == 4  # at 0, 1, 2 and 3 min
    assert built == []
    assert not (tmp_path / "latest.json").exists()
    assert "cycle 2026-07-13T06:00Z not available after 3 min (4 checks" in capsys.readouterr().out


def test_wait_never_sleeps_past_the_deadline(monkeypatch):
    cube, fake = make_weather_cube(), FakeClock()
    _gfs_ready_after(monkeypatch, cube, 10_000, [])
    with pytest.raises(cli.CycleWaitExpired):
        cli.wait_for_cycle("weather", cube.cycle, 1.5, clock=fake.clock, sleep=fake.sleep)
    assert fake.sleeps == [60, 30]


def test_wait_polls_ecmwf_and_copernicus_every_two_minutes(monkeypatch):
    from ingest.sources import ecmwf_open

    fake, calls = FakeClock(), []

    def resolve(requested=None):
        calls.append(requested)
        if len(calls) < 3:
            raise CycleNotAvailableError("not yet")
        return requested

    monkeypatch.setattr(ecmwf_open, "resolve", resolve)
    cycle = make_weather_cube().cycle.replace(hour=0)
    got = cli.wait_for_cycle("weather-ecmwf", cycle, 120, clock=fake.clock, sleep=fake.sleep)
    assert got == cycle
    assert fake.sleeps == [120, 120]
    assert cli.POLL_SECONDS["currents"] == cli.POLL_SECONDS["currents-ibi"] == 120


def test_regional_waits_for_required_variables_after_metadata_appears(monkeypatch):
    from ingest.sources.openmeteo import catalog, registry
    from ingest.sources.openmeteo.reader import ObjectRecord

    product = registry.ICON_EU
    cycle = make_weather_cube().cycle.replace(hour=0)
    complete = catalog.RunMeta(
        cycle=cycle,
        record=ObjectRecord("meta.json", '"complete"', 1),
        reference_time=cycle,
        valid_times=tuple(catalog.lead_times(product, cycle)),
        variables=frozenset(product.files.values()),
        created_at="",
    )
    partial = replace(complete, variables=frozenset({"temperature_2m"}))
    checks = []

    def fetch(product, requested, **kwargs):
        checks.append(requested)
        return partial if len(checks) < 3 else complete

    monkeypatch.setattr(catalog, "fetch_meta", fetch)
    fake = FakeClock()
    assert cli.wait_for_cycle(product.layer, cycle, 45, clock=fake.clock, sleep=fake.sleep) == cycle
    assert len(checks) == 3 and fake.sleeps == [120, 120]


def test_incomplete_regional_metadata_still_obeys_wait_deadline(monkeypatch):
    from ingest.sources.openmeteo import catalog, registry
    from ingest.sources.openmeteo.reader import ObjectRecord

    product = registry.ICON_EU
    cycle = make_weather_cube().cycle.replace(hour=0)
    partial = catalog.RunMeta(
        cycle=cycle,
        record=ObjectRecord("meta.json", '"partial"', 1),
        reference_time=cycle,
        valid_times=tuple(catalog.lead_times(product, cycle)),
        variables=frozenset({"temperature_2m"}),
        created_at="",
    )
    monkeypatch.setattr(catalog, "fetch_meta", lambda *a, **kw: partial)
    fake = FakeClock()
    with pytest.raises(cli.CycleWaitExpired, match="wind_u_component_10m"):
        cli.wait_for_cycle(product.layer, cycle, 1, clock=fake.clock, sleep=fake.sleep)
    assert fake.sleeps == [60]


def test_wait_then_exits_already_published_before_building(tmp_path, monkeypatch, capsys):
    cube, built = make_weather_cube(), []
    _gfs_ready_after(monkeypatch, cube, 0, built)
    assert cli.main(_wait_args(tmp_path)) == 0
    fake = FakeClock()
    assert cli.main(_wait_args(tmp_path), clock=fake.clock, sleep=fake.sleep) == 0
    assert "already published" in capsys.readouterr().out
    assert len(built) == 1


def test_wait_minutes_needs_an_explicit_cycle(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["weather", "--wait-minutes", "90", "--dry-run", str(tmp_path)])


def test_wait_minutes_zero_is_a_single_check(tmp_path, monkeypatch, capsys):
    cube = make_weather_cube()
    _gfs_ready_after(monkeypatch, cube, 1, [])
    rc = cli.main(
        ["weather", "--cycle", "20260713T06", "--wait-minutes", "0", "--dry-run", str(tmp_path)]
    )
    assert rc == 1  # weather is not a skip-when-not-available layer
    assert "no complete cycle available" in capsys.readouterr().out


# ------------------------------------------------ GLO12 readiness and outages


def _currents_cube():
    """The IBI fixture relabelled: a small currents cube for CLI wiring."""
    ibi_cube = make_ibi_cube()
    return replace(ibi_cube, layer="currents", model="cmems_glo12")


def test_glo12_not_ready_skips_without_publishing(tmp_path, monkeypatch, capsys):
    from ingest.sources import cmems

    cycle = _currents_cube().cycle
    monkeypatch.setattr(cmems, "resolve", lambda requested=None: requested or cycle)

    def not_yet(c):
        raise CycleNotAvailableError("GLO12: last update finished before cycle")

    monkeypatch.setattr(cmems, "build_cube", not_yet)
    assert cli.main(["currents", "--dry-run", str(tmp_path)]) == 0
    assert "cycle not available yet, skipping" in capsys.readouterr().out
    assert not (tmp_path / "latest.json").exists()


def test_glo12_waits_on_its_readiness_check(tmp_path, monkeypatch):
    from ingest.sources import cmems

    cube, fake, checks = _currents_cube(), FakeClock(), []

    def resolve(requested=None):
        checks.append(requested)
        if len(checks) < 3:
            raise CycleNotAvailableError("GLO12: Copernicus is updating the data")
        return requested

    monkeypatch.setattr(cmems, "resolve", resolve)
    monkeypatch.setattr(cmems, "build_cube", lambda cycle: cube)
    rc = cli.main(
        _wait_args(tmp_path, minutes="180", layer="currents", cycle="20260713T00"),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert rc == 0
    assert fake.sleeps == [120, 120]
    assert (tmp_path / "forecast-runs" / "currents-20260713T00Z" / "manifest.json").exists()


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_cmems_outage_preserves_cause_and_published_forecast(tmp_path, monkeypatch, error_type):
    from ingest.sources import base, cmems

    cube = _currents_cube()
    monkeypatch.setattr(cmems, "resolve", lambda requested=None: requested or cube.cycle)
    monkeypatch.setattr(cmems, "build_cube", lambda cycle: cube)
    assert cli.main(["currents", "--dry-run", str(tmp_path)]) == 0
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    requests = []

    def request(method, url, **kwargs):
        requests.append((method, url))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(base.SESSION, "request", request)
    primary_error = error_type("Copernicus service unavailable")

    def broken(cycle):
        raise primary_error

    monkeypatch.setattr(cmems, "build_cube", broken)
    next_cycle = cube.cycle + timedelta(days=1)
    with pytest.raises(error_type, match="Copernicus service unavailable") as raised:
        cli.main(
            ["currents", "--cycle", next_cycle.strftime("%Y%m%dT%H"), "--dry-run", str(tmp_path)]
        )

    assert raised.value is primary_error
    assert any(
        "CMEMS GLO12" in note and next_cycle.strftime("%Y-%m-%dT%H:00Z") in note
        for note in raised.value.__notes__
    )
    assert requests == []
    assert before == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


def test_a_cycle_one_day_on_is_not_ready_before_its_update(monkeypatch):
    """GLO12's readiness is date-based: yesterday's update does not cover today."""
    from ingest.sources import cmems

    cycle = _currents_cube().cycle
    state = cmems.ProviderState(
        end=cycle + timedelta(hours=240) - timedelta(days=1),
        updated=cycle - timedelta(hours=17, minutes=34),
        updating_from=None,
    )
    with pytest.raises(CycleNotAvailableError, match="not published yet"):
        cmems.require_published(state, cycle, 240, "GLO12")


# ------------------------------------------- late files of a ready cycle


class _Response:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} for url")


class _Session:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        status = self.statuses.pop(0)
        return _Response(status, b"idx" if status == 200 else b"")


F219 = (
    "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260929/18/atmos/gfs.t18z.pgrb2.0p25.f219.idx"
)


def test_a_late_file_of_a_just_published_cycle_is_waited_for(monkeypatch):
    # GFS 18Z, 2026-09-29: f240.idx at 22:39:44, f219.idx still 404 at 22:40:35
    from ingest.sources import base

    fake, session = FakeClock(), _Session(404, 404, 200)
    monkeypatch.setattr(base, "SESSION", session)
    base.allow_missing_files(15 * 60, clock=fake.clock)
    assert base.http(F219, sleep=fake.sleep, clock=fake.clock) == b"idx"
    assert fake.sleeps == [30, 30]
    assert len(session.urls) == 3


def test_without_the_grace_a_404_fails_as_before(monkeypatch):
    import requests

    from ingest.sources import base

    fake, session = FakeClock(), _Session(404, 404, 404)
    monkeypatch.setattr(base, "SESSION", session)
    with pytest.raises(requests.HTTPError):
        base.http(F219, sleep=fake.sleep, clock=fake.clock)
    assert fake.sleeps == [2, 4]  # the ordinary retries


def test_the_grace_ends(monkeypatch):
    import requests

    from ingest.sources import base

    fake = FakeClock()
    monkeypatch.setattr(base, "SESSION", _Session(*[404] * 40))
    base.allow_missing_files(90, clock=fake.clock)
    with pytest.raises(requests.HTTPError):
        base.http(F219, sleep=fake.sleep, clock=fake.clock)
    assert fake.sleeps == [30, 30, 30, 2, 4]


def test_a_waited_for_cycle_opens_the_grace(monkeypatch):
    from ingest.sources import base

    cube, fake = make_weather_cube(), FakeClock()
    _gfs_ready_after(monkeypatch, cube, 1, [])
    assert not base.missing_files_grace()
    cli.wait_for_cycle("weather", cube.cycle, 90, clock=fake.clock, sleep=fake.sleep)
    assert base.missing_files_grace()


def test_ensemble_waits_for_late_members_during_the_grace(monkeypatch):
    import numpy as np

    from ingest.cube import GridMeta
    from ingest.sources import base, gefs

    counts = iter([29, 30, 31])
    monkeypatch.setattr(gefs, "available_members", lambda cycle: gefs.MEMBERS[: next(counts)])
    monkeypatch.setattr(gefs, "gust_available", lambda cycle: False)
    grid = GridMeta(lat0=40.0, lon0=-10.0, dlat=0.5, dlon=0.5, nlat=2, nlon=2)

    def stack(cycle, members, url_fn, wanted, workers):
        shape = (len(members), len(gefs.STEP_AXIS), 2, 2)
        return np.full(shape, 10.0, dtype=np.float32), grid

    monkeypatch.setattr(gefs, "_speed_stack", stack)
    sleeps = []
    base.allow_missing_files(15 * 60)
    cube = gefs.build_cube(make_weather_cube().cycle, sleep=sleeps.append)
    assert cube.member_count == 31
    assert sleeps == [30, 30]


def test_wait_for_a_short_range_ecmwf_cycle_as_the_dispatcher_starts_it(tmp_path, monkeypatch):
    """The 00:15 / 12:15 dispatch: an 06Z/18Z cycle, polled every 2 min, then published."""
    from ingest.sources import ecmwf_open

    cube, fake, calls = make_weather_cube(), FakeClock(), []
    cube.layer, cube.model = "weather-ecmwf-short", "ecmwf_ifs_0p25"

    def resolve_short(requested=None):
        calls.append(requested)
        if len(calls) < 3:
            raise CycleNotAvailableError("not published to 144 h yet")
        return requested

    monkeypatch.setattr(ecmwf_open, "resolve_short", resolve_short)
    monkeypatch.setattr(ecmwf_open, "build_short_cube", lambda cycle: cube)
    args = _wait_args(tmp_path, minutes="120", layer="weather-ecmwf-short")
    assert cli.main(args, clock=fake.clock, sleep=fake.sleep) == 0
    assert calls == [cube.cycle] * 3
    assert fake.sleeps == [120, 120]
    assert (
        tmp_path / "forecast-runs" / "weather-ecmwf-short-20260713T06Z" / "manifest.json"
    ).exists()
