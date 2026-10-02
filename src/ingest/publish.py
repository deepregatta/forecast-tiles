"""Atomic R2 publish protocol (spec § Publish protocol).

Order is load-bearing:
  1. refuse a cycle older than the layer's current one, and check the storage
     guard, before uploading anything
  2. upload all tiles under the new immutable run_id
  3. upload manifest.json LAST (its presence marks the run complete)
  4. re-download manifest + 3 random tiles and decode (post-publish check)
  5. commit this layer's latest.json entry with compare-and-swap
  6. only after a confirmed commit: delete this layer's complete runs older
     than the new previous
  7. write status/{layer}.json

Several layers' jobs can run at once (the dispatcher starts ensemble and
weather-ecmwf-short together, and every fallback cron fires at :37), and they
share one latest.json. Step 5 therefore never writes a whole document it read
earlier: it re-reads the pointer, changes only its own layer's entry, and
writes with If-Match on the ETag it read (If-None-Match: * when the pointer
does not exist yet). A conflict re-reads and merges again, a bounded number of
times. Each attempt refuses to move a layer back to an older cycle, and an
attempt whose outcome is unknown is settled by reading the pointer again
before anything is deleted. A publisher that did not commit prunes nothing,
and retention never deletes a run that is newer than the retained previous,
still incomplete, or referenced by the pointer when it comes to delete it.

The object store is injectable: S3Store wraps boto3 for R2, DirStore writes
the same layout to a local directory (--dry-run and browser dev fixtures),
and tests use an in-memory fake. All three honour the same conditional
writes.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import os
import random
import re
import time
import struct
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ingest.cube import ForecastCube, utcnow_iso
from ingest.validate import ValidationReport
from tilekit.codec import decode_tile

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_MUTABLE = "public, max-age=300, must-revalidate"
OCTET_STREAM = "application/octet-stream"
APPLICATION_JSON = "application/json"

DEFAULT_MAX_BUCKET_BYTES = 8_000_000_000  # 8 GB storage guard

LATEST_KEY = "latest.json"
# Regional models (src/ingest/sources/openmeteo) have a pointer of their own,
# so root-only consumers never see them. Storage, retention and the audit
# count both; the two layer allowlists are disjoint.
REGIONAL_KEY = "latest-regional.json"
POINTER_KEYS = (LATEST_KEY, REGIONAL_KEY)

# Compare-and-swap attempts on the pointer before giving up, and the base of
# the jittered exponential backoff between them (0.5, 1, 2, 4, 8 s, each
# scaled by 0.5-1.5). A conflict means another layer's job committed in the
# same second or so; six attempts ride out a burst of every layer at once.
COMMIT_ATTEMPTS = 6
COMMIT_BACKOFF_S = 0.5
COMMIT_BACKOFF_MAX_S = 8.0

# Hours between a layer's scheduled publications, written to latest.json as
# `cadence_hours` so consumers can estimate the next run without copying this
# repo's schedule. Since 2026-10-01 the dispatcher (README.md -> Dispatcher)
# ingests every provider cycle: four a day for GFS, GFS-Wave and GEFS, the
# two full-horizon ECMWF cycles and its two 144 h ones, and one Copernicus
# bulletin a day.
CADENCE_HOURS = {
    "weather": 6,
    "weather-ecmwf": 12,
    "weather-ecmwf-short": 12,
    "ensemble": 6,
    "waves": 6,
    "currents": 24,
    "currents-ibi": 24,
}
_RUN_ID_RE_TAIL = r"\d{8}T\d{2}Z"


class PublishError(RuntimeError):
    pass


class StorageGuardError(PublishError):
    pass


class PreconditionFailed(PublishError):
    """A conditional write was refused: the object changed since it was read
    (If-Match), or it already exists (If-None-Match: *)."""


class UncertainWriteError(PublishError):
    """A conditional write failed in a way that leaves its outcome unknown
    (connection lost, timeout, 5xx): it may or may not have been applied."""


class StalePublishError(PublishError):
    """The pointer already holds a newer cycle of this layer. Publication is
    monotonic: an older cycle never replaces a newer one."""


class PointerConflictError(PublishError):
    """Every compare-and-swap attempt on the pointer lost to another writer."""


def fnv64(data: bytes) -> str:
    """FNV-1a 64-bit hex digest (manifest hashes are over the gzipped object
    as stored)."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def z_res(resolution_deg: float) -> str:
    """Native-resolution path segment: 0.25 -> z025, 0.5 -> z050, 1/12 -> z008."""
    return f"z{round(resolution_deg * 100):03d}"


