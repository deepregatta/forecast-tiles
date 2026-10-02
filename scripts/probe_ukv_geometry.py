#!/usr/bin/env python3
"""Read-only UKV metadata probe; never registers, converts or publishes data.

uv run --with pyproj --with h5py python scripts/probe_ukv_geometry.py \
    --cycle 20261002T12 --output /tmp/ukv-discovery.json

Records primary NetCDF attributes and independent corner coordinates beside
bulk metadata. Does not infer gust windows from output spacing or missing
time bounds. Temporary native files are removed after reading.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


def norm(value):
    if isinstance(value, bytes):
        return value.decode()
    if hasattr(value, "tolist"):
        return norm(value.tolist())
    if isinstance(value, list):
        return [norm(item) for item in value]
    return value


def main():
    import h5py
    import pyproj

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    cycle = datetime.strptime(args.cycle, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    meta_url = (
        "https://openmeteo.s3.amazonaws.com/data_run/ukmo_uk_deterministic_2km/"
        f"{cycle:%Y/%m/%d/%H%M}Z/meta.json"
    )
    response = requests.get(meta_url, timeout=(10, 60))
    response.raise_for_status()
    meta = response.json()
    records = []
    coordinates = []
    with tempfile.TemporaryDirectory() as scratch:
        for variable, lead in [
            ("wind_direction_at_10m", 0),
            ("wind_gust_at_10m", 0),
            ("wind_gust_at_10m", 1),
            ("wind_gust_at_10m", 54),
        ]:
            valid = cycle + timedelta(hours=lead)
            url = (
                "https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/"
                f"uk-deterministic-2km/{cycle:%Y%m%dT%H%M}Z/"
                f"{valid:%Y%m%dT%H%M}Z-PT{lead:04d}H00M-{variable}.nc"
            )
            r = requests.get(url, timeout=(10, 60))
            r.raise_for_status()
            path = Path(scratch) / "source.nc"
            path.write_bytes(r.content)
            with h5py.File(path) as source:
                data_name = (
                    "wind_from_direction" if "direction" in variable else "wind_speed_of_gust"
                )
                attrs = {
                    key: norm(value)
                    for key, value in source[data_name].attrs.items()
                    if not key.startswith("_") and key not in ("DIMENSION_LIST", "REFERENCE_LIST")
                }
                projection = {
                    key: norm(value)
                    for key, value in source["lambert_azimuthal_equal_area"].attrs.items()
                }
                x, y = source["projection_x_coordinate"][:], source["projection_y_coordinate"][:]
                records.append(
                    {
                        "url": url,
                        "etag": r.headers.get("ETag"),
                        "bytes": len(r.content),
                        "lead_h": lead,
                        "shape": list(source[data_name].shape),
                        "attributes": attrs,
                        "projection": projection,
                        "x0_m": float(x[0]),
                        "y0_m": float(y[0]),
                        "dx_m": float(x[1] - x[0]),
                        "dy_m": float(y[1] - y[0]),
                        "has_time_bounds": "bounds" in source["time"].attrs,
                    }
                )
                if not coordinates:
                    scalar_projection = {
                        key: value[0] if isinstance(value, list) else value
                        for key, value in projection.items()
                    }
                    native = pyproj.Transformer.from_crs(
                        pyproj.CRS.from_cf(scalar_projection), "EPSG:4326", always_xy=True
                    )
                    bulk = pyproj.Transformer.from_crs(
                        pyproj.CRS.from_wkt(meta["crs_wkt"]), "EPSG:4326", always_xy=True
                    )
                    geod = pyproj.Geod(ellps="WGS84")
                    for i, j in [
                        (0, 0),
                        (0, len(x) - 1),
                        (len(y) - 1, 0),
                        (len(y) - 1, len(x) - 1),
                        (len(y) // 2, len(x) // 2),
                    ]:
                        a = native.transform(x[j], y[i])
                        b = bulk.transform(x[j], y[i])
                        distance = geod.inv(*a, *b)[2]
                        coordinates.append(
                            {
                                "row": i,
                                "column": j,
                                "native_lon_lat": a,
                                "bulk_lon_lat": b,
                                "difference_m": distance,
                            }
                        )
    out = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "attribution": "British Crown copyright, Met Office UKV via Open-Meteo",
        "native_data_licence": "CC BY-SA 4.0",
        "native_licence_source": "https://registry.opendata.aws/met-office-uk-deterministic/",
        "bulk_catalogue_licence": "CC BY 4.0; upstream ShareAlike question remains separate",
        "meta_url": meta_url,
        "meta_etag": response.headers.get("ETag"),
        "bulk_meta": meta,
        "native_records": records,
        "reference_coordinates": coordinates,
        "pyproj_version": pyproj.__version__,
        "production_enabled": False,
        "unresolved": [
            "bulk/native projection discrepancy",
            "gust interval or instantaneous semantics",
            "native/bulk field identity and missing footprint",
            "redistribution notice",
        ],
    }
    Path(args.output).write_text(json.dumps(out, indent=1) + "\n")
    print(
        json.dumps(
            {
                "source_records": len(records),
                "largest_coordinate_difference_m": max(c["difference_m"] for c in coordinates),
                "gust_time_bounds": [r["has_time_bounds"] for r in records if "gust" in r["url"]],
                "output": args.output,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
