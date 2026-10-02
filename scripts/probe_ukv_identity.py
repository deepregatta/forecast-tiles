#!/usr/bin/env python3
"""Read-only native/bulk identity and attributed offline crop for UKV.

uv run --extra openmeteo --with h5py python scripts/probe_ukv_identity.py \
    --cycle 20261002T12 --scratch /tmp/ukv-identity --output /tmp/ukv-identity.json

The probe compares entire 2-D slices, not fitted coordinates or a few weather
points. Optional --fixture writes a 9x9, all-55-step source crop and metadata;
it is data under CC BY-SA 4.0, separate from MIT probe/adapter code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

PARAMETERS = (
    "https://www.metoffice.gov.uk/binaries/content/assets/metofficegovuk/pdf/data/"
    "ukv-parameters-may-2019.pdf"
)


def fetch(url: str, path: Path) -> dict:
    """Reusable cache only when current ETag and size match its saved receipt."""
    receipt = path.with_suffix(path.suffix + ".json")
    head = requests.head(url, timeout=(10, 60))
    head.raise_for_status()
    etag, size = head.headers["ETag"], int(head.headers["Content-Length"])
    old = json.loads(receipt.read_text()) if receipt.exists() else {}
    if not (path.exists() and old.get("etag") == etag and path.stat().st_size == size):
        with requests.get(url, stream=True, timeout=(10, 120)) as r:
            r.raise_for_status()
            with path.open("wb") as out:
                for chunk in r.iter_content(1 << 20):
                    out.write(chunk)
            if r.headers.get("ETag") != etag or path.stat().st_size != size:
                raise RuntimeError(f"object changed/truncated during probe: {url}")
    with path.open("rb") as body:
        digest = hashlib.file_digest(body, "sha256").hexdigest()
    record = {
        "url": url,
        "etag": etag,
        "bytes": size,
        "sha256": digest,
    }
    if old and old.get("etag") == etag and old.get("sha256") != record["sha256"]:
        raise RuntimeError(f"cached object checksum differs: {url}")
    receipt.write_text(json.dumps(record) + "\n")
    return record


def write_crop(path, data, source):
    import omfiles

    writer = omfiles.OmFileWriter(str(path))
    children = []
    for key in ("crs_wkt", "unit", "coordinates", "forecast_reference_time", "created_at"):
        children.append(writer.write_scalar(source.get_child_by_name(key).read_scalar(), name=key))
    times = source.get_child_by_name("time").read_array((slice(None),))
    children.append(writer.write_array(times, chunks=[len(times)], name="time"))
    root = writer.write_array(
        np.ascontiguousarray(data),
        chunks=[1, data.shape[1], data.shape[2]],
        scale_factor=10,
        compression="pfor_delta_2d_int16",
        children=children,
    )
    writer.close(root)


def main():
    import h5py
    import omfiles

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    if args.fixture:
        args.fixture.mkdir(parents=True, exist_ok=True)
    cycle = datetime.strptime(args.cycle, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    bulk_base = (
        "https://openmeteo.s3.amazonaws.com/data_run/ukmo_uk_deterministic_2km/"
        f"{cycle:%Y/%m/%d/%H%M}Z/"
    )
    records, comparisons, crops = [], [], []
    cases = [
        ("wind_speed_10m", "wind_speed_on_height_levels", "wind_speed", (0, 54), 0.051),
        (
            "wind_direction_10m",
            "wind_direction_on_height_levels",
            "wind_from_direction",
            (0, 54),
            1.001,
        ),
        ("wind_gusts_10m", "wind_gust_at_10m", "wind_speed_of_gust", (0, 1, 54), 0.051),
    ]
    for bulk, native, field, leads, tolerance in cases:
        path = args.scratch / (bulk + ".om")
        record = fetch(bulk_base + bulk + ".om", path)
        records.append(record)
        source = omfiles.OmFileReader(str(path))
        try:
            if tuple(source.shape) != (970, 1042, 55):
                raise RuntimeError("UKV bulk geometry/axis changed")
            for lead in leads:
                valid = cycle + timedelta(hours=lead)
                url = (
                    "https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/"
                    f"uk-deterministic-2km/{cycle:%Y%m%dT%H%M}Z/"
                    f"{valid:%Y%m%dT%H%M}Z-PT{lead:04d}H00M-{native}.nc"
                )
                p = args.scratch / f"{native}-{lead}.nc"
                receipt = fetch(url, p)
                records.append(receipt)
                with h5py.File(p) as dataset:
                    if "height_levels" in native:
                        heights = dataset["height"][:]
                        if (heights == 10).sum() != 1:
                            raise RuntimeError("native 10 m height missing/ambiguous")
                        a = dataset[field][np.flatnonzero(heights == 10)[0]]
                    else:
                        a = dataset[field][:].squeeze()
                    standard_name = dataset[field].attrs["standard_name"].decode()
                b = source.read_array((slice(None), slice(None), slice(lead, lead + 1))).squeeze()
                if not np.array_equal(np.isfinite(a), np.isfinite(b)):
                    raise RuntimeError("native/bulk footprint differs")
                error = abs(a - b)
                if "direction" in bulk:
                    error = np.minimum(error, 360 - error)
                comparisons.append(
                    {
                        "bulk_variable": bulk,
                        "native_parameter": native,
                        "standard_name": standard_name,
                        "lead_h": lead,
                        "cells": int(a.size),
                        "missing_cells": int((~np.isfinite(a)).sum()),
                        "max_abs_error": float(np.nanmax(error)),
                        "tolerance": tolerance,
                        "passed": bool(np.nanmax(error) <= tolerance),
                        "points": [
                            {
                                "row": i,
                                "column": j,
                                "native": float(a[i, j]),
                                "bulk": float(b[i, j]),
                            }
                            for i, j in [(0, 0), (0, 1041), (969, 0), (969, 1041), (485, 521)]
                        ],
                    }
                )
            # Fixed all-55-step source crop, not a reprojected fixture.
            if args.fixture:
                data = source.read_array((slice(481, 490), slice(517, 526), slice(None)))
                write_crop(args.fixture / (bulk + ".om"), data, source)
                crops.append({"variable": bulk, "rows": [481, 490], "columns": [517, 526]})
        finally:
            source.close()
    out = {
        "cycle": f"{cycle:%Y-%m-%dT%HZ}",
        "attribution": "British Crown copyright, Met Office UKV via Open-Meteo",
        "data_licence": "https://creativecommons.org/licenses/by-sa/4.0/",
        "modifications": "Whole-field comparison and cropped/recompressed offline fixture",
        "parameters_source": PARAMETERS,
        "direction_frame_source": "https://cfconventions.org/mailing-list-archive/Data/1497.html",
        "gust_kind": "instantaneous diagnostic, not max-PT01H",
        "objects": records,
        "comparisons": comparisons,
        "crops": crops,
        "production_enabled": False,
    }
    args.output.write_text(json.dumps(out, indent=1) + "\n")
    print(
        json.dumps(
            {
                "comparisons": len(comparisons),
                "passed": all(c["passed"] for c in comparisons),
                "output": str(args.output),
            }
        )
    )
    return 0 if all(c["passed"] for c in comparisons) else 1


if __name__ == "__main__":
    raise SystemExit(main())
