"""Publish one chart build: cells, their depth companions and the world overview.

The protocol is the land index's (`ingest.land.publish`), for the same reasons:
guard the bucket before uploading anything, write archives under a build id
nothing will ever overwrite, write the manifest **last** so its presence marks
the build complete, prove the upload by reading it back, and only then move
the pointer by compare-and-swap, never backwards.

Charts have their own pointer and prefix, `charts/latest.json` and
`charts/{build_id}/`, so no forecast or land reader ever sees them. Two things
are their own:

- **Reuse.** An archive whose `inputs_sha256` (everything its build read, see
  `build.inputs_digest`) equals the current build's entry for it is not
  uploaded again: the new manifest points at the object already there, under
  the build that wrote it. Cells a run did not build are carried forward the
  same way, so a run over changed cells only keeps the rest.
- **Retention** keeps every object the new and the previous manifests name.
  An older build's other objects are deleted; its manifest goes last, once
  nothing kept still points into it.

Every archive's bytes are listed as one FNV-1a 64 digest per 1 MiB chunk, the
digest PFT1 and TLI1 use per object, so a client can range-read part of an
archive and still prove each chunk it keeps. Charts are display only: routing
never reads them, and no polar data is ever written under `charts/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ingest.publish import (
    APPLICATION_JSON,
    CACHE_IMMUTABLE,
    CACHE_MUTABLE,
    OCTET_STREAM,
    PointerConflictError,
    PreconditionFailed,
    PublishError,
    StalePublishError,
    StorageGuardError,
    UncertainWriteError,
    fnv64,
    json_bytes,
    put_immutable,
)

PREFIX = "charts"
LATEST_KEY = f"{PREFIX}/latest.json"
STATUS_KEY = "status/charts.json"
SCHEMA_VERSION = 1
SPEC = "TCP1"
CHUNK_BYTES = 1 << 20
BUILD_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{4}Z$")
WORLD = "world"


@dataclass
class ChartsPublishResult:
    build_id: str
    uploaded: list[str]
    reused: list[str]
    bytes_uploaded: int
    bytes_referenced: int
    duration_s: float
    previous_build_id: str | None
    deleted_keys: list[str]


def chunk_digests(data: bytes, chunk: int = CHUNK_BYTES) -> list[str]:
    """FNV-1a 64 of each `chunk` bytes of an archive, the last chunk short."""
    return [fnv64(data[i : i + chunk]) for i in range(0, len(data), chunk)] or [fnv64(b"")]


def _get_json(store, key: str) -> dict | None:
    raw = store.get(key)
    return None if raw is None else json.loads(raw)


def _object(key: str, data: bytes) -> dict:
    return {
        "key": key,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "chunks": chunk_digests(data),
    }


def _metadata(path: Path) -> dict:
    from pmtiles.reader import MmapSource, Reader

    with open(path, "rb") as f:
        return Reader(MmapSource(f)).metadata()


def _revisions(md: dict) -> dict:
    """The source revisions a crew or a refresh offer reads, by source id."""
    out = {}
    for s in md.get("sources", []):
        sid = s.get("id")
        if sid == "protomaps":
            out[sid] = {
                "build": s.get("build"),
                "osm_replication_time": s.get("osm_replication_time"),
            }
        elif sid == "seamap":
            out[sid] = {
                "date": s.get("date"),
                "osm_replication_time": s.get("osm_replication_time"),
            }
        elif sid == "shom":
            out[sid] = {
                "fetched": s.get("fetched"),
                "content_sha256": s.get("content_sha256"),
                "marks": s.get("marks"),
            }
        elif sid == "emodnet":
            out[sid] = {"product": s.get("product")}
    return out


@dataclass
class Built:
    """One archive the build wrote (a cell with its depth companion, or the world)."""

    name: str  # the cell id, or "world"
    path: Path
    depth_path: Path | None
    metadata: dict


def collect(build_dir: Path, build_id: str) -> dict[str, Built]:
    """The archives a build left in build_dir, each checked as a reader checks
    it, all of this build id, a cell's depth companion beside it."""
    from ingest.charts.check import check, style_fields

    build_dir = Path(build_dir)
    fields = style_fields()
    found: dict[str, Built] = {}
    errors = []
    for path in sorted(build_dir.glob("*.pmtiles")):
        if path.name.endswith("-depth.pmtiles"):
            continue
        md = _metadata(path)
        errors += check(path, fields, quiet=True)
        kind = md.get("kind")
        name = WORLD if kind == "world" else (md.get("cell") or {}).get("id")
        if kind not in ("world", "cell") or path.stem != name:
            errors.append(f"{path.name}: a build publishes cells and the world, not {kind!r}")
            continue
        if md.get("build_id") != build_id:
            errors.append(f"{path.name}: build {md.get('build_id')}, publishing {build_id}")
        if not md.get("inputs_sha256"):
            errors.append(f"{path.name}: no inputs_sha256")
        depth_path = None
        if kind == "cell":
            depth_path = path.with_name(md["depth"]["archive"])
            if not depth_path.is_file():
                errors.append(f"{path.name}: its depth companion {depth_path.name} is missing")
            else:
                errors += check(depth_path, fields, quiet=True)
                dmd = _metadata(depth_path)
                if (
                    dmd.get("kind") != "depth"
                    or dmd.get("build_id") != md.get("build_id")
                    or dmd.get("cell") != md.get("cell")
                    or dmd.get("inputs_sha256") != md.get("inputs_sha256")
                ):
                    errors.append(f"{depth_path.name}: not {path.name}'s depth companion")
        found[name] = Built(name, path, depth_path, md)
    if errors:
        raise PublishError("chart build refused:\n  " + "\n  ".join(errors))
    if not found:
        raise PublishError(f"no chart archives in {build_dir}")
    return found


