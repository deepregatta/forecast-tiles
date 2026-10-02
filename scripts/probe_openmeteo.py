#!/usr/bin/env python3
"""Focused checks and reproducible measurements for the Open-Meteo regional
layers (docs/open-meteo-bulk-implementation-plan.md). Needs the `openmeteo`
extra and network access; nothing here writes to R2.

  runs LAYER [--days N]          complete data_run/ runs: completion delay, meta ETag
  footprint LAYER [--write] [--check-runs N]
                                 derive the expected footprint from the static
                                 terrain file, compare with the registered hash,
                                 and check it against N recent runs' wind fields
  benchmark LAYER [--cycle C] [--assume-gust-windows] [--tile-deg 5 10]
                                 the real download -> decode -> validate -> tile
                                 path, with sizes, timings and peak RSS
  gust-window LAYER [--cycle C]  read the upstream GRIB's gust stepRange
                                 (Météo-France object.data.gouv.fr, DWD
                                 opendata.dwd.de) to verify a registry window
  fixture LAYER --cycle C OUT    crop a real run into tiny attributed .om
                                 test fixtures (tests/fixtures/openmeteo/)

Usage: uv run --extra openmeteo scripts/probe_openmeteo.py benchmark weather-arome
"""

from __future__ import annotations

import argparse
import bz2
import json
import re
import resource
import struct
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ingest.sources.base import SESSION
from ingest.sources.base import parse_cycle_arg
from ingest.sources.openmeteo import adapter, catalog, reader
from ingest.sources.openmeteo.grids import (
    footprint_sha256,
    save_footprint,
)
from ingest.sources.openmeteo.registry import BUCKET_URL, product
from ingest.tile import build_tiles


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def list_keys(prefix: str, delimiter: str | None = None) -> tuple[list[dict], list[str]]:
    params = {"list-type": "2", "prefix": prefix}
    if delimiter:
        params["delimiter"] = delimiter
    out, prefixes, token = [], [], None
    while True:
        if token:
            params["continuation-token"] = token
        text = SESSION.get(BUCKET_URL + "/", params=params, timeout=60).text
        for block in re.findall(r"<Contents>(.*?)</Contents>", text, re.S):
            get = lambda tag: re.search(rf"<{tag}>([^<]*)</{tag}>", block).group(1)  # noqa: E731
            out.append(
                {"key": get("Key"), "bytes": int(get("Size")), "modified": get("LastModified")}
            )
        prefixes += re.findall(r"<Prefix>([^<]+)</Prefix>", text)[1:]
        m = re.search(r"<NextContinuationToken>([^<]+)<", text)
        if not m:
            return out, prefixes
        token = m.group(1)


# ------------------------------------------------------------------- runs


