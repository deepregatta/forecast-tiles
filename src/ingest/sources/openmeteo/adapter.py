"""Whole-file regional data -> a validated ForecastCube.

Require registered files, timestamps, units and source geometry. Align fields
by timestamp; convert UKV from-direction to geographic vectors before bounded
remapping. AROME/ICON-EU initial gust is missing, while UKV preserves its
instantaneous +0 h diagnostic. Verified maxima retain their source windows.
Check the pinned footprint and record transformations; never synthesize gust
or clamp data to pass validation.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from ingest.cube import ForecastCube, Statistic, VariableSpec, utcnow_iso
from ingest.sources.base import MS_TO_KT
from ingest.sources.openmeteo import catalog, reader
from ingest.sources.openmeteo.grids import grid_meta, load_footprint
from ingest.sources.openmeteo.reader import ObjectRecord, SourceError
from ingest.sources.openmeteo.registry import ADAPTER_VERSION, Product
from ingest.validate import ValidationReport, validate_cube

WIND_U = "wind_u_kt"
WIND_V = "wind_v_kt"
GUST = "gust_kt"


@dataclass
class SourceRun:
    """What a cube was built from, kept for the pre-publication recheck."""

    meta: ObjectRecord
    files: list[ObjectRecord]


def include_gust(product: Product, assume_gust_windows: bool = False) -> bool:
    return product.gust_windows.verified or assume_gust_windows


def gust_windows(product: Product, axis: list[int]) -> tuple[int | None, ...]:
    """One window per wind-axis step; None where gust carries no value."""
    return tuple(
        None if lead < product.gust_first_lead_h else product.gust_windows.window(lead)
        for lead in axis
    )


def align(
    decoded: reader.Decoded, targets: list[datetime], *, allowed_absent: set[datetime], name: str
) -> np.ndarray:
    """Pick `targets` from a decoded field by timestamp. A target the file lacks
    is all-missing when allowed, an error otherwise."""
    index = {t: i for i, t in enumerate(decoded.times)}
    absent = [t for t in targets if t not in index and t not in allowed_absent]
    if absent:
        raise SourceError(f"{name}: no data at {[f'{t:%d %HZ}' for t in absent[:5]]}")
    nt = len(targets)
    if [index.get(t) for t in targets] == list(range(nt)) and len(decoded.times) == nt:
        return decoded.values  # already exactly the target axis
    out = np.full((nt, *decoded.values.shape[1:]), np.nan, dtype=np.float32)
    for k, t in enumerate(targets):
        if t in index:
            out[k] = decoded.values[index[t]]
    return out


def apply_footprint(
    arrays: dict[str, np.ndarray], footprint: np.ndarray | None, max_exterior_fraction: float
) -> dict:
    """Mask cells outside the footprint, refusing a run whose data spill past
    it by more than the registered tolerance (a changed or shifted grid)."""
    if footprint is None:
        return {"footprint": None}
    exterior = ~footprint
    n_ext = int(exterior.sum())
    worst = 0
    for name, values in arrays.items():
        per_step = (~np.isnan(values[:, exterior])).sum(axis=1)
        step_max = int(per_step.max()) if per_step.size else 0
        if n_ext and step_max / n_ext > max_exterior_fraction:
            t = int(np.argmax(per_step))
            raise SourceError(
                f"{name}: {step_max} valid cells outside the registered footprint at step "
                f"index {t} (tolerance {max_exterior_fraction:.2%} of {n_ext}): footprint changed"
            )
        worst = max(worst, step_max)
        values[:, exterior] = np.nan
    return {"exterior_cells": n_ext, "exterior_valid_max_per_step": worst}


def to_cube(
    product: Product,
    cycle: datetime,
    fields: dict[str, reader.Decoded],
    *,
    source: SourceRun,
    meta_created_at: str = "",
) -> ForecastCube:
    """Build from u/v or speed/direction, and optional verified gust."""
    axis = list(product.axes[cycle.hour])
    targets = catalog.lead_times(product, cycle)
    gust_absent = {t for t, lead in zip(targets, axis) if lead < product.gust_first_lead_h}
    with_gust = "gust" in fields
    if product.wind_encoding == "speed_direction":
        from ingest.sources.openmeteo.projected import wind_vectors

        speed, direction = (
            align(fields[role], targets, allowed_absent=set(), name=product.files[role])
            for role in ("speed", "direction")
        )
        u, v = wind_vectors(speed, direction)
        arrays = {WIND_U: u, WIND_V: v}
        del speed, direction
        # These originals are no longer needed. Keeping them alive alongside
        # native vectors and all served fields would double the working set.
        del fields["speed"], fields["direction"]
    elif product.wind_encoding == "uv":
        arrays = {
            WIND_U: align(fields["u"], targets, allowed_absent=set(), name=product.files["u"]),
            WIND_V: align(fields["v"], targets, allowed_absent=set(), name=product.files["v"]),
        }
    else:
        raise SourceError(f"{product.layer}: unsupported wind encoding")
    if with_gust:
        arrays[GUST] = align(
            fields["gust"], targets, allowed_absent=gust_absent, name=product.files["gust"]
        )
    remap_note = None
    if product.source_grid is not None:
        from dataclasses import asdict

        from ingest.sources.openmeteo.projected import weights

        mapping = weights(product.source_grid, product.grid)
        footprint = load_footprint(product)
        if footprint is None or not np.array_equal(
            mapping.valid.reshape(mapping.served_shape), footprint
        ):
            raise SourceError(f"{product.layer}: remapping footprint differs from registered mask")
        for name in list(arrays):
            values = arrays[name]
            for t, data in enumerate(values):
                if np.isinf(data).any() or (
                    np.isnan(data).mean() > product.max_interior_missing
                    and axis[t] >= (product.gust_first_lead_h if name == GUST else 0)
                ):
                    raise SourceError(
                        f"{name}: unexpected native hole/nonfinite value at +{axis[t]} h"
                    )
            arrays[name] = mapping.remap(values)
        del values
        remap_note = {
            "fingerprint": mapping.fingerprint,
            "method": "bilinear earth-relative u/v and instantaneous gust; four finite neighbours",
            "extrapolation": False,
            "crs_correction": "native Met Office ellipsoid replaces bulk-declared sphere",
            "native": asdict(product.source_grid),
        }
    mask_note = apply_footprint(
        arrays, load_footprint(product), product.max_exterior_valid_fraction
    )
    for values in arrays.values():
        values *= np.float32(MS_TO_KT)

    variables = [
        VariableSpec(WIND_U, product.axis_name, "i16", 0.01),
        VariableSpec(WIND_V, product.axis_name, "i16", 0.01),
    ]
    gw = product.gust_windows
    if with_gust and product.gust_kind == "instant":
        if not gw.verified:
            raise SourceError(f"{product.layer}: instantaneous gust semantics are unverified")
        variables.append(VariableSpec(GUST, product.axis_name, "i16", 0.1))
        gust_note = f"instantaneous diagnostic at validity time, no maximum window; {gw.evidence}"
    elif with_gust:
        windows = gust_windows(product, axis)
        if any(w is None for w, lead in zip(windows, axis) if lead >= product.gust_first_lead_h):
            raise SourceError(f"{product.layer}: no registered gust window for every step")
        variables.append(
            VariableSpec(GUST, product.axis_name, "i16", 0.1, statistic=Statistic("max", windows))
        )
        gust_note = (
            f"max over the preceding window_h hours per step; {gw.evidence}"
            if gw.verified
            else f"ASSUMED registry windows for measurement only, unverified: {gw.evidence}"
        )
    else:
        gust_note = f"omitted, wind-only run: gust window unverified ({gw.evidence})"
    if product.footprint:
        mask_note |= {"footprint": product.footprint, "footprint_sha256": product.footprint_sha256}

    g = product.grid
    geometry = f"regular {g.step:g}° lat/lon, {g.nlat}x{g.nlon} from {g.lat0}N {g.lon0}E"
    conversion = "m/s x 1.943844 -> kt; [lat, lon, time] -> [time, lat, lon]"
    if remap_note:
        conversion += "; speed/from-direction -> earth-relative u/v before bilinear remapping"
    return ForecastCube(
        layer=product.layer,
        model=product.model,
        cycle=cycle,
        grid=grid_meta(g),
        time_axes={product.axis_name: axis},
        variables=variables,
        arrays=arrays,
        member_count=1,
        provenance={
            "source": f"{product.originator} via Open-Meteo bulk data_run ({product.domain})",
            "originator": product.originator,
            "distributor": "Open-Meteo open-data bucket (openmeteo.s3.amazonaws.com)",
            "attribution": product.attribution,
            "licence": product.data_licence,
            "meta": source.meta.public() | {"created_at": meta_created_at},
            "source_objects": [r.public() for r in source.files],
            "source_digest": catalog.source_digest([source.meta, *source.files]),
            "adapter_version": ADAPTER_VERSION,
            "precision": product.source_precision,
            "conversions": conversion,
            "grid": {
                "native": remap_note["native"] if remap_note else geometry,
                "served": geometry,
            },
            **({"remapping": remap_note} if remap_note else {}),
            "mask": mask_note,
            "capabilities": ["wind", "gust"] if with_gust else ["wind"],
            "gust": gust_note,
            "fetched_at": utcnow_iso(),
        },
        tile_deg=product.tile_deg,
        path_label=g.label,
    )


def build_cube(
    product: Product,
    cycle: datetime,
    *,
    workdir: Path | None = None,
    assume_gust_windows: bool = False,
    metrics: dict | None = None,
    session=None,
    sleep=time.sleep,
) -> tuple[ForecastCube, SourceRun]:
    """Download, decode and convert one complete run."""
    kwargs = {"sleep": sleep} | ({"session": session} if session is not None else {})
    roles = ["speed", "direction"] if product.wind_encoding == "speed_direction" else ["u", "v"]
    roles += ["gust"] if include_gust(product, assume_gust_windows) else []
    meta = catalog.fetch_meta(product, cycle, **kwargs)
    if metrics is not None:
        metrics.update(
            meta=meta.record.public(),
            metadata_created_at=meta.created_at,
            metadata_lag_s=None,
            completed_objects=[],
            completed_download_bytes=0,
            downloads_complete=False,
            download_s=0.0,
            decode_s=0.0,
            temporary_file_peak_bytes=0,
        )
        try:
            completed = datetime.fromisoformat(meta.created_at.replace("Z", "+00:00"))
            if completed.tzinfo is not None and completed >= cycle:
                metrics["metadata_lag_s"] = (completed - cycle).total_seconds()
        except (AttributeError, TypeError, ValueError):
            pass  # optional timing metadata is unknown, never a zero lag
    catalog.require_complete(product, meta, roles)

    with tempfile.TemporaryDirectory(dir=workdir, prefix=f"{product.layer}-") as tmp:
        if metrics is not None:
            metrics["temporary_disk_free_bytes_before"] = shutil.disk_usage(tmp).free
        records: list[ObjectRecord] = []
        fields: dict[str, reader.Decoded] = {}
        for role in roles:  # sequential: three GETs, then each file freed once decoded
            key = catalog.file_key(product, cycle, role)
            path = Path(tmp) / f"{product.files[role]}.om"
            download_started = time.perf_counter()
            try:
                record = reader.download(key, path, **kwargs)
            finally:
                if metrics is not None:
                    metrics["download_s"] += time.perf_counter() - download_started
                    metrics["temporary_file_peak_bytes"] = max(
                        metrics["temporary_file_peak_bytes"],
                        path.stat().st_size if path.exists() else 0,
                    )
            if metrics is not None:
                metrics["completed_objects"].append(record.public())
                metrics["completed_download_bytes"] += record.bytes
                metrics["downloads_complete"] = len(metrics["completed_objects"]) == len(roles)
            records.append(record)
            print(f"ingest {product.layer}: {key} {record.bytes / 1e6:.1f} MB", flush=True)
            decode_started = time.perf_counter()
            try:
                fields[role] = reader.decode(
                    path,
                    grid=product.source_grid or product.grid,
                    unit=product.direction_unit if role == "direction" else product.source_unit,
                    reference=cycle,
                )
            finally:
                if metrics is not None:
                    metrics["decode_s"] += time.perf_counter() - decode_started
            path.unlink()
        source = SourceRun(meta=meta.record, files=records)
        conversion_started = time.perf_counter()
        cube = to_cube(
            product,
            cycle,
            fields,
            source=source,
            meta_created_at=meta.created_at,
        )
        if metrics is not None:
            metrics["conversion_s"] = time.perf_counter() - conversion_started
    return cube, source


def validate(product: Product, cube: ForecastCube) -> ValidationReport:
    """The shared cube validation with this product's footprint and limits."""
    axis = cube.time_axes[product.axis_name]
    return validate_cube(
        cube,
        expected_axes={product.axis_name: list(product.axes[cube.cycle.hour])},
        footprint=load_footprint(product),
        max_interior_missing=product.max_interior_missing,
        allowed_missing_steps={GUST: [lead for lead in axis if lead < product.gust_first_lead_h]},
    )