# ----------------------------------------------------------- object stores
#
# Every store offers the same five operations:
#   get(key) -> bytes | None
#   get_with_etag(key) -> (bytes, etag) | (None, None)
#   put(key, data, *, content_type, cache_control, if_match=None,
#       if_none_match=False) -> etag | None
#   list_keys(prefix) -> sorted keys
#   delete(key)
# `if_match` writes only while the object's ETag is still the one given;
# `if_none_match` writes only when no object exists. Either raises
# PreconditionFailed otherwise. ETags are concurrency tokens only; manifest
# fnv64 hashes remain the content check.


def content_etag(data: bytes) -> str:
    """The ETag S3 and R2 give a single-part upload: the quoted MD5 hex."""
    return f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"'


class S3Store:
    """boto3-backed store for Cloudflare R2 (S3 API).

    `prefix` places every key under a sub-path of the bucket, so the whole
    protocol can be exercised against real R2 without touching live objects
    (tests/test_r2_conditional.py)."""

    def __init__(self, client, bucket: str, prefix: str = ""):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return self.prefix + key

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str | None:
        kwargs = {
            "Bucket": self.bucket,
            "Key": self._key(key),
            "Body": data,
            "ContentType": content_type,
            "CacheControl": cache_control,
        }
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        conditional = if_match is not None or if_none_match
        try:
            return self.client.put_object(**kwargs).get("ETag")
        except self.client.exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if conditional and (
                error.get("Code") in ("PreconditionFailed", "ConditionalRequestConflict")
                or status == 412
            ):
                raise PreconditionFailed(f"{key}: {error.get('Code') or status}") from exc
            if conditional and isinstance(status, int) and status >= 500:
                raise UncertainWriteError(f"{key}: HTTP {status}") from exc
            raise
        except Exception as exc:
            from botocore.exceptions import BotoCoreError

            if conditional and isinstance(exc, BotoCoreError):
                raise UncertainWriteError(f"{key}: {type(exc).__name__}: {exc}") from exc
            raise

    def get(self, key: str) -> bytes | None:
        return self.get_with_etag(key)[0]

    def get_with_etag(self, key: str) -> tuple[bytes | None, str | None]:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.NoSuchKey:
            return None, None
        except self.client.exceptions.ClientError as exc:  # R2 may 404 differently
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None, None
            raise
        return resp["Body"].read(), resp.get("ETag")

    def list_keys(self, prefix: str) -> list[str]:
        return [obj["key"] for obj in self.list_objects(prefix)]

    def list_objects(self, prefix: str) -> list[dict]:
        """[{key, bytes, modified}] under `prefix` (modified: aware UTC datetime)."""
        out: list[dict] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            out += [
                {
                    "key": obj["Key"][len(self.prefix) :],
                    "bytes": int(obj.get("Size", 0)),
                    "modified": obj.get("LastModified"),
                }
                for obj in resp.get("Contents", [])
            ]
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