def _entry(built: Built, build_id: str) -> tuple[dict, list[tuple[str, Path]]]:
    """A manifest entry for a newly uploaded archive, and its uploads."""
    md = built.metadata
    key = f"{PREFIX}/{build_id}/{built.path.name}"
    data = built.path.read_bytes()
    entry = {
        "build_id": build_id,
        "inputs_sha256": md["inputs_sha256"],
        "bounds": md["bounds"],
        "minzoom": md["minzoom"],
        "maxzoom": md["maxzoom"],
        "sources": _revisions(md),
        **_object(key, data),
    }
    uploads = [(key, built.path)]
    if built.depth_path is not None:
        c = md["cell"]
        entry = {"z": c["z"], "x": c["x"], "y": c["y"], **entry}
        dkey = f"{PREFIX}/{build_id}/{built.depth_path.name}"
        entry["depth"] = _object(dkey, built.depth_path.read_bytes())
        uploads.append((dkey, built.depth_path))
    return entry, uploads


def plan_build(built: dict[str, Built], build_id: str, previous: dict | None):
    """The new manifest's entries: each built archive uploaded, or the
    previous entry kept where its inputs are unchanged; previous cells this
    run did not build carried forward. Returns (world, cells, uploads, reused)."""
    prev_cells = (previous or {}).get("cells", {})
    prev_world = (previous or {}).get("world")
    cells: dict[str, dict] = {}
    uploads: list[tuple[str, Path]] = []
    reused: list[str] = []
    world = None
    for name, b in sorted(built.items()):
        old = prev_world if name == WORLD else prev_cells.get(name)
        if old is not None and old.get("inputs_sha256") == b.metadata["inputs_sha256"]:
            entry = old
            reused.append(name)
        else:
            entry, up = _entry(b, build_id)
            uploads += up
        if name == WORLD:
            world = entry
        else:
            cells[name] = entry
    for name, old in prev_cells.items():
        if name not in cells:
            cells[name] = old
            reused.append(name)
    if world is None:
        world = prev_world
        if world is not None:
            reused.append(WORLD)
    if world is None:
        raise PublishError("a chart build needs the world overview: none built, none published")
    return world, dict(sorted(cells.items())), uploads, reused


def referenced_keys(manifest: dict | None) -> set[str]:
    if not manifest:
        return set()
    keys = {manifest["world"]["key"]}
    for c in manifest["cells"].values():
        keys.add(c["key"])
        if c.get("depth"):
            keys.add(c["depth"]["key"])
    return keys


def _entry_bytes(entry: dict) -> int:
    return entry["bytes"] + (entry["depth"]["bytes"] if entry.get("depth") else 0)


