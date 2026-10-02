# Regional weather models: access and integration

Research date: **2 October 2026**. Project baseline: `2c3b59f`.
This is an integration assessment, not an implemented or live-tested adapter.
“Asperge” is interpreted as **ARPEGE**; “NEMS” as meteoblue's model family,
not NOAA's modeling framework with the same acronym.

**Recommendation.** Start with AROME France and ICON-EU, using their published
regular latitude/longitude products. Add ICON-D2 where its footprint serves the
route. Build shared reprojection support for UKV, HRRR and HRDPS next. Prioritize
RRFS over a new NAM implementation: NOAA's registry currently schedules NAM's
retirement and RRFS operational implementation for **14 October 2026** [S1].
Recheck that notice before implementation; a scheduled transition is not a
completed transition. Keep GFS/ECMWF for coverage and lead times beyond each
regional model.

This ordering assumes the European coastal use already represented by IBI in
this repository. For North American users, move HRRR/RRFS and HRDPS ahead of UKV.
No model is ranked as more accurate merely because its grid is finer.

**What was verified.** The local ingestion, validation, tiling, contracts,
publication and dispatcher code was inspected. Provider documentation and
provider-managed AWS registry entries were read through their GitHub sources;
Open-Meteo implementation code supplies additional evidence about formats and
products. Links and source revisions are recorded below. The workspace permits
GitHub access but its enforced network policy does not allow the weather-provider
and forecast-bucket hosts, and it has no configured provider credentials.
Consequently, live catalogs, API subscriptions, current GRIB/NetCDF payloads,
latency, compression ratios and quotas were **not tested**. “Documented access”
below must not be read as “download succeeded here.”

**Access matrix.** Resolutions describe the selected distribution product where
known, not necessarily the model's computational mesh. Degree spacing is not a
constant distance in kilometres. Effort is relative to this codebase: small =
new adapter on a regular grid; medium = format/auth/time semantics; large =
reprojection, delivery changes or an unresolved commercial feed.

