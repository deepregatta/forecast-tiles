"""Publish one tidal-atlas build: its TCA1 grids, a manifest and the pointer.

The chart pack's protocol (`ingest.charts.publish`), smaller: guard the
bucket before uploading anything, write grids under a build id nothing will
overwrite, write the manifest **last** so its presence marks the build
complete, read the upload back, and only then move `tidal-atlas/latest.json`
by compare-and-swap, never backwards. Retention keeps the new and the
previous builds; older builds are deleted once the pointer has moved.

**Published from 2026-11-15** on the owner's approval. Staged first
(`PUBLICATION_ENABLED` False: `publish_build` refused any store but a
scratch directory); set it False to stage again. Display only: routing never reads the atlas, and no polar data
is ever written under `tidal-atlas/`.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ingest.atlas import codec
from ingest.atlas.build import bounds
from ingest.atlas.sources import (
    ATTRIBUTION,
    BY_ID,
    GRIDS,
    LICENCE,
    LICENCE_URL,
    LIMITATION,
)
from ingest.publish import (
    APPLICATION_JSON,
    CACHE_IMMUTABLE,
    CACHE_MUTABLE,
    OCTET_STREAM,
    DirStore,
    PointerConflictError,
    PreconditionFailed,
    PublishError,
    StalePublishError,
    UncertainWriteError,
    json_bytes,
    put_immutable,
)

# The owner approved publication on 2026-11-15. Set False to stage again:
# publish_build then refuses every store but a scratch directory.
PUBLICATION_ENABLED = True

PREFIX = "tidal-atlas"
LATEST_KEY = f"{PREFIX}/latest.json"
STATUS_KEY = "status/tidal-atlas.json"
BUILD_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{4}Z$")


@dataclass
class AtlasPublishResult:
    build_id: str
    uploaded: list[str]
    bytes_uploaded: int
    previous_build_id: str | None
    deleted_keys: list[str]


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    return json.loads(raw) if raw is not None else None


def collect(build_dir: Path) -> dict[str, tuple[dict, bytes]]:
    """Every grid in the build directory, decoded and checked, by id. A file
    that does not decode, or is not a grid the pack carries, refuses the
    build."""
    found = {}
    for path in sorted(build_dir.glob("*.tca1.gz")):
        data = path.read_bytes()
        header, _ = codec.decode(data)
        grid = path.name.removesuffix(".tca1.gz")
        if header["grid"] != grid or grid not in BY_ID:
            raise PublishError(f"{path.name} is not a grid this pack carries")
        found[grid] = (header, data)
    if not found:
        raise PublishError(f"no TCA1 grid in {build_dir}")
    return found


def build_manifest(built: dict, build_id: str, previous: str | None, published_at: str) -> dict:
    grids = []
    for rank, spec in enumerate(g for g in GRIDS if g.id in built):
        header, data = built[spec.id]
        grids.append(
            {
                "id": spec.id,
                "zone": header["zone"],
                "title": header["title"],
                "rank": rank,
                "key": f"{PREFIX}/{build_id}/{spec.id}.tca1.gz",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bounds": bounds(header),
                "resolution_m": header["resolution_m"],
                "reference_port": header["reference_port"],
                "offsets_min": header["offsets_min"],
                "source": header["source"],
            }
        )
    return {
        "spec": codec.SPEC,
        "schema_version": codec.SCHEMA_VERSION,
        "build_id": build_id,
        "previous_build_id": previous,
        "published_at": published_at,
        "attribution": ATTRIBUTION,
        "limitation": LIMITATION,
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
        "not_for_navigation": True,
        "grids": grids,
        "totals": {"grids": len(grids), "bytes": sum(g["bytes"] for g in grids)},
        "validation": {
            "checks_passed": [
                f"{len(grids)} grids decode with the TCA1 reader",
                "each grid's source file is the one pinned for it",
            ]
        },
    }


def latest_document(manifest: dict, previous: str | None, published_at: str) -> dict:
    return {
        "spec": codec.SPEC,
        "schema_version": codec.SCHEMA_VERSION,
        "build_id": manifest["build_id"],
        "previous_build_id": previous,
        "updated_at": published_at,
        "manifest": f"{PREFIX}/{manifest['build_id']}/manifest.json",
        "attribution": manifest["attribution"],
        "limitation": manifest["limitation"],
        "grids": manifest["grids"],
        "totals": manifest["totals"],
    }


def atlas_build_ids(store) -> list[str]:
    found = set()
    for key in store.list_keys(f"{PREFIX}/"):
        rest = key[len(PREFIX) + 1 :]
        if "/" in rest and BUILD_ID_RE.match(rest.split("/", 1)[0]):
            found.add(rest.split("/", 1)[0])
    return sorted(found)


def publish_build(
    store,
    build_dir: Path,
    build_id: str,
    *,
    max_bucket_bytes: int,
    rng: random.Random | None = None,
    now: str | None = None,
    capacity_admission=None,
    regional_check: bool | None = None,
    log=print,
) -> AtlasPublishResult:
    if not PUBLICATION_ENABLED and not isinstance(store, DirStore):
        raise PublishError("tidal-atlas is staged: publication is switched off; use --dry-run")
    if not BUILD_ID_RE.match(build_id):
        raise PublishError(f"build id {build_id!r} is not UTC YYYYMMDDTHHMMZ")
    rng = rng or random.Random()
    published_at = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    built = collect(build_dir)
    current = _get_json(store, LATEST_KEY)
    current_id = (current or {}).get("build_id")
    if current_id and current_id > build_id:
        raise StalePublishError(f"tidal-atlas pointer already names a newer build {current_id}")
    base_id = current_id if current_id != build_id else (current or {}).get("previous_build_id")
    manifest = build_manifest(built, build_id, base_id, published_at)
    existing = _get_json(store, f"{PREFIX}/{build_id}/manifest.json")
    if existing is not None:
        if {k: v for k, v in existing.items() if k != "published_at"} != {
            k: v for k, v in manifest.items() if k != "published_at"
        }:
            raise PublishError(
                f"{PREFIX}/{build_id}/ already exists with different content: a published "
                "build is immutable; build a new one instead"
            )
        manifest, published_at = existing, existing["published_at"]
    uploads = [(g["key"], built[g["id"]][1]) for g in manifest["grids"]]
    upload_bytes = sum(len(d) for _, d in uploads)
    previous = _get_json(store, f"{PREFIX}/{base_id}/manifest.json") if base_id else None
    retained = upload_bytes + ((previous or {}).get("totals", {}).get("bytes", 0))

    # 1. guards, before any upload: the forecast and land bytes kept plus the
    # atlas kept after this publish, and the regional forecasts' reserved peak
    from ingest.charts.publish import check_regional_room, check_storage_guard

    check_storage_guard(store, retained, max_bucket_bytes)
    if regional_check is None:
        regional_check = not isinstance(store, DirStore)
    if regional_check:
        log(
            f"tidal-atlas: regional room ok: {check_regional_room(store, upload_bytes, max_bucket_bytes)}"
        )

    from ingest.capacity import reserved_publication
    from ingest.storage_admission import MUTABLE_UPLOAD_BYTES

    reservation = upload_bytes + len(json_bytes(manifest)) + MUTABLE_UPLOAD_BYTES
    with reserved_publication(
        store,
        "tidal-atlas",
        build_id,
        reservation,
        prefix=f"{PREFIX}/{build_id}/",
        mutable_keys=(LATEST_KEY, STATUS_KEY),
        admission=capacity_admission,
    ) as store:
        # 2. grids under the immutable build id
        for key, data in uploads:
            put_immutable(
                store, key, data, content_type=OCTET_STREAM, cache_control=CACHE_IMMUTABLE
            )
            log(f"tidal-atlas: uploaded {key} ({len(data) / 1e6:.2f} MB)")
        # 3. manifest LAST
        put_immutable(
            store,
            f"{PREFIX}/{build_id}/manifest.json",
            json_bytes(manifest),
            content_type=APPLICATION_JSON,
            cache_control=CACHE_IMMUTABLE,
        )
        # 4. read back
        _post_publish_check(store, manifest, rng)
        # 5. the pointer
        prior = _update_latest(store, manifest, published_at)
        # 6. retention
        deleted = _retain(store, build_id, prior)
        store.put(
            STATUS_KEY,
            json_bytes(
                {
                    "build_id": build_id,
                    "previous_build_id": prior,
                    "published_at": published_at,
                    "grids": [g["id"] for g in manifest["grids"]],
                    "bytes": manifest["totals"]["bytes"],
                }
            ),
            content_type=APPLICATION_JSON,
            cache_control=CACHE_MUTABLE,
        )
    return AtlasPublishResult(
        build_id=build_id,
        uploaded=[k for k, _ in uploads],
        bytes_uploaded=upload_bytes,
        previous_build_id=prior,
        deleted_keys=deleted,
    )


def _post_publish_check(store, manifest: dict, rng: random.Random) -> None:
    key = f"{PREFIX}/{manifest['build_id']}/manifest.json"
    if _get_json(store, key) != manifest:
        raise PublishError(f"post-publish check: {key} does not round-trip")
    listed = {o["key"]: o["bytes"] for o in store.list_objects(f"{PREFIX}/")}
    for grid in manifest["grids"]:
        if listed.get(grid["key"]) != grid["bytes"]:
            raise PublishError(f"post-publish check: {grid['key']} size differs")
    for grid in rng.sample(manifest["grids"], min(2, len(manifest["grids"]))):
        data = store.get(grid["key"])
        if data is None or hashlib.sha256(data).hexdigest() != grid["sha256"]:
            raise PublishError(f"post-publish check: {grid['key']} digest differs")
        codec.decode(data)


def _update_latest(store, manifest: dict, published_at: str) -> str | None:
    target = manifest["build_id"]
    for _ in range(6):
        raw, etag = store.get_with_etag(LATEST_KEY)
        latest = json.loads(raw) if raw is not None else {}
        current = latest.get("build_id")
        if current == target:
            return latest.get("previous_build_id")
        if current and current > target:
            raise StalePublishError("newer atlas build committed during upload; nothing pruned")
        try:
            store.put(
                LATEST_KEY,
                json_bytes(latest_document(manifest, current, published_at)),
                content_type=APPLICATION_JSON,
                cache_control=CACHE_MUTABLE,
                if_match=etag if raw is not None else None,
                if_none_match=raw is None,
            )
            return current
        except (PreconditionFailed, UncertainWriteError):
            continue
    settled = _get_json(store, LATEST_KEY) or {}
    if settled.get("build_id") == target:
        return settled.get("previous_build_id")
    raise PointerConflictError("tidal-atlas pointer outcome unresolved; nothing pruned")


def _retain(store, build_id: str, prior: str | None) -> list[str]:
    """Delete complete builds older than the previous one; a build with no
    manifest may still be uploading and is left alone."""
    live = _get_json(store, LATEST_KEY) or {}
    protected = {build_id, prior, live.get("build_id"), live.get("previous_build_id")} - {None}
    oldest_kept = min(protected)
    deleted = []
    for stale in atlas_build_ids(store):
        if stale in protected or stale >= oldest_kept:
            continue
        keys = store.list_keys(f"{PREFIX}/{stale}/")
        manifest_key = f"{PREFIX}/{stale}/manifest.json"
        if manifest_key not in keys:
            continue
        for key in sorted(k for k in keys if k != manifest_key):
            store.delete(key)
            deleted.append(key)
        store.delete(manifest_key)
        deleted.append(manifest_key)
    return deleted
