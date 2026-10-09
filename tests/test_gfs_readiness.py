"""GFS may expose its final index while an earlier required file is still missing."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import pytest

from ingest import cli
from ingest.publish import DirStore
from ingest.sources import base, gfs
from ingest.sources.base import CycleNotAvailableError

CYCLE = datetime(2026, 10, 9, 6, tzinfo=timezone.utc)
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def files(cycle=CYCLE):
    # The declared consumer axes require hourly files through 120h and then
    # three-hourly files through 240h, with a GRIB and index for each.
    return {
        urlparse(gfs.step_url(cycle, step)).path.lstrip("/") + suffix
        for step in [*range(121), *range(123, 241, 3)]
        for suffix in ("", ".idx")
    }


def inventory(keys, *, truncated=False, token=None):
    root = ElementTree.Element(NS + "ListBucketResult")
    ElementTree.SubElement(root, NS + "IsTruncated").text = str(truncated).lower()
    for key in sorted(keys):
        item = ElementTree.SubElement(root, NS + "Contents")
        ElementTree.SubElement(item, NS + "Key").text = key
    if token:
        ElementTree.SubElement(root, NS + "NextContinuationToken").text = token
    return ElementTree.tostring(root)


@pytest.mark.parametrize("missing", ["f237.idx", "f237", "f001.idx"])
def test_final_index_cannot_admit_a_cycle_missing_an_earlier_required_file(monkeypatch, missing):
    keys = {k for k in files() if not k.endswith(missing)}
    assert any(k.endswith("f240.idx") for k in keys)
    monkeypatch.setattr(base, "head_ok", lambda url: True)
    monkeypatch.setattr(gfs, "http", lambda url: inventory(keys))
    with pytest.raises(CycleNotAvailableError, match="requested cycle.*incomplete"):
        gfs.resolve(CYCLE)


def test_all_required_files_across_pages_can_resolve_only_the_requested_cycle(monkeypatch):
    keys = sorted(files())
    calls = []

    def fetch(url):
        query = parse_qs(urlparse(url).query)
        calls.append(query)
        assert query["prefix"] == ["gfs.20261009/06/atmos/gfs.t06z.pgrb2.0p25.f"]
        if "continuation-token" not in query:
            return inventory(keys[:100], truncated=True, token="cursor/with+spaces")
        assert query["continuation-token"] == ["cursor/with+spaces"]
        return inventory(keys[100:])

    monkeypatch.setattr(base, "head_ok", lambda url: True)
    monkeypatch.setattr(gfs, "http", fetch)
    assert gfs.resolve(CYCLE) == CYCLE
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [b"<html>not an inventory</html>", b"<broken", inventory(files(), truncated=True)],
)
def test_unknown_or_truncated_inventory_never_confirms_readiness(monkeypatch, body):
    monkeypatch.setattr(base, "head_ok", lambda url: True)
    monkeypatch.setattr(gfs, "http", lambda url: body)
    with pytest.raises(CycleNotAvailableError, match="inventory cannot confirm"):
        gfs.resolve(CYCLE)


def test_repeated_inventory_cursor_stops_without_unbounded_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(base, "head_ok", lambda url: True)
    monkeypatch.setattr(
        gfs,
        "http",
        lambda url: calls.append(url) or inventory(files(), truncated=True, token="same"),
    )
    with pytest.raises(CycleNotAvailableError, match="inventory cannot confirm"):
        gfs.resolve(CYCLE)
    assert len(calls) == 2


def test_latest_resolution_uses_complete_previous_cycle_without_substituting_explicit_cycle(
    monkeypatch,
):
    previous = CYCLE - timedelta(hours=6)
    monkeypatch.setattr(base, "datetime", SimpleNamespace(now=lambda tz: CYCLE))
    monkeypatch.setattr(base, "head_ok", lambda url: True)

    def fetch(url):
        prefix = parse_qs(urlparse(url).query)["prefix"][0]
        if "/06/" in prefix:
            return inventory({k for k in files() if not k.endswith("f237.idx")})
        return inventory(files(previous))

    monkeypatch.setattr(gfs, "http", fetch)
    assert gfs.resolve() == previous
    with pytest.raises(CycleNotAvailableError, match="requested cycle.*incomplete"):
        gfs.resolve(CYCLE)


def test_missing_earlier_file_waits_within_existing_deadline_before_any_admission(
    tmp_path, monkeypatch, capsys
):
    checks, sleeps = [], []

    def forbidden(*args, **kwargs):
        pytest.fail("incomplete GFS source must not charge, build, finish a lease or publish")

    guard = SimpleNamespace(check=lambda: checks.append(True), acquire=forbidden, finish=forbidden)
    monkeypatch.setattr(base, "head_ok", lambda url: True)
    monkeypatch.setattr(
        gfs,
        "http",
        lambda url: inventory(files() - {min(k for k in files() if k.endswith("f237.idx"))}),
    )
    monkeypatch.setattr(gfs, "build_cube", forbidden)
    monkeypatch.setattr(cli, "enforced", lambda: True)
    monkeypatch.setattr(cli.Guard, "from_env", lambda: guard)
    monkeypatch.setattr(cli, "make_r2_store_from_env", lambda: DirStore(tmp_path))
    elapsed = [0]

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    assert (
        cli.main(
            ["weather", "--cycle", "20261009T06", "--wait-minutes", "2"],
            clock=lambda: elapsed[0],
            sleep=sleep,
        )
        == 1
    )
    assert sleeps == [60, 60] and checks == [True]
    assert "not available after 2 min (3 checks" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