| Model / product | Coverage and forecast characteristics | Access and redistribution | Fit and priority |
|---|---|---|---|
| **AROME France / HD** | France and surrounding waters. Distributed grids 0.025° and 0.01°; current Open-Meteo code uses hourly steps through 51 h and 3-hourly cycles. Its selected rectangle is 37.5–55.4°N, 12°W–16°E. | Météo-France API: GRIB2 packages or WCS, API key. `object.data.gouv.fr/meteofrance-pnt/` is an alternative package route evidenced in the downloader. Open-data reuse/attribution terms must be captured for the chosen product. The third-party AWS mirror `mf-nwp-models` still advertises older 42 h / 6-hourly products [S2, S3]. | **First coastal pilot, small–medium.** Start at 0.025° or a bounded 0.01° domain. Discover the actual cycle/package horizon; do not combine old mirror metadata with a newer API. |
| **ARPEGE Europe / global** | ARPEGE is a global model, with a useful 0.1° Europe distribution. Current implementation uses 0.25° global, 102 h for most cycles and 114 h at 12Z; packages and WCS can expose different step selections. | Same Météo-France routes. The older AWS mirror lists a 0.5° world product, so the two feeds are not interchangeable [S2, S3]. | **Small–medium.** Reuse the AROME transport/discovery work. Useful European comparison, but less incremental coastal detail than AROME. |
| **ICON-EU** | Europe/North Atlantic margins; published regular grid 0.0625°. Main 00/06/12/18Z runs to 120 h; 03/09/15/21Z side runs to 30 h in the inspected implementation. | DWD anonymous HTTPS, per-variable/per-step GRIB2 compressed with bzip2. Provider terms and attribution need to be recorded; do not infer a license from the client code [S4]. | **First broad regional pilot, small.** Choose the `regular-lat-lon` product. Start with main runs so a newer short run does not displace a longer forecast. |
| **ICON-D2** | Germany and surroundings, including parts of North Sea/Baltic; regular distribution 0.02°, hourly through 48 h, cycles every 3 h in the inspected implementation. | Same DWD route; use the deterministic regular-grid files. Other ICON products, particularly ensembles, can have different grids [S4]. | **Small–medium.** Reuse the DWD adapter. Check the actual footprint: this is not coverage of all France or the Atlantic. |
| **ICON global** | Global; native unstructured icosahedral mesh. Inspected implementation remaps to 0.125°; main runs to 180 h, side runs to 120 h. | DWD open GRIB2/bzip2 and matching grid geometry [S4]. | **Large, later.** Requires remapping; limited priority while GFS and ECMWF already provide global fields. |
| **UKV / UK deterministic 2 km** | UK/Ireland and surrounding waters. Official archive: nowcasts to 12 h; short runs to 54 h; 03/15Z medium runs to 120 h. Hourly to 54 h, then 3-hourly for most parameters. Typical archive lag 3–6 h. | **Public bulk NetCDF**, `met-office-atmospheric-model-data` S3 bucket, prefix `uk-deterministic-2km`. Official registry specifies **CC BY-SA 4.0**, a two-year rolling archive and an unsupported service [S5]. | **High value, medium–large.** The distributed 2 km grid is Lambert azimuthal equal-area in the inspected client, not geographic 0.018° spacing. Reproject and honor attribution/share-alike obligations on redistributed adapted data. |
| **HRRR CONUS** | US mainland and adjacent waters; 3 km Lambert conformal grid. Hourly cycles; 18 h on ordinary cycles, 48 h on 00/06/12/18Z in the inspected implementation. Alaska is a separate product. | NOAA anonymous S3 `noaa-hrrr-bdp-pds`, GRIB2 with inventories; NOMADS is another route. NOAA permits reuse; identify modifications and avoid implying endorsement [S6]. | **Best initial US adapter, medium–large.** Reuse NOAA inventory/range fetching, but replace the regular-grid decoder path. Start with the long cycles. |
| **HRDPS continental** | Canada and northern US; 2.5 km, rotated latitude/longitude grid, 00/06/12/18Z, hourly to 48 h. | ECCC Datamart GRIB2, free and reusable including commercially with attribution. Current usage policy requires **AMQPS for systematic retrieval** and forbids directory polling for new-data discovery. A dynamical.org Icechunk/Zarr mirror is another documented route [S7, S8]. | **Medium–large.** Rotate/remap coordinates and, if necessary, winds. Add AMQPS collection or validate the mirror's variables, cycle preservation and update behavior. Plain scheduled directory polling is not the proposed production approach. |
| **NAM / NAM nests** | North America, traditionally 12 km parent and 3 km CONUS nest; products and horizons differ (commonly 84 h parent, 60 h nest). | NOAA anonymous `noaa-nam-pds`, GRIB2. Registry announces retirement on 14 October 2026 [S1]. | **Defer new production work.** Only justify for historical comparison or a specific legacy need. Do not build a new long-lived dependency on it. |
| **RRFS / REFS** | NOAA's planned replacement: 3 km North American grid, hourly deterministic 18 h; 00/06/12/18Z deterministic 84 h and ensemble 60 h in the registry. | Operational/parallel destination is **`noaa-rrfs-ops-pds`**, not the older `noaa-rrfs-pds` prototype feed. Registry states the prototype stopped updating with the parallel phase on 12 August 2026 [S1]. | **Priority research alongside HRRR.** Validate operational product paths, projection, variables and readiness through the transition. Start deterministic; ensemble ingestion is a separate scope. |
| **ACCESS-C / ACCESS-G** | ACCESS-C is the Australian convection-permitting city/regional family (nominally about 1.5 km); ACCESS-G is global and does not substitute for ACCESS-C. Exact C domains/cycles/horizons need product confirmation. | Start with BoM's NWP data catalog and registered/commercial data-service offerings. Anonymous access to the required ACCESS-C wind/gust grids and public-tile redistribution rights were **not established**. Open-Meteo implements ACCESS-G but requires an externally supplied server root [S9]. | **Conditional, large until access is resolved.** Obtain the exact product, delivery route, authentication, tariff and redistribution terms before designing a C adapter. A G API is not evidence of C availability. |
| **NEMS (meteoblue)** | Several domains/resolutions; choose the actual domain and a run-specific forecast product. A blended point forecast is not a raw NEMS grid. | Official meteoblue Python Dataset SDK requires an API key and demonstrates `NEMSGLOBAL` access. That archive example does **not** establish a real-time regional bulk feed or redistribution rights [S10]. | **Commercial/access investigation first.** Confirm current model availability, gridded export, cycle metadata, volume pricing, caching and onward PFT1 redistribution with the provider. No anonymous operational feed was verified. |
| **ALADIN / ALARO national products** | A model family operated by multiple national services; domains, resolution, physics and publication policies differ. Select country/product first. | Czech CHMI has an open-data lead at `opendata.chmi.cz/meteorology/weather/nwp_aladin/`, with `Lambert_2.3km` and `CZ_1km` distribution directories evidenced by a third-party client. Croatian DHMZ and other national services require separate investigations; there is no single ALADIN API/license [S11]. | **Conditional.** Verify official product metadata, coastal footprint, wind/gust identities, format and license. The Czech 1 km distribution label alone does not prove a 1 km native model. Do not infer raw-data rights from viewable forecast charts. |