class DirStore:
    """Filesystem store writing the exact R2 layout to a local directory
    (--dry-run; also used to generate browser dev fixtures).

    Writes go through a temporary file and a rename, so a reader never sees
    half an object. Conditional writes hold an exclusive lock on the object's
    directory while they compare and write, which makes them atomic across
    processes as R2's are."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    @contextmanager
    def _locked(self, directory: Path):
        fd = os.open(directory, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _write(self, path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str | None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if if_match is None and not if_none_match:
            self._write(path, data)
            return content_etag(data)
        with self._locked(path.parent):
            exists = path.is_file()
            if if_none_match and exists:
                raise PreconditionFailed(f"{key}: already exists")
            if if_match is not None and (not exists or content_etag(path.read_bytes()) != if_match):
                raise PreconditionFailed(f"{key}: changed since it was read")
            self._write(path, data)
        return content_etag(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def get_with_etag(self, key: str) -> tuple[bytes | None, str | None]:
        data = self.get(key)
        return (data, content_etag(data)) if data is not None else (None, None)

    def list_keys(self, prefix: str) -> list[str]:
        return [obj["key"] for obj in self.list_objects(prefix)]

    def list_objects(self, prefix: str) -> list[dict]:
        base = self._path(prefix)
        root = base if base.is_dir() else base.parent
        if not root.is_dir():
            return []
        out = []
        for p in root.rglob("*"):
            if p.name.startswith("."):  # a write's temporary file
                continue
            key = str(p.relative_to(self.root))
            if not key.startswith(prefix) or not p.is_file():
                continue
            stat = p.stat()
            out.append(
                {
                    "key": key,
                    "bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                }
            )
        return sorted(out, key=lambda obj: obj["key"])

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            parent = path.parent
            while parent != self.root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


def make_r2_store_from_env(prefix: str = "") -> S3Store:
    """R2 S3 client from R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
    R2_BUCKET; `prefix` isolates every key under a sub-path (tests only)."""
    import boto3

    missing = [
        k
        for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        if not os.environ.get(k)
    ]
    if missing:
        raise PublishError(f"missing R2 configuration env vars: {', '.join(missing)}")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return S3Store(client, os.environ["R2_BUCKET"], prefix=prefix)


def max_bucket_bytes_from_env() -> int:
    return int(os.environ.get("MAX_BUCKET_BYTES", DEFAULT_MAX_BUCKET_BYTES))


# ----------------------------------------------------------------- helpers


def json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, indent=1, sort_keys=False).encode()


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    return json.loads(raw) if raw is not None else None


def published_layer(store, layer: str, pointer_key: str = LATEST_KEY) -> dict | None:
    """This layer's `latest.json` entry, or None before its first publish."""
    latest = _get_json(store, pointer_key) or {}
    return latest.get("layers", {}).get(layer)


def parse_cycle_iso(value: str) -> datetime:
    """A pointer entry's `cycle` ("2026-07-13T06:00Z") as an aware datetime."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _run_cycle(run_id: str) -> datetime:
    """The cycle a run id names (`weather-20260713T06Z` -> 2026-07-13 06Z)."""
    return datetime.strptime(run_id[-12:], "%Y%m%dT%HZ").replace(tzinfo=timezone.utc)


def _layer_runs(store, layer: str) -> dict[str, list[str]]:
    """This layer's run ids found by listing, each with its object keys. The
    regex keeps `weather-…` from matching `weather-ecmwf-…` runs."""
    pattern = re.compile(rf"^forecast-runs/({re.escape(layer)}-{_RUN_ID_RE_TAIL})/")
    runs: dict[str, list[str]] = {}
    for key in store.list_keys("forecast-runs/"):
        if m := pattern.match(key):
            runs.setdefault(m.group(1), []).append(key)
    return dict(sorted(runs.items()))


def referenced_run_ids(store, pointer_keys: tuple[str, ...] = POINTER_KEYS) -> set[str]:
    """Every current and previous run id the pointers name, read now."""
    referenced: set[str] = set()
    for key in pointer_keys:
        for entry in (_get_json(store, key) or {}).get("layers", {}).values():
            referenced.add(entry["run_id"])
            if entry.get("previous_run_id"):
                referenced.add(entry["previous_run_id"])
    return referenced


def check_not_older(layer: str, current: dict | None, cycle: str, pointer_key: str = LATEST_KEY):
    """Refuse to move `layer` from its current entry back to an older `cycle`."""
    if current and parse_cycle_iso(current["cycle"]) > parse_cycle_iso(cycle):
        raise StalePublishError(
            f"{pointer_key} already has {layer} cycle {current['cycle']} "
            f"({current['run_id']}), newer than {cycle}: publication never moves a layer back"
        )


# ------------------------------------------------------------ publish steps


def cadence_hours_for(layer: str) -> int | None:
    """`cadence_hours` for a layer's pointer entry, root or regional."""
    if layer in CADENCE_HOURS:
        return CADENCE_HOURS[layer]
    from ingest.sources.openmeteo import registry

    return registry.product(layer).cadence_hours if registry.is_regional(layer) else None


