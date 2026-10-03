"""Open-Meteo reader and catalog, offline: whole-file GETs against a fake
session, completion metadata, cycle selection and the pre-publication
recheck (docs/open-meteo-bulk-implementation-plan.md, Reader and model registry)."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ingest import cli, publish
from ingest.sources.base import CycleNotAvailableError
from ingest.sources.openmeteo import catalog, reader, registry
from ingest.sources.openmeteo.catalog import CycleNotRegistered
from ingest.sources.openmeteo.reader import NotFound, ObjectRecord, SourceError

AROME = registry.AROME
CYCLE = datetime(2026, 10, 2, 3, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, short_by=0):
        self.status_code = status
        self._body = body
        self.headers = {"ETag": '"e1"', "Content-Length": str(len(body))} | (headers or {})
        self._short_by = short_by

    @property
    def content(self):
        return self._body

    def json(self):
        return json.loads(self._body)

    def iter_content(self, n):
        body = self._body[: len(self._body) - self._short_by]
        for i in range(0, len(body), 4):
            yield body[i : i + 4]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Serves queued responses per URL suffix, recording every request."""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def _next(self, method, url):
        self.calls.append((method, url))
        for suffix, queue in self.routes.items():
            if url.endswith(suffix):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        return FakeResponse(404)

    def get(self, url, stream=False, timeout=None):
        return self._next("GET", url)

    def head(self, url, timeout=None):
        return self._next("HEAD", url)


def meta_doc(cycle=CYCLE, leads=range(52), variables=None):
    return json.dumps(
        {
            "reference_time": f"{cycle:%Y-%m-%dT%H:%M:%SZ}",
            "created_at": "2026-10-02T05:47:40Z",
            "valid_times": [
                f"{datetime.fromtimestamp(cycle.timestamp() + 3600 * h, timezone.utc):%Y-%m-%dT%H:%MZ}"
                for h in leads
            ],
            "variables": variables or list(AROME.files.values()) + ["temperature_2m"],
        }
    ).encode()


def no_sleep(seconds):
    pass


# ------------------------------------------------------------- downloads


def test_download_records_key_etag_and_length(tmp_path):
    session = FakeSession({"u.om": [FakeResponse(body=b"0123456789", headers={"ETag": '"x-3"'})]})
    record = reader.download("data_run/d/u.om", tmp_path / "u.om", session=session)
    assert record == ObjectRecord("data_run/d/u.om", '"x-3"', 10)
    assert (tmp_path / "u.om").read_bytes() == b"0123456789"
    assert session.calls == [("GET", f"{registry.BUCKET_URL}/data_run/d/u.om")]


def test_a_truncated_body_is_retried_then_refused(tmp_path):
    sleeps = []
    short = FakeResponse(body=b"0123456789", short_by=3)
    session = FakeSession({"u.om": [short, FakeResponse(body=b"0123456789")]})
    record = reader.download("k/u.om", tmp_path / "u.om", session=session, sleep=sleeps.append)
    assert record.bytes == 10 and sleeps == [2]

    session = FakeSession({"u.om": [short]})
    with pytest.raises(SourceError, match="truncated: 7 of 10 bytes"):
        reader.download("k/u.om", tmp_path / "u.om", session=session, sleep=no_sleep)
    assert len(session.calls) == reader.ATTEMPTS


def test_throttling_and_server_errors_are_retried_with_a_bound(tmp_path):
    import requests

    session = FakeSession(
        {
            "u.om": [
                FakeResponse(429),
                requests.ConnectionError("reset"),
                FakeResponse(503),
                FakeResponse(body=b"ok"),
            ]
        }
    )
    sleeps = []
    assert (
        reader.download("k/u.om", tmp_path / "u", session=session, sleep=sleeps.append).bytes == 2
    )
    assert sleeps == [2, 4, 8]

    session = FakeSession({"u.om": [FakeResponse(500)]})
    with pytest.raises(SourceError, match="HTTP 500"):
        reader.download("k/u.om", tmp_path / "u", session=session, sleep=no_sleep)
    assert len(session.calls) == reader.ATTEMPTS


def test_a_missing_file_is_not_retried(tmp_path):
    session = FakeSession({})
    with pytest.raises(NotFound):
        reader.download("k/u.om", tmp_path / "u", session=session, sleep=no_sleep)
    assert len(session.calls) == 1