For the API and DWD routes whose licenses could not be read from an authoritative
accessible source in this session, the table deliberately leaves final license
verification open. Access fees, data licenses and API service terms are separate
questions. An open-source downloader's license grants no rights to its data.

**Other regional products worth evaluating.** These are follow-on leads, not
equally complete access assessments:

| Product | Why it could help | Access / work to investigate |
|---|---|---|
| KNMI HARMONIE / DMI HARMONIE-DINI | North Sea, Netherlands, Denmark and neighboring coasts | KNMI Data Platform API (key/access tier); DMI forecast catalog and bulk GRIB. Current client source demonstrates both. Validate distribution grid, footprint and reuse terms [S12]. |
| MET Norway MEPS control / AROME-Arctic | Nordic and Arctic sailing areas | Official NetCDF/CF via HTTPS THREDDS/OPeNDAP; geographic subsetting before remapping. MEPS has 2.5 km spacing and a control member to 66 h; ensemble members can be lagged. Preserve each selected initialization [S13]. |
| MeteoSwiss ICON-CH | Alpine lakes and neighboring areas | Official STAC discovery via `data.geo.admin.ch`, with a working third-party downloader. Inspect product-specific grids, members and license [S12]. |

**Concrete retrieval patterns.** These are documented starting points, not
commands proven against a current run in this workspace. Enumerate the current
provider inventory before constructing a production URL.

- Météo-France portal: <https://portail-api.meteofrance.fr/>. The inspected
  client uses `/previnum/DPPaquetAROME/v1/models/AROME/grids/0.025/packages/…`
  and `/public/arome/1.0/wcs/…/GetCoverage` on `public-api.meteofrance.fr`,
  with an `apikey` header. WCS can reduce bytes through variable/domain/time
  selection; package downloads need GRIB-message filtering. API quotas are
  subscription-specific and were not measured.
- DWD: <https://opendata.dwd.de/weather/nwp/>. Typical layout is
  `{icon-eu|icon-d2}/grib/{HH}/{variable}/…grib2.bz2`; near-surface fields
  include `u_10m`, `v_10m` and `vmax_10m`. Decompress with Python `bz2`;
  NOAA's `.idx` convention does not apply to these individual files.
- UKV: <https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/index.html>.
  Discover cycle/parameter NetCDF objects under `uk-deterministic-2km/`.
  Read CF coordinates, `grid_mapping`, heights, units, time bounds and masks.
