"""Open-Meteo bulk products: one frozen definition per regional layer.

Every per-layer setting the CLI and publisher look up for an existing layer
(LAYERS, MAX_MISSING, POLL_SECONDS, CADENCE_HOURS, SKIP_WHEN_NOT_AVAILABLE)
comes from here for a regional one, through the accessors in ingest.cli and
ingest.publish. A layer registered here is never written to the root
`latest.json`: its pointer is `latest-regional.json`, and the two allowlists
are disjoint.

Values are what was measured from live `data_run/` files on 2026-10-02 (see
each product's `evidence`), not upstream documentation alone. A field that
could not be checked against a primary source says so and blocks what
depends on it: an unverified gust window means a wind-only run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from ingest.cube import axis_offsets

BUCKET_URL = "https://openmeteo.s3.amazonaws.com"
ADAPTER_VERSION = 1


@dataclass(frozen=True)
class Grid:
    """A regular geographic grid exactly as Open-Meteo stores it: ascending
    latitude rows from lat0, longitude columns from lon0, `cells_per_degree`
    cells per degree on both axes."""

    lat0: float
    lon0: float
    cells_per_degree: int
    nlat: int
    nlon: int

    @property
    def step(self) -> float:
        return 1 / self.cells_per_degree

    @property
    def label(self) -> str:
        """Explicit path label (grid-0p025), never inferred from resolution."""
        text = f"{self.step:.4f}".rstrip("0")
        return "grid-" + text.replace(".", "p")


@dataclass(frozen=True)
class GustWindows:
    """The interval, in hours, of the maximum reported at each lead hour.

    `segments` are (first_lead, last_lead, window_h). Output step spacing is
    not evidence of the interval, so `verified` stays False until a primary
    source (the upstream GRIB's stepRange, or the provider's product
    documentation) has been read; until then the adapter publishes wind only
    (scripts/probe_openmeteo.py can still measure with them, labelled assumed).
    """

    segments: tuple[tuple[int, int, int], ...]
    verified: bool
    evidence: str

    def window(self, lead_h: int) -> int | None:
        for first, last, window_h in self.segments:
            if first <= lead_h <= last:
                return window_h
        return None


@dataclass(frozen=True)
class Product:
    layer: str
    model: str  # manifest/tile `model`
    domain: str  # Open-Meteo data_run/<domain>/
    originator: str  # the weather service that ran the model
    attribution: str
    data_licence: str
    grid: Grid
    axis_name: str
    # Lead hours of the wind fields per published cycle (UTC hour -> axis).
    axes: dict[int, tuple[int, ...]]
    # Files in the run directory, by role.
    files: dict[str, str]
    source_unit: str
    source_precision: str
    gust_first_lead_h: int  # gust is absent before this lead (no +0 h maximum)
    gust_windows: GustWindows
    # Versioned expected footprint (ingest.sources.openmeteo.grids), or None
    # when every cell carries data.
    footprint: str | None
    footprint_sha256: str | None
    # Validation inside the footprint, per field and time step.
    max_interior_missing: float
    # Valid cells outside the footprint tolerated per step before the run is
    # refused as a changed footprint (a shifted mask shows thousands).
    max_exterior_valid_fraction: float
    # Scheduling: lag after the cycle and how long a dispatched run waits.
    lag_minutes: dict[int, int]
    wait_minutes: int
    cadence_hours: int
    poll_seconds: int
    lookback_cycles: int
    tile_deg: int
    max_run_bytes: int  # compressed run cap b_i, refused before upload
    production_enabled: bool
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cycles(self) -> tuple[int, ...]:
        return tuple(sorted(self.axes))


WIND_FILES = {
    "u": "wind_u_component_10m",
    "v": "wind_v_component_10m",
    "gust": "wind_gusts_10m",
}

AROME = Product(
    layer="weather-arome",
    model="meteofrance_arome_france_0p025",
    domain="meteofrance_arome_france0025",
    originator="Météo-France AROME France 0.025°",
    attribution="Météo-France AROME via Open-Meteo (open-data, CC BY 4.0)",
    data_licence="CC BY 4.0 (Open-Meteo bulk catalogue); Météo-France open data",
    grid=Grid(lat0=37.5, lon0=-12.0, cells_per_degree=40, nlat=717, nlon=1121),
    axis_name="hourly",
    axes={h: tuple(axis_offsets((0, 51, 1))) for h in (3, 9, 15, 21)},
    files=WIND_FILES,
    source_unit="m/s",
    source_precision="0.1 m/s (Open-Meteo scale_factor 10)",
    gust_first_lead_h=1,
    gust_windows=GustWindows(
        segments=((1, 51, 1),),
        verified=True,
        evidence=(
            "Météo-France SP1 GRIB2, 2026-10-02T09Z: max_i10fg/10efg/10nfg at +13..18 "
            "and +49..51 h all have endStep-startStep=1 h. Also checked +1..6 h at 12Z. "
            "Recorded in tests/fixtures/openmeteo/gust-windows/arome.json"
        ),
    ),
    footprint="meteofrance_arome_france0025.v1",
    footprint_sha256="ad74c408e154c8139f28dad8ed5e74ccf1312b5d3c995e7f1269f4ae1d9c0920",
    max_interior_missing=0.005,
    max_exterior_valid_fraction=0.0005,
    lag_minutes={3: 165, 9: 255, 15: 225, 21: 255},
    wait_minutes=90,
    cadence_hours=6,
    poll_seconds=120,
    lookback_cycles=4,
    tile_deg=5,
    max_run_bytes=110_000_000,
    production_enabled=False,
    evidence=(
        "2026-10-02 probe of data_run 2026-10-02T03Z: u/v 52 steps (0-51 h), gust 51 "
        "steps (1-51 h), [lat, lon, time] float32, 717x1121, BBOX 37.5..55.4N 12W..16E",
        "Orientation: static HSURF puts Mont Blanc (45.833N 6.865E) at 3887 m with ascending "
        "rows (1250 m if flipped), Aneto 2875 m, Monte Cinto 2165 m",
        "Footprint v1: static HSURF NaN mask (17.18 % of cells). No missing cell inside it at "
        "any step of u/v/gust 2026-10-02T03Z or v 2026-09-30T09Z, 2026-10-01T15Z, "
        "2026-10-01T21Z, 2026-10-02T09Z; at most 2 valid cells outside it per step",
    ),
)

ICON_EU = Product(
    layer="weather-icon-eu",
    model="dwd_icon_eu_0p0625",
    domain="dwd_icon_eu",
    originator="DWD ICON-EU 0.0625°",
    attribution="Deutscher Wetterdienst ICON-EU via Open-Meteo (CC BY 4.0)",
    data_licence="CC BY 4.0 (Open-Meteo bulk catalogue); DWD open data",
    grid=Grid(lat0=29.5, lon0=-23.5, cells_per_degree=16, nlat=657, nlon=1377),
    axis_name="steps",
    axes={h: tuple(axis_offsets((0, 78, 1), (81, 120, 3))) for h in (0, 6, 12, 18)},
    files=WIND_FILES,
    source_unit="m/s",
    source_precision="0.1 m/s (Open-Meteo scale_factor 10)",
    gust_first_lead_h=1,
    gust_windows=GustWindows(
        segments=((1, 120, 1),),
        verified=True,
        evidence=(
            "DWD VMAX_10M GRIB2, 2026-10-02T06Z at +1,2,78,81,84,120 h: "
            "endStep-startStep=1 h (including 80-81, 83-84, 119-120); output spacing "
            "after +78 h does not change the maximum window. Recorded in "
            "tests/fixtures/openmeteo/gust-windows/icon-eu.json"
        ),
    ),
    footprint=None,
    footprint_sha256=None,
    max_interior_missing=0.005,
    max_exterior_valid_fraction=0.0,
    lag_minutes={0: 205, 6: 205, 12: 205, 18: 205},
    wait_minutes=45,
    cadence_hours=6,
    poll_seconds=120,
    lookback_cycles=4,
    tile_deg=10,
    max_run_bytes=200_000_000,
    production_enabled=False,
    evidence=(
        "2026-10-02 probe of data_run 2026-10-02T06Z: u/v 93 steps (hourly 0-78 h, 3-hourly "
        "81-120 h), gust 92 steps from +1 h, 657x1377, BBOX 29.5..70.5N 23.5W..62.5E, no "
        "missing cells at any step",
        "03/09/15/21Z runs stop at +30 h (31 steps), so only 00/06/12/18Z are registered",
        "Orientation: static HSURF puts Mont Blanc at 2868 m, Etna 2228 m and Elbrus 3717 m "
        "with ascending rows (-999 sea / 337 m / 101 m if flipped)",
    ),
)

PRODUCTS: dict[str, Product] = {p.layer: p for p in (AROME, ICON_EU)}
LAYERS: tuple[str, ...] = tuple(PRODUCTS)


def product(layer: str) -> Product:
    p = PRODUCTS[layer]
    if os.environ.get("OPENMETEO_CANARY") == "true":
        hours = (3, 15) if layer == "weather-arome" else (0, 12)
        return replace(
            p,
            axes={h: p.axes[h] for h in hours},
            lag_minutes={h: p.lag_minutes[h] for h in hours},
            cadence_hours=12,
        )
    return p


def is_regional(layer: str) -> bool:
    return layer in PRODUCTS
