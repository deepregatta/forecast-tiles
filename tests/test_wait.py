"""`ingest --wait-minutes`: a dispatched run waits for its provider's cycle.

The clock and sleep are injected, so these tests take no time and touch no
network; the currents tests also cover GLO12 readiness against the RTOFS
fallback."""

from dataclasses import replace
from datetime import timedelta

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


# ------------------------------------------------ GLO12 readiness vs RTOFS


def _currents_cube():
    """The IBI fixture relabelled: a small currents cube for CLI wiring."""
    ibi_cube = make_ibi_cube()
    return replace(ibi_cube, layer="currents", model="cmems_glo12")


def _stub_rtofs(monkeypatch, calls: list, cube=None):
    from ingest.sources import rtofs

    def resolve(requested=None):
        calls.append(("resolve", requested))
        return requested or _currents_cube().cycle

    def build(cycle):
        calls.append(("build", cycle))
        if cube is None:
            raise NotImplementedError("RTOFS fallback is a fetch skeleton")
        return cube

    monkeypatch.setattr(rtofs, "resolve", resolve)
    monkeypatch.setattr(rtofs, "build_cube", build)


def test_glo12_not_ready_skips_without_publishing_rtofs(tmp_path, monkeypatch, capsys):
    from ingest.sources import cmems

    cycle = _currents_cube().cycle
    monkeypatch.setattr(cmems, "resolve", lambda requested=None: requested or cycle)

    def not_yet(c):
        raise CycleNotAvailableError("GLO12: last update finished before cycle")

    monkeypatch.setattr(cmems, "build_cube", not_yet)
    rtofs_calls: list = []
    _stub_rtofs(monkeypatch, rtofs_calls, cube=_currents_cube())

    assert cli.main(["currents", "--dry-run", str(tmp_path)]) == 0
    assert "cycle not available yet, skipping" in capsys.readouterr().out
    assert rtofs_calls == []
    assert not (tmp_path / "latest.json").exists()


def test_glo12_waits_on_its_readiness_check_not_rtofs(tmp_path, monkeypatch):
    from ingest.sources import cmems

    cube, fake, checks = _currents_cube(), FakeClock(), []

    def resolve(requested=None):
        checks.append(requested)
        if len(checks) < 3:
            raise CycleNotAvailableError("GLO12: Copernicus is updating the data")
        return requested

    monkeypatch.setattr(cmems, "resolve", resolve)
    monkeypatch.setattr(cmems, "build_cube", lambda cycle: cube)
    rtofs_calls: list = []
    _stub_rtofs(monkeypatch, rtofs_calls)
    rc = cli.main(
        _wait_args(tmp_path, minutes="180", layer="currents", cycle="20260713T00"),
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert rc == 0
    assert fake.sleeps == [120, 120]
    assert rtofs_calls == []
    assert (tmp_path / "forecast-runs" / "currents-20260713T00Z" / "manifest.json").exists()


def test_a_real_cmems_failure_still_falls_back_to_rtofs(tmp_path, monkeypatch, capsys):
    from ingest.sources import cmems

    cycle = _currents_cube().cycle
    monkeypatch.setattr(cmems, "resolve", lambda requested=None: requested or cycle)

    def broken(c):
        raise RuntimeError("GLO12 missing 3/41 requested instants")

    monkeypatch.setattr(cmems, "build_cube", broken)
    fallback = replace(_currents_cube(), model="rtofs_global", provenance={"source": "rtofs"})
    rtofs_calls: list = []
    _stub_rtofs(monkeypatch, rtofs_calls, cube=fallback)

    assert cli.main(["currents", "--dry-run", str(tmp_path)]) == 0
    assert "CMEMS failed (RuntimeError" in capsys.readouterr().out
    assert rtofs_calls == [("resolve", None), ("build", cycle)]
    assert fallback.provenance["fallback"].startswith("CMEMS unavailable: RuntimeError")


def test_the_rtofs_skeleton_still_fails_loudly(tmp_path, monkeypatch):
    from ingest.sources import cmems

    cycle = _currents_cube().cycle
    monkeypatch.setattr(cmems, "resolve", lambda requested=None: requested or cycle)
    monkeypatch.setattr(
        cmems, "build_cube", lambda c: (_ for _ in ()).throw(RuntimeError("catalogue down"))
    )
    rtofs_calls: list = []
    _stub_rtofs(monkeypatch, rtofs_calls)
    with pytest.raises(NotImplementedError):
        cli.main(["currents", "--dry-run", str(tmp_path)])
    assert ("build", cycle) in rtofs_calls


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