- HRRR: `https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{YYYYMMDD}/conus/`
  with `hrrr.t{HH}z.wrfsfcf{FF}.grib2` and its `.idx` as a candidate surface
  product. Confirm `UGRD:10 m above ground`, `VGRD:10 m above ground` and
  `GUST:surface` in the chosen inventory; field/product availability is a
  live-probe requirement. The inspected Open-Meteo adapter uses `wrfprs`
  files, illustrating that the bucket has several products.
- HRDPS: official docs give
  `https://dd.weather.gc.ca/today/model_hrdps/continental/2.5km/{HH}/{hhh}/`.
  Names include `…_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT{hhh}H.grib2`.
  Confirm the run date inside every message: rolling paths are not immutable.
  Use the provider's AMQPS workflow for systematic collection [S7].
- BoM starting catalog: <https://www.bom.gov.au/nwp/doc/access/NWPData.shtml>.
  Treat this as a discovery link, not a confirmed download entitlement [S9].

**Fit to the existing pipeline.** The recommended flow retains the PFT1 binary
layout for an initial hourly release:

```text
provider cycle + complete variable/step inventory
    -> download GRIB/NetCDF subset
    -> decode native geometry and vector convention
    -> if needed: rotate winds to east/north, remap to regular geographic grid
    -> normalize units/time windows -> quantize -> ForecastCube
    -> validate -> existing PFT1 tiles -> immutable run -> manifest/latest
```

The code already supports a bounded rectangular domain, partial edge tiles,
variable-specific time axes, missing values and provenance. IBI demonstrates
regional publication, but its regular geographic grid avoids the harder
atmospheric-grid issues. `ForecastCube` can describe a regular target grid;
it cannot describe projected x/y axes, a rotated pole or an unstructured mesh.

| Existing code / contract | Necessary integration work |
|---|---|
| [`sources/base.py`](../src/ingest/sources/base.py), `decode_field()` | Explicitly inspect `gridType` and scanning metadata before regular-grid decoding. Rotated lat/lon can otherwise look superficially regular yet place data incorrectly. Handle reversed/alternating scans. Compare grid metadata across all fields/steps. |
| New provider adapters in [`sources/`](../src/ingest/sources/) | Keep `resolve(requested)` and `build_cube(cycle)` entry points. Implement provider-specific inventories and completion checks. Reuse NOAA range helpers only for matching inventories; stream package messages or individual files elsewhere. |
| [`cube.py`](../src/ingest/cube.py), [`tile.py`](../src/ingest/tile.py) | Before constructing a cube, remap projected/rotated/unstructured input onto a documented target lattice. Cache interpolation weights by source/target-grid fingerprint. Retain native-product identity and remapping method in provenance. |
| [`cli.py`](../src/ingest/cli.py) | Register layer, resolver, builder, polling behavior and missing-data policy. A projected domain's masked rectangular corners need a domain-aware completeness check, not an indiscriminately raised global missing allowance. |
| [`validate.py`](../src/ingest/validate.py) | Validate mandatory wind coverage inside the expected footprint and each required step. Check real gust semantics before reusing the GFS-derived gust tolerance. Validate transformed coordinates and known wind vectors. |
| [`publish.py`](../src/ingest/publish.py) | Register cadence; include license/source attribution in provenance; measure retained storage. `_update_latest()` is currently a shared read/modify/write without compare-and-swap: concurrent layer publishers can lose another layer's pointer. Serialize that update or implement conditional writes before increasing concurrent publication. |
| [`contracts/forecast-*.schema.json`](../contracts/) and Passage's canonical forecast contracts | Add every new layer in lockstep with consumers. All three current schemas restrict run prefixes to `[a-z-]+`; a name such as `weather-icon-d2` needs a coordinated pattern change to permit digits. Existing variable declarations and time windows must remain readable. |
| [`dispatcher/src/timetable.ts`](../dispatcher/src/timetable.ts), workflows | Add schedules only after measuring the chosen feed's publication lag and all-field readiness. The existing five cron expressions already use the documented account allowance; the README's single 5-minute tick design is a possible extension. HRDPS notification delivery needs separate design. |
| Passage consumers (outside this inspected checkout) | Add model selection, attribution, footprint/lead-time eligibility, stale-data handling and regional-to-global fallback. Verify how they decode partial/masked tiles and resolve path templates before publication. This assessment does not claim those consumer changes already exist. |