def build_manifest(
    *, build_id: str, previous_build_id, world: dict, cells: dict, published_at: str, checks
) -> dict:
    referenced = {}
    for e in [world, *cells.values()]:
        referenced[e["key"]] = e["bytes"]
        if e.get("depth"):
            referenced[e["depth"]["key"]] = e["depth"]["bytes"]
    new = sum(b for k, b in referenced.items() if k.startswith(f"{PREFIX}/{build_id}/"))
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": SPEC,
        "build_id": build_id,
        "previous_build_id": previous_build_id,
        "purpose": (
            "a picture of the water for the crew: display only. Routing never reads it; "
            "land avoidance is the TLI1 routing index"
        ),
        "not_for_navigation": (
            "Not for navigation. Built from open data (OpenStreetMap, Open Waters Seamap, "
            "SHOM Balisage, EMODnet Bathymetry), none of which is an official chart; "
            "EMODnet: DO NOT USE FOR NAVIGATION."
        ),
        "format": "docs/chart-pack-format.md",
        "chunk_bytes": CHUNK_BYTES,
        "cell_zoom": 6,
        "world": world,
        "cells": cells,
        "totals": {
            "cell_count": len(cells),
            "objects": len(referenced),
            "bytes": sum(referenced.values()),
            "bytes_this_build": new,
        },
        "validation": {"checks_passed": list(checks)},
        "published_at": published_at,
    }


def charts_build_ids(store) -> list[str]:
    found = set()
    for key in store.list_keys(f"{PREFIX}/"):
        rest = key[len(PREFIX) + 1 :]
        if "/" in rest:
            found.add(rest.split("/", 1)[0])
    return sorted(found)


def check_storage_guard(store, charts_bytes: int, max_bucket_bytes: int):
    """Forecast runs, land indexes and charts share one bucket and one budget:
    the retained forecast and land bytes (`ingest.land.publish`'s guard) plus
    the chart bytes kept after this publish must stay under MAX_BUCKET_BYTES."""
    from ingest.land.publish import check_storage_guard as land_guard

    return land_guard(store, charts_bytes, max_bucket_bytes)


