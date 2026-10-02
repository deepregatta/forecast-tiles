"""Bounded bilinear remapping of verified projected cell-centre grids.

Directions become earth-relative vectors before interpolation. Four finite
neighbours are required even where one has zero weight. Cells outside the
native centre rectangle are missing, never edge-clamped or extrapolated.
Weights and their footprint depend on registered geometry, not weather data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import lru_cache

import numpy as np

from ingest.sources.openmeteo.registry import Grid, ProjectedGrid


def fingerprint(native: ProjectedGrid, served: Grid) -> str:
    return hashlib.sha256(
        json.dumps({"native": asdict(native), "served": asdict(served)}, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Weights:
    native_shape: tuple[int, int]
    served_shape: tuple[int, int]
    row: np.ndarray
    col: np.ndarray
    fx: np.ndarray
    fy: np.ndarray
    valid: np.ndarray
    fingerprint: str

    def remap(self, values: np.ndarray) -> np.ndarray:
        if values.ndim != 3 or values.shape[1:] != self.native_shape:
            raise ValueError("remap input does not match registered native grid")
        out = np.full((len(values), *self.served_shape), np.nan, dtype=np.float32)
        # One time step per operation: avoid four full time/served-grid arrays.
        for t, data in enumerate(values):
            a = data[self.row, self.col]
            b = data[self.row, self.col + 1]
            c = data[self.row + 1, self.col]
            d = data[self.row + 1, self.col + 1]
            valid = self.valid & np.isfinite(a) & np.isfinite(b) & np.isfinite(c) & np.isfinite(d)
            result = (a * (1 - self.fx) + b * self.fx) * (1 - self.fy)
            result += (c * (1 - self.fx) + d * self.fx) * self.fy
            out[t].ravel()[valid] = result[valid]
        return out


@lru_cache(maxsize=2)
def weights(native: ProjectedGrid, served: Grid) -> Weights:
    import pyproj

    if min(native.nlat, native.nlon) < 2 or min(native.dx, native.dy) <= 0:
        raise ValueError("bilinear remapping needs ascending native axes and a halo")
    lon, lat = np.meshgrid(
        served.lon0 + np.arange(served.nlon) * served.step,
        served.lat0 + np.arange(served.nlat) * served.step,
    )
    transform = pyproj.Transformer.from_crs("EPSG:4326", native.crs, always_xy=True)
    x, y = transform.transform(lon.ravel(), lat.ravel())
    qx, qy = (x - native.x0) / native.dx, (y - native.y0) / native.dy
    # Inverse-transform roundoff at exact source centres, less than 0.02 mm.
    qx = np.where(abs(qx - np.rint(qx)) < 1e-8, np.rint(qx), qx)
    qy = np.where(abs(qy - np.rint(qy)) < 1e-8, np.rint(qy), qy)
    valid = (
        np.isfinite(qx)
        & np.isfinite(qy)
        & (qx >= 0)
        & (qy >= 0)
        & (qx <= native.nlon - 1)
        & (qy <= native.nlat - 1)
    )
    # Safe indices also for masked targets; their values are never admitted.
    qx = np.nan_to_num(qx, nan=0, posinf=0, neginf=0)
    qy = np.nan_to_num(qy, nan=0, posinf=0, neginf=0)
    col = np.clip(np.floor(qx), 0, native.nlon - 2).astype(np.int32)
    row = np.clip(np.floor(qy), 0, native.nlat - 2).astype(np.int32)
    return Weights(
        native_shape=(native.nlat, native.nlon),
        served_shape=(served.nlat, served.nlon),
        row=row,
        col=col,
        fx=(qx - col).astype(np.float32),
        fy=(qy - row).astype(np.float32),
        valid=valid,
        fingerprint=fingerprint(native, served),
    )


def wind_vectors(speed: np.ndarray, direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Meteorological from-direction, clockwise from true north. No rotation:
    CF wind_from_direction already refers to geographic rather than grid axes.
    Input angles are quantized bearings, not projection-relative components.
    """
    if speed.shape != direction.shape or speed.ndim != 3:
        raise ValueError("speed/direction axes differ")
    if np.any(np.isfinite(speed) & (speed < 0)):
        raise ValueError("negative native wind speed")
    if np.any(np.isfinite(direction) & ((direction < 0) | (direction > 360))):
        raise ValueError("native wind direction outside 0..360 degrees")
    u, v = np.empty_like(speed), np.empty_like(speed)
    for t in range(len(speed)):
        theta = np.deg2rad(direction[t])
        u[t] = -speed[t] * np.sin(theta)
        v[t] = -speed[t] * np.cos(theta)
    return u, v