Preserve eastward `wind_u_kt` and northward `wind_v_kt`. Inspect GRIB
`uvRelativeToGrid` or the CF vector definition; rotate only grid-relative
vectors. If a provider publishes meteorological **from** direction and speed,
convert with `u = -speed * sin(direction)` and `v = -speed * cos(direction)`
(angle converted to radians). Do not interpolate direction angles directly.
Remap floats before quantization; do not interpolate missing integer sentinels.
Use a footprint mask to prevent extrapolation past a regional model's boundary.

Start with 10 m wind components and an independently validated gust field.
Gust may be an interval maximum, unavailable at analysis time or differently
defined between products. Populate `Statistic(kind="max", window_h=…)` from
metadata where applicable; do not fabricate gust from mean wind. Precipitation
and radiation require accumulation/averaging semantics beyond today's max-only
statistic contract. Add them after the wind product is correct. The current
integer `offsets_h` and integer statistic windows cannot express 15-minute
AROME-PI/ICON-D2/HRRR output; use hourly products initially.

A “latest” short run must not silently remove usable future hours from a longer
run. Either select only the long cycles initially or publish separate short/long
layers, following the ECMWF precedent. Keep original cycles explicit when
falling back to a global forecast. A missing regional tile or exhausted horizon
means “use the configured fallback,” not zero wind. A previous regional run is
also not a substitute for geographical fallback.

**Storage and browser cost.** A whole time series resides in every 10° tile.
For 49 hourly samples and three int16 fields, one full tile has the following
payload *before* headers and gzip:

| Geographic grid spacing | Cells in a full 10° tile | Raw bytes / tile |
|---|---:|---:|
| 0.05° | 200 × 200 | 11.76 MB |
| 0.025° | 400 × 400 | 47.04 MB |
| 0.01° | 1,000 × 1,000 | 294 MB |

These are arithmetic estimates (`cells × 49 × 3 × 2`), not measured download
sizes. Four full 0.01° tiles already contain 1.176 GB uncompressed; client
float arrays and temporary buffers add more. Actual domain-edge tiles can be
smaller. Measure gzip bytes, browser decode peak, runner peak memory and elapsed
ingest time on real data before choosing HD. The repository's default bucket
guard is **8 GB** and retention is current + previous per layer. New layers add
retained storage even when increasing one layer's cadence does not. Also budget
the temporary upload overlap before the old previous run is deleted.

Use a bounded pilot and stream/preallocate per-step arrays: current
`collect_steps()` retains downloaded fields before stacking arrays, and
`build_tiles()` retains every compressed tile. These patterns can multiply
memory use at kilometre scale. A smaller geographic tile or time chunks may
eventually be preferable, but both require consumer/contract work; the current
manifest fixes `tile_deg` to 10. `z_res()` rounds degrees to hundredths, so
distinct fine resolutions can share a path label. Read exact header geometry;
do not infer scientific resolution from that label.

**Direct feeds versus intermediaries.** Prefer direct provider grids for the
first integrations: they preserve run identity and fit immutable publication.
Open-Meteo point APIs are useful for comparisons, but reconstructing entire
tiles through point requests is inefficient and can incorporate interpolation,
downscaling or mixed runs. Open-Meteo also publishes a **bulk OM-format database**
on AWS [S14]. Its documented `data_run/<model>/<YYYY/MM/DD/hhmmZ>/` layout
keeps individual runs, native forecast steps and one file per variable, with
`meta.json` written on completion. The documentation specifies three months of
public retention and at most one retained run every three hours. This is a
credible second integration path: use its Python OM reader, select a completed
run and spatial subset, then normalize to a `ForecastCube`. Probe each desired
model/variable before relying on the layout; a general model listing is not
proof every run-format product is present. Avoid the rolling `data/` layout
when an exact cycle is required: it is overwritten and temporally interpolated.
Compare this route against direct ingestion during the first pilot, especially
if API credentials or download volumes delay AROME. Check upstream attribution
and model-specific license obligations, and preserve any upstream remapping or
quantization in provenance. Code and data licenses differ. Likewise,
dynamical.org's HRDPS/ICON-EU/HRRR
datasets can reduce download/format work but do not automatically eliminate
projection or cycle-selection work. Do not use rendered weather maps or WMS
images as numerical wind input.