def check_regional_room(store, new_bytes: int, limit: int, env=os.environ) -> dict | None:
    """Charts must not starve the regional forecasts. Their capacity check
    reserves the measured root peak, three capped runs per enabled regional
    model and headroom, and counts every other object in the bucket, charts
    included, against the same limit (`ingest.capacity.check_regional_capacity`).
    Refuse a chart upload that would leave that reservation over the limit.
    Without the regional settings this cannot be known, so R2 publication
    refuses; scratch stores skip it."""
    from ingest.audit import audit_runs
    from ingest.sources.openmeteo import registry

    try:
        peak = int(env["REGIONAL_EXISTING_PEAK_BYTES"])
        headroom = int(env["REGIONAL_HEADROOM_BYTES"])
        layers = [x for x in env["REGIONAL_ENABLED_LAYERS"].split(",") if x]
    except (KeyError, ValueError) as exc:
        raise StorageGuardError(
            "charts: regional capacity unknown; configure REGIONAL_EXISTING_PEAK_BYTES, "
            "REGIONAL_HEADROOM_BYTES and REGIONAL_ENABLED_LAYERS as the forecasts do"
        ) from exc
    caps = sum(registry.product(layer).max_run_bytes for layer in layers)
    report = audit_runs(store)
    if report.damaged:
        raise StorageGuardError("charts: referenced forecast runs are damaged; reconcile first")
    physical = sum(obj["bytes"] for obj in store.list_objects(""))
    referenced = sum(run.bytes for run in report.runs.values() if run.referenced_as)
    other = max(0, physical - referenced)
    reserved = peak + 3 * caps + other + new_bytes + headroom
    if reserved > limit:
        raise StorageGuardError(
            f"charts: the regional forecasts' reserved peak would be {reserved} B with this "
            f"build's {new_bytes} B over {limit} B; raise MAX_BUCKET_BYTES or publish fewer cells"
        )
    return {"physical_bytes": physical, "other_bytes": other, "reserved_peak_bytes": reserved}


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
) -> ChartsPublishResult:
    from ingest.publish import DirStore

    if not BUILD_ID_RE.match(build_id):
        raise PublishError(f"build id {build_id!r} is not UTC YYYYMMDDTHHMMZ")
    t0 = time.time()
    rng = rng or random.Random()
    published_at = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    built = collect(build_dir, build_id)

    current = _get_json(store, LATEST_KEY)
    current_id = (current or {}).get("build_id")
    if current_id and current_id > build_id:
        raise StalePublishError(f"charts pointer already names a newer build {current_id}")
    # the build this one follows: the pointer's, or on a retry after the
    # pointer moved, the one before it
    base_id = current_id if current_id != build_id else (current or {}).get("previous_build_id")
    previous = None
    if base_id:
        previous = _get_json(store, f"{PREFIX}/{base_id}/manifest.json")
        if previous is None:
            raise PublishError(f"charts pointer names {base_id}, whose manifest is missing")
    world, cells, uploads, reused = plan_build(built, build_id, previous)
    checks = [
        f"{len(built)} archives built as build {build_id} pass the TCP1 reader checks",
        "every cell stores and covers every tile of its z6 cell at z6-14",
        "each cell's depth companion has its build id, cell and inputs",
    ]
    manifest = build_manifest(
        build_id=build_id,
        previous_build_id=base_id,
        world=world,
        cells=cells,
        published_at=published_at,
        checks=checks,
    )
    existing = _get_json(store, f"{PREFIX}/{build_id}/manifest.json")
    if existing is not None:
        same = {k: v for k, v in existing.items() if k != "published_at"} == {
            k: v for k, v in manifest.items() if k != "published_at"
        }
        if not same:
            raise PublishError(
                f"{PREFIX}/{build_id}/ already exists with different content: a published "
                "build is immutable; build a new one instead"
            )
        manifest = existing
        published_at = existing["published_at"]

    # 1. guards, before any upload
    upload_bytes = sum(p.stat().st_size for _, p in uploads)
    # what charts/ keeps after this publish: the new and previous builds' objects, each once
    sizes = {}
    for m in (manifest, previous):
        if not m:
            continue
        for e in [m["world"], *m["cells"].values()]:
            sizes[e["key"]] = e["bytes"]
            if e.get("depth"):
                sizes[e["depth"]["key"]] = e["depth"]["bytes"]
    retained = sum(sizes.values())
    check_storage_guard(store, retained, max_bucket_bytes)
    if regional_check is None:
        regional_check = not isinstance(store, DirStore)
    if regional_check:
        room = check_regional_room(store, upload_bytes, max_bucket_bytes)
        log(f"charts: regional room ok: {room}")

    from ingest.capacity import reserved_publication
    from ingest.storage_admission import MUTABLE_UPLOAD_BYTES

    reservation_bytes = upload_bytes + len(json_bytes(manifest)) + MUTABLE_UPLOAD_BYTES
    with reserved_publication(
        store,
        "charts",
        build_id,
        reservation_bytes,
        prefix=f"{PREFIX}/{build_id}/",
        mutable_keys=(LATEST_KEY, STATUS_KEY),
        admission=capacity_admission,
    ) as store:
        # 2. archives under the immutable build id
        for key, path in uploads:
            put_immutable(
                store,
                key,
                path.read_bytes(),
                content_type=OCTET_STREAM,
                cache_control=CACHE_IMMUTABLE,
            )
            log(f"charts: uploaded {key} ({path.stat().st_size / 1e6:.2f} MB)")

        # 3. manifest LAST: its presence marks the build complete
        put_immutable(
            store,
            f"{PREFIX}/{build_id}/manifest.json",
            json_bytes(manifest),
            content_type=APPLICATION_JSON,
            cache_control=CACHE_IMMUTABLE,
        )

        # 4. post-publish check
        _post_publish_check(store, manifest, [k for k, _ in uploads], rng)

        # 5. the pointer
        prior = _update_latest(store, manifest, published_at)

        # 6. retention: what the new and the previous manifests name
        keep_manifest = _get_json(store, f"{PREFIX}/{prior}/manifest.json") if prior else None
        deleted = _retain(store, build_id, prior, manifest, keep_manifest)

        duration_s = round(time.time() - t0, 1)
        store.put(
            STATUS_KEY,
            json_bytes(
                {
                    "build_id": build_id,
                    "previous_build_id": prior,
                    "published_at": published_at,
                    "cells": len(cells),
                    "uploaded": [k for k, _ in uploads],
                    "reused": reused,
                    "bytes": manifest["totals"]["bytes"],
                    "duration_s": duration_s,
                    "checks_passed": manifest["validation"]["checks_passed"],
                }
            ),
            content_type=APPLICATION_JSON,
            cache_control=CACHE_MUTABLE,
        )
    return ChartsPublishResult(
        build_id=build_id,
        uploaded=[k for k, _ in uploads],
        reused=reused,
        bytes_uploaded=upload_bytes,
        bytes_referenced=manifest["totals"]["bytes"],
        duration_s=duration_s,
        previous_build_id=prior,
        deleted_keys=deleted,
    )


