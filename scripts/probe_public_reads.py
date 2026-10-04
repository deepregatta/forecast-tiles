"""Bounded, sequential, read-only HTTP checks for explicit public object samples.

Never discovers credentials, publishes data, purges cache, or estimates origin
operations from CF-Cache-Status. Run manually with private sample/output files.
Each sample is {path, kind: json|png|pft|tli, mutable: bool}.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

HEADERS = {
    "cache-control",
    "cf-cache-status",
    "age",
    "etag",
    "last-modified",
    "content-type",
    "content-length",
    "content-range",
    "content-encoding",
    "accept-ranges",
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
}
MAX_BYTES = 64 * 1024 * 1024


def cache_max_age(value: str) -> int | None:
    values = []
    for directive in value.split(","):
        key, _, seconds = directive.strip().partition("=")
        if key.lower() == "max-age":
            seconds = seconds.strip().strip('"')
            if not seconds.isdecimal():
                return None
            values.append(int(seconds))
    return values[0] if len(values) == 1 else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, method: str = "GET", headers: dict | None = None) -> tuple[dict, bytes]:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "forecast-read-check/1",
            **(headers or {}),
        },
    )
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Sample exceeds the compressed/read byte limit")
        record = {
            "status": response.code,
            "headers": {k.lower(): v for k, v in response.headers.items() if k.lower() in HEADERS},
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    return record, body


def validate_body(body: bytes, kind: str) -> None:
    if kind == "json":
        json.loads(body)
    elif kind == "png":
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG signature")
    elif kind in {"pft", "tli"}:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            raw = stream.read(MAX_BYTES * 2 + 1)
        if len(raw) > MAX_BYTES * 2 or not raw.startswith(b"PFT1" if kind == "pft" else b"TLI1"):
            raise ValueError("Invalid or oversized decompressed tile")
    else:
        raise ValueError("Unknown sample kind")


def probe(base: str, samples: list[dict], *, queries: bool = False) -> dict:
    origin = urllib.parse.urlsplit(base)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or (origin.username or origin.password or origin.query or origin.fragment)
    ):
        raise ValueError("Expected a public base URL without credentials, query or fragment")
    if not 1 <= len(samples) <= 20:
        raise ValueError("Supply 1 to 20 representative samples")
    records, failures, ttl_discrepancies = [], [], []

    def read(path, mode, method="GET", headers=None):
        record, body = request(base.rstrip("/") + "/" + path, method, headers)
        record.update(path=path, mode=mode)
        records.append(record)
        return record, body

    for sample in samples:
        path = sample["path"]
        parsed = urllib.parse.urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or (path.startswith("/") or ".." in path.split("/"))
        ):
            raise ValueError("Expected a relative object path, without query or traversal")
        first, body = read(path, "get", headers={"Origin": "https://passage.deepregatta.com"})
        if first["status"] != 200:
            failures.append(f"{path}: GET {first['status']}")
            continue
        if first["headers"].get("access-control-allow-origin") not in {
            "*",
            "https://passage.deepregatta.com",
        }:
            failures.append(f"{path}: browser GET lacks the required CORS origin")
        if "bytes" in sample and len(body) != sample["bytes"]:
            failures.append(f"{path}: manifest byte count mismatch")
        if "fnv64" in sample:
            from fnv_c import fnv1a_64

            if f"{fnv1a_64(body):016x}" != sample["fnv64"]:
                failures.append(f"{path}: manifest compressed-byte digest mismatch")
        if "sha256" in sample and first["sha256"] != sample["sha256"]:
            failures.append(f"{path}: content revision digest mismatch")
        try:
            validate_body(body, sample["kind"])
        except (ValueError, OSError) as exc:
            failures.append(f"{path}: {type(exc).__name__} validating bytes")
        repeat, repeated = read(path, "repeat")
        if repeat["status"] != 200 or (not sample["mutable"] and repeated != body):
            failures.append(f"{path}: immutable repeat changed or failed")
        head, _ = read(path, "head", "HEAD")
        if head["status"] != 200:
            failures.append(f"{path}: HEAD {head['status']}")
        expected = 300 if sample["mutable"] else 31536000
        for label, item in [("get", first), ("head", head)]:
            if cache_max_age(item["headers"].get("cache-control", "")) != expected:
                ttl_discrepancies.append(f"{path}: {label} does not advertise max-age={expected}")
        for validator, condition in [
            ("etag", "If-None-Match"),
            ("last-modified", "If-Modified-Since"),
        ]:
            if value := first["headers"].get(validator):
                conditional, _ = read(
                    path, validator, headers={condition: value, "Cache-Control": "max-age=0"}
                )
                if conditional["status"] not in ({200, 304} if sample["mutable"] else {304}):
                    failures.append(f"{path}: {condition} {conditional['status']}")
        bypass, bypassed = read(
            path, "reload", headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}
        )
        if bypass["status"] != 200 or (not sample["mutable"] and bypassed != body):
            failures.append(f"{path}: reload failed or changed immutable bytes")
        if sample["kind"] in {"pft", "tli"}:
            for start, end in [(0, 31), (32, 63)]:
                ranged, chunk = read(
                    path, f"range-{start}", headers={"Range": f"bytes={start}-{end}"}
                )
                if ranged["status"] == 206:
                    if (
                        chunk != body[start : end + 1]
                        or ranged["headers"].get("content-range")
                        != f"bytes {start}-{end}/{len(body)}"
                    ):
                        failures.append(f"{path}: incorrect range bytes/total")
                elif ranged["status"] != 200 or chunk != body:
                    failures.append(f"{path}: unusable range response")
    if queries:
        query_paths = [samples[0]["path"]]
        if immutable := next((s for s in samples if not s["mutable"]), None):
            query_paths.append(immutable["path"])
        for path in dict.fromkeys(query_paths):
            for suffix in ["?f02=a", "?f02=a", "?f02=b"]:
                read(path + suffix, "query")
    for _ in range(2):
        missing, _ = read("__f02_missing_object__", "missing")
        if missing["status"] != 404:
            failures.append(f"Missing object: expected 404, got {missing['status']}")
    return {
        "base_url": base,
        "records": records,
        "failures": failures,
        "browser_ttl_discrepancies": ttl_discrepancies,
        "origin_operations": None,
        "origin_note": "CDN statuses are observations, not billed R2 operation counters",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--samples", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--queries", action="store_true", help="Up to six extra query reads")
    parser.add_argument("--require-origin-browser-ttl", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.output.resolve() == repo or repo in args.output.resolve().parents:
        parser.error("write provider observations outside the public repository")
    result = probe(args.base_url, json.loads(args.samples.read_text()), queries=args.queries)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{len(result['records'])} reads; {len(result['failures'])} usability failures; "
        f"{len(result['browser_ttl_discrepancies'])} browser TTL discrepancies"
    )
    raise SystemExit(
        bool(
            result["failures"]
            or (args.require_origin_browser_ttl and result["browser_ttl_discrepancies"])
        )
    )


if __name__ == "__main__":
    main()