def pointer_key_for(layer: str) -> str:
    from ingest.sources.openmeteo import registry

    return REGIONAL_KEY if registry.is_regional(layer) else LATEST_KEY


def check_pointer_ownership(store, layer: str, pointer_key: str) -> None:
    """A layer cannot be named by both catalogues, even after a manual edit."""
    for key in POINTER_KEYS:
        if key != pointer_key and layer in (_get_json(store, key) or {}).get("layers", {}):
            raise PublishError(f"{layer}: duplicate catalogue ownership in {key} and {pointer_key}")


def check_storage_guard(
    store, layer: str, new_run_bytes: int, max_bucket_bytes: int
) -> dict[str, int]:
    """Sum manifest totals of the runs that will be retained after this publish
    plus the new run's bytes; abort before uploading anything if over budget.
    Both pointers count: a separate regional pointer is not another budget."""
    retained: set[str] = set()
    for key in POINTER_KEYS:
        for lyr, entry in (_get_json(store, key) or {}).get("layers", {}).items():
            retained.add(entry["run_id"])
            prev = entry.get("previous_run_id")
            if lyr != layer and prev:
                retained.add(prev)  # this layer's previous gets deleted after publish
    sizes: dict[str, int] = {}
    for run_id in sorted(retained):
        manifest = _get_json(store, f"forecast-runs/{run_id}/manifest.json")
        if manifest is not None:
            sizes[run_id] = int(manifest["totals"]["bytes"])
    total = sum(sizes.values()) + new_run_bytes
    if total > max_bucket_bytes:
        raise StorageGuardError(
            f"storage guard: retained {sum(sizes.values())} B ({sizes}) + new run "
            f"{new_run_bytes} B = {total} B > {max_bucket_bytes} B — refusing to upload"
        )
    return sizes


def build_manifest(
    cube: ForecastCube,
    tiles: list[tuple[str, bytes]],
    report: ValidationReport,
    *,
    published_at: str,
    validated_at: str,
) -> dict:
    tile_map = {tid: {"bytes": len(gz), "fnv64": fnv64(gz)} for tid, gz in tiles}
    regional = pointer_key_for(cube.layer) == REGIONAL_KEY
    if regional:
        for tid, gz in tiles:
            with gzip.GzipFile(fileobj=io.BytesIO(gz)) as stream:
                prefix = stream.read(8)
                header_len = struct.unpack("<I", prefix[4:8])[0]
                header = json.loads(stream.read(header_len))
            decoded = sum(
                var["byte_length"] // (2 if var["dtype"] == "i16" else 1) * 4
                for var in header["variables"]
            )
            inflated = (8 + header_len + 3) // 4 * 4 + sum(
                (var["byte_length"] + 3) // 4 * 4 for var in header["variables"]
            )
            tile_map[tid].update(decoded_bytes=decoded, uncompressed_bytes=inflated)
            if len(gz) > 8 << 20 or decoded > 32 << 20:
                raise PublishError(f"{tid}: exceeds regional browser tile budgets (8/32 MiB)")
    manifest = {
        "schema_version": 1,
        "run_id": cube.run_id,
        "layer": cube.layer,
        "model": cube.model,
        "cycle": cube.cycle_iso,
        "member_count": cube.member_count,
        "resolution_deg": cube.resolution_deg,
        "horizon_h": cube.horizon_h,
        "time_axes": cube.header_time_axes(),
        "variables": [v.public() for v in cube.variables],
        "tiling": {
            "tile_deg": cube.tile_deg,
            "path_template": (
                f"{cube.layer}/{cube.path_label or z_res(cube.resolution_deg)}/{{tile_id}}.bin.gz"
            ),
        },
        "tiles": tile_map,
        "totals": {
            "tile_count": len(tiles),
            "bytes": sum(len(gz) for _, gz in tiles),
        },
        "validation": {"checks_passed": report.checks_passed, "validated_at": validated_at},
        "provenance": cube.provenance,
        "published_at": published_at,
    }
    if regional:
        from ingest.sources.openmeteo.registry import product

        p = product(cube.layer)
        manifest.update(
            coverage={
                "minLat": cube.grid.lat0,
                "maxLat": float(cube.grid.lats()[-1]),
                "minLon": cube.grid.lon0,
                "maxLon": float(cube.grid.lons()[-1]),
            },
            served_grid={
                "lat0": cube.grid.lat0,
                "lon0": cube.grid.lon0,
                "dlat": cube.grid.dlat,
                "dlon": cube.grid.dlon,
                "nlat": cube.grid.nlat,
                "nlon": cube.grid.nlon,
            },
            capabilities=cube.provenance.get("capabilities", ["wind"]),
            attribution=cube.provenance.get("attribution", p.attribution),
            schedule={
                "cycles_utc": list(p.cycles),
                "lag_minutes": {str(hour): lag for hour, lag in p.lag_minutes.items()},
                "wait_minutes": p.wait_minutes,
            },
        )
    return manifest