def _post_publish_check(store, manifest: dict, uploaded: list[str], rng: random.Random) -> None:
    build_id = manifest["build_id"]
    key = f"{PREFIX}/{build_id}/manifest.json"
    if _get_json(store, key) != manifest:
        raise PublishError(f"post-publish check: {key} does not round-trip")
    entries = {}
    for e in [manifest["world"], *manifest["cells"].values()]:
        entries[e["key"]] = e
        if e.get("depth"):
            entries[e["depth"]["key"]] = e["depth"]
    listed = {o["key"]: o["bytes"] for o in store.list_objects(f"{PREFIX}/")}
    for k, e in entries.items():
        if listed.get(k) != e["bytes"]:
            raise PublishError(
                f"post-publish check: {k} is {listed.get(k)} B, manifest says {e['bytes']}"
            )
    for k in rng.sample(sorted(uploaded), min(2, len(uploaded))):
        data = store.get(k)
        e = entries[k]
        if data is None or len(data) != e["bytes"] or chunk_digests(data) != e["chunks"]:
            raise PublishError(f"post-publish check: {k} bytes/chunk digests mismatch")


def latest_document(manifest: dict, previous: str | None, published_at: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": SPEC,
        "build_id": manifest["build_id"],
        "previous_build_id": previous,
        "updated_at": published_at,
        "manifest": f"{PREFIX}/{manifest['build_id']}/manifest.json",
        "chunk_bytes": manifest["chunk_bytes"],
        "cell_zoom": manifest["cell_zoom"],
        "world": manifest["world"],
        "cells": manifest["cells"],
    }


def _update_latest(store, manifest: dict, published_at: str) -> str | None:
    target = manifest["build_id"]
    for _ in range(6):
        raw, etag = store.get_with_etag(LATEST_KEY)
        latest = json.loads(raw) if raw is not None else {}
        current = latest.get("build_id")
        if current == target:
            return latest.get("previous_build_id")
        if current and current > target:
            raise StalePublishError("newer chart build committed during upload; nothing pruned")
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
            continue  # re-read before merging or any retention
    settled = _get_json(store, LATEST_KEY) or {}
    if settled.get("build_id") == target:
        return settled.get("previous_build_id")
    raise PointerConflictError("charts pointer outcome unresolved; nothing pruned")


def _retain(store, build_id: str, prior: str | None, manifest: dict, prior_manifest) -> list[str]:
    """Delete what neither the new nor the previous manifest names, in builds
    older than the previous one. A build with no manifest may still be
    uploading (or is an interrupted upload, counted until reconciled) and is
    left alone; so is anything newer than the previous build."""
    keep = referenced_keys(manifest) | referenced_keys(prior_manifest)
    protected = {build_id} | ({prior} if prior else set())
    live = _get_json(store, LATEST_KEY) or {}
    protected |= {live.get("build_id"), live.get("previous_build_id")} - {None}
    oldest_kept = min(protected)
    deleted = []
    for stale in charts_build_ids(store):
        if stale in protected or stale >= oldest_kept:
            continue
        keys = store.list_keys(f"{PREFIX}/{stale}/")
        manifest_key = f"{PREFIX}/{stale}/manifest.json"
        if manifest_key not in keys:
            continue
        for key in sorted(keys):
            if key != manifest_key and key not in keep:
                store.delete(key)
                deleted.append(key)
        if not any(k in keep for k in keys):
            store.delete(manifest_key)
            deleted.append(manifest_key)
    return deleted
