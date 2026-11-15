"""`ingest-atlas` — build and publish TCA1 tidal-atlas packs (docs/tidal-atlas-format.md).

    uv sync --extra atlas
    uv run ingest-atlas fetch --out DOWNLOADS        # SHOM's archives, checked, unpacked
    uv run ingest-atlas build --source DOWNLOADS --out BUILD
    uv run ingest-atlas publish --build-dir BUILD --build-id ID --dry-run OUT
    uv run ingest-atlas check GRID.tca1.gz [...]

`fetch` downloads each carried zone's archive from SHOM's own download
service, refuses one whose SHA-256 is not the pinned one, and unpacks it with
`7z` into its own empty directory. `build` writes one `{grid}.tca1.gz` and
its report per grid. `publish` is the only writer of `tidal-atlas/`; while
the atlas is staged it writes only to --dry-run's scratch layout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def fetch(out: Path) -> int:
    import requests

    from ingest.atlas.sources import GRIDS

    out.mkdir(parents=True, exist_ok=True)
    for archive in dict.fromkeys(g.archive for g in GRIDS):
        grid = next(g for g in GRIDS if g.archive == archive)
        target = out / f"ATLASCOURANTS2D_NETCDF_{archive}.7z"
        if not (
            target.exists()
            and hashlib.sha256(target.read_bytes()).hexdigest() == grid.archive_sha256
        ):
            with requests.get(grid.url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with target.open("wb") as f:
                    for chunk in response.iter_content(1 << 20):
                        f.write(chunk)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != grid.archive_sha256:
            print(f"atlas: {target.name} is {digest}, not the pinned archive", file=sys.stderr)
            return 1
        unpacked = out / archive
        if not unpacked.exists():
            unpacked.mkdir()
            subprocess.run(
                ["7z", "x", "-y", f"-o{unpacked}", str(target)], check=True, capture_output=True
            )
        print(f"atlas: {archive} {target.stat().st_size} B checked")
    return 0


def _find(source: Path, name: str) -> Path:
    found = sorted(source.rglob(name))
    if len(found) != 1:
        raise SystemExit(f"atlas: expected one {name} under {source}, found {len(found)}")
    return found[0]


def build(source: Path, out: Path, only: list[str] | None) -> int:
    from ingest.atlas.build import build as build_grid
    from ingest.atlas.sources import GRIDS

    total = 0
    for grid in GRIDS:
        if only and grid.id not in only:
            continue
        report = build_grid(_find(source, grid.file), grid, out)
        total += report["bytes"]
        print(
            f"atlas: {grid.id} {report['bytes']} B, {report['rows']}x{report['columns']}, "
            f"{report['steps']} steps"
        )
    print(f"atlas: {total} B in all")
    return 0


def publish(build_dir: Path, build_id: str, dry_run: Path | None) -> int:
    from ingest.atlas.publish import publish_build
    from ingest.publish import DirStore, make_r2_store_from_env, max_bucket_bytes_from_env

    store = DirStore(dry_run) if dry_run else make_r2_store_from_env()
    if dry_run:
        dry_run.mkdir(parents=True, exist_ok=True)
    result = publish_build(store, build_dir, build_id, max_bucket_bytes=max_bucket_bytes_from_env())
    print(json.dumps(result.__dict__, indent=1))
    return 0


def check(paths: list[Path]) -> int:
    from ingest.atlas import codec

    for path in paths:
        header, _ = codec.decode(path.read_bytes())
        print(f"{path.name}: TCA1 {header['grid']} {header['rows']}x{header['columns']} ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest-atlas")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--grids", default="", help="grid ids, comma-separated; all by default")
    p = sub.add_parser("publish")
    p.add_argument("--build-dir", type=Path, required=True)
    p.add_argument("--build-id", required=True)
    p.add_argument("--dry-run", type=Path, default=None)
    c = sub.add_parser("check")
    c.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return fetch(args.out)
    if args.command == "build":
        return build(args.source, args.out, [g for g in args.grids.split(",") if g] or None)
    if args.command == "publish":
        return publish(args.build_dir, args.build_id, args.dry_run)
    return check(args.paths)


if __name__ == "__main__":
    sys.exit(main())