def _post_publish_check(store, run_id: str, manifest: dict, rng: random.Random) -> None:
    """Re-download the manifest and 3 random tiles; decode and verify hashes."""
    key = f"forecast-runs/{run_id}/manifest.json"
    remote = _get_json(store, key)
    if remote != manifest:
        raise PublishError(f"post-publish check: {key} does not round-trip")
    tile_ids = sorted(manifest["tiles"])
    template = manifest["tiling"]["path_template"]
    for tid in rng.sample(tile_ids, min(3, len(tile_ids))):
        tile_key = f"forecast-runs/{run_id}/" + template.format(tile_id=tid)
        gz = store.get(tile_key)
        if gz is None:
            raise PublishError(f"post-publish check: {tile_key} missing")
        entry = manifest["tiles"][tid]
        if len(gz) != entry["bytes"] or fnv64(gz) != entry["fnv64"]:
            raise PublishError(f"post-publish check: {tile_key} bytes/fnv64 mismatch")
        decoded = decode_tile(gzip.decompress(gz))
        if decoded.header["tile_id"] != tid or decoded.header["run_id"] != run_id:
            raise PublishError(f"post-publish check: {tile_key} decodes to wrong tile/run")


@dataclass
class CommitResult:
    previous_run_id: str | None
    attempts: int
    recovered: bool = False  # found already applied after a conflicting/unknown attempt


def _same_commit(current: dict | None, entry: dict) -> bool:
    return bool(
        current
        and current.get("run_id") == entry["run_id"]
        and current.get("published_at") == entry["published_at"]
    )


