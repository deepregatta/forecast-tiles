"""S3Store's conditional writes and key prefix, against botocore's Stubber
(no network). tests/test_r2_conditional.py runs the same protocol on R2."""

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import EndpointConnectionError  # noqa: E402
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
