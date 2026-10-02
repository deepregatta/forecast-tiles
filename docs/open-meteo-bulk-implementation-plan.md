# Open-Meteo bulk integration implementation plan

Status, 2026-10-02: Phase 0's isolated live R2 conditional-write check passed
(three tests, [run 37032148191](https://github.com/deepregatta/forecast-tiles/actions/runs/37032148191)).
Phases 1–2 now include verified one-hour gust windows. Phase 3's producer,
Passage consumer, schemas, scheduling, capacity admission and regional rollback
are implemented, with production disabled. Desktop scratch-data browser checks
passed with representative production root bytes. Phase 4 UKV now passes
primary identity, corrected geometry, instantaneous-gust, tile-size and desktop
checks. Individual physical-phone selections also passed; the final combined
cache refinement still needs a phone retest. The owner-approved capacity
settings are now applied and verified; representative-cycle capacity refreshes,
maintainer deployment and seven-day canaries remain gates.
Release 1 is not complete. See the evidence and operations
in [regional-delivery.md](regional-delivery.md). Historical review measurements
below remain attributed to the earlier review.

Add new deterministic weather models from Open-Meteo's public AWS files while
keeping every existing layer on its current provider, with its current fields,
grid, cycles, horizon, tile layout, retention and fallback behavior. Release 1
adds AROME, ICON-EU and UKV in stages. The publication race affecting existing
layers is a separate, immediate first fix.

This supersedes the direct-provider order in the
[regional model access assessment](regional-model-access.md), which remains
the reference for alternatives and models absent from bulk.

## Decisions

- Fetch three required whole `.om` files from a complete `data_run/` cycle
  using existing `requests`; decode locally with plain `omfiles`. No range
  reader, fsspec, s3fs, aiobotocore, block cache or spatial-layout comparison.
- No hosted Open-Meteo API, paid subscription, self-hosted server or permanent
  raw-data mirror. Temporary source files stay on the ingestion runner.
- Publish all new layers in **`latest-regional.json`**, keeping that pointer
  separate after Passage integration. Root `latest.json` retains its seven
  existing layers. Opted-in consumers combine the catalogues.
- Start with surface u/v and verified gust. Do not blend models, replace
  existing sources or treat deterministic models as ensemble members.
- Mask-aware validation is required in the first AROME dry run: regular
  coordinates do not imply an unmasked rectangular footprint.
- Use one GitHub concurrency group per layer across all triggers, conditional
  immutable-object creation and conditional pointer updates. No claim/lease
  protocol or new coordination service.
- Preserve PFT1 encoding and existing URLs. Propose smaller tiles only for new
  regional layers that exceed browser budgets, with coordinated schema and
  consumer changes before activation.
- Retain current plus previous run in the existing project bucket. Release 1
  is a conditional fit against the historical projection; Release 2 is blocked
  under the current 8 GB allocation until re-budgeted.
- Keep the five current cron expressions and extend their hours. Do not switch
  to `*/5` in this release or remove the existing missed-slot alarm.

### Existing layers to preserve

| Layer | Source and behavior retained |
|---|---|
| `weather` | NOAA GFS 0.25°, current wind/gust and hazard fields, axes through 240 h, four cycles/day |
| `weather-ecmwf` | ECMWF open data, 00/12Z through 240 h, current gust intervals |
| `weather-ecmwf-short` | ECMWF open data, 06/18Z through 144 h, current gust intervals |
| `ensemble` | NOAA GEFS, all 31 members, current mean/anomaly encoding and axes through 384 h |
| `waves` | NOAA GFS-Wave, current wave/wind-wave/swell fields and horizons |
| `currents` | Copernicus GLO12, current six-hourly currents and RTOFS outage fallback |
| `currents-ibi` | Copernicus IBI, current regional hourly means through 120 h |

The routing-index pipeline is outside this change. Existing model coverage
and retention are not reduced to fund the additions.

## Evidence already available

The review reports 160 complete runs checked between 24 September and
1 October. Adopt those findings instead of repeating a broad discovery phase.
These are **review-supplied measurements**, not trials rerun during this
revision. Capture source keys, metadata/ETags, benchmark commands and small
attributed fixtures during implementation to make them reproducible.

| Product | Observed time axis | Required whole-file downloads per run |
|---|---|---:|
| AROME 0.025° | 52 steps, 0–51 h on every cycle | 57 MB for u, v, gust |
| ICON-EU | 93 steps: hourly 0–78 h, then 81–120 h every 3 h | 122 MB |
| UKV | 55 steps, 0–54 h on every three-hourly cycle | 90 MB for speed, direction, gust |
| HRRR CONUS | 49 steps at 06Z; verify other selected extended cycles before activation | 160 MB |

The review confirms `data_run/<domain>/YYYY/MM/DD/HHMMZ/meta.json` and
`data_run/<domain>/latest.json`, metadata written after the run's files, and
at most one retained run every three hours. Arrays are `[lat, lon, time]`,
requiring transpose to `[time, lat, lon]`. Latitude appears south-to-north;
confirm with a known-point fixture.

Reported conversion results use this repo's current **10° tiles** on
1 October 2026. MB/GB are rounded decimal figures as reported, not MiB/GiB.

| Metric | AROME 00Z | ICON-EU 06Z |
|---|---:|---:|
| Tiles | 11 | 60 |
| One run, gzipped | 76 MB | 171 MB |
| Two retained runs, gzipped | 152 MB | 341 MB |
| Earlier two-run raw-field estimate | 502 MB | 1,010 MB |
| Largest tile, gzip / decoded Float32 arrays | 20.2 MB / 100 MB | 6.5 MB / 29 MB |
| Encode time / peak memory | 27 s / 1.7 GB | 64 s / 3.6 GB |
| Gust ≥ wind check | Passes, 99.99% | Passes |

The 171/341 MB rounding is retained as reported. Each AROME source file
reportedly decodes in about one second. These sizes do not justify a range-read
stack. Re-measure totals and tile counts after any tile-size change; this table
does not establish the size of the proposed new delivery layout.

### Verification status

- UKV primary identity and semantics are verified: geographic from-direction
  at 2° precision, instantaneous gust including +0 h, corrected native
  ellipsoid and a pinned remapping footprint. See [ukv-discovery.md](ukv-discovery.md).
- AROME and ICON-EU gust windows are now verified from primary upstream GRIBs,
  including ICON-EU after +78 h: every sampled maximum covers one hour.
  Output spacing of three hours does not imply a three-hour maximum. Recorded
  source URLs, ETags and intervals are in `tests/fixtures/openmeteo/gust-windows/`.
  The former AROME object.data.gouv.fr URL returned 404; the current official
  listing points to the OVH host used by `scripts/probe_openmeteo.py`.
- UKV 12Z now measures 164.04 MB gzip in 80 tiles at 3° after remapping.
- UKV data/fixtures retain upstream CC BY-SA terms in [DATA-LICENSES.md](../DATA-LICENSES.md).
  Code is **MIT**, chosen by the owner on 2026-10-02; `omfiles` stays optional.
- Live R2 conditional writes passed the isolated-prefix workflow on 2 October.
  The installed boto3 supports both headers; no dependency upgrade was needed.
- Browser budgets with the actual consumer and current combined storage
  headroom. Source completeness alone does not establish these. Known-point
  and mask fixtures now exist for AROME and ICON-EU (`tests/fixtures/openmeteo/`).

## Model order and definitions

Layer IDs and policies below are proposed. First-release axes have review
measurements; later products still need independent samples. Freeze verified
definitions in the registry before production.

| Stage | Model / layer | Open-Meteo domain | Initial product |
|---|---|---|---|
| Pilot | AROME / `weather-arome` | `meteofrance_arome_france0025` | Preserve 0.025° grid/mask, 0–51 h; prefer 03/09/15/21Z; proposed 5° tiles |
| Release 1 | ICON-EU / `weather-icon-eu` | `dwd_icon_eu` | Preserve 0.0625° grid, 93 steps through 120 h; 00/06/12/18Z; 10° tiles if browser gates pass |
| Release 1 | UKV / `weather-ukv` | `ukmo_uk_deterministic_2km` | Remap verified ellipsoidal 2 km grid to 0.025°; 0–54 h; 00/06/12/18Z; measured 3° tiles |
| Release 2 | ICON-D2 / `weather-icon-d2` | `dwd_icon_d2` | Preserve 0.02° grid; expected 0–48 h; initially four cycles/day |
| Release 2 | HRRR CONUS / `weather-hrrr` | `ncep_hrrr_conus` | Remap Lambert grid to proposed 0.025°; extended 00/06/12/18Z cycles, expected 0–48 h |
| Release 2 | HRDPS / `weather-hrdps` | `cmc_gem_hrdps` | Remap rotated grid to proposed 0.025°; expected 0–48 h; four cycles/day |
| Optional | AROME HD / `weather-arome-hd` | `meteofrance_arome_france_hd` | 0.01° distribution grid; separate coverage/tile-size and budget decision |
| Optional | ARPEGE Europe / `weather-arpege-eu` | `meteofrance_arpege_europe` | Preserve 0.1° grid; verify cycle-dependent horizon |
| Optional | ARPEGE global, ICON global, ACCESS-G | `meteofrance_arpege_world025`, `dwd_icon`, `bom_access_global` | Separate additions if useful and budgeted; no replacement of current globals |
| Optional | DMI/KNMI HARMONIE, Nordic, Swiss products | Product-specific domains | Add by geography after sample, identity and capacity checks |

Four daily publications are an operating choice, not native update frequency.
Later three-hourly ingestion increases downloads/writes without retaining more
runs. Do not promise every hourly run through `data_run/`. UKV's 120 h direct
product is outside this implementation; HRRR Alaska is not the CONUS layer.

NEMS, national ALADIN and ACCESS-C remain uncommitted because suitable bulk
entries have not been established. NAM is deferred: the NOAA registry announces
retirement on 2026-10-14. Recheck that transition and RRFS bulk access before
adding a successor. Do not substitute ACCESS-G for ACCESS-C or a generic
HARMONIE product for a named ALADIN model.

## Immediate standalone publication fix

This is **Phase 0**, independent of Open-Meteo, and should ship first as a
separate change to `publish.py`, its stores and focused tests. The race exists
now: ensemble and ECMWF-short dispatch together at 00:15/12:15, and all seven
fallback crons fire at :37. These are overlapping jobs, not proof of a collision
on every run.

Two unconditional pointer updates can lose a layer's new entry. Its cleanup
still executes, potentially deleting the previous run referenced by the winning
document and orphaning its new run outside manifest-based storage accounting.

1. Add reads returning body plus ETag and conditional pointer writes. Create
   with `If-None-Match: *`; update with `If-Match`.
2. On conflict, reread and merge only the publishing layer, then retry with
   bounded backoff. Never retry a stale whole document.
3. At each commit attempt reject a cycle older than the layer's current one.
   Same-cycle operations must preserve the existing previous pointer. Recover
   an uncertain write result by rereading before cleanup.
4. Cleanup follows only confirmed pointer success. Normal retention removes
   completed runs older than the retained previous; it must not delete newer
   or incomplete uploads encountered by listing. Recheck references before
   deletion. Failed/stale publishers must not prune runs.
5. Test controlled interleavings: different-layer updates, pointer creation,
   stale same-layer completion, retry exhaustion and uncertain success. Assert
   both updates survive and all referenced manifests remain. Provide equivalent
   dry-run store behavior and test R2 using an isolated object prefix.

No SDK upgrade is needed for the headers. The isolated R2 trial passed; audit
referenced runs and abandoned objects afterward so existing damage, if any,
is accounted for. The manual `r2-audit` workflow performs this read-only check.

**Implemented 2026-10-02** (`src/ingest/publish.py`, `tests/test_publish_concurrency.py`,
`tests/test_s3_store.py`):

- `commit_layer_entry` re-reads `latest.json` with its ETag on every attempt,
  merges only the publishing layer, writes with `If-Match` (`If-None-Match: *`
  on creation), and retries conflicts up to six times with jittered backoff.
  An older cycle is refused before upload and again at each attempt. A write
  whose outcome is unknown (transport error, 5xx, or boto3's own retry hitting
  412 after the first attempt landed) is settled by the next read, which
  recognizes the exact entry by run ID and `published_at`.
- `apply_retention` runs only after a confirmed commit and deletes only
  complete runs older than the retained previous, re-reading every pointer
  before each deletion and removing tiles before the manifest so an
  interrupted deletion finishes next time. Newer and incomplete runs are
  reported, not deleted.
- `S3Store`, `DirStore` (directory lock plus atomic rename) and the test fake
  share the conditional semantics. `S3Store` takes a key prefix for isolated
  live checks.
- `--force` now re-publishes only the current cycle for existing root layers.
  Regional `--force` is refused. The regional-only operator tool is
  `scripts/regional_control.py`; it conditionally disables one entry or restores
  its validated previous run after dispatch and in-flight writers stop.
- Because retention no longer removes incomplete uploads, `scripts/audit_runs.py`
  reports referenced, superseded and incomplete runs plus dangling references
  and missing tiles, and with `--delete-unreferenced` removes runs nothing
  names once their newest object is older than `--min-age-hours` (default 24, minimum 4).
- Isolated R2 verification: three tests passed in 37.06 s under
  `r2-check/37032148191/`, with temporary objects cleaned afterward.

## Reader and model registry

### Whole-file ingestion

1. Resolve an allowlisted model/cycle. Explicit `--cycle` means exactly that
   cycle; automatic selection uses bounded lookback over allowed cycles. Read
   the configured pointer and skip already-published runs before array downloads.
2. GET the run's `meta.json`, recording ETag, reference time, variables and
   valid times; require the registered files and intended horizon.
3. Download only required whole variable files anonymously over HTTPS with
   `requests`, streaming to temporary files. Record response ETags, lengths
   and source keys; reject truncated responses. Start with three sequential
   GETs, finite timeouts and bounded retries for transient errors/429/5xx.
4. Decode locally with `omfiles==1.2.0` as the initially tested candidate.
   Verify embedded run time, units, dimensions and timestamps; transpose the
   axes and release each source array when its converted output is ready.
5. Re-read `meta.json` and compare its ETag before publication; reject a changed
   run. If source files can be rewritten before the completion marker changes,
   add final HEAD checks of their ETags. A stable marker is not proof of
   transactional updates under an undocumented rewrite process.
6. Align times, normalize vectors/grids, apply the registered mask, validate,
   tile and publish. Unexpected missing files/steps or grid changes abort;
   only explicitly documented analysis-step omissions are allowed.

Use `data_run/` only. Its three-month archive is distinct from our two-run
retention. Do not use rolling `data/` or an in-progress marker for immutable
runs. An unavailable explicit cycle waits within its configured window, then
records failure and preserves the prior good forecast.

### Dependencies and licensing

Add plain `omfiles` to an `openmeteo` optional dependency group, using existing
NumPy/requests. The UKV extra now also pins `pyproj==3.7.2`. The review confirms 1.2.0
wheels work on Python 3.13. Do not add `omfiles[fsspec]`, s3fs or aiobotocore:
their botocore constraints affect the single lockfile even when other workflows
do not install the extra. Preserve existing boto3/botocore pins.

The repo now has an **MIT LICENSE**, chosen by the owner on 2 October.
GPL-2.0-only `omfiles` remains an optional ingestion dependency. The repository
licence does not relicense that dependency; preserve its own licence obligations
when redistributing software containing it.
Data licensing is separate: the bulk catalogue declares CC BY 4.0, while the
UKV upstream listing specifies CC BY-SA. [DATA-LICENSES.md](../DATA-LICENSES.md)
records its attribution, licence link and modifications before redistribution. Do not copy AGPL server code; consuming files does not
require running that server.

### Registry and files

Register model/layer IDs, domain, cycle-specific axes/lag, cadence, polling and
readiness policy, variables/units, vector frame, gust windows, expected masks,
exact grid, tile size, resource caps, attribution and production enablement.

`LAYERS`, `MAX_MISSING`, `POLL_SECONDS`, `CADENCE_HOURS` and
`SKIP_WHEN_NOT_AVAILABLE` must all obtain new-layer settings through registry
accessors. Adding only a CLI name leaves `MAX_MISSING[layer]` and
`POLL_SECONDS[layer]` raising `KeyError`; missing cadence/skip entries cause
incorrect metadata or handling. Preserve all existing configured values and
test every new entry through every accessor and the CLI dry-run path.

| Planned file/area | Responsibility |
|---|---|
| `src/ingest/sources/openmeteo/registry.py` | Product definitions and per-layer settings |
| `src/ingest/sources/openmeteo/catalog.py` | Complete-run metadata, cycle selection, object identity |
| `src/ingest/sources/openmeteo/reader.py` | Whole-file GETs and local decoding |
| `src/ingest/sources/openmeteo/grids.py` | Exact geometry, masks, projection and interpolation weights |
| `src/ingest/sources/openmeteo/adapter.py` | Variable conversion and `ForecastCube` construction |
| `src/ingest/cli.py`, `validate.py`, `publish.py` | Registry integration, masks, chosen pointer and combined accounting |
| `src/ingest/tile.py`, `src/tilekit/tiles.py` | Per-product tile size, defaulting to existing 10° |
| `scripts/probe_openmeteo.py` | Remaining focused checks and reproducible benchmarks |
| `tests/fixtures/openmeteo/`, `tests/test_openmeteo_*.py` | Small attributed samples and meaningful behavior tests |
| `.github/workflows/ingest-openmeteo.yml` | Single-model dispatch and scheduled matrix catch-up |

Implemented command (regional publication remains disabled):

```sh
uv run --extra openmeteo ingest weather-arome --cycle YYYYMMDDTHH --dry-run /tmp/arome-tiles
```

## Numerical conversion and masks

### AROME masks are part of Phase 1

AROME is reported missing at 17.2% of grid cells at every wind step, and about
18.8% overall for gust because gust also lacks lead zero. ICON-EU had no spatial
holes in the checked runs. The existing 5% global-weather limit rejects valid
AROME and must not be reused unchanged.

Define a versioned expected footprint from checked reference cycles and
geometry, with a fingerprint verified across independent runs. Do not infer an
allowed mask afresh from each field; that could hide a failed download. Validate
missingness **inside** the footprint at every required time, with explicit
allowed missing times for gust. Track external masking separately and detect
footprint changes. Start with a proposed 0.5% interior-missing limit per field/
time and calibrate from fixtures, not an arbitrary 20% aggregate allowance.
Keep existing-layer thresholds unchanged.

Validation must report mismatched wind/gust axes or shapes instead of throwing
a broadcasting exception. Test an unexpected interior hole and an all-missing
required timestep. Fully masked edge tiles remain omitted; partial masks remain
missing values in delivered tiles.

### Time, wind and precision

Keep u/v/gust on the **same wind time axis** in this implementation. Align by
actual timestamp, never array position. Add an all-missing +0 h gust slice for
AROME/ICON-EU, whose gust begins at +1 h. Preserve UKV's provided step-0 gust
subject to verifying its interval definition. Separate gust axes are outside
scope because the current check compares arrays directly.

Output existing `wind_u_kt`, `wind_v_kt`, `gust_kt` at int16 scales 0.01, 0.01,
0.1 kt. Upstream u/v, speed and gust are reported at 0.1 m/s precision; UKV
direction is quantized to 2°. One-degree rounding at 30 kt can contribute
about 0.52 kt crosswind error. Record source precision separately from output
encoding: finer scales cannot recover lost information.

UKV stores speed/direction. After verifying a meteorological direction measured
clockwise from true north, use `u = -speed * sin(theta)` and
`v = -speed * cos(theta)` with theta in radians, then m/s to knots. Convert
before interpolation across 359°/1°. Rotate grid-relative vectors once where
required; never rotate already earth-relative fields twice. Test cardinal and
nonzero projection-angle cases.

Establish gust windows independently of timestep spacing, especially AROME
and ICON-EU after +78 h. Use `Statistic("max", window_h)` for verified
integer-hour windows. Unknown semantics block wind-plus-gust activation; any
deliberate wind-only product must advertise that capability. Do not synthesize
gust or clamp data to pass validation. Subhourly data and other hazards are
later extensions.

### Geometry

Keep AROME/ICON-EU's documented geographic grid origin/spacing and AROME's
mask. Verify orientation/longitude convention at known points. Do not infer
spacing from two noisy coordinates.

Register exact UKV geometry: `nx=1042`, `ny=970`, `x0=-1158000 m`,
`y0=-1036000 m`, `dx=dy=2000 m`; Lambert azimuthal equal area centred on
longitude −2.5°, latitude 54.9°, native ellipsoid semi-major 6378137 m and
semi-minor 6356752.314140356 m. Bulk WKT incorrectly substitutes a 6371229 m
sphere, shifting sampled cells by up to 4.681 km. Complete native/bulk value
identity establishes shared indices; the adapter explicitly records the primary
CRS correction. Native axes and independent reference coordinates, rather than
a rounded BBOX, fix the origin.

Approve a fixed served geographic grid for projected models, initially
proposing 0.025°, and record both native and served geometry. Bilinearly
interpolate earth-relative vectors and gust in verified source coordinates,
cache weights by grid fingerprint, include the required halo, and mask invalid
neighbors/corners rather than extrapolating. Changed geometry requires review.
HRRR/HRDPS need independent fixtures despite shared transformation code.

Use the existing cube pipeline initially: reported AROME/ICON-EU working sets
fit standard runners. Target peak RAM below 6 GiB and temporary disk below half
the runner's measured free space. Do not build a streaming framework without
a measured requirement.

## Browser budgets and tile layout

Passage's 64 MiB decoded cache cannot retain a reported 100 MB AROME tile.
A route crossing three tiles can download about 45 MB. The current 10° AROME
layout must therefore not be included automatically in normal briefings.

Use these **proposed release gates**, measured with the actual consumer:

| Resource | Initial gate |
|---|---|
| Each compressed regional tile | At most 8 MiB |
| Each tile's decoded Float32 arrays | At most 32 MiB; retain existing 64 MiB shared cache |
| Automatic regional transfer per analysis | At most 20 MiB across all enabled regionals, preflighted from manifests |
| Explicit larger request | Display transfer estimate and require opt-in; initial 50 MiB cap or a smaller requested region |
| Added browser working set from regional loading | Target at most 128 MiB above the same root-only workload, including regional retained, in-flight and decompression buffers |
| Repeated reads | Reuse retained tiles without repeated decoding; initially allow one regional decode at a time |

These are product limits, not measured passes. Keep new regional comparison
opt-in by default. If an automatic request exceeds its budget, retain the global
briefing and offer an explicit regional request. Do not silently coarsen data.
Measure cold/warm transfer, requests, decode CPU time, repeated decodes and both
total and additional peak memory on a representative phone and desktop. Enforce
regional working-set admission against that baseline;
an LRU retention limit alone does not cap transient memory.

**Preferred AROME remedy: 5° tiles at the same 0.025° forecast resolution.**
A full 200 × 200 tile with 52 times and three Float32 fields is about 25 MB
decoded versus about 100 MB at 10°. Compression and route totals still need
measurement. UKV 5° failed the gzip cap: use measured 3° instead, at unchanged
forecast resolution. ICON-EU stays at 10° after passing its gates. Never change layout under a published immutable run ID.

This requires coordinated work: the manifest schema formerly fixed `tile_deg` at 10,
and producer/Passage tile math formerly hardcoded 10°. Permit 3°/5°/10° layouts
and pass manifest tile size through enumeration, point lookup, edge-neighbor
probes, mosaics and export. Existing products default to 10°, preserving their
bytes and URLs. Test all supported sizes together. PFT1 encoding and coordinate-bearing
headers remain compatible, but smaller tiles cannot ship before consumer support.

## Regional catalogue and consumers

Deployed Passage reads every root-listed manifest in `initOnce()` and passes
`describe()` into next-run estimates for any layer with `cadence_hours`.
It does not ignore unknown layers: publishing AROME to root would change
briefings before model support ships.

Use `latest-regional.json` from the first regional publish. Thread an explicit
catalogue key through publication, duplicate checks, previous-run lookup,
status and rollback; default every existing command to `latest.json`. Keep
immutable runs under distinct layer IDs in `forecast-runs/`. Root and regional
allowlists are disjoint; reject duplicate layer ownership.

Both pointers need CAS since regionals still share a pointer. Separation
isolates consumers and rollback; it does not fix the existing root race.
Storage accounting/reconciliation must include **both** catalogues, their
current/previous runs, metadata and abandoned uploads. A separate pointer is
not another storage allowance.

Coordinate canonical Passage schemas and vendored forecast-tiles copies:

1. Add new layer IDs and permit digits in run-ID prefixes for ICON-D2. Define
   a regional-pointer schema with the existing entry structure and regional
   allowlist; keep root's seven-layer contract. Support 5°/10° explicitly.
2. Add optional coverage, source/served geometry, capabilities, attribution and
   schedule fields. Regional next-run estimates must use cycle-specific delay
   windows; six-hour cycle cadence does not predict AROME file arrival. Omit
   these estimates until supported rather than display invented times.
3. Make regional catalogue fetching optional under explicit feature/model
   allowlists. Root remains required; regional 404/outage is nonfatal. Disabled
   models must not appear in ordinary briefings. Cache refresh/eviction uses
   the union of active runs from both pointers.
4. Extend `getHazardForecasts()` for selected regionals meeting coverage, time,
   capability and transfer budgets; preserve ECMWF's `modelRuns.ts` behavior.
   Unprovided hazard fields stay unavailable, not zero-filled.
5. Keep default `getWindGrid()` and routing on GFS. Named regional selection
   validates route/time coverage and returns unavailable outside its domain/
   horizon. Never insert unlabeled GFS values. Do not count the distributor as
   another model or treat related AROME products as independent evidence.
6. Update selectors, provenance and GRIB/export registries. Verify model cycle,
   grid, times and gust intervals before enabling export. Audit Tactician and
   other consumers separately; decoding tiles alone does not prove export support.

`z_res()` labels 0.025° as `z002` and 0.0625° as `z006`. Keep old URLs unchanged
but give new products explicit labels such as `grid-0p025` and `grid-0p0625`.
Consumers must follow `path_template`, not infer resolution from its label.

Provenance separates originating weather service/model from Open-Meteo. Record
run, source identity digest, adapter version, precision, conversions, masks,
native/served geometry and attribution. Store large inventories once per run,
not in every tile header.

## Concurrency and immutable publication

The regional path uses three controls:

1. **Same per-layer GitHub group for every trigger.** Normalize dispatched and
   scheduled work into a job matrix and use job-level
   `group: ingest-${{ matrix.layer }}` with `cancel-in-progress: false`.
   A catch-up job spanning several layers must not bypass those groups. Bound
   matrix parallelism; the next latest-cycle catch-up recovers replaced pending
   work. GitHub concurrency is not a durable queue for every historic cycle.
2. **`If-None-Match: *` on every immutable tile and manifest.** A second writer
   fails rather than overwrites. Manifest is last; skip already-published
   complete cycles before upload. A partial-upload conflict does not authorize
   overwrite: inspect and clean an unreferenced abandoned prefix after active
   writers stop, then retry. No automated lease takeover or routine `--force`.
3. **CAS plus the older-cycle check on the chosen pointer.** Reuse the
   standalone fix. Validate the complete run before discovery, and only clean
   retention after confirmed pointer success.

GitHub groups do not coordinate arbitrary CLI processes. Conditional object
creation prevents same-run corruption, CAS prevents lost/older updates, and
cleanup must skip newer/incomplete prefixes so a slower writer cannot delete
a newer upload. Orphan cleanup is separate, age-gated and reference-aware
across both pointers. Count failed uploads until actually deleted. ETags are
concurrency tokens, not substitutes for our tile content hashes.

Changed data/adapter behavior uses a new cycle, or a separately designed
revision ID if a same-cycle correction is indispensable. Do not mutate a served
immutable run as part of normal migration.

## Storage decision and costs

The root publisher retains its historical 8,000,000,000-byte fallback guard,
with an owner-approved GitHub variable override applied consistently across
workflows. Regional admission additionally
reserves calculated root upload overlap, three capped runs per enabled model,
all nonreferenced objects and explicit headroom. Both catalogues and the whole
bucket inventory are required; unknown or damaged capacity fails closed.

Use compressed measurements instead of the earlier raw estimates:

| Release 1 component | Measured gzip/run | Two runs | Three runs during upload | Fixed run cap |
|---|---:|---:|---:|---:|
| AROME 09Z, 5° | 0.0733 GB | 0.1466 GB | 0.2198 GB | 0.110 GB |
| ICON-EU 12Z, 10° | 0.1698 GB | 0.3397 GB | 0.5095 GB | 0.200 GB |
| UKV 12Z, 3° | 0.1640 GB | 0.3281 GB | 0.4921 GB | 0.200 GB |
| Combined regional addition | **0.4072 GB** | **0.8143 GB** | **1.2215 GB** | **0.510 GB** |

These tile measurements exclude small manifests; caps include them. Do not
combine historical root projections with a current dashboard counter.
`scripts/capacity_profile.py` measures larger current/previous root runs,
calculates simultaneous third-run overlap with 10% variation, adds three
regional caps, nonreferenced objects and headroom, and prepares a rounded guard.
It establishes a calculated envelope, not an observed historical peak.

**Owner decision, 2026-10-02:** the prepared capacity/cost proposal was approved.
The three capacity variables are applied and verified by GitHub read-back after
a [fresh read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37055878696)
confirmed that the calculated reservation fits the approved guard. Private
configuration and account figures remain outside this public repository.
No model enablement or deployment was performed. Refresh the read-only profile
across representative cycles before activation and preserve existing products.
The code fallback remains 8 GB; Release 1 is not assumed to fit that fallback.

**Release 2 decision:** do not enable ICON-D2, HRRR or HRDPS using the remaining
Release 1 allocation. Re-budget first. The review expects the full set to exceed
8 GB; exact compressed sizes remain unmeasured. Prefer a deliberate guard
increase if the small expense is acceptable; otherwise choose fewer new models
or an explicitly smaller product. Keep existing products intact.

Reserve measured existing-layer peak capacity plus headroom. Give each new
model a fixed compressed run cap `b_i`; allocate `2 * sum(b_i)` for retained
regional tiles and `3 * sum(b_i)` for simultaneous uploads, plus all other
bytes. Refuse oversized runs before upload. Fixed allocations and one active
job per layer avoid competing for the same spare capacity; the current guard
is not an atomic global reservation. Reconcile after failed cleanup and refuse
regional admission on unknown/exhausted capacity. No lease service is needed.

Raising a software guard does not itself incur charges: actual R2 usage does.
Standard storage is $0.015 per billable GB-month beyond the account's shared
10 GB-month allowance, before rounding/tax. Free Class A (1 million) and
Class B (10 million) operations are also account-wide; storage billing averages
daily peaks. A new bucket/pointer adds no allowance. A separate account is not
required. Keep private account figures outside this public document.

Measure source bytes/requests, gzip totals, largest tile, writes, retained and
upload peak, runtime/RAM/disk, metadata polls, readback and cleanup. Verify
GitHub runner eligibility and artifact limits. Fewer updates reduce processing/
writes, not two-run storage. No numerical bulk quota is documented, but neither
throughput nor uninterrupted access is guaranteed. No paid API is required;
zero infrastructure expense is not promised.

## Scheduling and observability

The review's eight-day `meta.json` sample reports these completion delays:

| Product/cycle | Cycle to complete files |
|---|---|
| ICON-EU | 3.6–3.75 h |
| UKV | About 4.4 h; one run at 5.6 h |
| AROME 00 / 03 / 06 / 09Z | About 2.9 / 2.8 / 5.3 / 4.3 h |
| AROME 12 / 15 / 18 / 21Z | About 4.1 / 3.9 / 5.1 / 4.2 h |

These are observations, not an SLA or p95 guarantee. Give AROME a separate
entry for each selected cycle. Prefer 03/09/15/21Z: 03Z arrives around
05:45–05:50 UTC, fresher than 00Z well before 06Z lands around 11:20.

Proposed first four-cycle timetable, refined later from canary logs:

| Model cycle | Dispatch UTC | Lag / wait |
|---|---|---|
| AROME 03Z | 05:45 | 2 h 45 / 90 min |
| AROME 09Z | 13:15 | 4 h 15 / 90 min |
| AROME 15Z | 18:45 | 3 h 45 / 90 min |
| AROME 21Z | 01:15 next day | 4 h 15 / 90 min |
| ICON-EU 00/06/12/18Z | 03:25 / 09:25 / 15:25 / 21:25 | 3 h 25 / 45 min |
| UKV 00/06/12/18Z | 04:15 / 10:15 / 16:15 / 22:15 | 4 h 15 / 120 min |

All new slots align to five-minute boundaries and reuse existing :15/:25/:45
cron minutes. Extend hours on those three expressions; leave :00/:20 and
existing timetable entries unchanged. Dispatch all entries sharing a slot.
Replay a full UTC day, late fires and date rollovers to prove current cycle
requests and wait windows are identical.

Do not switch casually to `*/5`: `dispatch.ts` suppresses `MISSED SLOT` for
those ticks and `slotAt()` selects only one slot per five-minute window. A future
switch needs explicit missed-dispatch monitoring and coverage tests. This
release preserves the current alarm without adding a Cron Trigger.

Add explicit workflow/layer routing, allowlist inputs in dispatcher and CLI,
and run catch-up as the same per-layer matrix. Start canaries with AROME 03/15Z
and ICON/UKV 00/12Z. Bound timeouts/parallelism so new jobs do not starve existing
jobs. Worker deployment remains a maintainer operation under current repo rules.

Track attempts separately from last success: failure category, source lag,
ETags/bytes, output/large-tile sizes, decode/encode time and chosen pointer.
Failures do not replace good forecasts. Flag stale data after two missed
publication opportunities plus measured source lag. Keep manual retry and a
per-model disable switch; no new notification service is needed.

Implemented: `ingest --attempt-report FILE` writes separate JSON evidence;
`ingest-openmeteo` uploads it on success or failure with 30-day retention.
Reports distinguish scratch, confirmed R2 publication, upstream skips and
failures, and capture the prior successful entry without modifying it. The
read-only `scripts/regional_health.py` flags the two-opportunity threshold
using an explicitly supplied observed source lag and full/canary cycle profile.
Missing reports and missing/invalid pointers remain unknown; no alert delivery
or seven-day acceptance is inferred. Commands and measurement limits are in
[regional operations](regional-delivery.md).

## Implementation order and gates

### Phase 0 — Standalone current-publisher fix

Implement the CAS/older-cycle/cleanup fix and interleaving tests above. Check
R2 using isolated objects, then release independently. No GPL dependency is
involved; this must not wait for UKV or the repository licence decision.

**Exit:** both layer updates survive; stale jobs cannot roll back or prune
references; retries are bounded; existing publisher behavior/tests pass.

### Phase 1 — AROME dry run

Resolve the repository licence prerequisite, add plain `omfiles`, registry and
whole-file reader. Capture source fixtures; close AROME gust semantics and
known-point orientation. Implement footprint/time validation, gust padding,
transpose and all CLI settings. Produce local PFT1 tiles and a benchmark.

**Exit:** expected mask/+0 h gust are accepted; unexpected interior holes,
missing wind steps and changed geometry fail clearly; no new-layer `KeyError`;
numerics and reported resources are reproducible. No production writes yet.

**Done 2026-10-02**, including primary-source gust semantics:

- MIT `LICENSE`; `omfiles==1.2.0` in the `openmeteo` extra only, with no
  fsspec/s3fs. Production workflows still run plain `uv sync`, and every
  existing layer imports and runs without `omfiles`.
- `src/ingest/sources/openmeteo/`: `registry` (layers, grids, axes, files,
  footprint, limits, scheduling, run caps, production gate; every CLI setting
  through `cli.max_missing` / `poll_seconds` / `skip_when_not_available` and
  `publish.cadence_hours_for`), `catalog` (meta.json completion, exact
  explicit cycle, bounded lookback, source digest, ETag recheck before
  publication), `reader` (three sequential whole-file GETs with ETag/length
  records, truncation refusal and bounded retries; decode checks grid, CRS
  BBOX, unit, run time and timestamps, then transposes band by band),
  `grids` (exact geometry, versioned footprints) and `adapter`.
- Live probe of AROME 2026-10-02T03Z: 52 wind steps (0–51 h) and 51 gust
  steps (1–51 h), stored `[lat, lon, time]` at 0.1 m/s. Static terrain
  confirms ascending rows: Mont Blanc reads 3,887 m in the expected cell and
  would read 1,250 m if the rows were flipped. The missing outline (17.18 %)
  equals the static terrain file's NaN mask. Checked against five runs, it
  has no interior gap at any step and at most 2 valid edge cells outside it per
  step. It is registered as footprint `meteofrance_arome_france0025.v1`
  (SHA-256 pinned). Validation is per step inside it (limit 0.5 %), with +0 h
  gust exempt. More than 0.05 % of outside cells carrying data is refused as
  a changed footprint, and the outside is masked.
- Measurements reproduced, not just reported: AROME 03Z with gust under its
  expected 1 h window makes 11 tiles and 74.2 MB at 10° (largest 19.6 MB gz,
  99.8 MB decoded). At 5° it makes **28 tiles and 73.9 MB (largest 5.2 MB gz,
  25.0 MB decoded)**, inside the 8 MiB / 32 MiB gates. Peak RSS was 1.3 GB and
  encoding took 30 s. A live wind-only `ingest weather-arome --dry-run` of 09Z
  wrote 48.4 MB in 32 s.
- Gust remains fail-closed unless `GustWindows.verified` is set. It is now set
  from sampled +1..6, +13..18 and +49..51 h GRIB messages, all one-hour maxima.
  A fresh 09Z dry run with verified gust made 28 tiles / 73,281,185 bytes.

### Phase 2 — ICON-EU dry run

Reuse the adapter; verify the 93-step axis, gust windows after +78 h and the
unmasked grid. Capture references and repeat the benchmark. Add no reader
abstraction without a demonstrated incompatibility.

**Exit:** timestamp-aligned wind/gust through 120 h; unexpected missing data
rejected without weakening current model thresholds.

**Done 2026-10-02**, including the post-+78 h gust window. The same adapter
needed no new reader code. ICON-EU 2026-10-02T06Z has 93 wind steps (hourly
0–78 h, 3-hourly 81–120 h) and 92 gust steps from +1 h, with no missing cell
at any step. Its 03/09/15/21Z runs stop at +30 h, so only 00/06/12/18Z are
registered. Terrain confirms the orientation (Etna 2,228 m, Elbrus 3,717 m).
The earlier assumed-window benchmark at 10° gave **60 tiles and
170.3 MB (largest 6.6 MB gz, 28.6 MB decoded)**, inside the gates without
smaller tiles. Peak RSS was 2.7 GB. A live wind-only dry run wrote 105.6 MB
in 70 s. Missing data is validated per step over the whole grid at 0.5 %,
stricter than the 5 % global rule, which is unchanged. Primary DWD messages at
+1, +2, +78, +81, +84 and +120 h all declare one-hour windows. A fresh 12Z
dry run with those verified windows made 60 tiles / 169,837,222 bytes.

**Started ahead of Phase 3** (producer side only, dry runs only): a
per-product `tile_deg` (5 or 10; existing layers keep 10° and
byte-identical tiles) with explicit `grid-0p025` / `grid-0p0625` path labels;
`latest-regional.json` with the same compare-and-swap commit; refusal to
commit a layer to the other pointer; both pointers counted by the storage
guard, retention's reference recheck and the audit; and a per-model run cap
(`max_run_bytes`) enforced before upload. Phase 3 now supplies the coordinated
consumer/schema and immutable-publication work described below.

### Phase 3 — Regional pointer, browser delivery and Passage

Implement separate-pointer publication, both-catalogue capacity accounting,
immutable object creation and shared matrix groups. Coordinate layer/digit-ID
and 5°/10° schemas, model-aware tile math, optional catalogue fetch, comparison/
selection, cache behavior, attribution and transfer admission. Preserve GFS
defaults and root-only clients. Re-measure AROME with 5° tiles and combined
storage/writes before regional activation.

**Exit:** root-only briefing behavior is unchanged, regional absence is harmless,
browser gates pass, conflicts cannot overwrite immutable data and rollback is
regional-only. Activate AROME and ICON-EU individually through seven-day canaries.

**Engineering implemented 2026-10-02; activation gates remain open.**
Both repositories support 3°/5°/10° geometry, opt-in catalogues and attribution.
Regional comparison shares a 20 MiB preflight allowance; explicit exports cap
transfer at 50 MiB. Decompression/decoded allocations are bounded, regional
decode is serialized, and transient admission supplements the shared 64 MiB LRU.
Root defaults and ECMWF run selection pass existing regressions. Immutable
creation rejects complete/partial conflicts; same-cycle retry never overwrites.
Fixed reservations count existing upload peaks, three capped regional runs,
nonreferenced objects and headroom. Unknown capacity refuses before upload.
The workflow/Worker are disabled by registry and allowlist gates and default to
the reduced canary cadence. Desktop measurements are recorded in
[regional-delivery.md](regional-delivery.md); representative desktop root
workload and individual physical-phone selections passed. A combined phone
retest after the cache refinement remains open. Seven-day live canaries have
not started.

### Phase 4 — UKV

Resolve direction frame, gust and licence; implement exact registered geometry,
vector conversion, remapping/masks and smaller tiles. Measure post-remap storage
and browser behavior, replacing the 90–110 MB/run estimate. Check geographic
reference points independently of production transformation code.

**Exit:** numeric/footprint/browser gates pass, current capacity fits and UKV
passes its seven-day canary. Release 1 is complete only after all three models.

**Implemented 2026-10-02, production disabled.** Full native/bulk comparisons
resolve cell identity and wind/gust semantics. The adapter corrects the bulk
sphere to the primary ellipsoid, remaps geographic u/v and instantaneous gust
onto a pinned 0.025° footprint and preserves +0 h. A 3° layout passes tile
limits after 5° failed. Producer/Passage contracts, lookup, export, selection
and disabled scheduling support it. Source precision, licence and post-remap
measurements are in [ukv-discovery.md](ukv-discovery.md); representative desktop
checks and individual phone selections pass. Final combined-phone retest,
representative-cycle capacity checks, deployment and seven-day canary
criteria remain open; the approved capacity configuration is applied.

### Phase 5 — Expansion after a capacity decision

Re-budget before ICON-D2/HRRR/HRDPS. Reuse the reader and projection code but
require per-product masks, time/variable fixtures, resource and consumer checks.
AROME HD, ARPEGE and other models remain explicit later choices. Do not discover
and activate every catalogue domain automatically.

Each canary requires at least 95% of scheduled cycles within the configured
source-completion-plus-ingestion window, every miss explained, no invalid run
published, replacement/retry and disable/rollback exercised. Inject an outage
locally if needed. Distinguish upstream delay from our failures and verify
existing delivery stays unaffected. Seven-day observation can overlap later work.

## Verification and rollback

| Concern | Required evidence |
|---|---|
| Reader | Length/ETag records, truncated GET, transient retries, changed final metadata, exact cycle |
| Arrays/masks | Transpose, known latitude point, stable AROME outline, interior-hole and missing-step failures |
| Gust | Shared axis; AROME/ICON +0 h missing; UKV +0 h preserved; verified windows |
| Wind | Units/precision, cardinal/359°–1° cases, one rotation, independent values within quantization/conversion tolerance |
| Grid | Exact UKV origin versus rounded metadata, fingerprints, no extrapolation, corner/edge masks |
| Browser | Both tile sizes and boundary lookup/mosaic, cold/warm route transfer, decoded/transient memory, retention |
| Pointers | Competing writers, creation/conflicts, stale cycles, uncertain success, no cleanup after failed commit |
| Immutability | Duplicate objects refuse overwrite, partial retry fails safely, manual writer cannot corrupt a run |
| Consumers | Root-only behavior, regional outage, opt-in allowlist, next-run estimates, missing coverage and exports |
| Capacity | Both-pointer union, caps, existing upload headroom, failed uploads and post-retiling totals |
| Scheduler | Shared layer group across triggers, unchanged current dispatches, aligned/grouped slots and missed-slot alarm |

Keep unit tests offline with small attributed fixtures. Implementation changes
run relevant Python tests/ruff, dispatcher typecheck/tests and Passage schema,
store, tile-math, UI/export tests. Live R2 and source benchmarks are explicit
integration checks. Agreement with another model or a default best-match API
response does not validate an exact source run.

Rollback disables model dispatch and consumer selection, stops in-flight work,
then conditionally removes/restores only its `latest-regional.json` entry.
Never restore an old whole root catalogue. An intentional operator rollback
is distinct from normal monotonic publication; keep faulty versions disabled
so catch-up cannot republish them. Retain referenced good runs through the
normal fallback/cache window and clean abandoned objects separately.

## Effort and outstanding evidence

Revised estimate: **8–12 engineering days** for the standalone fix, registry/
reader, adapters, regional catalogue, scheduling and selection, plus **2–4 days**
for smaller tiles and browser-budget enforcement if that preferred remedy is
required: roughly **10–16 days for Release 1**, with seven-day canaries
overlapping subsequent work where possible. This replaces 12–20 days: dropping
range/lease work saves effort, but browser work cannot be omitted. Licence
decisions and access to an R2 trial can add calendar delay. These are estimates,
not delivery commitments.

The original revision used supplied review measurements. The 2 October
implementation additionally reproduced bulk dry runs, primary gust probes,
isolated R2 writes and a desktop browser benchmark; these are separately
recorded in [regional-delivery.md](regional-delivery.md), including individual
physical-phone selections. The final combined-phone retest and seven-day
canaries remain open.

Release 1 is complete only when all three models meet their numerical,
browser, capacity and canary gates; existing seven-layer sources and behavior
remain intact; attribution is visible; and regional rollback has been exercised.
No hosted Open-Meteo API dependency or paid API subscription may be introduced.

## References

- [Passage's recorded capacity projection](https://github.com/deepregatta/passage/blob/main/docs/grib-export-plan.md): Phase 5C projects 6.66 GB after adding the short ECMWF runs; this is not a current inventory.
- [Open-Meteo bulk catalogue and layouts](https://github.com/open-meteo/open-data/blob/4fd52ad16c417c49bff45fab4bf175e5ea5760f2/README.md): models, completion metadata, retention, exclusions and dataset licence.
- [Python OM reader](https://github.com/open-meteo/python-omfiles/blob/8082fd0dac4fdbe89b8e9c16d79a622d2ba4ceab/README.md) and [dependency/licence metadata](https://github.com/open-meteo/python-omfiles/blob/8082fd0dac4fdbe89b8e9c16d79a622d2ba4ceab/pyproject.toml): local decoding and Python support, with GPL-2.0-only declaration; remote-reader extras are not selected.
- [Full-run metadata](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Helper/File/FullRunMetaJson.swift) and [run-file writer](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/Helper/Writer/GenericVariableHandle.swift): reference time, CRS, units and variable timestamps.
- [UKV domain](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/UKMO/UkmoDomain.swift) and [variables](https://github.com/open-meteo/open-meteo/blob/b06f4760fd1f997e5559bb380f64c5e496b4a509/Sources/App/UKMO/UkmoVariable.swift): projected grid, cycle-dependent horizon and stored speed/direction. Source-code evidence is not a live inventory guarantee.
- [Met Office UK deterministic registry](https://github.com/awslabs/open-data-registry/blob/00462862e4c1926cd35809ed2ac1c7b5b67b5bdd/datasets/met-office-uk-deterministic.yaml): direct-product horizons and upstream licence.
- [Cloudflare R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/) and [pricing](https://developers.cloudflare.com/r2/pricing/): conditional writes, Standard storage/operations and free allowances. Recheck during implementation.
- [Current publisher](../src/ingest/publish.py), [CLI](../src/ingest/cli.py), [cube](../src/ingest/cube.py), [validator](../src/ingest/validate.py), [tiler](../src/ingest/tile.py) and [dispatcher](../dispatcher/src/timetable.ts): current extension points and constraints.
- [Passage forecast store](https://github.com/deepregatta/passage/blob/main/engine/src/forecast/tileStore.ts), [model run selection](https://github.com/deepregatta/passage/blob/main/engine/src/forecast/modelRuns.ts) and [canonical schemas](https://github.com/deepregatta/passage/tree/main/contracts): coordinated consumer work; moving links, verify at implementation time.
