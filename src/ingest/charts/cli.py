"""`ingest-charts` — build and publish TCP1 chart archives (docs/chart-pack-format.md).

    uv sync --extra charts
    uv run ingest-charts plan --cells europe --seamap newest --protomaps newest   # build id, inputs, matrix
    uv run ingest-charts build world --build-id ID --protomaps YYYYMMDD --seamap YYYY-MM-DD --out DIR
    uv run ingest-charts build 6-31-21 --build-id ID --protomaps YYYYMMDD --seamap YYYY-MM-DD --out DIR
    uv run ingest-charts publish --build-dir DIR --build-id ID [--dry-run OUT]
    uv run ingest-charts check PACK.pmtiles [...]
    uv run ingest-charts build-area channel --world W.pmtiles --out P.pmtiles ...   # a local stage 1 pack

`build` reads Protomaps' daily build and Open Waters' dated Seamap archive by
HTTP range at the dates it is given, SHOM Balisage over WFS, and the cell's
EMODnet product (products.py), from the published build when `--previous`
names one with the same product key, otherwise cut from the DTM blocks the
land index caches. It writes `{target}.pmtiles` (a cell also
`{target}-depth.pmtiles`, and `{target}.emodnet.npz` when it cut a new
product) and a `.report.json` into --out, and publishes nothing; a target
whose inputs equal the published archive's writes only
`{target}.unchanged.json`. `publish` uploads a build directory to R2 (or
--dry-run's scratch layout): its only writer of `charts/`.

The monthly run (WP10 phase 8) is started by the dispatcher with `newest`
for both dates, which `plan` resolves; the owner told Open Waters about the
monthly range reads on 2026-11-07.
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


def resolve(seamap: str, protomaps: str, previous: dict | None = None) -> tuple[str, str]:
    """The run's inputs: dates as given, or `newest` resolved. Never older
    than what the published build already read."""
    from ingest.charts import sources

    seamap = sources.newest_seamap() if seamap == "newest" else seamap
    protomaps = sources.newest_protomaps() if protomaps == "newest" else protomaps
    if not DATE_PROTOMAPS.match(protomaps or "") or not DATE_SEAMAP.match(seamap or ""):
        raise SystemExit(f"cannot build from Seamap {seamap!r} and Protomaps {protomaps!r}")
    if previous:
        dates = [
            ((e.get("sources") or {}).get("seamap") or {}).get("date")
            for e in [previous["world"], *previous["cells"].values()]
        ]
        newest = max((d for d in dates if d), default=None)
        if newest and seamap < newest:
            raise SystemExit(f"Seamap {seamap} is older than the published build's {newest}")
    return seamap, protomaps


def cmd_plan(args) -> int:
    from ingest.charts.build import utc_build_id

    cells = _targets(args.cells)
    build_id = args.build_id or utc_build_id()
    matrix = (["world"] if not args.no_world else []) + cells
    print(f"build_id={build_id}")
    print(f"matrix={json.dumps(matrix)}")
    print(f"cells={len(cells)}")
    if args.seamap or args.protomaps:
        previous = load_pointer(args.previous) if args.previous else None
        seamap, protomaps = resolve(args.seamap or "newest", args.protomaps or "newest", previous)
        print(f"seamap={seamap}")
        print(f"protomaps={protomaps}")
        if previous:
            was = previous["world"].get("sources", {})
            print(
                f"published={previous['build_id']} "
                f"(Seamap {was.get('seamap', {}).get('date')}, Protomaps {was.get('protomaps', {}).get('build')})",
                file=sys.stderr,
            )
    return 0


def load_pointer(source: str) -> dict:
    """The published `charts/latest.json`, by URL or as a scratch layout's file."""
    return json.loads(_read(source))


def _read(source: str) -> bytes:
    if source.startswith(("https://", "http://")):
        from ingest.charts.sources import session

        r = session().get(source, timeout=120)
        r.raise_for_status()
        return r.content
    return Path(source).read_bytes()


