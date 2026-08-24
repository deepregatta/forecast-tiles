"""Publish one compiled routing index, immutably.

The protocol is the forecast pipeline's, with the same order and for the same
reasons: guard the bucket before uploading anything, write the tiles under a
version nothing will ever overwrite, write the manifest **last** so its
presence is what marks the version complete, prove the upload by reading it
back, and only then move the pointer.

`land-index/latest.json` is a pointer of its own rather than an entry in the
forecast `latest.json`. The forecast document is a contract Passage reads and
tactician's `shore` reads; a land index dropped into it would be a layer both
of them would try to fetch forecast tiles for.

The pointer is keyed by **domain**, the way the forecast pointer is keyed by
layer. Domains are compiled and published independently — a Mediterranean index
must not retire a north-west European one — so retention only ever considers
indexes of the domain being published.
"""

from __future__ import annotations

import gzip
import random
import time
from dataclasses import dataclass

from ingest.publish import (
    APPLICATION_JSON,
    CACHE_IMMUTABLE,
    CACHE_MUTABLE,
    OCTET_STREAM,
    PublishError,
    StorageGuardError,
    fnv64,
    json_bytes,
)
from landkit.codec import decode_tile

PREFIX = "land-index"
LATEST_KEY = f"{PREFIX}/latest.json"
STATUS_KEY = "status/land-index.json"
MANIFEST_SCHEMA_VERSION = 1
PATH_TEMPLATE = "{tile_id}.bin.gz"


@dataclass
class LandPublishResult:
    index_id: str
    tile_count: int
    bytes: int
    duration_s: float
    previous_index_id: str | None
    deleted_indexes: list[str]


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    if raw is None:
        return None
    import json

    return json.loads(raw)


def existing_index_ids(store, domain: str | None = None) -> list[str]:
    """Published index ids, optionally only those of one domain.

    An index id starts with its domain name and a hyphen, which is how a
    domain's own versions are told apart from every other domain's without
    reading a manifest for each.
    """
    prefix = f"{PREFIX}/"
    found = set()
    for key in store.list_keys(prefix):
        rest = key[len(prefix) :]
        if "/" in rest:
            found.add(rest.split("/", 1)[0])
    if domain is not None:
        found = {index_id for index_id in found if index_id.startswith(f"{domain}-")}
    return sorted(found)


def check_storage_guard(store, new_bytes: int, max_bucket_bytes: int) -> dict[str, int]:
    """Sum what the bucket already holds and refuse before uploading anything.

    Forecast runs and routing indexes share one bucket and one budget, so the
    guard counts both. An index is a few megabytes against a forecast run's
    hundreds, but a guard that only counted its own kind would be a guard that
    lets the bucket fill.
    """
    sizes: dict[str, int] = {}
    latest = _get_json(store, "latest.json") or {"layers": {}}
    retained = set()
    for entry in latest.get("layers", {}).values():
        retained.add(entry["run_id"])
        if entry.get("previous_run_id"):
            retained.add(entry["previous_run_id"])
    for run_id in sorted(retained):
        manifest = _get_json(store, f"forecast-runs/{run_id}/manifest.json")
        if manifest is not None:
            sizes[run_id] = int(manifest["totals"]["bytes"])
    for index_id in existing_index_ids(store):
        manifest = _get_json(store, f"{PREFIX}/{index_id}/manifest.json")
        if manifest is not None:
            sizes[index_id] = int(manifest["totals"]["bytes"])
    total = sum(sizes.values()) + new_bytes
    if total > max_bucket_bytes:
        raise StorageGuardError(
            f"storage guard: retained {sum(sizes.values())} B ({sizes}) + new index "
            f"{new_bytes} B = {total} B > {max_bucket_bytes} B — refusing to upload"
        )
    return sizes


def build_manifest(
    *,
    index_id: str,
    domain: str,
    domain_label: str,
    tiles: list[tuple[str, bytes]],
    coverage: dict,
    grid: dict,
    conservatism: dict,
    sources: list[dict],
    composition: dict,
    checks: list[str],
    published_at: str,
) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "spec": "TLI1",
        "index_id": index_id,
        "domain": domain,
        "domain_label": domain_label,
        "purpose": (
            "routing legality only: whether a route segment may cross a cell. Not a chart, "
            "not a navigation product, and not the display basemap"
        ),
        "not_for_navigation": (
            "DO NOT USE FOR NAVIGATION. Derived from public bathymetry and shoreline data "
            "that carry that constraint themselves; it resolves shoal areas, not individual "
            "rocks, and it is no substitute for an official chart"
        ),
        "coverage": coverage,
        "grid": grid,
        "conservatism": conservatism,
        "sources": sources,
        "composition": composition,
        "tiling": {"tile_deg": grid["tile_deg"], "path_template": PATH_TEMPLATE},
        "tiles": {tile_id: {"bytes": len(gz), "fnv64": fnv64(gz)} for tile_id, gz in tiles},
        "totals": {"tile_count": len(tiles), "bytes": sum(len(gz) for _, gz in tiles)},
        "validation": {"checks_passed": checks},
        "published_at": published_at,
    }