def commit_layer_entry(
    store,
    layer: str,
    entry: dict,
    *,
    pointer_key: str = LATEST_KEY,
    attempts: int = COMMIT_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> CommitResult:
    """Point `layer` at `entry` (run_id, cycle, member_count, published_at and
    optional cadence_hours) with compare-and-swap, leaving every other layer's
    entry as the pointer holds it at that moment.

    Each attempt re-reads the pointer, refuses an older cycle, derives
    previous_run_id from what it read (the replaced run, or for the same cycle
    the previous it already had), and writes with If-Match on the ETag it read,
    or If-None-Match: * when there is no pointer yet. A refused write is retried
    from a fresh read; a write with an unknown outcome is settled by that read,
    which finds this exact entry if the write landed. Returns once the commit
    is confirmed; raises StalePublishError or PointerConflictError otherwise,
    and then the caller must not prune anything."""
    expected_pointer = pointer_key_for(layer)
    if pointer_key != expected_pointer:
        raise PublishError(f"{layer} belongs in {expected_pointer}, not {pointer_key}")
    rng = rng or random.Random()
    outcome = ""
    for attempt in range(1, attempts + 1):
        raw, etag = store.get_with_etag(pointer_key)
        doc = json.loads(raw) if raw is not None else None
        layers = (doc or {}).get("layers", {})
        check_pointer_ownership(store, layer, pointer_key)
        current = layers.get(layer)
        if _same_commit(current, entry):
            return CommitResult(current.get("previous_run_id"), attempt, recovered=True)
        check_not_older(layer, current, entry["cycle"], pointer_key)
        previous = current.get("run_id") if current else None
        if previous == entry["run_id"]:  # same cycle again: keep the older previous
            previous = current.get("previous_run_id")
        new_entry = {
            "run_id": entry["run_id"],
            "previous_run_id": previous,
            "cycle": entry["cycle"],
            "member_count": entry["member_count"],
            "published_at": entry["published_at"],
        }
        if entry.get("cadence_hours") is not None:
            new_entry["cadence_hours"] = entry["cadence_hours"]
        new_doc = dict(doc) if doc is not None else {}
        new_doc["schema_version"] = new_doc.get("schema_version", 1)
        new_doc["updated_at"] = entry["published_at"]
        new_doc["layers"] = {**layers, layer: new_entry}
        try:
            store.put(
                pointer_key,
                json_bytes(new_doc),
                content_type=APPLICATION_JSON,
                cache_control=CACHE_MUTABLE,
                if_match=etag if doc is not None else None,
                if_none_match=doc is None,
            )
            return CommitResult(previous, attempt)
        except PreconditionFailed as exc:
            outcome = f"lost to another writer ({exc})"
        except UncertainWriteError as exc:
            outcome = f"outcome unknown ({exc})"
        if attempt < attempts:
            delay = min(COMMIT_BACKOFF_MAX_S, COMMIT_BACKOFF_S * 2 ** (attempt - 1))
            delay *= 0.5 + rng.random()
            print(
                f"publish {layer}: {pointer_key} attempt {attempt} {outcome}; "
                f"re-reading in {delay:.1f} s",
                flush=True,
            )
            sleep(delay)
    # the last attempt may have landed after all: settle it before giving up
    raw, _ = store.get_with_etag(pointer_key)
    current = (json.loads(raw) if raw is not None else {}).get("layers", {}).get(layer)
    if _same_commit(current, entry):
        return CommitResult(current.get("previous_run_id"), attempts, recovered=True)
    raise PointerConflictError(
        f"{pointer_key}: {layer} -> {entry['run_id']} not committed after {attempts} "
        f"attempts (last: {outcome}); nothing pruned"
    )


def apply_retention(
    store,
    layer: str,
    run_id: str,
    previous: str | None,
    *,
    pointer_keys: tuple[str, ...] = POINTER_KEYS,
) -> tuple[list[str], list[str]]:
    """After a confirmed commit, delete this layer's complete runs older than
    the retained previous (older than the new run when there is none).

    Runs found by listing that are newer than that, have no manifest (still
    uploading, or abandoned: the audit script cleans those, age-gated), or are
    named by a pointer re-read just before deletion are left alone. Tiles go
    before the manifest, mirroring upload, so an interrupted deletion leaves a
    run this function still recognizes and finishes next time.

    Returns (deleted run ids, ["run_id (reason)"] left in place)."""
    floor = _run_cycle(previous or run_id)
    deleted: list[str] = []
    kept: list[str] = []
    for rid, keys in _layer_runs(store, layer).items():
        if rid in (run_id, previous):
            continue
        manifest_key = f"forecast-runs/{rid}/manifest.json"
        if _run_cycle(rid) >= floor:
            kept.append(f"{rid} (not older than the retained previous)")
        elif manifest_key not in keys:
            kept.append(f"{rid} (incomplete: no manifest)")
        elif rid in referenced_run_ids(store, pointer_keys):
            kept.append(f"{rid} (referenced by a pointer)")
        else:
            for key in sorted(keys, key=lambda k: k == manifest_key):
                store.delete(key)
            deleted.append(rid)
    return deleted, kept


# ------------------------------------------------------------------- entry


@dataclass
class PublishResult:
    run_id: str
    tile_count: int
    bytes: int
    duration_s: float
    previous_run_id: str | None
    deleted_runs: list[str]
    kept_runs: list[str] = field(default_factory=list)  # "run_id (reason)" left by retention
    commit_attempts: int = 1


def publish_run(
    store,
    cube: ForecastCube,
    tiles: list[tuple[str, bytes]],
    report: ValidationReport,
    *,
    max_bucket_bytes: int = DEFAULT_MAX_BUCKET_BYTES,
    rng: random.Random | None = None,
    started_at: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    pointer_key: str | None = None,
) -> PublishResult:
    """Run the full atomic publish protocol for an already-validated cube.

    Raises StalePublishError (before uploading anything, or at the commit if a
    newer cycle landed meanwhile) rather than move the layer back a cycle, and
    PointerConflictError when the pointer kept changing under every attempt.
    Neither deletes anything.

    `pointer_key` defaults to the layer's own pointer (latest.json, or
    latest-regional.json for a registered regional model); a layer is never
    committed to the other one."""
    expected_pointer = pointer_key_for(cube.layer)
    pointer_key = pointer_key or expected_pointer
    if pointer_key != expected_pointer:
        raise PublishError(f"{cube.layer} belongs in {expected_pointer}, not {pointer_key}")
    if not report.ok:
        raise PublishError(
            f"refusing to publish a cube that failed validation:\n{report.summary()}"
        )
    if not tiles:
        raise PublishError("refusing to publish a run with zero tiles")
    t0 = started_at if started_at is not None else time.time()
    rng = rng or random.Random()
    run_id = cube.run_id
    regional = pointer_key == REGIONAL_KEY
    published_at = utcnow_iso()
    manifest = build_manifest(
        cube, tiles, report, published_at=published_at, validated_at=published_at
    )

    # 1. an older cycle than the pointer's, and the storage guard: before any upload
    current = published_layer(store, cube.layer, pointer_key)
    check_pointer_ownership(store, cube.layer, pointer_key)
    check_not_older(cube.layer, current, cube.cycle_iso, pointer_key)
    if regional and store.get(f"forecast-runs/{run_id}/manifest.json") is not None:
        raise PreconditionFailed(f"{run_id}: immutable manifest already exists; nothing uploaded")
    check_storage_guard(store, cube.layer, manifest["totals"]["bytes"], max_bucket_bytes)
    if regional:
        from ingest.sources.openmeteo.registry import product

        run_bytes = manifest["totals"]["bytes"] + len(json_bytes(manifest))
        if run_bytes > product(cube.layer).max_run_bytes:
            raise StorageGuardError(f"{cube.layer}: compressed run exceeds its registered cap")
        if isinstance(store, S3Store):
            from ingest.capacity import RegionalAllocation, check_regional_capacity

            check_regional_capacity(
                store,
                run_bytes,
                max_bucket_bytes,
                RegionalAllocation.from_env(cube.layer),
            )

    # 2. tiles under the immutable run id
    template = manifest["tiling"]["path_template"]
    for tid, gz in tiles:
        store.put(
            f"forecast-runs/{run_id}/" + template.format(tile_id=tid),
            gz,
            content_type=OCTET_STREAM,  # stored gzipped; client decompresses explicitly
            cache_control=CACHE_IMMUTABLE,
            if_none_match=regional,
        )

    # 3. manifest LAST — its presence marks the run complete
    store.put(
        f"forecast-runs/{run_id}/manifest.json",
        json_bytes(manifest),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_IMMUTABLE,
        if_none_match=regional,
    )

    # 4. post-publish check
    _post_publish_check(store, run_id, manifest, rng)

    # 5. this layer's latest.json entry, compare-and-swap
    entry = {
        "run_id": run_id,
        "cycle": cube.cycle_iso,
        "member_count": cube.member_count,
        "published_at": published_at,
        "cadence_hours": cadence_hours_for(cube.layer),
    }
    commit = commit_layer_entry(
        store, cube.layer, entry, pointer_key=pointer_key, sleep=sleep, rng=rng
    )
    previous = commit.previous_run_id

    # 6. retention, only now that the commit is confirmed
    deleted, kept = apply_retention(store, cube.layer, run_id, previous)

    # 7. per-layer status
    duration_s = round(time.time() - t0, 1)
    status = {
        "layer": cube.layer,
        "run_id": run_id,
        "cycle": cube.cycle_iso,
        "published_at": published_at,
        "tile_count": manifest["totals"]["tile_count"],
        "bytes": manifest["totals"]["bytes"],
        "duration_s": duration_s,
        "checks_passed": report.checks_passed,
    }
    store.put(
        f"status/{cube.layer}.json",
        json_bytes(status),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_MUTABLE,
    )

    return PublishResult(
        run_id=run_id,
        tile_count=manifest["totals"]["tile_count"],
        bytes=manifest["totals"]["bytes"],
        duration_s=duration_s,
        previous_run_id=previous,
        deleted_runs=deleted,
        kept_runs=kept,
        commit_attempts=commit.attempts,
    )
