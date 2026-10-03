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
    footprint: np.ndarray | None = None,
    max_interior_missing: float | None = None,
    allowed_missing_steps: dict[str, list[int]] | None = None,
) -> ValidationReport:
    """Validate step coverage, physical ranges, gust consistency and missing
    fraction. `max_missing` applies to every variable/member/time slice over
    the grid, preserving land/ice masks with a higher limit for ocean layers.

    A regional product passes `max_interior_missing` instead: the missing
    fraction is then checked at every step, inside its registered `footprint`
    (the whole grid when None), so an expected outline is accepted while a
    hole in it or an empty step is not. Steps listed in
    the cube's source-declared or caller-supplied `allowed_missing_steps[name]`
    (forecast-hour offsets, e.g. gust at +0 h) are exempt. Cells outside a
    regional footprint must carry no data at all."""
    report = ValidationReport()
    interior = None
    if max_interior_missing is not None:
        interior = (
            np.ones((cube.grid.nlat, cube.grid.nlon), dtype=bool)
            if footprint is None
            else np.asarray(footprint, dtype=bool)
        )
        if interior.shape != (cube.grid.nlat, cube.grid.nlon):
            report.check(
                "footprint_shape",
                False,
                f"footprint {interior.shape} != grid {(cube.grid.nlat, cube.grid.nlon)}",
            )
            return report
        if not interior.any():
            report.check("footprint_nonempty", False, "footprint contains no expected cells")
            return report

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

        allowed = set(cube.allowed_missing_steps.get(spec.name, ()))
        allowed.update((allowed_missing_steps or {}).get(spec.name, ()))
        _check_missing_slices(
            report,
            spec.name,
            values,
            cube.time_axes[spec.axis],
            max_missing if interior is None else max_interior_missing,
            allowed,
            interior,
        )

        # a statistic (e.g. max over the last N h) needs a window for every
        # step that carries data, or consumers can't label the interval
        if spec.statistic is not None:
            windows = spec.statistic.window_h
            time_dim = 1 if spec.per_member else 0
            other = tuple(d for d in range(values.ndim) if d != time_dim)
            has_data = ~np.isnan(values).all(axis=other)
            unlabeled = [
                off
                for off, w, data in zip(cube.time_axes[spec.axis], windows, has_data)
                if data and not (w is not None and w > 0)
            ]
            report.check(
                f"statistic_windows[{spec.name}]",
                spec.statistic.kind == "max" and len(windows) == len(has_data) and not unlabeled,
                f"kind {spec.statistic.kind!r}, {len(windows)} windows for "
                f"{len(has_data)} steps, steps with data but no window: {unlabeled[:5]}",
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
            axes = [cube.var(n).axis for n in (u_name, v_name, g_name)]
            shapes = [tuple(cube.arrays[n].shape) for n in (u_name, v_name, g_name)]
            if len(set(axes)) > 1 or len(set(shapes)) > 1:
                # compared point for point, so they must share one axis
                report.check(
                    f"gust_ge_wind[{g_name}]",
                    False,
                    f"wind and gust are on different axes or shapes: "
                    f"{dict(zip((u_name, v_name, g_name), zip(axes, shapes)))}",
                )
                continue
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


def _check_missing_slices(
    report: ValidationReport,
    name: str,
    values: np.ndarray,
    offsets: list[int],
    limit: float,
    allowed: set[int],
    interior: np.ndarray | None,
) -> None:
    """Each member/time must be usable within the expected spatial domain."""
    missing = (
        np.isnan(values).mean(axis=(-2, -1))
        if interior is None
        else np.isnan(values[..., interior]).mean(axis=-1)
    )
    over = [
        (member, off, float(frac))
        for member, fractions in enumerate(np.atleast_2d(missing))
        for off, frac in zip(offsets, fractions)
        if off not in allowed and (frac > limit or frac == 1)
    ]
    check = "missing_fraction" if interior is None else "interior_missing"
    domain = "grid" if interior is None else "footprint"
    report.check(
        f"{check}[{name}]",
        not over,
        f"{len(over)} required slices unusable or over {limit:.2%} missing inside the {domain}: "
        + ", ".join(
            (f"member {member} " if values.ndim == 4 else "") + f"+{off} h {frac:.2%}"
            for member, off, frac in over[:5]
        ),
    )
    if interior is not None and (~interior).any():
        outside = int((~np.isnan(values[..., ~interior])).sum())
        report.check(
            f"exterior_masked[{name}]",
            outside == 0,
            f"{outside} values outside the footprint",
        )