# ------------------------------------------------------------- catalog


def test_run_keys_follow_the_data_run_layout():
    assert (
        catalog.run_prefix(AROME, CYCLE)
        == "data_run/meteofrance_arome_france0025/2026/10/02/0300Z/"
    )
    assert catalog.file_key(AROME, CYCLE, "gust").endswith("/0300Z/wind_gusts_10m.om")


def test_explicit_cycle_means_exactly_that_cycle():
    session = FakeSession({"0300Z/meta.json": [FakeResponse(body=meta_doc())]})
    assert catalog.resolve(AROME, CYCLE, session=session) == CYCLE
    with pytest.raises(CycleNotRegistered, match="not 00Z"):
        catalog.resolve(AROME, CYCLE.replace(hour=0), session=session)
    with pytest.raises(CycleNotAvailableError, match="no meta.json"):
        catalog.resolve(AROME, CYCLE.replace(hour=9), session=session)


def test_automatic_selection_takes_the_newest_complete_registered_cycle():
    session = FakeSession(
        {"/0900Z/meta.json": [FakeResponse(body=meta_doc(CYCLE.replace(hour=9)))]}
    )
    now = datetime(2026, 10, 2, 17, 40, tzinfo=timezone.utc)  # 15Z not complete yet
    assert catalog.resolve(AROME, now=now, session=session) == CYCLE.replace(hour=9)
    probed = [url.split("/")[-2] for _, url in session.calls]
    assert probed == ["1500Z", "0900Z"], "only registered hours, newest first"


def test_lookback_is_bounded():
    session = FakeSession({})
    now = datetime(2026, 10, 2, 17, tzinfo=timezone.utc)
    with pytest.raises(CycleNotAvailableError, match="no complete"):
        catalog.resolve(AROME, now=now, session=session)
    assert len(session.calls) == AROME.lookback_cycles


def test_explicit_cycle_with_early_metadata_is_not_ready():
    session = FakeSession(
        {"0300Z/meta.json": [FakeResponse(body=meta_doc(variables=["temperature_2m"]))]}
    )
    with pytest.raises(CycleNotAvailableError, match="wind_u_component_10m"):
        catalog.resolve(AROME, CYCLE, session=session)


def test_automatic_selection_skips_incomplete_newer_metadata():
    newer = CYCLE.replace(hour=9)
    session = FakeSession(
        {
            "0900Z/meta.json": [FakeResponse(body=meta_doc(newer, leads=range(48)))],
            "0300Z/meta.json": [FakeResponse(body=meta_doc())],
        }
    )
    assert catalog.resolve(AROME, now=newer.replace(hour=10), session=session) == CYCLE


def test_resolver_does_not_wait_on_wrong_reference_time():
    session = FakeSession(
        {"0300Z/meta.json": [FakeResponse(body=meta_doc(cycle=CYCLE.replace(hour=0)))]}
    )
    with pytest.raises(SourceError, match="reference_time"):
        catalog.resolve(AROME, CYCLE, session=session)


def test_a_complete_run_needs_its_files_and_the_whole_axis():
    def meta(**kwargs):
        session = FakeSession({"meta.json": [FakeResponse(body=meta_doc(**kwargs))]})
        return catalog.fetch_meta(AROME, CYCLE, session=session)

    catalog.require_complete(AROME, meta(), ["u", "v", "gust"])
    with pytest.raises(SourceError, match="wind_gusts_10m"):
        catalog.require_complete(
            AROME,
            meta(variables=["wind_u_component_10m", "wind_v_component_10m"]),
            ["u", "v", "gust"],
        )
    with pytest.raises(SourceError, match=r"lack \+\[48, 49, 50, 51\] h"):
        catalog.require_complete(AROME, meta(leads=range(48)), ["u", "v"])
    with pytest.raises(SourceError, match="reference_time"):
        catalog.require_complete(AROME, meta(cycle=CYCLE.replace(hour=0)), ["u", "v"])


