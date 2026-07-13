"""Cube validation — runs BEFORE anything is uploaded. Any failure aborts the
run (spec: publish protocol step 2)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ingest.cube import ForecastCube

# Physical plausibility ranges per variable name (decoded units). A small
# tolerance of one quantization step is added on each side when checking.
PHYSICAL_RANGES: dict[str, tuple[float, float]] = {
    "wind_u_kt": (-150.0, 150.0),
    "wind_v_kt": (-150.0, 150.0),
    "gust_kt": (0.0, 200.0),
    "wind_kt_mean": (0.0, 200.0),
    "wind_kt_anom": (-25.0, 25.0),
    "gust_kt_mean": (0.0, 250.0),
    "gust_kt_anom": (-25.0, 25.0),
    "visibility_m": (0.0, 100_000.0),
    "cape_jkg": (0.0, 10_000.0),
    "temp_c": (-90.0, 60.0),
    "dew_point_c": (-90.0, 60.0),
    "precip_mm": (0.0, 1_000.0),
    "hs_m": (0.0, 30.0),
    "wind_wave_h_m": (0.0, 30.0),
    "swell_h_m": (0.0, 30.0),
    "period_s": (0.0, 40.0),
    "wind_wave_period_s": (0.0, 40.0),
    "swell_period_s": (0.0, 40.0),
    "dir_deg": (0.0, 360.0),
    "wind_wave_dir_deg": (0.0, 360.0),
    "swell_dir_deg": (0.0, 360.0),
    "cur_u_kt": (-15.0, 15.0),
    "cur_v_kt": (-15.0, 15.0),
}

# Fraction of points where hypot(wind) <= gust must hold (spec: >= 99 %).
GUST_CONSISTENCY_MIN = 0.99
# Tolerance for the gust check. GFS diagnoses GUST slightly below the 10 m
# wind speed at ~9 % of points (measured live 2026-07-12T18Z: median violation
# ~0.2 kt, p99 ~1.3 kt, pass rate 0.997 at 2 kt across f000..f240). The check
# exists to catch unit/scale bugs (which shift gust by ~2x), so a 2 kt slack
# keeps it meaningful without tripping on provider physics.
GUST_TOLERANCE = 2.0  # kt

# Wind/gust variable triplets checked for gust >= wind speed.
_GUST_CHECKS = [
    ("wind_u_kt", "wind_v_kt", "gust_kt"),
]


@dataclass
class ValidationReport:
    checks_passed: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        if passed:
            self.checks_passed.append(name)
        else:
            self.failures.append(f"{name}: {detail}" if detail else name)

    def summary(self) -> str:
        lines = [f"validation: {len(self.checks_passed)} passed, {len(self.failures)} failed"]
        lines += [f"  FAIL {f}" for f in self.failures]
        return "\n".join(lines)


def validate_cube(
    cube: ForecastCube,
    *,
    max_missing: float = 0.05,
    expected_axes: dict[str, list[int]] | None = None,
) -> ValidationReport:
    """Validate step coverage, physical ranges, gust consistency and missing
    fraction. `max_missing` is the allowed missing fraction (ocean-only layers
    pass a higher value)."""
    report = ValidationReport()

    # --- axes: monotonically increasing, and complete vs the committed axes
    for name, offs in cube.time_axes.items():
        mono = len(offs) > 0 and all(b > a for a, b in zip(offs, offs[1:]))
        report.check(
            f"axis_monotonic[{name}]", mono, f"offsets not strictly increasing: {offs[:5]}…"
        )
    if expected_axes is not None:
        for name, expected in expected_axes.items():
            got = cube.time_axes.get(name)
            report.check(
                f"axis_complete[{name}]",
                got == expected,
                f"expected {len(expected)} steps, got {len(got) if got else 0}",
            )

    # --- per-variable: presence, shape (= complete step coverage), range, missing
    usable: set[str] = set()  # present with the expected shape
    for spec in cube.variables:
        arr = cube.arrays.get(spec.name)
        if arr is None:
            report.check(f"present[{spec.name}]", False, "array missing from cube")
            continue
        expected_shape = cube.expected_shape(spec)
        report.check(
            f"step_coverage[{spec.name}]",
            tuple(arr.shape) == expected_shape,
            f"shape {tuple(arr.shape)} != expected {expected_shape}",
        )
        if tuple(arr.shape) != expected_shape:
            continue
        usable.add(spec.name)

        values = cube.decoded(spec.name)
        finite = values[~np.isnan(values)]
        lo, hi = PHYSICAL_RANGES.get(spec.name, (-np.inf, np.inf))
        tol = spec.scale
        in_range = finite.size == 0 or (finite.min() >= lo - tol and finite.max() <= hi + tol)
        report.check(
            f"physical_range[{spec.name}]",
            bool(in_range),
            f"[{finite.min():.3g}, {finite.max():.3g}] outside [{lo}, {hi}]"
            if finite.size
            else "no data",
        )

        missing_frac = float(np.isnan(values).mean())
        report.check(
            f"missing_fraction[{spec.name}]",
            missing_frac <= max_missing,
            f"{missing_frac:.3f} > allowed {max_missing}",
        )

    # --- gust >= wind speed at >= 99 % of jointly-valid points
    # (only checkable on variables that are present with the expected shape;
    # missing/misshapen ones already failed above)
    names = usable

    def check_gust(speed: np.ndarray, gust: np.ndarray, label: str) -> None:
        valid = ~(np.isnan(speed) | np.isnan(gust))
        if not valid.any():
            report.check(label, False, "no jointly valid points")
            return
        frac_ok = float((speed[valid] <= gust[valid] + GUST_TOLERANCE).mean())
        report.check(
            label,
            frac_ok >= GUST_CONSISTENCY_MIN,
            f"speed<=gust at {frac_ok:.4f} < {GUST_CONSISTENCY_MIN}",
        )

    for u_name, v_name, g_name in _GUST_CHECKS:
        if {u_name, v_name, g_name} <= names:
            u, v, g = (cube.decoded(n) for n in (u_name, v_name, g_name))
            check_gust(np.hypot(u, v), g, f"gust_ge_wind[{g_name}]")
    if {"wind_kt_mean", "gust_kt_mean"} <= names:  # ensemble: means are speeds already
        check_gust(
            cube.decoded("wind_kt_mean"),
            cube.decoded("gust_kt_mean"),
            "gust_ge_wind[gust_kt_mean]",
        )

    # --- member consistency for per-member variables
    per_member = [v for v in cube.variables if v.per_member]
    if per_member:
        report.check(
            "member_count",
            all(cube.arrays[v.name].shape[0] == cube.member_count for v in per_member),
            f"per-member arrays disagree with member_count={cube.member_count}",
        )

    return report
