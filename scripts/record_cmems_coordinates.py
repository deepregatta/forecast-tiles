#!/usr/bin/env python3
"""Record the real GLO12 and IBI coordinate arrays the currents ingests tile.

Writes tests/fixtures/cmems-coordinates.npz: the latitude/longitude arrays in
their stored dtype (GLO12 float32, IBI float64), IBI over the ingest's own
subset box, plus the dataset ids and recording date. The grid-derivation
tests check GridMeta against these rather than against a synthetic grid.

Needs Copernicus Marine credentials (environment or the toolbox's store):

    uv run --extra currents scripts/record_cmems_coordinates.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ingest.sources import cmems, ibi  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cmems-coordinates.npz"


def main() -> None:
    import copernicusmarine

    glo12 = copernicusmarine.open_dataset(dataset_id=cmems.DATASET_ID, variables=["uo", "vo"])
    ibi_id = ibi.resolve_dataset_id(copernicusmarine)
    regional = copernicusmarine.open_dataset(
        dataset_id=ibi_id,
        variables=["uo", "vo"],
        minimum_longitude=ibi.MIN_LON,
        maximum_longitude=ibi.MAX_LON,
        minimum_latitude=ibi.MIN_LAT,
        maximum_latitude=ibi.MAX_LAT,
    )
    arrays = {
        "glo12_latitude": np.asarray(glo12["latitude"].values),
        "glo12_longitude": np.asarray(glo12["longitude"].values),
        "ibi_latitude": np.asarray(regional["latitude"].values),
        "ibi_longitude": np.asarray(regional["longitude"].values),
    }
    np.savez_compressed(
        OUT,
        **arrays,
        glo12_dataset_id=np.array(cmems.DATASET_ID),
        ibi_dataset_id=np.array(ibi_id),
        recorded=np.array(datetime.now(timezone.utc).strftime("%Y-%m-%d")),
    )
    for name, values in arrays.items():
        print(f"{name}: {values.dtype} x{values.size} [{values[0]}, {values[-1]}]")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
