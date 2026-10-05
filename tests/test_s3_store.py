"""S3Store's conditional writes and key prefix, against botocore's Stubber
(no network). tests/test_r2_conditional.py runs the same protocol on R2."""

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import EndpointConnectionError, ReadTimeoutError  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from ingest.publish import (  # noqa: E402
    PreconditionFailed,
    S3Store,
    UncertainWriteError,
)

KW = {"content_type": "application/json", "cache_control": "no-cache"}


@pytest.fixture
def client():
    return boto3.client(
        "s3",
        endpoint_url="https://example.invalid",
        aws_access_key_id="x",
        aws_secret_access_key="x",
        region_name="auto",
    )


def put_params(key, body, **extra):
    return {
        "Bucket": "b",
        "Key": key,
        "Body": body,
        "ContentType": "application/json",
        "CacheControl": "no-cache",
        **extra,
    }


def test_conditional_headers_and_returned_etag(client):
    store = S3Store(client, "b", prefix="check/1/")
    with Stubber(client) as stub:
        stub.add_response(
            "put_object", {"ETag": '"e1"'}, put_params("check/1/latest.json", b"a", IfNoneMatch="*")
        )
        stub.add_response(
            "put_object", {"ETag": '"e2"'}, put_params("check/1/latest.json", b"b", IfMatch='"e1"')
        )
        stub.add_response("put_object", {"ETag": '"e3"'}, put_params("check/1/t.bin", b"c"))
        assert store.put("latest.json", b"a", **KW, if_none_match=True) == '"e1"'
        assert store.put("latest.json", b"b", **KW, if_match='"e1"') == '"e2"'
        assert store.put("t.bin", b"c", **KW) == '"e3"'
        stub.assert_no_pending_responses()


def test_precondition_failures_and_unknown_outcomes(client):
    store = S3Store(client, "b")
    with Stubber(client) as stub:
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        stub.add_client_error("put_object", "ConditionalRequestConflict", http_status_code=409)
        stub.add_client_error("put_object", "InternalError", http_status_code=500)
        stub.add_client_error("put_object", "AccessDenied", http_status_code=403)
        stub.add_client_error("put_object", "InternalError", http_status_code=500)
        with pytest.raises(PreconditionFailed):
            store.put("latest.json", b"a", **KW, if_match='"e"')
        with pytest.raises(PreconditionFailed):
            store.put("latest.json", b"a", **KW, if_none_match=True)
        with pytest.raises(UncertainWriteError):
            store.put("latest.json", b"a", **KW, if_match='"e"')
        with pytest.raises(client.exceptions.ClientError, match="AccessDenied"):
            store.put("latest.json", b"a", **KW, if_match='"e"')
        # an unconditional write's errors are left as they were
        with pytest.raises(client.exceptions.ClientError, match="InternalError"):
            store.put("t.bin", b"a", **KW)


def test_a_lost_connection_on_a_conditional_write_is_an_unknown_outcome(client, monkeypatch):
    store = S3Store(client, "b")

    def lost(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(client, "put_object", lost)
    with pytest.raises(UncertainWriteError, match="EndpointConnectionError"):
        store.put("latest.json", b"a", **KW, if_match='"e"')
    with pytest.raises(EndpointConnectionError):
        store.put("t.bin", b"a", **KW)


def test_reads_lists_and_deletes_stay_under_the_prefix(client):
    store = S3Store(client, "b", prefix="check/1/")
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": _body(b"{}"), "ETag": '"e1"'},
            {"Bucket": "b", "Key": "check/1/latest.json"},
        )
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "check/1/forecast-runs/a/manifest.json", "Size": 3}],
                "IsTruncated": False,
            },
            {"Bucket": "b", "Prefix": "check/1/forecast-runs/"},
        )
        stub.add_response(
            "delete_object", {}, {"Bucket": "b", "Key": "check/1/forecast-runs/a/manifest.json"}
        )
        assert store.get_with_etag("latest.json") == (b"{}", '"e1"')
        assert store.get_with_etag("missing.json") == (None, None)
        assert store.list_keys("forecast-runs/") == ["forecast-runs/a/manifest.json"]
        store.delete("forecast-runs/a/manifest.json")
        stub.assert_no_pending_responses()


def _body(data: bytes):
    import io

    from botocore.response import StreamingBody

    return StreamingBody(io.BytesIO(data), len(data))


def test_missing_size_or_incomplete_pagination_is_never_empty_inventory():
    from types import SimpleNamespace
    from ingest.publish import StorageGuardError

    for response in (
        {},
        {"IsTruncated": True, "Contents": []},
        {"IsTruncated": False, "Contents": [{"Key": "x"}]},
    ):
        store = S3Store(SimpleNamespace(list_objects_v2=lambda **_: response), "b")
        with pytest.raises((StorageGuardError, KeyError)):
            store.list_objects("")


def test_unknown_multipart_page_cannot_return_zero_bytes():
    from types import SimpleNamespace
    from ingest.publish import StorageGuardError

    for response in ({}, {"IsTruncated": True, "Uploads": []}):
        paginator = SimpleNamespace(paginate=lambda **_: [response])
        store = S3Store(SimpleNamespace(get_paginator=lambda _: paginator), "b")
        with pytest.raises(StorageGuardError):
            store.multipart_bytes()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_get_retries_without_changing_the_returned_etag(client, status):
    sleeps = []
    store = S3Store(client, "b", read_sleep=sleeps.append)
    with Stubber(client) as stub:
        stub.add_client_error("get_object", "Transient", http_status_code=status)
        stub.add_response("get_object", {"Body": _body(b"fresh"), "ETag": '"fresh"'})
        assert store.get_with_etag("latest.json") == (b"fresh", '"fresh"')
        stub.assert_no_pending_responses()
    assert sleeps == [1]


def test_get_transport_failure_and_exhaustion_are_bounded(client, monkeypatch):
    calls, sleeps = [], []

    def timeout(**kwargs):
        calls.append(kwargs)
        raise ReadTimeoutError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(client, "get_object", timeout)
    store = S3Store(client, "b", read_sleep=sleeps.append)
    with pytest.raises(ReadTimeoutError):
        store.get("latest.json")
    assert len(calls) == 3 and sleeps == [1, 2]


def test_get_stream_retry_closes_bodies_and_discards_partial_response(client, monkeypatch):
    from types import SimpleNamespace

    closed, sleeps = [], []

    def failed_read():
        raise ReadTimeoutError(endpoint_url="https://example.invalid")

    first = SimpleNamespace(read=failed_read, close=lambda: closed.append("first"))
    second = SimpleNamespace(read=lambda: b"complete", close=lambda: closed.append("second"))
    responses = iter([{"Body": first, "ETag": '"old"'}, {"Body": second, "ETag": '"new"'}])
    monkeypatch.setattr(client, "get_object", lambda **_: next(responses))
    assert S3Store(client, "b", read_sleep=sleeps.append).get_with_etag("latest.json") == (
        b"complete",
        '"new"',
    )
    assert closed == ["first", "second"] and sleeps == [1]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_nontransient_get_is_not_retried(client, status):
    sleeps = []
    with Stubber(client) as stub:
        stub.add_client_error("get_object", "Denied", http_status_code=status)
        with pytest.raises(client.exceptions.ClientError):
            S3Store(client, "b", read_sleep=sleeps.append).get("latest.json")
        stub.assert_no_pending_responses()
    assert sleeps == []