def cmd_runs(args) -> int:
    p = product(args.layer)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t = now
    print(f"{p.domain}: registered cycles {p.cycles}")
    while t > now - timedelta(days=args.days):
        if t.hour in p.cycles:
            try:
                meta = catalog.fetch_meta(p, t)
            except Exception as exc:
                print(f"{t:%Y-%m-%dT%HZ}  -  ({type(exc).__name__})")
            else:
                created = datetime.strptime(meta.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                delay = (created - t).total_seconds() / 3600
                leads = len(meta.valid_times)
                print(
                    f"{t:%Y-%m-%dT%HZ}  meta created {meta.created_at}  delay {delay:4.2f} h  "
                    f"{leads} steps  etag {meta.record.etag}"
                )
        t -= timedelta(hours=1)
    return 0


# -------------------------------------------------------------- footprint


def _static_mask(p, tmp: Path) -> tuple[np.ndarray, reader.ObjectRecord]:
    import omfiles

    key = f"data/{p.domain}/static/HSURF.om"
    record = reader.download(key, tmp / "HSURF.om")
    r = omfiles.OmFileReader(str(tmp / "HSURF.om"))
    h = r.read_array((slice(None), slice(None)))
    r.close()
    return ~np.isnan(h), record


def cmd_footprint(args) -> int:
    p = product(args.layer)
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        valid, record = _static_mask(p, tmp)
        digest = footprint_sha256(valid)
        print(
            f"static HSURF {record.key} etag {record.etag}: {valid.shape}, "
            f"{(~valid).mean():.4%} missing, sha256 {digest}"
        )
        print(f"registered: {p.footprint} sha256 {p.footprint_sha256}")
        checked = []
        if args.check_runs:
            now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            t = now
            while len(checked) < args.check_runs and t > now - timedelta(days=3):
                if t.hour in p.cycles:
                    try:
                        catalog.fetch_meta(p, t)
                    except Exception:
                        t -= timedelta(hours=1)
                        continue
                    key = catalog.file_key(p, t, "v")
                    path = tmp / "v.om"
                    rec = reader.download(key, path)
                    d = reader.decode(path, grid=p.grid, unit=p.source_unit, reference=t)
                    interior_nan = int(np.isnan(d.values[:, valid]).sum(axis=1).max())
                    exterior = (~np.isnan(d.values[:, ~valid])).sum(axis=1)
                    print(
                        f"  {key} etag {rec.etag}: max interior missing per step {interior_nan}, "
                        f"max valid outside per step {int(exterior.max())}"
                    )
                    checked.append(f"{key} (etag {rec.etag})")
                    path.unlink()
                t -= timedelta(hours=1)
        if args.write:
            save_footprint(
                p.footprint,
                valid,
                {
                    "derived_from": f"{record.key} (etag {record.etag}) NaN mask",
                    "derived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                    "rows": "row 0 = southernmost latitude, ascending",
                    "checked_against": checked,
                },
            )
            print(f"wrote footprint {p.footprint}: set footprint_sha256={digest!r} in the registry")
    return 0


# -------------------------------------------------------------- benchmark


def cmd_benchmark(args) -> int:
    p = product(args.layer)
    cycle = parse_cycle_arg(args.cycle) if args.cycle else catalog.resolve(p)
    t0 = time.time()
    cube, source = adapter.build_cube(p, cycle, assume_gust_windows=args.assume_gust_windows)
    t_build = time.time() - t0
    report = adapter.validate(p, cube)
    print(report.summary())
    gust_line = next((c for c in report.checks_passed if c.startswith("gust_ge_wind")), None)
    out = {
        "layer": p.layer,
        "cycle": cube.cycle_iso,
        "source_bytes": sum(r.bytes for r in source.files),
        "source_objects": [r.public() for r in source.files],
        "download_decode_s": round(t_build, 1),
        "validation_ok": report.ok,
        "gust_check": gust_line or "not run (no gust)",
        "capabilities": cube.provenance["capabilities"],
        "layouts": {},
    }
    nvars = len(cube.variables)
    for tile_deg in args.tile_deg:
        cube.tile_deg = tile_deg
        t1 = time.time()
        tiles = build_tiles(cube)
        t_enc = time.time() - t1
        sizes = sorted((len(gz), tid) for tid, gz in tiles)
        n_t = len(next(iter(cube.time_axes.values())))
        cells = round(tile_deg * p.grid.cells_per_degree) ** 2
        out["layouts"][f"{tile_deg}deg"] = {
            "tiles": len(tiles),
            "gz_bytes": sum(s for s, _ in sizes),
            "largest_tile": {"id": sizes[-1][1], "gz_bytes": sizes[-1][0]},
            "largest_decoded_float32_bytes": cells * n_t * nvars * 4,
            "encode_s": round(t_enc, 1),
        }
    out["peak_rss_mb"] = round(rss_mb())
    print(json.dumps(out, indent=1))
    return 0 if report.ok else 1


# ------------------------------------------------------------ gust window


def _grib_messages(stream, limit_bytes: int):
    """Split a GRIB2 byte stream into messages without reading it all."""
    buf = b""
    read = 0
    it = stream.iter_content(1 << 20)
    while True:
        while len(buf) < 16:
            chunk = next(it, None)
            if chunk is None:
                return
            buf += chunk
            read += len(chunk)
        start = buf.find(b"GRIB")
        if start < 0:
            buf = buf[-3:]
            continue
        buf = buf[start:]
        if len(buf) < 16:
            continue
        total = struct.unpack(">Q", buf[8:16])[0]
        while len(buf) < total:
            chunk = next(it, None)
            if chunk is None:
                return
            buf += chunk
            read += len(chunk)
        yield buf[:total]
        buf = buf[total:]
        if read > limit_bytes:
            return


def _describe(msg: bytes) -> dict:
    import eccodes

    gid = eccodes.codes_new_from_message(msg)
    try:
        out = {}
        for key in (
            "shortName",
            "name",
            "stepType",
            "stepRange",
            "startStep",
            "endStep",
            "typeOfStatisticalProcessing",
            "lengthOfTimeRange",
            "indicatorOfUnitForTimeRange",
        ):
            try:
                out[key] = eccodes.codes_get_string(gid, key)
            except Exception:
                pass
        return out
    finally:
        eccodes.codes_release(gid)


def cmd_gust_window(args) -> int:
    p = product(args.layer)
    cycle = parse_cycle_arg(args.cycle) if args.cycle else catalog.resolve(p)
    found = []
    if p.domain == "meteofrance_arome_france0025":
        run = f"{cycle:%Y-%m-%dT%H:%M}:00Z"
        for group in ("00H06H", "49H51H"):
            url = (
                f"https://object.data.gouv.fr/meteofrance-pnt/pnt/{run}/arome/0025/SP1/"
                f"arome__0025__SP1__{group}__{run}.grib2"
            )
            print(f"GET {url}")
            with SESSION.get(url, stream=True, timeout=(10, 300)) as r:
                r.raise_for_status()
                for msg in _grib_messages(r, limit_bytes=400 << 20):
                    d = _describe(msg)
                    if "fg" in d.get("shortName", "") or "gust" in d.get("name", "").lower():
                        found.append(d)
                        print(json.dumps(d))
    elif p.domain == "dwd_icon_eu":
        for step in (1, 2, 78, 81, 84, 120):
            url = (
                f"https://opendata.dwd.de/weather/nwp/icon-eu/grib/{cycle:%H}/vmax_10m/"
                f"icon-eu_europe_regular-lat-lon_single-level_{cycle:%Y%m%d%H}_{step:03d}_"
                "VMAX_10M.grib2.bz2"
            )
            print(f"GET {url}")
            r = SESSION.get(url, timeout=(10, 300))
            r.raise_for_status()
            d = _describe(bz2.decompress(r.content)) | {"lead_h": step}
            found.append(d)
            print(json.dumps(d))
    else:
        print(f"no upstream gust check for {p.domain}")
        return 2
    print(f"registered: {p.gust_windows}")
    return 0 if found else 1


# ---------------------------------------------------------------- fixture


def _write_om(path: Path, values_lat_lon_time: np.ndarray, *, src, unit: str, times, wkt: str):
    import omfiles

    w = omfiles.OmFileWriter(str(path))
    children = [
        w.write_scalar(wkt, name="crs_wkt"),
        w.write_scalar(unit, name="unit"),
        w.write_scalar(np.int64(src["forecast_reference_time"]), name="forecast_reference_time"),
        w.write_array(np.asarray(times, dtype=np.int64), chunks=[len(times)], name="time"),
        w.write_scalar("lat lon time", name="coordinates"),
        w.write_scalar(np.int64(src["created_at"]), name="created_at"),
    ]
    root = w.write_array(
        np.ascontiguousarray(values_lat_lon_time, dtype=np.float32),
        chunks=[1, values_lat_lon_time.shape[1], values_lat_lon_time.shape[2]],
        scale_factor=10.0,
        add_offset=0.0,
        compression="pfor_delta_2d_int16",
        name="",
        children=children,
    )
    w.close(root)


def cmd_fixture(args) -> int:
    import omfiles

    p = product(args.layer)
    cycle = parse_cycle_arg(args.cycle)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    i0, i1, j0, j1 = args.rows[0], args.rows[1], args.cols[0], args.cols[1]
    n = p.grid.cells_per_degree
    lat0, lon0 = (p.grid.lat0 * n + i0) / n, (p.grid.lon0 * n + j0) / n
    lat1, lon1 = (p.grid.lat0 * n + i1 - 1) / n, (p.grid.lon0 * n + j1 - 1) / n
    wkt = f'GEOGCRS["WGS 84"] BBOX[{lat0:g},{lon0:g},{lat1:g},{lon1:g}]'
    record = {
        "layer": p.layer,
        "domain": p.domain,
        "cycle": f"{cycle:%Y-%m-%dT%HZ}",
        "crop": {"rows": [i0, i1], "cols": [j0, j1], "lat0": lat0, "lon0": lon0},
        "attribution": p.attribution,
        "licence": p.data_licence,
        "note": "Cropped from the source files below; values unchanged (0.1 m/s precision)",
        "files": {},
    }
    meta = catalog.fetch_meta(p, cycle)
    record["meta_etag"] = meta.record.etag
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for role in ("u", "v", "gust"):
            key = catalog.file_key(p, cycle, role)
            rec = reader.download(key, tmp / "f.om")
            r = omfiles.OmFileReader(str(tmp / "f.om"))
            src = {
                "forecast_reference_time": r.get_child_by_name(
                    "forecast_reference_time"
                ).read_scalar(),
                "created_at": r.get_child_by_name("created_at").read_scalar(),
            }
            times = r.get_child_by_name("time").read_array((slice(None),))
            crop = r.read_array((slice(i0, i1), slice(j0, j1), slice(None)))
            unit = r.get_child_by_name("unit").read_scalar()
            r.close()
            name = f"{p.files[role]}.om"
            _write_om(out_dir / name, crop, src=src, unit=unit, times=times, wkt=wkt)
            record["files"][name] = rec.public()
        hs_key = f"data/{p.domain}/static/HSURF.om"
        rec = reader.download(hs_key, tmp / "h.om")
        r = omfiles.OmFileReader(str(tmp / "h.om"))
        hs = r.read_array((slice(i0, i1), slice(j0, j1)))
        r.close()
        np.save(out_dir / "HSURF.npy", hs.astype(np.float32))
        record["files"]["HSURF.npy"] = rec.public()
    (out_dir / "fixture.json").write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(record, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("runs")
    s.add_argument("layer")
    s.add_argument("--days", type=float, default=2)
    s = sub.add_parser("footprint")
    s.add_argument("layer")
    s.add_argument("--write", action="store_true")
    s.add_argument("--check-runs", type=int, default=0)
    s = sub.add_parser("benchmark")
    s.add_argument("layer")
    s.add_argument("--cycle")
    s.add_argument(
        "--assume-gust-windows",
        action="store_true",
        help="include gust with the registry's unverified windows (measurement only)",
    )
    s.add_argument("--tile-deg", type=int, nargs="+", default=[10, 5])
    s = sub.add_parser("gust-window")
    s.add_argument("layer")
    s.add_argument("--cycle")
    s = sub.add_parser("fixture")
    s.add_argument("layer")
    s.add_argument("--cycle", required=True)
    s.add_argument("out")
    s.add_argument("--rows", type=int, nargs=2, required=True)
    s.add_argument("--cols", type=int, nargs=2, required=True)
    args = parser.parse_args(argv)
    return {
        "runs": cmd_runs,
        "footprint": cmd_footprint,
        "benchmark": cmd_benchmark,
        "gust-window": cmd_gust_window,
        "fixture": cmd_fixture,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