def _origin(pointer_source: str) -> str:
    """Where a pointer's keys are read: the URL or folder above `charts/`."""
    suffix = "/charts/latest.json"
    if not pointer_source.endswith(suffix):
        raise SystemExit(f"--previous must end in {suffix}: {pointer_source}")
    return pointer_source[: -len(suffix)]


def _published_product(args, area, key, entry):
    """The cell's EMODnet product from the published build, when it is this
    code's and the DTM release's; None otherwise (it is cut afresh)."""
    import hashlib

    from ingest.charts import products
    from ingest.charts.common import cache_dir

    product = (entry or {}).get("emodnet")
    if not product or product.get("product_key") != key:
        return None
    dest = cache_dir() / "products" / key[:16] / f"{area.name}{products.SUFFIX}"
    if not dest.is_file() or hashlib.sha256(dest.read_bytes()).hexdigest() != product["sha256"]:
        data = _read(f"{_origin(args.previous)}/{product['key']}")
        if hashlib.sha256(data).hexdigest() != product["sha256"]:
            raise SystemExit(f"{product['key']} does not match its sha256 in the pointer")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return products.Product.load(dest, area, key)


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
    product = unchanged_if = None
    if args.previous:
        from ingest.charts import products
        from ingest.charts.publish import CHUNK_BYTES

        previous = load_pointer(args.previous)
        entry = previous["world"] if kind == "world" else previous["cells"].get(area.name)
        # a pointer of another chunk size lists chunks this build must replace
        # (and a cell published before its EMODnet product was is built again)
        if (
            entry
            and previous.get("chunk_bytes") == CHUNK_BYTES
            and (kind == "world" or entry.get("emodnet"))
        ):
            unchanged_if = entry.get("inputs_sha256")
        if kind == "cell":
            key = products.product_key(build.style_fields()["layers"]["depth"])
            product = _published_product(args, area, key, entry)
    report = build.run(
        area,
        kind=kind,
        protomaps=args.protomaps,
        seamap=args.seamap,
        shom_tag=args.shom_tag,
        out=out / f"{area.name}.pmtiles",
        build_id=args.build_id,
        built=built,
        workers=args.workers,
        product=product,
        unchanged_if=unchanged_if,
        save_product=out / f"{area.name}.emodnet.npz" if kind == "cell" else None,
    )
    if report.get("unchanged"):
        return 0
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

    rows = [
        "| Archive | Chart MB | Depth MB | EMODnet product MB | Tiles | Seconds |",
        "|---|---|---|---|---|---|",
    ]
    for report in sorted(build_dir.glob("*.report.json")):
        r = json.loads(report.read_text())
        name = report.name.removesuffix(".report.json")
        depth = r.get("depth_archive", {}).get("file_bytes", 0)
        product = build_dir / f"{name}.emodnet.npz"
        cut = f"{product.stat().st_size / 1e6:.2f} (new)" if product.is_file() else "kept"
        rows.append(
            f"| {name} | {r['file_bytes'] / 1e6:.2f} | {depth / 1e6:.2f} | "
            f"{cut if r['kind'] == 'cell' else ''} | {r['tiles']} | {r.get('seconds', '')} |"
        )
    for marker in sorted(build_dir.glob("*.unchanged.json")):
        r = json.loads(marker.read_text())
        name = marker.name.removesuffix(".unchanged.json")
        rows.append(f"| {name} | unchanged | | | | {r.get('seconds', '')} |")
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

    p = sub.add_parser("plan", help="print the build id, the inputs and the build matrix")
    p.add_argument(
        "--cells", default="stage1", help="a cell set (stage1, europe) or cell ids, comma-separated"
    )
    p.add_argument("--build-id")
    p.add_argument("--no-world", action="store_true")
    p.add_argument("--seamap", help="YYYY-MM-DD or newest: resolve and print the run's Seamap date")
    p.add_argument("--protomaps", help="YYYYMMDD or newest: resolve and print the Protomaps build")
    p.add_argument("--previous", help="the published charts/latest.json (URL or file)")
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
    p.add_argument(
        "--previous",
        help="the published charts/latest.json (URL or file): reuse its EMODnet product, "
        "and write nothing but a marker when the inputs are its archive's",
    )
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
