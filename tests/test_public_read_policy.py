"""Offline policy and loopback HTTP tests; no production network or persistence."""

from copy import deepcopy
from decimal import Decimal
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_public_read_policy import FORECAST_EXPRESSION, class_b_exposure, prepare  # noqa: E402
from probe_public_reads import cache_max_age, probe, validate_body  # noqa: E402


@pytest.fixture
def rule():
    return {
        "id": "reviewed",
        "version": "1",
        "action": "set_cache_settings",
        "enabled": True,
        "expression": FORECAST_EXPRESSION,
        "action_parameters": {
            "cache": True,
            "edge_ttl": {"mode": "bypass_by_default"},
            "origin_range_requests": True,
        },
    }


def test_single_rule_patch_preserves_range_settings_and_other_rules(rule):
    before = deepcopy(rule)
    doc = {"result": {"rules": [{"id": "other", "action": "block"}, rule]}}
    plan = prepare(doc, "reviewed")
    assert plan["update"]["action_parameters"]["origin_range_requests"] is True
    restored = deepcopy(plan["update"])
    del restored["action_parameters"]["browser_ttl"]
    assert restored == plan["rollback"]
    assert rule == before
    assert doc["result"]["rules"][0] == {"id": "other", "action": "block"}
    assert "id" not in plan["update"] and "version" not in plan["update"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("expression", '(http.host eq "another.example")'),
        ("enabled", False),
        ("action", "block"),
        ("unreviewed_parameter", True),
    ],
)
def test_refuse_rule_scope_or_unknown_top_level_drift(rule, field, value):
    rule[field] = value
    with pytest.raises(ValueError):
        prepare(rule, "reviewed")


def test_refuse_ttl_drift_and_duplicate_id(rule):
    rule["action_parameters"]["browser_ttl"] = {"mode": "override_origin", "default": 14400}
    with pytest.raises(ValueError):
        prepare(rule, "reviewed")
    del rule["action_parameters"]["browser_ttl"]
    with pytest.raises(ValueError):
        prepare({"rules": [rule, rule]}, "reviewed")
    rule["action_parameters"]["browser_ttl"] = {"mode": "respect_origin"}
    assert prepare(rule, "reviewed")["changed"] is False


def test_shared_class_b_allowance_and_billing_unit_jump():
    assert class_b_exposure(10_000_000)["class_b_usd"] == "0.00"
    first = class_b_exposure(10_000_001, other_cost_usd=Decimal("0.12"))
    assert first["billable_millions"] == 1
    assert Decimal(first["scenario_total_eur"]) == Decimal("0.576")
    assert class_b_exposure(20_000_000)["scenario_total_eur"] == "4.320"
    assert class_b_exposure(10_000_001)["universal_cap"] is False
    with pytest.raises(ValueError):
        class_b_exposure(-1)


def test_ttl_does_not_confuse_longer_or_ambiguous_freshness():
    assert cache_max_age("public, max-age=3000") == 3000
    assert cache_max_age('public, max-age="300", must-revalidate') == 300
    assert cache_max_age("max-age=300, max-age=14400") is None
    assert cache_max_age("max-age=invalid") is None


@pytest.fixture
def origin():
    tile = gzip.compress(b"PFT1" + bytes(range(256)) * 4, mtime=0)
    bodies = {"/latest.json": b'{"layers":{}}', "/tile.bin.gz": tile}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.respond(head=True)

        def do_GET(self):
            self.respond()

        def respond(self, head=False):
            path = self.path.split("?")[0]
            if path not in bodies:
                self.send_response(404)
                self.end_headers()
                return
            body, status = bodies[path], 200
            total = len(body)
            if self.headers.get("If-None-Match") == '"fixture"' or self.headers.get(
                "If-Modified-Since"
            ):
                status, body = 304, b""
            content_range = None
            if value := self.headers.get("Range"):
                start, end = map(int, value.removeprefix("bytes=").split("-"))
                status, body = 206, body[start : end + 1]
                content_range = f"bytes {start}-{end}/{total}"
            self.send_response(status)
            # Reproduce the measured edge/browser discrepancy independently.
            age = 300 if head else 14400
            self.send_header(
                "Cache-Control",
                f"public, max-age={age}"
                if path == "/latest.json"
                else "public, max-age=31536000, immutable",
            )
            self.send_header("CF-Cache-Status", "DYNAMIC" if head else "HIT")
            self.send_header("ETag", '"fixture"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Last-Modified", "Sun, 04 Oct 2026 12:00:00 GMT")
            if content_range:
                self.send_header("Content-Range", content_range)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_probe_checks_actual_conditional_range_and_error_bytes(origin):
    result = probe(
        origin,
        [
            {"path": "latest.json", "kind": "json", "mutable": True},
            {"path": "tile.bin.gz", "kind": "pft", "mutable": False},
        ],
        queries=True,
    )
    assert result["failures"] == []
    assert result["browser_ttl_discrepancies"] == [
        "latest.json: get does not advertise max-age=300"
    ]
    assert [r["status"] for r in result["records"] if r["mode"].startswith("range-")] == [206, 206]
    assert [r["status"] for r in result["records"] if r["mode"] == "etag"] == [304, 304]
    assert result["origin_operations"] is None  # HIT/DYNAMIC are not billing counters.


def test_probe_refuses_credential_urls_and_invalid_bytes(origin):
    with pytest.raises(ValueError):
        probe("https://user:password@example.invalid", [{"path": "latest.json"}])
    with pytest.raises(ValueError):
        probe(origin, [{"path": "../private.json"}])
    with pytest.raises(ValueError):
        validate_body(b"not a png", "png")
    with pytest.raises(ValueError):
        validate_body(gzip.compress(b"TLI1"), "pft")


def test_probe_detects_corruption_against_pinned_revision(origin):
    result = probe(
        origin,
        [
            {
                "path": "latest.json",
                "kind": "json",
                "mutable": True,
                "bytes": 999,
                "sha256": "0" * 64,
            }
        ],
    )
    assert result["failures"] == [
        "latest.json: manifest byte count mismatch",
        "latest.json: content revision digest mismatch",
    ]
