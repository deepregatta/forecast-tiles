"""`ingest-charts` — build and publish TCP1 chart archives (docs/chart-pack-format.md).

    uv sync --extra charts
    uv run ingest-charts plan --cells stage1                 # build id and the matrix
    uv run ingest-charts build world --build-id ID --protomaps YYYYMMDD --seamap YYYY-MM-DD --out DIR
    uv run ingest-charts build 6-31-21 --build-id ID --protomaps YYYYMMDD --seamap YYYY-MM-DD --out DIR
    uv run ingest-charts publish --build-dir DIR --build-id ID [--dry-run OUT]
    uv run ingest-charts check PACK.pmtiles [...]
    uv run ingest-charts build-area channel --world W.pmtiles --out P.pmtiles ...   # a local stage 1 pack

`build` reads Protomaps' daily build and Open Waters' dated Seamap archive by
HTTP range at the dates it is given, SHOM Balisage over WFS, and the EMODnet
blocks the land index caches. It writes `{target}.pmtiles` (a cell also
`{target}-depth.pmtiles`) and a `.report.json` into --out, and publishes
nothing. `publish` uploads a build directory to R2 (or --dry-run's scratch
layout): its only writer of `charts/`. There is no schedule (WP10 phase 6):
Open Waters' archive is read only in runs the owner approves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from contextlib import nullcontext
from pathlib import Path

DATE_PROTOMAPS = re.compile(r"^[0-9]{8}$")
DATE_SEAMAP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# Matches the land and other root allowances (docs/paid-work.md).
PAID_SECONDS = 14_400


def _targets(spec: str) -> list[str]:
    from ingest.charts.common import CELL_SETS, parse_cell

    cells = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if part in CELL_SETS:
            cells += CELL_SETS[part]
        else:
            parse_cell(part)
            cells.append(part)
    return sorted(dict.fromkeys(cells))


def cmd_plan(args) -> int:
    from ingest.charts.build import utc_build_id

    cells = _targets(args.cells)
    build_id = args.build_id or utc_build_id()
    matrix = (["world"] if not args.no_world else []) + cells
    print(f"build_id={build_id}")
    print(f"matrix={json.dumps(matrix)}")
    print(f"cells={len(cells)}")
    return 0


def _dates(args):
    if not DATE_PROTOMAPS.match(args.protomaps or ""):
        raise SystemExit("--protomaps must name one daily build, YYYYMMDD")
    if not DATE_SEAMAP.match(args.seamap or ""):
        raise SystemExit("--seamap must name one dated Open Waters archive, YYYY-MM-DD")


def cmd_build(args) -> int:
    from ingest.charts import build
    from ingest.charts.common import WORLD, cell_area

    _dates(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    built = args.built or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.target == "world":
        area, kind = WORLD, "world"
    else:
        area, kind = cell_area(args.target), "cell"
    build.run(
        area,
        kind=kind,
        protomaps=args.protomaps,
        seamap=args.seamap,
        shom_tag=args.shom_tag,
        out=out / f"{area.name}.pmtiles",
        build_id=args.build_id,
        built=built,
        workers=args.workers,
    )
    from ingest.charts.check import check, style_fields

    paths = [out / f"{area.name}.pmtiles"]
    if kind == "cell" and (out / f"{area.name}-depth.pmtiles").is_file():
        paths.append(out / f"{area.name}-depth.pmtiles")
    errors = [e for p in paths for e in check(p, style_fields())]
    for e in errors:
        print(e)
    return 1 if errors else 0


def cmd_build_area(args) -> int:
    """Stage 1's local pack: an area's box with the world folded in below z6."""
    from ingest.charts import build
    from ingest.charts.common import AREAS, WORLD

    _dates(args)
    area = AREAS.get(args.area)
    if area is None or area is WORLD:
        raise SystemExit(
            f"unknown area {args.area}; known: {', '.join(a for a in AREAS if a != 'world')}"
        )
    built = args.built or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build.run(
        area,
        kind="area",
        protomaps=args.protomaps,
        seamap=args.seamap,
        shom_tag=args.shom_tag,
        out=Path(args.out),
        build_id=args.build_id or build.utc_build_id(),
        built=built,
        world=Path(args.world) if args.world else None,
        workers=args.workers,
    )
    return 0


def cmd_publish(args) -> int:
    from ingest.charts.publish import PREFIX, publish_build
    from ingest.paid_work import Guard, Paused, RuntimeExpired, deadline, enforced
    from ingest.publish import (
        DirStore,
        PublishError,
        make_r2_store_from_env,
        max_bucket_bytes_from_env,
    )

    guard = token = None
    try:
        if not args.dry_run and enforced():
            guard = Guard.from_env()
            guard.check()
            token = guard.acquire("charts", args.build_id, PAID_SECONDS)
        store = DirStore(args.dry_run) if args.dry_run else make_r2_store_from_env()
        with deadline(PAID_SECONDS) if guard else nullcontext():
            result = publish_build(
                store,
                Path(args.build_dir),
                args.build_id,
                max_bucket_bytes=max_bucket_bytes_from_env(),
            )
    except (Paused, RuntimeExpired) as exc:
        print(f"ingest-charts: paused: {exc}; the published charts remain available")
        return 0
    except PublishError as error:
        print(f"ingest-charts: publish FAILED: {error}")
        return 1
    finally:
        if guard and token:
            try:
                guard.finish("charts", token)
            except Paused as exc:
                print(f"ingest-charts: lease retained until expiry: {exc}")
    dest = args.dry_run or "R2"
    print(
        f"ingest-charts: published {PREFIX}/{result.build_id}/ to {dest}: uploaded "
        f"{len(result.uploaded)} objects ({result.bytes_uploaded / 1e6:.2f} MB), reused "
        f"{result.reused}, {result.bytes_referenced / 1e6:.2f} MB referenced, {result.duration_s}s; "
        f"previous={result.previous_build_id}, deleted={result.deleted_keys}"
    )
    _summary(Path(args.build_dir), result)
    return 0


def _summary(build_dir: Path, result) -> None:
    """Each archive's measured size, for the plan's table (and the job summary)."""
    import os

    rows = ["| Archive | Chart MB | Depth MB | Tiles | Seconds |", "|---|---|---|---|---|"]
    for report in sorted(build_dir.glob("*.report.json")):
        r = json.loads(report.read_text())
        depth = r.get("depth_archive", {}).get("file_bytes", 0)
        rows.append(
            f"| {report.name.removesuffix('.report.json')} | {r['file_bytes'] / 1e6:.2f} | {depth / 1e6:.2f} | {r['tiles']} | "
            f"{r.get('seconds', '')} |"
        )
    text = "\n".join(rows)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(f"### Chart build {result.build_id}\n\n{text}\n")


def cmd_check(args) -> int:
    from ingest.charts.check import main as check_main

    return check_main(args.paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ingest-charts", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="print the build id and the build matrix")
    p.add_argument(
        "--cells", default="stage1", help="a cell set (stage1) or cell ids, comma-separated"
    )
    p.add_argument("--build-id")
    p.add_argument("--no-world", action="store_true")
    p.set_defaults(fn=cmd_plan)

    def inputs(p):
        p.add_argument("--protomaps", required=True, help="Protomaps daily build, YYYYMMDD")
        p.add_argument("--seamap", required=True, help="Open Waters Seamap archive, YYYY-MM-DD")
        p.add_argument("--shom-tag", help="SHOM cache tag (default: today's date)")
        p.add_argument("--built", help="ISO build time (default: now)")
        p.add_argument("--workers", type=int, default=4)

    p = sub.add_parser("build", help="build the world overview or one cell")
    p.add_argument("target", help="world, or a cell id such as 6-31-21")
    p.add_argument("--build-id", required=True)
    p.add_argument("--out", required=True, help="the build directory")
    inputs(p)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("build-area", help="a local area pack (stage 1); never published")
    p.add_argument("area")
    p.add_argument("--out", required=True)
    p.add_argument("--world", help="world.pmtiles to fold in below z6")
    p.add_argument("--build-id")
    inputs(p)
    p.set_defaults(fn=cmd_build_area)

    p = sub.add_parser("publish", help="publish a build directory to R2")
    p.add_argument("--build-dir", required=True)
    p.add_argument("--build-id", required=True)
    p.add_argument("--dry-run", metavar="DIR", help="write the R2 layout to DIR instead")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("check", help="check archives as a TCP1 reader would")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_check)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
