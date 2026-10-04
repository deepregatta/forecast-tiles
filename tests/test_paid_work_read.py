"""Control reads recover transport failures without retrying uncertain writes."""

import io
from types import SimpleNamespace

import pytest


@pytest.fixture
def paid():
    from ingest import paid_work

    return paid_work


class ReadClient:
    def __init__(self, events):
        self.events = list(events)
        self.calls = 0
        self.bodies = []

    def get_object(self, **_):
        self.calls += 1
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        if isinstance(event, bytes):
            body = io.BytesIO(event)
        else:
            body = event
        self.bodies.append(body)
        return {"Body": body, "ETag": '"receipt"'}


def test_transient_read_recovers_without_mutating_policy(paid, monkeypatch):
    sleeps = []
    monkeypatch.setattr(paid.time, "sleep", sleeps.append)
    client = ReadClient([TimeoutError("private endpoint"), b'{"paused":true}'])
    doc, etag = paid.Guard(client, "scratch").read()
    assert doc == {"paused": True} and etag == '"receipt"'
    assert client.calls == 2 and sleeps == [1]
    assert client.bodies[0].closed


def test_transient_reads_stop_after_three_attempts(paid, monkeypatch):
    sleeps = []
    monkeypatch.setattr(paid.time, "sleep", sleeps.append)
    client = ReadClient([TimeoutError("secret") for _ in range(4)])
    with pytest.raises(paid.Paused, match="control state unavailable") as error:
        paid.Guard(client, "scratch").read()
    assert client.calls == 3 and sleeps == [1, 2]
    assert "secret" not in str(error.value)


def test_stream_timeout_closes_body_before_reopening(paid, monkeypatch):
    monkeypatch.setattr(paid.time, "sleep", lambda _: None)
    broken = SimpleNamespace(
        read=lambda _: (_ for _ in ()).throw(ConnectionError("lost")),
        close=lambda: closed.append(True),
    )
    closed = []
    client = ReadClient([broken, b"{}"])
    assert paid.Guard(client, "scratch").read()[0] == {}
    assert closed == [True] and client.calls == 2


@pytest.mark.parametrize("status,calls", [(403, 1), (404, 1), (429, 2), (503, 2)])
def test_http_errors_retry_only_transient_status(paid, monkeypatch, status, calls):
    monkeypatch.setattr(paid.time, "sleep", lambda _: None)
    error = RuntimeError("private bucket")
    error.response = {"ResponseMetadata": {"HTTPStatusCode": status}}
    client = ReadClient([error, b"{}"])
    if calls == 1:
        with pytest.raises(paid.Paused):
            paid.Guard(client, "scratch").read()
    else:
        assert paid.Guard(client, "scratch").read()[0] == {}
    assert client.calls == calls


@pytest.mark.parametrize("kind", ["invalid", "oversized"])
def test_invalid_or_oversized_control_does_not_retry(paid, monkeypatch, kind):
    body = b"{invalid" if kind == "invalid" else b"x" * 1_000_001
    monkeypatch.setattr(paid.time, "sleep", lambda _: pytest.fail("invalid state retried"))
    client = ReadClient([body, b"{}"])
    with pytest.raises(paid.Paused):
        paid.Guard(client, "scratch").read()
    assert client.calls == 1 and client.bodies[0].closed


def test_unknown_reservation_write_still_has_one_attempt(paid, monkeypatch):
    monkeypatch.setattr(paid.time, "sleep", lambda _: pytest.fail("write retried"))
    writes = []

    def lost_response(**kwargs):
        writes.append(kwargs)
        raise TimeoutError("committed but reply lost")

    guard = paid.Guard(SimpleNamespace(put_object=lost_response), "scratch")
    with pytest.raises(paid.Paused, match="outcome unknown"):
        guard.write({"usage": {"seconds": 9000}}, '"previous"')
    assert len(writes) == 1


def test_current_sdk_read_timeout_is_retryable(paid, monkeypatch):
    errors = pytest.importorskip("botocore.exceptions")
    monkeypatch.setattr(paid.time, "sleep", lambda _: None)
    client = ReadClient([errors.ReadTimeoutError(endpoint_url="https://private.invalid"), b"{}"])
    assert paid.Guard(client, "scratch").read()[0] == {}
    assert client.calls == 2
