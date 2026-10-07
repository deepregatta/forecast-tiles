"""A fallback cannot consume admission for the preceding short IFS cycle."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from ingest import cli
from ingest.ecmwf_short_fallback import fallback_cycle
from ingest.publish import DirStore
from ingest.sources import ecmwf_open


@pytest.mark.parametrize(
    "now, expected",
    [
        ("2026-10-07T00:03:00Z", "2026-10-06T18:00:00Z"),  # observed delayed fallback
        ("2026-10-07T02:37:00Z", "2026-10-06T18:00:00Z"),
        ("2026-10-07T05:59:59Z", "2026-10-06T18:00:00Z"),
        ("2026-10-07T06:00:00Z", "2026-10-07T06:00:00Z"),
        ("2026-10-07T08:37:00Z", "2026-10-07T06:00:00Z"),
        ("2026-10-07T14:37:00Z", "2026-10-07T06:00:00Z"),
        ("2026-10-07T17:59:59Z", "2026-10-07T06:00:00Z"),
        ("2026-10-07T18:00:00Z", "2026-10-07T18:00:00Z"),
        ("2026-10-07T20:37:00Z", "2026-10-07T18:00:00Z"),
        ("2027-01-01T00:03:00Z", "2026-12-31T18:00:00Z"),
        ("2026-10-07T07:59:59+02:00", "2026-10-06T18:00:00Z"),
    ],
)
def test_fallback_targets_newest_started_cycle_across_delays_and_utc_boundaries(now, expected):
    assert fallback_cycle(datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_fallback_refuses_an_unzoned_clock():
    with pytest.raises(ValueError, match="timezone"):
        fallback_cycle(datetime(2026, 10, 7))


def test_previous_complete_cycle_cannot_acquire_or_build_before_successor_ready(
    tmp_path, monkeypatch, capsys
):
    previous = datetime.fromisoformat("2026-10-06T06:00:00Z")
    target = fallback_cycle(datetime.fromisoformat("2026-10-07T00:03:00Z"))
    probes, checks = [], []

    def latest(**kwargs):
        probes.append(kwargs["time"])
        return previous  # 06Z is complete, but requested 18Z is still uploading.

    def forbidden(*args, **kwargs):
        pytest.fail("an unavailable successor must not charge, build or finish a lease")

    guard = SimpleNamespace(check=lambda: checks.append(True), acquire=forbidden, finish=forbidden)
    monkeypatch.setattr(ecmwf_open, "_client", lambda: SimpleNamespace(latest=latest))
    monkeypatch.setattr(ecmwf_open, "build_short_cube", forbidden)
    monkeypatch.setattr(cli, "enforced", lambda: True)
    monkeypatch.setattr(cli.Guard, "from_env", lambda: guard)
    monkeypatch.setattr(cli, "make_r2_store_from_env", lambda: DirStore(tmp_path))

    assert cli.main(["weather-ecmwf-short", "--cycle", target.strftime("%Y%m%dT%H")]) == 0
    assert probes == [18] and checks == [True]
    assert "cycle not available yet, skipping" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
