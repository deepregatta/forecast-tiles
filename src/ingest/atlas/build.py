"""Read one SHOM atlas NetCDF4 file and write its TCA1 grid.

The file is read with h5py (NetCDF4 is HDF5), so the app never needs HDF5:
it reads the TCA1 grid instead. What is read, and refused when it is not
there: `U` and `V` (m/s, eastward/northward, `_FillValue` -9999 or NaN) of
shape (coefficient, time, depth 1, latitude, longitude); `coeff` [45, 95];
`time` in minutes from 6 h before the reference high water (0 to 720);
regular `latitude`/`longitude` axes, ascending; the global attributes
`Portref`, `code_du_port_de_reference`, `isBM` 0 (timed from high water),
`grid_type` regular and `sysgeo` WGS84.

Kept steps: every 30 minutes where the file is finer (5 or 30 minutes), every
step where it is coarser (Golfe normand-breton is hourly). Values are rounded
to 1 mm/s. A node with either component missing is missing in both.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from ingest.atlas import codec
from ingest.atlas.sources import ATTRIBUTION, LIMITATION, Grid

KEEP_STEP_MIN = 30
FILL = -9999.0
# Coordinates are stored rounded (Bretagne Nord's to 0.001 deg on a 0.0045 deg
# step); a node further than this share of a step from the regular line is refused.
AXIS_TOLERANCE = 0.25


class BuildError(RuntimeError):
    pass


def _attr(f, name):
    value = f.attrs.get(name)
    if value is None:
        raise BuildError(f"missing global attribute {name}")
    if isinstance(value, bytes):
        return value.decode("latin-1")
    if isinstance(value, np.ndarray):
        return value.tolist()[0] if value.size == 1 else value.tolist()
    return value


def regular_axis(values: np.ndarray, name: str) -> tuple[float, float]:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 1 or len(values) < 2 or not np.all(np.isfinite(values)):
        raise BuildError(f"{name} axis is not a finite 1-D axis")
    step = (values[-1] - values[0]) / (len(values) - 1)
    if not step > 0:
        raise BuildError(f"{name} axis does not rise")
    line = values[0] + step * np.arange(len(values))
    if np.max(np.abs(values - line)) > AXIS_TOLERANCE * step:
        raise BuildError(f"{name} axis is not regular")
    return float(values[0]), float(step)


def kept_steps(time_min: np.ndarray) -> list[int]:
    """Indices of the steps kept: on the half hour, from 0 to 720 minutes."""
    t = [int(x) for x in time_min]
    if t != sorted(set(t)) or t[0] != 0 or t[-1] != 2 * codec.MAX_OFFSET_MIN:
        raise BuildError(f"time axis must rise from 0 to 720 minutes, got {t[:3]}..{t[-1:]}")
    native = t[1] - t[0]
    if any(b - a != native for a, b in zip(t, t[1:])):
        raise BuildError("time axis is not regular")
    every = max(KEEP_STEP_MIN, native)
    if every % native:
        raise BuildError(f"a {native} min step does not divide {every} min")
    return [i for i, m in enumerate(t) if m % every == 0]


def read(path: Path, grid: Grid) -> tuple[dict, np.ndarray]:
    import h5py

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if grid.file_sha256 and digest != grid.file_sha256:
        raise BuildError(f"{path.name} is not the file pinned for {grid.id}")
    with h5py.File(path, "r") as f:
        if _attr(f, "grid_type") != "regular" or _attr(f, "sysgeo") != "WGS84":
            raise BuildError("not a regular WGS84 grid")
        if int(_attr(f, "isBM")) != 0:
            raise BuildError("timed from low water; only high-water atlases are carried")
        port, code = str(_attr(f, "Portref")), str(_attr(f, "code_du_port_de_reference"))
        if (port, code) != (grid.port, grid.shom_code):
            raise BuildError(f"reference port {port} {code}, expected {grid.port} {grid.shom_code}")
        if [int(c) for c in f["coeff"][:]] != list(codec.COEFFICIENTS):
            raise BuildError("coefficients are not [45, 95]")
        for name in ("U", "V"):
            units = f[name].attrs.get("units", b"")
            if (units.decode("latin-1") if isinstance(units, bytes) else str(units)) != "m/s":
                raise BuildError(f"{name} is not in m/s")
        lat0, dlat = regular_axis(f["latitude"][:], "latitude")
        lon0, dlon = regular_axis(f["longitude"][:], "longitude")
        steps = kept_steps(f["time"][:])
        shape = f["U"].shape
        rows, columns = len(f["latitude"]), len(f["longitude"])
        if shape != (2, len(f["time"]), 1, rows, columns) or f["V"].shape != shape:
            raise BuildError(f"U/V shape {shape} is not (2, time, 1, lat, lon)")
        offsets = [int(f["time"][i]) - codec.MAX_OFFSET_MIN for i in steps]
        values = np.empty((2, len(steps), rows, columns, 2), dtype=np.int16)
        for k, name in enumerate(("U", "V")):
            for j, i in enumerate(steps):
                raw = f[name][:, i, 0, :, :].astype(np.float64)
                bad = ~np.isfinite(raw) | (raw == FILL)
                if np.any(np.abs(np.where(bad, 0, raw)) > 32.0):
                    raise BuildError(f"{name} holds a speed over 32 m/s")
                q = np.rint(np.where(bad, 0, raw) / codec.SCALE_M_S).astype(np.int16)
                values[:, j, :, :, k] = np.where(bad, codec.MISSING, q)
        release = str(_attr(f, "Release_date"))
        lineage = str(_attr(f, "source"))
    either = (values[..., 0] == codec.MISSING) | (values[..., 1] == codec.MISSING)
    values[either] = codec.MISSING
    header = {
        "spec": codec.SPEC,
        "schema_version": codec.SCHEMA_VERSION,
        "grid": grid.id,
        "zone": grid.zone,
        "title": grid.title,
        "resolution_m": grid.resolution_m,
        "reference_port": {"name": grid.port, "shom_code": grid.shom_code, "event": "high_water"},
        "lat0": round(lat0, 9),
        "dlat": round(dlat, 9),
        "rows": rows,
        "lon0": round(lon0, 9),
        "dlon": round(dlon, 9),
        "columns": columns,
        "coefficients": list(codec.COEFFICIENTS),
        "offsets_min": offsets,
        "scale_m_s": codec.SCALE_M_S,
        "missing": codec.MISSING,
        "source": {
            "file": path.name,
            "sha256": digest,
            "archive_sha256": grid.archive_sha256,
            "release_date": release,
            "lineage": lineage,
        },
        "attribution": ATTRIBUTION,
        "limitation": LIMITATION,
        "not_for_navigation": True,
    }
    return header, values


def build(path: Path, grid: Grid, out: Path) -> dict:
    """Write `{grid}.tca1.gz` and `{grid}.report.json` into `out`."""
    header, values = read(path, grid)
    data = codec.encode(header, values)
    # read back with the decoder the checks use: what is written is what is read
    again, decoded = codec.decode(data)
    if again != header or not np.array_equal(decoded, values):
        raise BuildError("the grid does not read back as written")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{grid.id}.tca1.gz").write_bytes(data)
    wet = values[..., 0] != codec.MISSING
    report = {
        "grid": grid.id,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bounds": bounds(header),
        "rows": header["rows"],
        "columns": header["columns"],
        "steps": len(header["offsets_min"]),
        "wet_share": round(float(wet.mean()), 4),
        "max_speed_m_s": round(
            float(np.max(np.hypot(*np.moveaxis(np.where(wet[..., None], values, 0), -1, 0))))
            * codec.SCALE_M_S,
            3,
        ),
        "source": header["source"],
    }
    (out / f"{grid.id}.report.json").write_text(json.dumps(report, indent=1) + "\n")
    return report


def bounds(header: dict) -> list[float]:
    """West, south, east, north of the outer nodes."""
    return [
        header["lon0"],
        header["lat0"],
        round(header["lon0"] + header["dlon"] * (header["columns"] - 1), 9),
        round(header["lat0"] + header["dlat"] * (header["rows"] - 1), 9),
    ]
