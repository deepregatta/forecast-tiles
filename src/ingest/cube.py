"""ForecastCube: the in-memory, provider-independent product of every source.

A cube is plain numpy — per-variable arrays shaped [member?, time, nlat, nlon],
either float32 (NaN = missing) or already quantized to the target integer
dtype (sentinel = missing) — plus the grid geometry, time axes and provenance
needed to build PFT1 tile headers and the run manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from tilekit.codec import DTYPES


@dataclass(frozen=True)
class GridMeta:
    """Regular lat/lon grid, ascending latitude, longitudes in [-180, 180)."""

    lat0: float
    lon0: float
    dlat: float
    dlon: float
    nlat: int
    nlon: int

    def lats(self) -> np.ndarray:
        return self.lat0 + np.arange(self.nlat) * self.dlat

    def lons(self) -> np.ndarray:
        return self.lon0 + np.arange(self.nlon) * self.dlon


@dataclass(frozen=True)
class VariableSpec:
    """Public (header/manifest-facing) description of one encoded variable."""

    name: str
    axis: str
    dtype: str  # "i16" | "i8"
    scale: float
    offset: float = 0.0
    per_member: bool = False

    def public(self) -> dict:
        out: dict = {"name": self.name, "axis": self.axis, "dtype": self.dtype, "scale": self.scale}
        if self.offset:
            out["offset"] = self.offset
        if self.per_member:
            out["per_member"] = True
        return out


def axis_offsets(*segments: tuple[int, int, int]) -> list[int]:
    """Build a forecast-hour axis from (start, stop_inclusive, step) segments.

    Segments must be contiguous and strictly increasing overall, e.g. the
    weather hourly axis is axis_offsets((0, 120, 1), (123, 240, 3)).
    """
    offsets: list[int] = []
    for start, stop, step in segments:
        seg = list(range(start, stop + 1, step))
        if offsets and seg and seg[0] <= offsets[-1]:
            raise ValueError(f"axis segments overlap at {seg[0]} <= {offsets[-1]}")
        offsets.extend(seg)
    return offsets


def cycle_compact(cycle: datetime) -> str:
    return cycle.strftime("%Y%m%dT%H") + "Z"


def cycle_iso(cycle: datetime) -> str:
    return cycle.strftime("%Y-%m-%dT%H:%M") + "Z"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


@dataclass
class ForecastCube:
    layer: str
    model: str
    cycle: datetime
    grid: GridMeta
    time_axes: dict[str, list[int]]  # {axis name: forecast-hour offsets}
    variables: list[VariableSpec]
    arrays: dict[str, np.ndarray]  # [member?, time, nlat, nlon]
    member_count: int = 1
    provenance: dict = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return f"{self.layer}-{cycle_compact(self.cycle)}"

    @property
    def cycle_iso(self) -> str:
        return cycle_iso(self.cycle)

    @property
    def horizon_h(self) -> int:
        return max(offs[-1] for offs in self.time_axes.values())

    @property
    def resolution_deg(self) -> float:
        return self.grid.dlat

    def var(self, name: str) -> VariableSpec:
        return next(v for v in self.variables if v.name == name)

    def expected_shape(self, spec: VariableSpec) -> tuple[int, ...]:
        spatial = (len(self.time_axes[spec.axis]), self.grid.nlat, self.grid.nlon)
        return (self.member_count, *spatial) if spec.per_member else spatial

    def decoded(self, name: str) -> np.ndarray:
        """Variable values as float32 with NaN for missing, whether the stored
        array is float or already quantized."""
        spec = self.var(name)
        arr = self.arrays[name]
        if np.issubdtype(arr.dtype, np.floating):
            return arr.astype(np.float32, copy=False)
        _, sentinel = DTYPES[spec.dtype]
        values = arr.astype(np.float32) * spec.scale + spec.offset
        values[arr == sentinel] = np.nan
        return values

    def header_time_axes(self) -> dict:
        base = self.cycle_iso
        return {name: {"base": base, "offsets_h": offs} for name, offs in self.time_axes.items()}