**Proposed implementation sequence and acceptance criteria.**

1. **AROME + ICON-EU feasibility samples.** Obtain one complete recent cycle
   from each chosen feed, inspect u/v/gust metadata and the first/last lead
   times, and measure a Channel/Biscay subset. Choose AROME 0.025° versus
   bounded HD on measured transfer/memory. Verify provider attribution and
   current reuse terms. ICON-EU is the simpler credential-free first adapter
   if the Météo-France API key/package route is unavailable.
2. **One production-quality pilot.** Add a distinct regional layer and its
   consumer support. Demonstrate golden coordinate/vector cases, partial
   tiles, missing analysis gust, cycle incompleteness, provider delays and a
   local `--dry-run` output. Validate schemas in both repositories. Resolve
   the shared latest-pointer race before scheduling additional concurrent
   publishers. Publish only after the complete run and cost checks pass.
3. **Reusable projection support + UKV / HRRR.** Choose the target grid and
   vector-rotation/remapping tools (for example ecCodes plus pyproj and
   reusable interpolation weights; xarray/netCDF4 for CF products). Check a
   known location and cardinal wind vectors against native data. Apply UKV's
   share-alike and attribution requirements to its redistributed data.
4. **HRDPS and RRFS.** Validate ECCC AMQPS or a suitable bulk mirror. Sample
   RRFS operational/parallel inventories and recheck the 14 October transition
   notices. Reuse the projection/NOAA work rather than implementing NAM first.
5. **Geography-driven additions.** KNMI/DMI/MEPS, national ALADIN, then ACCESS-C
   where the user geography justifies access work. NEMS remains conditional on
   a suitable licensed forecast-grid product and a cost estimate.

For each pilot, record model/product/domain/version, native and served geometry,
required variables and levels, cycle/time windows, publication lag, source
object identities, license/attribution, bytes per retained run, peak memory and
client fallback behavior. Validate against actual metadata, not only nominal
resolution or a successful HTTP status. No application code, production
schedule, provider subscription or public forecast data was changed by this
assessment.

**Evidence and source links.** Primary provider documents and provider-managed
registry records carry more weight than implementation examples. Where a row
has only implementation evidence, its live availability and license remain
open. Repository snapshots make the observations reproducible:

- AWS registry: `00462862e4c1926cd35809ed2ac1c7b5b67b5bdd`.
- Open-Meteo implementation: `b06f4760fd1f997e5559bb380f64c5e496b4a509`.
- Open-Meteo bulk documentation: `4fd52ad16c417c49bff45fab4bf175e5ea5760f2`.
- ECCC official docs: `597ad8b04938d910f108e998b530a2cf485dd963`.
- meteoblue official SDK: `1fd7e0602865a292e2dd3c730e654b58a732cf91`.
- MET Norway official wiki: `1cebeaf6b32ec95b68a57dbc6fbfefa1c653a466`.
- Third-party CHMI client: `4d06b6dc3d684d041965ab7ee0f5fc7b514125fc`.

