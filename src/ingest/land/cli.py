"""`ingest land` — compile and publish one conservative routing index."""

from __future__ import annotations

import argparse
from pathlib import Path

from ingest.land import gshhg
from ingest.land.build import DOMAINS, build_index
from ingest.land.compile import Conservatism
from ingest.land.publish import PREFIX, publish_index
from ingest.publish import (
    DirStore,
    PublishError,
    make_r2_store_from_env,
    max_bucket_bytes_from_env,
)


def run_land(args: argparse.Namespace) -> int:
    domain = DOMAINS.get(args.domain)
    if domain is None:
        print(f"ingest land: unknown domain {args.domain}; known: {', '.join(sorted(DOMAINS))}")
        return 1

    conservatism = Conservatism()
    if args.buffer_m is not None:
        conservatism.buffer_m = args.buffer_m
    if args.safety_contour_m is not None:
        conservatism.safety_contour_m = args.safety_contour_m

    cache_dir = Path(args.cache_dir)
    print(f"ingest land: domain {domain.name} — {domain.label}")
    print(
        f"ingest land: buffer {conservatism.buffer_m} m outward, safety contour "
        f"{conservatism.safety_contour_m} m below LAT, "
        f"{len(domain.tiles)} tiles of {domain.span_deg} degrees"
    )
    source = gshhg.ensure_source(cache_dir)

    try:
        built = build_index(
            domain,
            gshhg_path=source,
            cache_dir=cache_dir,
            conservatism=conservatism,
            progress=lambda note: print(f"ingest land: {note}"),
        )
    except ValueError as error:
        print(f"ingest land: compilation FAILED: {error}")
        return 1

    composition = built.manifest["composition"]
    print(
        f"ingest land: {built.manifest['index_id']} — "
        f"{built.manifest['totals']['tile_count']} tiles, "
        f"{built.manifest['totals']['bytes'] / 1e6:.2f} MB gz, "
        f"{composition['blocked_cells'] / composition['cells']:.3%} of cells blocked, "
        f"sources agree on {composition['source_agreement']:.3%}"
    )
    for check in built.manifest["validation"]["checks_passed"]:
        print(f"ingest land:   check passed — {check}")

    store = DirStore(args.dry_run) if args.dry_run else make_r2_store_from_env()
    try:
        result = publish_index(
            store,
            built.manifest,
            built.tiles,
            max_bucket_bytes=max_bucket_bytes_from_env(),
        )
    except PublishError as error:
        print(f"ingest land: publish FAILED: {error}")
        return 1

    dest = args.dry_run or "R2"
    print(
        f"ingest land: published {PREFIX}/{result.index_id}/ to {dest} "
        f"({result.tile_count} tiles, {result.bytes / 1e6:.2f} MB, {result.duration_s}s; "
        f"previous={result.previous_index_id}, deleted={result.deleted_indexes})"
    )
    return 0
