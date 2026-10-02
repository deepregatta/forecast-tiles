# UKV Phase 4 evidence, 2026-10-02

UKV is registered for dry runs and Passage opt-in consumption. Production is
still disabled. Whole-file access uses `omfiles`; primary NetCDF files are
read-only verification inputs, not a second production ingestion path.

## Primary identity and semantics

```sh
uv sync --extra openmeteo
uv run --with h5py python scripts/probe_ukv_identity.py --cycle 20261002T12 --scratch /tmp/ukv-identity --output /tmp/ukv-identity.json
uv run ingest weather-ukv --cycle 20261002T12 --dry-run /tmp/ukv-dry-run
```

Seven complete 970×1042 native slices matched the bulk arrays: wind speed and
from-direction at +0/+54 h, and gust at +0/+1/+54 h. Speed/gust differences
were at most 0.050001 m/s; direction differences were at most 1°, consistent
with bulk precision of 0.1 m/s and 2°. Source URLs, ETags, lengths, full-object
SHA-256 values and results are in `tests/fixtures/openmeteo/ukv/identity.json`.
Attributed 9×9 crops retain all 55 times for offline reader-to-cube regressions.

The wind matches **`wind_speed_on_height_levels` and
`wind_direction_on_height_levels` at 10 m**, rather than the separately
adjusted surface fields. Native `wind_from_direction` is clockwise from
geographic north; the [CF explanation](https://cfconventions.org/mailing-list-archive/Data/1497.html)
distinguishes it from grid-relative direction. Convert to u/v before remapping,
including across 359°/1°; do not apply a second projection rotation.

The [Met Office parameter description](https://www.metoffice.gov.uk/binaries/content/assets/metofficegovuk/pdf/data/ukv-parameters-may-2019.pdf)
distinguishes `wind_gust_at_10m` from its one-hour maximum parameter.
Bulk gust matches the former: publish it as an **instantaneous diagnostic**,
without an invented maximum window, and preserve +0 h. An actual tile-to-GRIB
export was independently decoded with ecCodes: six +0/+1 h wind/gust messages
used instantaneous template 4.0, height 10 m and Met Office centre 74.

## Geometry and coverage

The initial geometry probe remains recorded in `discovery.json` and
`scripts/probe_ukv_geometry.py`. Native axes ascend with exact origin
x0 = −1,158,000 m, y0 = −1,036,000 m, spacing 2,000 m and shape 970×1042.
Lambert azimuthal equal area is centred on longitude −2.5°, latitude 54.9°.
Primary NetCDF uses an ellipsoid with semi-major 6,378,137 m and semi-minor
6,356,752.314140356 m. Bulk WKT instead declares a 6,371,229 m sphere:
sampled cell locations differ by up to **4,681 m**.

The complete native/bulk value comparisons establish matching cell indices.
The adapter therefore records and uses the primary **ellipsoid**, explicitly
correcting the bulk CRS. Independent primary corner/centre coordinates are
regression inputs; a spherical substitution fails them. A pinned bulk WKT
hash refuses unnoticed metadata changes.

The fixed 0.025° served grid starts at latitude 44.5° / longitude −24.525°,
742×1594 cells, enclosing the curved native boundary instead of the bulk BBOX.
Versioned footprint `ukv-0p025.v1` contains 899,361 valid cells.
Bilinear interpolation requires four finite native neighbours, including
zero-weight neighbours; outside cells remain missing, with no extrapolation.
Weights are cached by native/served geometry fingerprint. Unexpected native
holes fail before remapping. Provenance records geometry, CRS correction,
precision, vector conversion and remapping.

## Measured delivery

The 12Z cycle provides 55 times, +0–54 h; required whole files total
84,391,336 bytes. A 5° layout failed the 8 MiB gzip gate before publication.
The supported **3° layout** at unchanged 0.025° resolution passed:

| Metric | UKV 12Z |
|---|---:|
| Tiles / gzip total | 80 / 164,039,908 B |
| Manifest | 16,482 B |
| Largest gzip / decoded Float32 arrays | 3,166,351 / 9,504,000 B |
| Largest inflated PFT1 | 4,755,188 B |
| CLI / measured wall time | 48.2 / 48.46 s |
| Runner peak RSS | 2,007,896 KiB (1.915 GiB) |
| Cube validation | 15 checks passed |

The 200 MB run cap includes tiles and manifest; capacity reservations use this
measured cap rather than the former 90–110 MB estimate. The schedule registers
00/06/12/18Z, first polls at +4 h 15 min and waits up to 120 minutes.
Canary selection is 00/12Z only. All publication enablement gates remain closed.

## Data terms and remaining gates

The [Met Office-managed listing](https://registry.opendata.aws/met-office-uk-deterministic/)
identifies British Crown/Met Office copyright and **CC BY-SA 4.0**.
[DATA-LICENSES.md](../DATA-LICENSES.md) applies that notice to fixtures and
transformed data, identifies Open-Meteo distribution and modifications,
and preserves the licence link and ShareAlike terms. The general bulk CC BY
notice does not replace upstream UKV terms. Code remains MIT-licensed;
no AGPL server source is copied.

Representative desktop measurements are in [regional-delivery.md](regional-delivery.md).
Individual phone selections passed; the combined cache refinement still
requires a physical-phone retest. The three-model capacity proposal is prepared; reviewed configuration and
representative-cycle capacity refreshes, maintainer deployment, rollback/outage drills and seven-day
canaries remain activation gates. Numerical and desktop success do not
establish those live gates.
