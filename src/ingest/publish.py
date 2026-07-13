"""Atomic R2 publish protocol (spec § Publish protocol).

Order is load-bearing:
  1. storage guard FIRST (fail loudly before uploading anything)
  2. upload all tiles under the new immutable run_id
  3. upload manifest.json LAST (its presence marks the run complete)
  4. re-download manifest + 3 random tiles and decode (post-publish check)
  5. read-modify-write latest.json
  6. delete runs older than the new previous for this layer
  7. write status/{layer}.json

The object store is injectable: S3Store wraps boto3 for R2, DirStore writes
the same layout to a local directory (--dry-run and browser dev fixtures),
and tests use an in-memory fake.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ingest.cube import ForecastCube, utcnow_iso
from ingest.validate import ValidationReport
from tilekit.codec import decode_tile

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_MUTABLE = "public, max-age=300, must-revalidate"
OCTET_STREAM = "application/octet-stream"
APPLICATION_JSON = "application/json"

DEFAULT_MAX_BUCKET_BYTES = 8_000_000_000  # 8 GB storage guard
_RUN_ID_RE_TAIL = r"\d{8}T\d{2}Z"


class PublishError(RuntimeError):
    pass


class StorageGuardError(PublishError):
    pass


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


class S3Store:
    """boto3-backed store for Cloudflare R2 (S3 API)."""

    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl=cache_control,
        )

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except self.client.exceptions.ClientError as exc:  # R2 may 404 differently
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            keys += [obj["Key"] for obj in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class DirStore:
    """Filesystem store writing the exact R2 layout to a local directory
    (--dry-run; also used to generate browser dev fixtures)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        root = base if base.is_dir() else base.parent
        if not root.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.root))
            for p in root.rglob("*")
            if p.is_file() and str(p.relative_to(self.root)).startswith(prefix)
        )

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
            parent = path.parent
            while parent != self.root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


def make_r2_store_from_env() -> S3Store:
    """R2 S3 client from R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
    R2_BUCKET."""
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
    return S3Store(client, os.environ["R2_BUCKET"])


def max_bucket_bytes_from_env() -> int:
    return int(os.environ.get("MAX_BUCKET_BYTES", DEFAULT_MAX_BUCKET_BYTES))


# ----------------------------------------------------------------- helpers


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, indent=1, sort_keys=False).encode()


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    return json.loads(raw) if raw is not None else None


def _layer_run_ids(store, layer: str) -> list[str]:
    """Existing run ids for this layer, sorted ascending by cycle. The regex
    keeps `weather-…` from matching `weather-ecmwf-…` runs."""
    pattern = re.compile(rf"^forecast-runs/({re.escape(layer)}-{_RUN_ID_RE_TAIL})/")
    runs = {m.group(1) for k in store.list_keys("forecast-runs/") if (m := pattern.match(k))}
    return sorted(runs)


# ------------------------------------------------------------ publish steps


def check_storage_guard(
    store, layer: str, new_run_bytes: int, max_bucket_bytes: int
) -> dict[str, int]:
    """Sum manifest totals of the runs that will be retained after this publish
    plus the new run's bytes; abort before uploading anything if over budget."""
    latest = _get_json(store, "latest.json") or {"layers": {}}
    retained: set[str] = set()
    for lyr, entry in latest.get("layers", {}).items():
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
    return {
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
            "tile_deg": 10,
            "path_template": f"{cube.layer}/{z_res(cube.resolution_deg)}/{{tile_id}}.bin.gz",
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


def _update_latest(store, cube: ForecastCube, published_at: str) -> str | None:
    latest = _get_json(store, "latest.json") or {"schema_version": 1, "layers": {}}
    previous = latest["layers"].get(cube.layer, {}).get("run_id")
    if previous == cube.run_id:  # re-publish of the same cycle: keep older previous
        previous = latest["layers"][cube.layer].get("previous_run_id")
    latest["schema_version"] = latest.get("schema_version", 1)
    latest["updated_at"] = published_at
    latest["layers"][cube.layer] = {
        "run_id": cube.run_id,
        "previous_run_id": previous,
        "cycle": cube.cycle_iso,
        "member_count": cube.member_count,
        "published_at": published_at,
    }
    store.put(
        "latest.json",
        _json_bytes(latest),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_MUTABLE,
    )
    return previous


def _delete_old_runs(store, layer: str, keep: set[str]) -> list[str]:
    deleted = []
    for run_id in _layer_run_ids(store, layer):
        if run_id in keep:
            continue
        for key in store.list_keys(f"forecast-runs/{run_id}/"):
            store.delete(key)
        deleted.append(run_id)
    return deleted


# ------------------------------------------------------------------- entry


@dataclass
class PublishResult:
    run_id: str
    tile_count: int
    bytes: int
    duration_s: float
    previous_run_id: str | None
    deleted_runs: list[str]


def publish_run(
    store,
    cube: ForecastCube,
    tiles: list[tuple[str, bytes]],
    report: ValidationReport,
    *,
    max_bucket_bytes: int = DEFAULT_MAX_BUCKET_BYTES,
    rng: random.Random | None = None,
    started_at: float | None = None,
) -> PublishResult:
    """Run the full atomic publish protocol for an already-validated cube."""
    if not report.ok:
        raise PublishError(
            f"refusing to publish a cube that failed validation:\n{report.summary()}"
        )
    if not tiles:
        raise PublishError("refusing to publish a run with zero tiles")
    t0 = started_at if started_at is not None else time.time()
    rng = rng or random.Random()
    run_id = cube.run_id
    published_at = utcnow_iso()
    manifest = build_manifest(
        cube, tiles, report, published_at=published_at, validated_at=published_at
    )

    # 1. storage guard — before any upload
    check_storage_guard(store, cube.layer, manifest["totals"]["bytes"], max_bucket_bytes)

    # 2. tiles under the immutable run id
    template = manifest["tiling"]["path_template"]
    for tid, gz in tiles:
        store.put(
            f"forecast-runs/{run_id}/" + template.format(tile_id=tid),
            gz,
            content_type=OCTET_STREAM,  # stored gzipped; client decompresses explicitly
            cache_control=CACHE_IMMUTABLE,
        )

    # 3. manifest LAST — its presence marks the run complete
    store.put(
        f"forecast-runs/{run_id}/manifest.json",
        _json_bytes(manifest),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_IMMUTABLE,
    )

    # 4. post-publish check
    _post_publish_check(store, run_id, manifest, rng)

    # 5. latest.json read-modify-write
    previous = _update_latest(store, cube, published_at)

    # 6. retention: delete runs older than the new previous for this layer
    keep = {run_id} | ({previous} if previous else set())
    deleted = _delete_old_runs(store, cube.layer, keep)

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
        _json_bytes(status),
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
    )
