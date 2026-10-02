# UKV Phase 4 evidence, 2026-10-02

UKV is not registered or enabled. This narrow probe records the remaining
geometry and gust questions before adapter code can freeze a product definition.
The whole-file reader remains the chosen access method; no native-provider
production ingest or source-code reuse is introduced.

```sh
uv run --with pyproj --with h5py python scripts/probe_ukv_geometry.py --cycle 20261002T12 --output /tmp/ukv-discovery.json
```

The probe downloaded current bulk completion metadata plus primary Met Office
NetCDF direction at +0 h and gust at +0/+1/+54 h. Source URLs, ETags, lengths,
attributes, exact axes and independent reference coordinates are recorded in
`tests/fixtures/openmeteo/ukv/discovery.json`. It reads temporary files and
publishes nothing. Projection calculations use pyproj, not a production
transformation implementation under test.

## Confirmed and unresolved

- Bulk 12Z metadata declares 55 hourly valid times, 0–54 h, with required
  speed, direction and gust files. Native arrays are 970×1042, x0 = −1,158,000 m,
  y0 = −1,036,000 m and exact 2,000 m spacing. The source axes ascend south/north
  and west/east. Never infer that origin from rounded latitude/longitude bounds.
- Native NetCDF declares Lambert azimuthal equal area on an ellipsoid
  (semi-major 6,378,137 m; semi-minor 6,356,752.314140356 m). Bulk metadata
  declares the same projection centre on a **sphere of radius 6,371,229 m**.
  Using the exact same source cell indices, sampled corners/centre differ by
  as much as **4,681 m**. Bulk metadata's BBOX is also not a general enclosing
  rectangle of every projected corner. This must be resolved explicitly before
  serving a 0.025° product; a nominal 2 km/0.025° label does not establish location
  accuracy. Verify native/bulk cell values and static geometry independently,
  then record the selected/corrected CRS and conversion in provenance.
- Native direction uses `standard_name=wind_from_direction`; it is retained
  as source evidence. Confirm its reference frame and native/bulk value identity
  before vector conversion, and interpolate u/v rather than degree angles.
- Gust exists at +0 h, so dropping it as for AROME/ICON-EU would lose source
  data. All three primary gust samples lack time bounds and a `cell_methods`
  interval. Hourly spacing does not prove a one-hour maximum. Do not declare
  a maximum window, or export gust with one, until primary semantics are closed.

The [Met Office-managed registry](https://registry.opendata.aws/met-office-uk-deterministic/)
links **CC BY-SA 4.0** for upstream UKV, while the
[Open-Meteo bulk catalogue](https://github.com/open-meteo/open-data) gives
CC BY 4.0 generally. Treat the upstream ShareAlike notice as a separate
redistribution requirement; do not silently label transformed UKV tiles with
the catalogue's less restrictive licence. A future notice must identify the
Met Office/British Crown copyright, Open-Meteo distribution, our remapping and
quantization, and the applicable data-licence link. These data terms are distinct
from this repository's MIT code licence. No UKV delivery licence policy has
been activated.

Next implementation work is a projected source-grid definition, independent
native/bulk identity fixtures, a versioned footprint and a bounded remapping
adapter, followed by post-remap gzip/browser measurements. Gust and CRS gates
remain explicit. UKV capacity must replace the plan's unmeasured estimate and
is excluded from the current AROME/ICON-EU capacity proposal.