| Ref | Source and evidence status |
|---|---|
| S1 | NOAA-managed registry: [NAM retirement](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/noaa-nam.yaml), [RRFS operational feed](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/noaa-rrfs-ops.yaml). Includes links to the official service-change notices; those PDFs were not fetched here. |
| S2 | [Météo-France models AWS listing](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/meteo-france-models.yaml). Explicitly operated by **OpenMeteoData**, unaffiliated with Météo-France. Older specifications must not override current provider metadata. |
| S3 | Open-Meteo [Météo-France domain/products](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/MeteoFrance/MeteoFranceDomain.swift) and [API/package downloader](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/MeteoFrance/MeteoFranceDownloader.swift). Third-party implementation evidence. |
| S4 | Open-Meteo [ICON domains](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Icon/Icon.swift), [DWD downloader](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Icon/DownloadIconCommand.swift) and [remapping distinction](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Icon/Cdo.swift). Third-party evidence; [DWD NWP entry point](https://opendata.dwd.de/weather/nwp/) not fetched here. |
| S5 | [Met Office-managed UKV listing](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/met-office-uk-deterministic.yaml); [Open-Meteo grid implementation](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/UKMO/UkmoDomain.swift). The latter uses only 12/54 h runs; use the provider listing for the additional 120 h product, subject to live inventory verification. |
| S6 | [NOAA-managed HRRR listing](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/noaa-hrrr-pds.yaml); [Open-Meteo HRRR grid/steps](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Gfs/GfsDomain.swift). |
| S7 | ECCC official [HRDPS access/grid/naming](https://github.com/ECCC-MSC/open-data/blob/597ad8b04938d910f108e998b530a2cf485dd963/docs/msc-data/nwp_hrdps/readme_hrdps-datamart_en.md), [data license](https://github.com/ECCC-MSC/open-data/blob/597ad8b04938d910f108e998b530a2cf485dd963/docs/licence/readme_en.md), [usage policy](https://github.com/ECCC-MSC/open-data/blob/597ad8b04938d910f108e998b530a2cf485dd963/docs/usage-policy/readme_en.md) and [AMQPS documentation](https://eccc-msc.github.io/open-data/msc-datamart/amqp_en/). Access, license and usage-policy text read; AMQPS link is the next implementation reference. |
| S8 | [dynamical.org HRDPS registry entry](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/dynamical-eccc-hrdps.yaml), including its CC BY 4.0 statement and example notebook. Mirror metadata, not an ECCC service entitlement. |
| S9 | [BoM catalog link and ACCESS-G grid implementation](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Bom/BomDomain.swift); [downloader requiring a supplied server](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Bom/BomDownloader.swift). No live ACCESS-C access or license verified. |
| S10 | [meteoblue official SDK README](https://github.com/meteoblue/python-dataset-sdk/blob/1fd7e0602865a292e2dd3c730e654b58a732cf91/README.md). Proves an authenticated dataset API and archive example, not a public operational NEMS bulk feed. |
| S11 | [CHMI client URL examples](https://github.com/xertep/aladin-open-data-chmu/blob/4d06b6dc3d684d041965ab7ee0f5fc7b514125fc/alad_test_streamlit.py). Third-party discovery lead only; do not adopt its variable assumptions without checking provider definitions. |
| S12 | Third-party implementations: [KNMI](https://github.com/open-meteo/open-meteo/tree/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Knmi), [DMI](https://github.com/open-meteo/open-meteo/tree/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Dmi), [MeteoSwiss](https://github.com/open-meteo/open-meteo/tree/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/MeteoSwiss). |
| S13 | MET Norway official [data access](https://github.com/metno/NWPdocs/wiki/Data-access), [MEPS](https://github.com/metno/NWPdocs/wiki/MEPS-dataset), [AROME-Arctic](https://github.com/metno/NWPdocs/wiki/AROME-Arctic-dataset). Wiki read through its git repository; URLs are moving documentation. |
| S14 | [Open-Meteo bulk database registry](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/open-meteo.yaml), [bulk data documentation](https://github.com/open-meteo/open-data/blob/4fd52ad16c417c49bff45fab4bf175e5ea5760f2/README.md), [Python OM reader](https://github.com/open-meteo/python-omfiles). Registry and bulk documentation read; reader implementation not evaluated. The CC BY 4.0 label is not a substitute for checking upstream model-specific obligations. |
