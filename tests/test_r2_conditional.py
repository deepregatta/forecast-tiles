"""Conditional writes and the publish protocol against real Cloudflare R2.

Skipped unless R2_TEST_PREFIX is set alongside the R2_* credentials. Every
key lives under that prefix, which must start with `r2-check/`, and the test
deletes everything under it afterwards, so live objects are never touched.
Run it from the `r2-conditional-check` workflow (Actions -> Run workflow), or:

  R2_TEST_PREFIX=r2-check/manual-1/ uv run pytest tests/test_r2_conditional.py
"""

import json
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_weather_cube

from ingest.publish import (
    PreconditionFailed,
    StalePublishError,
    make_r2_store_from_env,
    publish_run,
)
from ingest.tile import build_tiles
from ingest.validate import validate_cube

PREFIX = os.environ.get("R2_TEST_PREFIX", "")
pytestmark = pytest.mark.skipif(not PREFIX, reason="R2_TEST_PREFIX not set (live R2 check)")

KW = {"content_type": "application/json", "cache_control": "no-store"}


@pytest.fixture(scope="module")
def store():
    assert PREFIX.startswith("r2-check/") and PREFIX.endswith("/"), (
        "R2_TEST_PREFIX must look like r2-check/<id>/ so no live key can be reached"
    )
    s = make_r2_store_from_env(prefix=PREFIX)
    yield s
    for key in s.list_keys(""):
        s.delete(key)
    assert s.list_keys("") == [], "the isolated prefix must be empty afterwards"


def test_if_none_match_creates_once(store):
    key = f"create-{uuid.uuid4().hex}.json"
    etag = store.put(key, b"one", **KW, if_none_match=True)
    assert etag
    with pytest.raises(PreconditionFailed):
        store.put(key, b"two", **KW, if_none_match=True)
    assert store.get(key) == b"one"


def test_if_match_swaps_only_from_the_etag_read(store):
    key = f"swap-{uuid.uuid4().hex}.json"
    store.put(key, b"one", **KW)
    body, etag = store.get_with_etag(key)
    assert body == b"one" and etag
    new_etag = store.put(key, b"two", **KW, if_match=etag)
    with pytest.raises(PreconditionFailed):
        store.put(key, b"three", **KW, if_match=etag)  # stale ETag
    assert store.get_with_etag(key) == (b"two", new_etag)
    with pytest.raises(PreconditionFailed):
        store.put(f"absent-{uuid.uuid4().hex}", b"x", **KW, if_match=etag)


def publish(store, layer, hour, **kwargs):
    cube = make_weather_cube()
    cube.layer = layer
    cube.cycle = datetime(2026, 7, 13, tzinfo=timezone.utc) + timedelta(hours=hour)
    report = validate_cube(cube)
    return publish_run(
        store,
        cube,
        build_tiles(cube),
        report,
        rng=random.Random(0),
        sleep=lambda s: None,
        **kwargs,
    )


def test_interleaved_layers_and_a_stale_job_on_r2(store):
    publish(store, "weather", 0)
    publish(store, "ensemble", 0)

    # ensemble 06Z commits between weather 06Z's read and its compare-and-swap
    original = store.put
    fired = []

    def put(key, data, **kwargs):
        if key == "latest.json" and kwargs.get("if_match") and not fired:
            fired.append(key)
            publish(store, "ensemble", 6)
        return original(key, data, **kwargs)

    store.put = put
    try:
        result = publish(store, "weather", 6)
    finally:
        store.put = original
    assert fired and result.commit_attempts == 2

    layers = json.loads(store.get("latest.json"))["layers"]
    assert layers["weather"]["run_id"] == "weather-20260713T06Z"
    assert layers["ensemble"]["run_id"] == "ensemble-20260713T06Z"
    for entry in layers.values():
        for run_id in (entry["run_id"], entry["previous_run_id"]):
            assert store.get(f"forecast-runs/{run_id}/manifest.json") is not None

    with pytest.raises(StalePublishError):
        publish(store, "weather", 0)