def test_recheck_refuses_changed_metadata_or_files():
    meta = ObjectRecord("r/meta.json", '"m1"', 100)
    files = [ObjectRecord("r/u.om", '"u1-3"', 10)]
    same = FakeSession(
        {
            "meta.json": [FakeResponse(body=b"{}", headers={"ETag": '"m1"'})],
            "u.om": [FakeResponse(headers={"ETag": '"u1-3"', "Content-Length": "10"})],
        }
    )
    catalog.recheck(meta, files, session=same)

    changed_meta = FakeSession({"meta.json": [FakeResponse(body=b"{}", headers={"ETag": '"m2"'})]})
    with pytest.raises(SourceError, match="meta.json changed"):
        catalog.recheck(meta, files, session=changed_meta)

    changed_file = FakeSession(
        {
            "meta.json": [FakeResponse(body=b"{}", headers={"ETag": '"m1"'})],
            "u.om": [FakeResponse(headers={"ETag": '"u2-3"', "Content-Length": "10"})],
        }
    )
    with pytest.raises(SourceError, match="u.om changed"):
        catalog.recheck(meta, files, session=changed_file)


def test_source_digest_is_order_independent():
    a, b = ObjectRecord("a", '"1"', 1), ObjectRecord("b", '"2"', 2)
    assert catalog.source_digest([a, b]) == catalog.source_digest([b, a])
    assert catalog.source_digest([a]) != catalog.source_digest([replace(a, etag='"9"')])


# ------------------------------------------------------------- registry


def test_regional_layers_reach_every_setting_without_a_key_error():
    for layer in registry.LAYERS:
        p = registry.product(layer)
        assert layer in cli.LAYERS
        assert cli.max_missing(layer) == p.max_interior_missing
        assert cli.poll_seconds(layer) == p.poll_seconds
        assert cli.skip_when_not_available(layer) is True
        assert publish.cadence_hours_for(layer) == p.cadence_hours
        assert publish.pointer_key_for(layer) == publish.REGIONAL_KEY
    for layer in publish.CADENCE_HOURS:  # existing settings are unchanged
        assert cli.max_missing(layer) == cli.MAX_MISSING[layer]
        assert cli.poll_seconds(layer) == cli.POLL_SECONDS[layer]
        assert publish.pointer_key_for(layer) == publish.LATEST_KEY


def test_root_and_regional_allowlists_are_disjoint():
    assert not set(registry.LAYERS) & set(publish.CADENCE_HOURS)
    assert not set(registry.LAYERS) & set(cli.MAX_MISSING)


def test_registered_axes_grids_and_labels():
    assert {h: len(a) for h, a in registry.AROME.axes.items()} == {3: 52, 9: 52, 15: 52, 21: 52}
    icon = registry.ICON_EU.axes[6]
    assert len(icon) == 93 and icon[78:80] == (78, 81) and icon[-1] == 120
    assert registry.AROME.grid.label == "grid-0p025"
    assert registry.ICON_EU.grid.label == "grid-0p0625"
    assert registry.AROME.gust_windows.window(51) == 1
    assert registry.ICON_EU.gust_windows.window(78) == 1
    assert registry.ICON_EU.gust_windows.window(81) == 1
    assert registry.ICON_EU.gust_windows.window(0) is None


def test_registered_gust_windows_match_recorded_upstream_grib_intervals():
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures/openmeteo/gust-windows"
    for path in fixtures.glob("*.json"):
        record = json.loads(path.read_text())
        p = registry.product(record["layer"])
        assert p.gust_windows.verified and record["messages"]
        for message in record["messages"]:
            start, end = int(message["startStep"]), int(message["endStep"])
            assert end - start == p.gust_windows.window(end)
            assert message["stepType"] == "max" and message["url"].startswith("https://")


def test_the_registered_footprint_loads_and_tampering_is_refused(tmp_path):
    from ingest.sources.openmeteo import grids

    valid = grids.load_footprint(registry.AROME)
    assert valid.shape == (717, 1121)
    assert abs((~valid).mean() - 0.1718) < 1e-3
    assert not valid[-1].any(), "the northernmost row is outside AROME's domain"
    assert grids.load_footprint(registry.ICON_EU) is None

    tampered = valid.copy()
    tampered[300, 300] = False
    grids.save_footprint(registry.AROME.footprint, tampered, {}, directory=tmp_path)
    with pytest.raises(grids.FootprintError, match="sha256"):
        grids.load_footprint(registry.AROME, directory=tmp_path)