def publish_index(
    store,
    manifest: dict,
    tiles: list[tuple[str, bytes]],
    *,
    max_bucket_bytes: int,
    rng: random.Random | None = None,
    started_at: float | None = None,
    now: str | None = None,
) -> LandPublishResult:
    if not tiles:
        raise PublishError("refusing to publish an index with zero tiles")
    index_id = manifest["index_id"]
    t0 = started_at if started_at is not None else time.time()
    rng = rng or random.Random()
    published_at = now or manifest["published_at"]

    existing = _get_json(store, f"{PREFIX}/{index_id}/manifest.json")
    if existing is not None and existing != manifest:
        raise PublishError(
            f"{PREFIX}/{index_id}/ already exists with different content — a published index "
            "is immutable; build a new version instead"
        )

    # 1. storage guard — before any upload
    check_storage_guard(store, manifest["totals"]["bytes"], max_bucket_bytes)

    # 2. tiles under the immutable index id
    for tile_id, gz in tiles:
        store.put(
            f"{PREFIX}/{index_id}/" + PATH_TEMPLATE.format(tile_id=tile_id),
            gz,
            content_type=OCTET_STREAM,  # stored gzipped; the client decompresses explicitly
            cache_control=CACHE_IMMUTABLE,
        )

    # 3. manifest LAST — its presence marks the index complete
    store.put(
        f"{PREFIX}/{index_id}/manifest.json",
        json_bytes(manifest),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_IMMUTABLE,
    )

    # 4. post-publish check
    _post_publish_check(store, index_id, manifest, rng)

    # 5. the pointer
    previous = _update_latest(store, manifest, published_at)

    # 6. retention: the current index and the one before it
    keep = {index_id} | ({previous} if previous else set())
    deleted = []
    for stale in existing_index_ids(store, manifest["domain"]):
        if stale in keep:
            continue
        for key in store.list_keys(f"{PREFIX}/{stale}/"):
            store.delete(key)
        deleted.append(stale)

    duration_s = round(time.time() - t0, 1)
    store.put(
        STATUS_KEY,
        json_bytes(
            {
                "index_id": index_id,
                "domain": manifest["domain"],
                "published_at": published_at,
                "tile_count": manifest["totals"]["tile_count"],
                "bytes": manifest["totals"]["bytes"],
                "duration_s": duration_s,
                "checks_passed": manifest["validation"]["checks_passed"],
            }
        ),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_MUTABLE,
    )
    return LandPublishResult(
        index_id=index_id,
        tile_count=manifest["totals"]["tile_count"],
        bytes=manifest["totals"]["bytes"],
        duration_s=duration_s,
        previous_index_id=previous,
        deleted_indexes=deleted,
    )


def _post_publish_check(store, index_id: str, manifest: dict, rng: random.Random) -> None:
    key = f"{PREFIX}/{index_id}/manifest.json"
    if _get_json(store, key) != manifest:
        raise PublishError(f"post-publish check: {key} does not round-trip")
    tile_ids = sorted(manifest["tiles"])
    for tile_id in rng.sample(tile_ids, min(2, len(tile_ids))):
        tile_key = f"{PREFIX}/{index_id}/" + PATH_TEMPLATE.format(tile_id=tile_id)
        gz = store.get(tile_key)
        if gz is None:
            raise PublishError(f"post-publish check: {tile_key} missing")
        entry = manifest["tiles"][tile_id]
        if len(gz) != entry["bytes"] or fnv64(gz) != entry["fnv64"]:
            raise PublishError(f"post-publish check: {tile_key} bytes/fnv64 mismatch")
        decoded = decode_tile(gzip.decompress(gz))
        if decoded.header["tile_id"] != tile_id or decoded.header["index_id"] != index_id:
            raise PublishError(f"post-publish check: {tile_key} decodes to wrong tile/index")


def _update_latest(store, manifest: dict, published_at: str) -> str | None:
    domain = manifest["domain"]
    latest = _get_json(store, LATEST_KEY) or {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "spec": "TLI1",
        "domains": {},
    }
    latest.setdefault("domains", {})
    entry = latest["domains"].get(domain, {})
    previous = entry.get("index_id")
    if previous == manifest["index_id"]:
        previous = entry.get("previous_index_id")
    latest["updated_at"] = published_at
    latest["domains"][domain] = {
        "index_id": manifest["index_id"],
        "previous_index_id": previous,
        "label": manifest["domain_label"],
        "tiles": sorted(manifest["tiles"]),
        "tile_deg": manifest["tiling"]["tile_deg"],
        "published_at": published_at,
    }
    store.put(
        LATEST_KEY,
        json_bytes(latest),
        content_type=APPLICATION_JSON,
        cache_control=CACHE_MUTABLE,
    )
    return previous
