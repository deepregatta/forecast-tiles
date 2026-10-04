# forecast-tiles

Scheduled ingestion pipeline that turns open NOAA / Copernicus / ECMWF forecast
data into compact, immutable, provider-independent **PFT1** tiles served from
Cloudflare R2 and consumed by [Passage](https://github.com/deepregatta/passage)
entirely in the browser.

[Regional model access and integration assessment](docs/regional-model-access.md)
covers AROME, ARPEGE, ICON, UKV, HRRR/RRFS, HRDPS, NAM, ACCESS, NEMS and national
ALADIN products, including delivery routes, reuse constraints and the pipeline
changes needed to support them.

[Open-Meteo bulk integration plan](docs/open-meteo-bulk-implementation-plan.md)
sets out the implementation of new regional models through public AWS files,
starting with AROME, ICON-EU and UKV while preserving every existing source.
It covers whole-file ingestion, a separate regional catalogue, mask validation,
browser and storage budgets, and staged rollout. Its first, standalone step,
the fix for concurrent publishers sharing `latest.json`, is in place (below);
the isolated live R2 conditional-write check passed on 2026-10-02, as recorded
in [regional delivery evidence](docs/regional-delivery.md#reproduced-evidence-2026-10-02).

```
NOAA GFS / GEFS / GFS-Wave · Copernicus GLO12 / IBI · ECMWF open data
        │  scheduled GitHub Actions (this repo)
        ▼
normalize → validate → quantize (int16/int8) → 10°×10° tiles → gzip
        │
        ▼
Cloudflare R2:  forecast-runs/{run_id}/…  (immutable)  +  latest.json

GSHHG shoreline · EMODnet Bathymetry DTM
        │  one-shot, manually dispatched (this repo)
        ▼
rasterize conservatively → union → buffer outward → 10°×10° TLI1 tiles → gzip
        │
        ▼
Cloudflare R2:  land-index/{index_id}/… (immutable) + land-index/latest.json
```

The second pipeline is [the conservative routing index](docs/land-index-format.md),
consumed by [Tactician](https://github.com/deepregatta/tactician)'s routing core
so that "no land crossing" means a coastline rather than a test polygon. It is
**routing legality only** — not a chart, not a navigation product, and not a
display basemap.

## Operational status and verification

Configuration and delivery evidence answer different questions:

| Capability | Checked-in configuration | Available operational evidence |
|---|---|---|
| Seven root forecast layers | Dedicated workflows; dispatcher `DRY_RUN="false"`; six-hourly :37 fallback crons | Dated 1–3 October dispatcher checkpoint below; current live operation requires fresh logs and publication read-back |
| AROME, ICON-EU, UKV | Registry production gates enabled; Worker allowlist includes all three; full cadence off; manual workflow dry run defaults on; :47 catch-up cron | [2–3 October publication receipts and read-back](docs/regional-canary-status.json); seven-day acceptance remains open |
| Current layers | `currents`: CMEMS GLO12, six-hourly samples to 240 h; `currents-ibi`: CMEMS IBI, hourly means to 120 h | Dated provider timings in the dispatcher table; no alternate provider is supported |
| Routing index | One-shot `ingest land`, manual workflow only | [Versioned index and reproducibility evidence](docs/land-index-format.md) |

For regionals, **enabled** means the registry gate is open; production also
needs matching GitHub `OPENMETEO_ENABLED_LAYERS`, runtime
`REGIONAL_ENABLED_LAYERS`, and capacity controls. **Dry run** means the exact
forecast layout is written to a scratch directory. **Disabled** means a closed
production gate or omitted activation allowlist; it does not disable scratch
validation. The GitHub and deployment flags in the dated record are evidence
at that checkpoint, not a read-back of current settings. Current provider,
Worker and publication health are **unverified** until checked; neither config
nor a successful job alone proves that a new forecast was published. Seven-day
acceptance and any full-cadence promotion are separate from local/hosted CI.

After installing the lockfile-managed dependencies (`uv sync --all-extras`
and `(cd dispatcher && npm ci)`), run the same gates used by CI:

```sh
bash scripts/verify.sh
```

The wrapper prints the commit, tool versions and exact commands, then runs
Python tests, Ruff lint/format, dispatcher TypeScript checks and Vitest in
sequence. It uses the installed environment without syncing dependencies.
`python` and `dispatcher` select those existing gate groups; the default is
both. It unsets `R2_TEST_PREFIX` and deliberately excludes
`tests/test_r2_conditional.py`, the provider-write suite. Its isolated live
proof is a separate operator gate, not part of offline CI. No ingestion,
deployment or provider writes are performed by the wrapper.

Maintained documentation: [shared contract ownership and drift checks](docs/shared-contracts.md),
[regional operations and evidence](docs/regional-delivery.md),
[canary status](docs/regional-canary-status.json),
[regional implementation plan](docs/open-meteo-bulk-implementation-plan.md),
[IBI geometry and horizon](docs/ibi-currents.md),
[spending controls](docs/paid-work.md),
[checksum phase measurements](docs/checksum-profile.md),
[routing index](docs/land-index-format.md). The
[regional access assessment](docs/regional-model-access.md) and
[Phase 0 measurements](docs/phase0-results.md) retain their dated evidence;
use the operational records above for deployment/acceptance status.

## Running the pipeline

[Optional production spending control](docs/paid-work.md) documents the
persistent frequency, runtime and duplicate guard, its activation variable,
intentional pause reports, and operator recovery. It preserves consumer reads.

```sh
uv sync
uv run ingest weather                        # latest complete GFS cycle -> R2
uv run ingest weather --cycle 20260713T06    # explicit cycle
uv run ingest weather --dry-run /tmp/tiles   # write the R2 layout locally instead
uv run ingest ensemble                      # other root weather layers: waves, weather-ecmwf
uv run ingest weather-ecmwf-short             # ECMWF's 06Z/18Z runs, to 144 h
uv sync --extra currents                    # required for either CMEMS current layer
uv run ingest currents                      # six-hourly global current field
uv run ingest currents-ibi                   # hourly regional current field
uv run ingest weather --force                # re-publish the cycle latest.json already has (repairs only)
uv run ingest weather --cycle 20260930T06 --wait-minutes 90   # wait for the provider, then publish
uv run ingest land --domain nweu             # rebuild the routing index (one-shot)
uv sync --extra openmeteo                    # regional dependency; activation is separate
uv run ingest weather-arome --cycle 20261002T09 --dry-run /tmp/arome
uv run ingest weather-icon-eu --dry-run /tmp/icon-eu
uv run ingest weather-ukv --dry-run /tmp/ukv
```

Each run: resolve the latest **complete** provider cycle (`.idx` presence,
falling back one cycle rather than publishing a partial run) → exit 0 with
"already published" when `latest.json` already has that cycle or a newer one
(`--force` re-publishes the same cycle, never an older one; a live run id's
tiles are cached forever, so only to repair a run) → download via
byte-range subsetting → decode/orient/quantize into a `ForecastCube` →
validate (step coverage, physical ranges, gust ≥ wind, missing fraction per
variable/member/time slice,
a window for every step of a `statistic` variable; any failure aborts before upload) → 10°×10° gzipped PFT1 tiles → atomic publish
(older-cycle refusal and storage guard first, tiles, `manifest.json` last,
post-publish re-download check, compare-and-swap of this layer's `latest.json`
entry, retention delete only after that commit, `status/{layer}.json`).

Several layers publish at once (ensemble and `weather-ecmwf-short` are
dispatched together at 00:15 and 12:15, and every fallback cron fires at :37),
and they share `latest.json`. The commit therefore re-reads the pointer,
changes only its own layer's entry and writes with `If-Match` on the ETag it
read (`If-None-Match: *` when there is no pointer yet), retrying a conflict
from a fresh read up to six times. It never moves a layer back to an older
cycle, and settles a write whose outcome is unknown by reading the pointer
again. A job that did not commit deletes nothing; retention deletes only a
layer's complete runs older than its retained previous, and never one a
pointer names. Abandoned uploads (a run with no manifest) and runs stranded
by a stale job are left for the audit:

```sh
uv run scripts/audit_runs.py                  # referenced / superseded / incomplete runs, damage first
uv run scripts/audit_runs.py --delete-unreferenced --min-age-hours 24
```

The deployment recorded on 2026-10-01 configured the [dispatcher](#dispatcher)
to start each layer's workflow
at its provider's usual publication time, for every provider cycle, and the
run waits for its cycle. Each root workflow also keeps a fallback `schedule`
(`37 2,8,14,20 * * *`) for a missed dispatch; GitHub starts those hours late,
and a cycle already published exits in about a minute. See
`.github/workflows/ingest-*.yml`. `ingest weather-ecmwf` exits 0 with a log
line when ECMWF hasn't published a full-horizon cycle yet, `ingest
weather-ecmwf-short` likewise for a 06Z/18Z cycle to 144 h, and `ingest
currents` and `ingest currents-ibi` do the same until Copernicus has finished
writing the bulletin (the public STAC item of the dataset says so).
`currents` build failures propagate their original error with layer/cycle context;
there is no RTOFS outage fallback or additional provider-resolution attempt.
An outage leaves the published pointer unchanged. The current layers' declared
land/ice masks and supported axes remain unchanged.

`--wait-minutes N` (workflow input `wait_minutes`, default 0) lets a run
start before the provider has finished its cycle. With an explicit
`--cycle`, it re-checks readiness every 60 s (ECMWF and Copernicus: 120 s)
until the cycle is out, then continues as above; if N minutes pass first it
exits 1 ("cycle not available after N min"), so a missed slot shows as a
failed run. This is how the [dispatcher](#dispatcher) starts each layer.
Readiness is the provider's own completion signal: the
final-step `.idx` for GFS, GFS-Wave and GEFS; the latest cycle with step 240
for ECMWF (step 144 and a 06Z/18Z cycle for `weather-ecmwf-short`); the STAC item's data end, finished update and no update in
progress for GLO12 and IBI.

The Phase 0 size-measurement prototype is still runnable:
`uv run scripts/size_prototype.py --layers weather`.

## One-time R2 setup (required before the workflows can publish)

1. Create a Cloudflare R2 bucket named `passage-forecast`.
2. Create an R2 API token with **Object Read & Write** scoped to that bucket
   (Cloudflare dashboard → R2 → Manage R2 API Tokens).
3. Add four GitHub Actions repo secrets:
   - `R2_ENDPOINT` — `https://<account-id>.r2.cloudflarestorage.com`
   - `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` — from the API token
   - `R2_BUCKET` — `passage-forecast`
4. Enable public read for the bucket (r2.dev public development URL or a
   custom domain) so the Passage client can fetch tiles.
5. For both Copernicus current layers also add
   `COPERNICUSMARINE_SERVICE_USERNAME` and
   `COPERNICUSMARINE_SERVICE_PASSWORD` (free Copernicus Marine account).
   Transient authentication-service connection failures are retried after 5
   and 15 minutes; invalid credentials still fail immediately.

6. Run the `r2-conditional-check` workflow once (Actions → Run workflow). It
   proves on the real bucket, under a throwaway `r2-check/<run id>/` prefix it
   deletes afterwards, that R2 honours `If-Match` / `If-None-Match` on
   `PutObject`, which the `latest.json` commit relies on.

The storage guard refuses to publish when retained runs + the new run would
exceed `MAX_BUCKET_BYTES` (default 8 GB). It counts tile bytes after retention
across both forecast pointers, and refuses before uploading if a retained
manifest is missing, unreadable, or has invalid/inconsistent size metadata.
Reconcile those references using verified information before retrying. This
guard does not measure physical upload overlap, abandoned objects, or account
usage; regional production also has a separate physical inventory and peak
reservation check.

Every required variable/member/time slice must satisfy its layer's missingness
limit; a cube average cannot hide an empty or damaged step. Ocean layers keep
their land/ice mask allowances, and regional layers use their registered
footprint and interior limit. Sources declare intentional missing offsets in
the cube's validation-only `allowed_missing_steps`: GFS precipitation at +0 h
and ECMWF interval gust at +0 h. These exceptions do not exempt later steps or
change the PFT1 encoding, checksums, or JSON schemas.

## Layers

| Layer | Source | Resolution | Configured cadence |
|---|---|---|---|
| `weather` | NOAA GFS (wind u/v, gust hourly; vis/CAPE/temp/dew-point/precip 3-hourly) | 0.25° | every cycle (00/06/12/18Z) |
| `weather-ecmwf` | ECMWF open data (wind u/v; gust = the maximum over the 1, 3 or 6 h before each step, published with its per-step window as the variable's `statistic`) | 0.25° | 00Z and 12Z (the full 240 h cycles) |
| `weather-ecmwf-short` | the same ECMWF open data, 06Z and 18Z cycles, which stop at 144 h: 3-hourly to 144 h (49 steps, the first part of `weather-ecmwf`'s axis; gust windows 1 h to +90 h, 3 h to +144 h) | 0.25° | 06Z and 18Z (since Passage plan Phase 5C) |
| `ensemble` | NOAA GEFS, 31 members (wind + gust, mean + int8 anomalies; 3-hourly to 144 h, 6-hourly to 384 h) | 0.5° | every cycle |
| `waves` | NOAA GFS-Wave (Hs, period, direction, wind-wave, swell) | 0.25° | every cycle |
| `currents` | Copernicus Marine GLO12 (surface u/v, 6-hourly to 240 h; sole provider) | 1/12° | 1×/day, soon after Copernicus |
| `currents-ibi` | Copernicus Marine IBI analysis-forecast (surface u/v, hourly through 120 h; 72 h before 2026-09-29; IBI domain only) | ≈1/36° (0.02777863°) | 1×/day, soon after Copernicus |

Every layer was ingested once a day until the [dispatcher](#dispatcher)
switched on (Passage `docs/grib-export-plan.md`, Phase 5B). Each layer's
`latest.json` entry carries `cadence_hours`, the hours between its scheduled
publications (`CADENCE_HOURS` in `src/ingest/publish.py`): 6 for GFS,
GFS-Wave and GEFS, 12 for each ECMWF layer and 24 for both current layers. It was 24
for every layer before 2026-10-01; each entry changes at its layer's next
publish.

Retention is by count (current + previous run per layer), so publishing more
often does not add storage; a superseded run is deleted one cycle later. The
ensemble quantizes its anomalies one member at a time, which keeps its array
peak near 4.9 GB of a runner's 16 GB (scaled replay, 2026-09-29,
`uv run scripts/ensemble_memory.py`; about 16 GB before).

Tiles slice each provider grid without resampling, and every header carries
that grid's geometry. The 0.25°/0.5° layers and GLO12 sit exactly on their
lattices, so each tile starts on its 10° lines (GLO12: `dlat = dlon = 1/12`).
Before 2026-09-24 GLO12 headers took the step from two float32 coordinates
(0.0833282° of longitude), drifted by up to 0.02°, and put each true 10°
column at the end of the western tile. IBI keeps the provider's own regular
0.02777863° lattice, which sits up to 0.0013° off the 1/36° lines
([docs/ibi-currents.md](docs/ibi-currents.md)).

### Regional layers (reduced-cadence production canaries)

Phases 1–4 of the [Open-Meteo bulk plan](docs/open-meteo-bulk-implementation-plan.md)
add regional deterministic models from Open-Meteo's public AWS files
(`src/ingest/sources/openmeteo/`). Each run is three whole `.om` files from a
complete `data_run/` cycle, decoded locally with `omfiles` (the optional
`openmeteo` extra). The registry production gates are enabled for all three;
runtime allowlists and capacity controls must also admit an R2 publication.
`--dry-run` remains available for scratch validation. They publish to their own
`latest-regional.json`, never to the root `latest.json`.

| Layer | Source | Grid / tiles | Cycles and axis |
|---|---|---|---|
| `weather-arome` | Météo-France AROME France 0.025° | 717×1121 from 37.5N 12W, `grid-0p025`, 5° tiles, versioned footprint mask | 03/09/15/21Z, hourly 0–51 h |
| `weather-icon-eu` | DWD ICON-EU 0.0625° | 657×1377 from 29.5N 23.5W, `grid-0p0625`, 10° tiles | 00/06/12/18Z, hourly to 78 h then 3-hourly to 120 h |
| `weather-ukv` | Met Office UKV 2 km | Corrected native ellipsoid → fixed 0.025° grid, 3° tiles, curved footprint | 00/06/12/18Z, hourly 0–54 h; instantaneous gust includes +0 h |

All three publish `wind_u_kt` / `wind_v_kt` with the global layers' int16
encoding. AROME/ICON-EU gust (`gust_kt`, missing at +0 h) has verified
**one-hour maximum windows**, including
ICON-EU's three-hourly output after +78 h. Primary GRIB records are saved in
`tests/fixtures/openmeteo/gust-windows/`. AROME covers only part of its
rectangle (17.18 % of cells are always missing), so it is validated per step
inside a registered footprint, not by the global 5 % rule. UKV preserves
instantaneous gust at +0 h; its corrected projection, 3° layout and measured
164 MB run are described in [UKV evidence](docs/ukv-discovery.md).

Measured 2026-10-02 with `uv run scripts/probe_openmeteo.py benchmark LAYER
--assume-gust-windows`, which includes gust under the unverified windows
(sizes gzipped, MB decimal):

| Run | Source | 10° tiles: total / largest gz / largest decoded | 5° tiles: total / largest gz / largest decoded | Peak RSS |
|---|---|---|---|---|
| AROME 2026-10-02T03Z | 55.7 MB | 11 tiles, 74.2 / 19.6 / 99.8 MB | 28 tiles, 73.9 / 5.2 / 25.0 MB | 1.3 GB |
| ICON-EU 2026-10-02T06Z | 121.7 MB | 60 tiles, 170.3 / 6.6 / 28.6 MB | 180 tiles, 162.1 / 1.8 / 7.1 MB | 2.7 GB |

Earlier wind-only dry runs of AROME 09Z and ICON-EU 06Z wrote 48.4 MB (28 tiles)
and 105.6 MB (60 tiles). Fresh verified-gust AROME 09Z / ICON-EU 12Z runs wrote
73.3 / 169.8 MB in 28 / 60 tiles. Coordinated Passage support, browser
admission, immutable regional creation, capacity reservations, scheduling and
regional-only disable/rollback are implemented. Reduced-cadence production
canaries were activated for AROME, ICON-EU and UKV on 2026-10-02:
see [regional delivery evidence and activation gates](docs/regional-delivery.md).

The `ingest-openmeteo` workflow normalizes manual and scheduled catch-up jobs
into per-layer concurrency groups; manual dry runs default on. The Worker has
AROME/ICON-EU/UKV entries with all three in `REGIONAL_MODELS`. Canary profiles
select AROME 03/15Z and ICON-EU/UKV 00/12Z. Existing-layer timetable entries
remain unchanged. Full cadence needs both the GitHub
`OPENMETEO_FULL_CADENCE` variable and Worker `REGIONAL_FULL_CADENCE` setting.
Deployment and publications are recorded in the dated evidence; this does
not establish current live health. Worker full cadence is false in checked-in
config and the GitHub setting was false at the latest recorded checkpoint.
Both must remain off until the plan's seven-day gates pass.

Regional jobs save a separate attempt JSON artifact for 30 days, including
failure category, prior successful run, source identities/bytes and metadata
lag, download/decode/encode times, output sizes, temporary disk and peak process
RSS. `--attempt-report /tmp/attempt.json` enables the same evidence locally;
the path must stay outside the `--dry-run` forecast layout. A scratch success,
an upstream skip and a confirmed R2 publication are distinct outcomes. Missing
reports are unknown evidence. For a read-only freshness check using a recently
measured completion lag, see [regional operations](docs/regional-delivery.md).

## Dispatcher

GitHub's `schedule` starts runs hours late and drops slots (measured
2026-09-29: every daily cron 5.5–6.5 h late; one of eight hourly IBI slots
run by 18:40), while `workflow_dispatch` runs start within about 10 s. So a
small Cloudflare Worker, [`dispatcher/`](dispatcher/), keeps the clock: at
each provider's usual publication time it dispatches that layer's ingest
workflow with the cycle and a `wait_minutes`, and the ingest waits for the
cycle. The Worker only calls GitHub's API. It never contacts a provider,
`latest.json` or the runs list, and a cycle that is already published exits
in about a minute.

**Existing-layer timetable** (UTC; `dispatcher/src/timetable.ts`, crons in
`dispatcher/wrangler.toml`):

| Provider slots | Layer | Cycle dispatched | Provider ready (measured; 1–3 Oct, live) | `wait_minutes` |
|---|---|---|---|---|
| `25 4,10,16,22 * * *` | `weather` | fire time − 4 h 25 | cycle + 4 h 35–4 h 55 (9 cycles; 7 at 4 h 35–4 h 40) | 90 |
| `0 5,11,17,23 * * *` | `waves` | fire time − 5 h | + 5 h 09–5 h 27 (9 cycles) | 90 |
| `15 0,6,12,18 * * *` | `ensemble` | fire time − 6 h 15 (00:15 → previous day 18Z) | + 6 h 28–6 h 36; 18Z of 2 Oct + 7 h 03 (8 cycles) | 90 |
| the same, 00:15 and 12:15 only | `weather-ecmwf-short` | fire time − 6 h 15 (00:15 → previous day 18Z) | + 6 h 27 (the four 06Z/18Z cycles of 29–30 Sep and four of 1–2 Oct; ECMWF releases a cycle's files at one minute) | 120 |
| `20 7,19 * * *` | `weather-ecmwf` | fire time − 7 h 20 | + 7 h 34 (5 cycles, 1–3 Oct) | 120 |
| `45 5,9 * * *` | 05:45 `currents`, 09:45 `currents-ibi` | that day's 00Z | GLO12 06:10–09:05 (29 Sep–3 Oct: 06:26, 06:10, 09:05, 06:49, 06:12); IBI 09:54–11:36 (24 Sep–3 Oct; 1–3 Oct 10:14, 10:04, 09:56) | 240 (`currents`, whose workflow allows 300 min), 180 (`currents-ibi`) |

The existing layers keep 18 dispatches from 16 provider slots per day. Two
layers share the 00:15 and 12:15 fires; each is dispatched independently.
The same five cron expressions now include regional hours, as shown in
`dispatcher/wrangler.toml`; the table above describes the original provider
slots rather than the extended expressions. Reduced-cadence regional canaries
add AROME at 05:45/18:45, ICON-EU at 03:25/15:25 and UKV at 04:15/16:15 UTC.
That totals 24 dispatched jobs per day, with six further regional-only fires
filtered out while full cadence is off. No additional Cron Trigger was added.
Changing to `*/5` requires redesigned missed-slot monitoring and tests first.

Cloudflare starts a fire 27–48 s after its minute (the dry run of
2026-09-29 to 10-01, 22 fires), and the `scheduledTime` it hands the Worker
already carries that delay. So a fire counts for the latest slot up to
4 min 59 s before it (`MAX_LATE_MINUTES`, under the 5 min of a `*/5` tick).
A fire of one of the five crons that matches no slot logs `MISSED SLOT` and
fails the invocation.

**On each fire** the Worker takes the cycle from the *scheduled* time, not
the clock, and posts
`{"ref":"main","inputs":{"cycle":"YYYYMMDDTHH","wait_minutes":"N"},"return_run_details":true}`
to
`/repos/deepregatta/forecast-tiles/actions/workflows/ingest-<layer>.yml/dispatches`.
It logs the run URL from the 200 response (without `return_run_details`
GitHub answers 204 and no run id). A 401 or 403 is logged as
`GITHUB TOKEN REJECTED`. If GitHub refuses the dispatch and the workflow's
`state` is `disabled_inactivity` (scheduled workflows in a public repo are
disabled after 60 days without repository activity), it re-enables the
workflow and dispatches once more; a workflow disabled by hand is left
alone. A failed dispatch also fails the invocation, so it shows in the
Worker's logs as an error.

**Dry run.** Any `DRY_RUN` but the exact string `"false"` (live in
`wrangler.toml` since 2026-10-01) makes the Worker only log what it would
do, one line per slot, e.g.
`would dispatch ingest-weather cycle=20260930T06 wait=90 scheduled=10:25:00Z fired=10:25:02Z`.
`scheduled` is the timetable minute and `fired` the Worker's clock, so the
difference is Cloudflare's start delay. Cloudflare documents no timing for
Cron Triggers, and its own `scheduledTime` already includes the delay
(22:25:27 for the first slot, on 2026-09-29), so a day of these lines
measured it. Live, the line reads `dispatched … run=<run URL>`.

**Token.** A fine-grained personal access token scoped to
`deepregatta/forecast-tiles` only, with **Actions: read and write** (Metadata:
read is added automatically), stored as the Worker secret `GITHUB_TOKEN`. It
can start, cancel and re-run workflows, delete run logs, and enable or
disable workflows; it cannot read secrets or change code. It is never
committed and never passed through anything but `wrangler secret put`.

**Dated deployment/delivery checkpoint:** deployed 2026-09-29 21:24 UTC in dry-run, with the token set.
The dry run logged all 16 slots of 2026-09-30, each for the timetable's
cycle, 27–48 s after the minute. Dispatching is live since 2026-10-01
(Passage plan, Phase 5B). Verified on 2026-10-03 over the first two
days: every slot dispatched its cycle, no wait ran out, and no fallback run
had to publish. NOAA and ECMWF times are when the ingest saw the cycle (it
checks every 60 s, ECMWF every 120 s); Copernicus times are the STAC
`admp_updated_data` recorded as `provider_updated_at`.
This checkpoint does not prove current operation; fresh Worker logs, workflow
outcomes and publication receipts/read-back are required for that claim.

**Setup** (once, by a maintainer):

1. Create the token on GitHub (Settings → Developer settings → Fine-grained
   tokens): resource owner `deepregatta`, only the repository
   `forecast-tiles`, Repository permissions → Actions: Read and write; no
   expiry if the organization allows it, otherwise at most a year with a
   reminder to renew. Approve it if the organization requires approval.
2. Check that no other Worker on the Cloudflare account uses Cron Triggers
   (the dispatcher uses all five of the free plan). If one does, switch to
   the single `*/5` expression above.
3. Deploy and store the token:
   ```sh
   cd dispatcher
   npm ci
   npx wrangler login
   npx wrangler deploy
   npx wrangler secret put GITHUB_TOKEN   # paste the token at the prompt
   ```
   If the shell exports a `CLOUDFLARE_API_TOKEN` without Workers
   permissions, wrangler uses it and refuses `login`; prefix each command
   with `env -u CLOUDFLARE_API_TOKEN`.
4. Watch the dry run for a day: the Worker's **Logs** tab in the Cloudflare
   dashboard (kept 3 days on the free plan) or `npx wrangler tail`.

**Switched on** 2026-10-01 (Passage plan, Phase 5B): `DRY_RUN = "false"`,
`CADENCE_HOURS` set to the provider cadences, and every root workflow's
`schedule` moved to the fallback `37 2,8,14,20 * * *`, away from the
dispatch times. The fallback covers a missed dispatch; the "already
published" exit makes it harmless when it isn't needed. To pause
dispatching, set `DRY_RUN = "true"` and `npx wrangler deploy`.

**Runbook.**

- *A run failed with "cycle not available after N min".* The provider was
  later than the wait. The fallback cron publishes the cycle later; if it
  recurs, widen that layer's `waitMinutes` or move its fire time.
- *`GITHUB TOKEN REJECTED` in the Worker logs.* Create a new token as in step
  1 and run `npx wrangler secret put GITHUB_TOKEN`. Meanwhile the fallback
  crons keep a slower cadence.
- *`re-enabled ingest-….yml`.* Expected after 60 quiet days; nothing to do.
- *A layer did not run at all.* Check the Worker's logs for that minute: no
  line means Cloudflare skipped the trigger; `MISSED SLOT` means it started
  more than 4 min late; a `FAILED` line gives GitHub's answer. `gh workflow run ingest-<layer>.yml -f cycle=YYYYMMDDTHH` starts one
  by hand.
- *Local check:* `bash scripts/verify.sh` (or `dispatcher` for the affected group).

## Routing index (`ingest land`)

A separate, non-scheduled artifact: simplified land and a selected depth
contour compiled into a conservative, versioned **routing spatial index** that
Tactician's router queries for legality. Full spec, licences and rebuild
instructions: **[docs/land-index-format.md](docs/land-index-format.md)**.

| | |
|---|---|
| Sources | GSHHG 2.3.7 full-resolution shoreline (LGPL-3.0-or-later, attribution) · EMODnet Digital Bathymetry DTM 2024 (CC-BY-4.0) |
| Domain `nweu` | 10°W–10°E, 40°N–60°N (four 10° tiles) — Biscay, Brittany, the Channel and its western approaches, the southern North Sea |
| Cell | 1/480° (~232 m of latitude), one bit per cell, packed south-to-north |
| Conservatism | outward buffer **200 m**; safety contour **0 m below LAT**; no-data blocked; every step adds blocked area and none removes any |
| Cadence | one-shot. `workflow_dispatch` only (`.github/workflows/land-index.yml`) |

Both sources carry **DO NOT USE FOR NAVIGATION**, and so does every manifest
this pipeline writes. The index resolves shoal areas, not individual rocks:
measured on the fixture region, neither source resolves Ar Men or La Vieille.

First public index: **`nweu-20260825T07Z`** from [run 32819560190](https://github.com/deepregatta/forecast-tiles/actions/runs/32819560190) — 154 KB gzipped for 92
million cells, 49.8 % of them blocked, the shoreline and the depth contour
agreeing on 98.8 %, and every probe passing. It **reproduces**: the same sources
and parameters compiled on a clean runner over a cold cache and locally over a
warm one give bitmaps identical in all four tiles, differing only in the
identity fields. Tactician consumes it through its `core/land` crate and routes
the 2025 RORC Channel replay against it with no segment crossing land.

Time axes reflect the Phase 0 size measurement — see
[docs/phase0-results.md](docs/phase0-results.md) (verdict: GO at 3.26 GB per
full generation, 6.53 GB at ×2 run retention against the 8 GB storage guard).
The IBI axis comes from a separate real-data size gate and a coverage rule:
0–120 h is the shortest horizon that keeps the next 3 days in the served run
at any moment, about 114 MB per full regional run and 36.6 MB for the four
Channel tiles (0–72 h until 2026-09-29: 67.5 MB and 21.6 MB). See
[docs/ibi-currents.md](docs/ibi-currents.md#horizon-0120-h).

The PFT1 format and the manifest/latest JSON schemas are canonically specified
in the Passage repo ([`docs/forecast-tile-format.md`](https://github.com/deepregatta/passage/blob/main/docs/forecast-tile-format.md), `contracts/forecast-*.schema.json`);
this repo vendors copies plus a shared golden fixture that both CIs must decode
identically. The routing index's own spec, schemas and golden fixture are
canonical **here** and consumed by tactician's `core/land` (`docs/land-index-format.md`,
`contracts/land-index-*.schema.json`, `tests/fixtures/land-index-raz/`). The
`forecast-*` schemas here match Passage's, `currents-ibi` included (since
2026-09-28) and `latest.json`'s optional `cadence_hours` (since 2026-09-29).
No per-tile provenance/resolution extension has been made; the later
blended-current contract remains separate.
The vendored manifest schema's provenance description still mentions a
historical RTOFS fallback. That descriptive text does not declare an
implemented provider; `currents` supports CMEMS GLO12 only. Correcting the
canonical Passage wording and syncing the vendors is a separate contract
documentation follow-up; schema bytes remain unchanged here.

## Data licensing

The code in this repository is MIT-licensed ([LICENSE](LICENSE)). The
optional `openmeteo` extra installs `omfiles`, which is GPL-2.0-only; it is a
dependency, not vendored or redistributed here.

- NOAA data: US Government work, public reuse permitted.
- Copernicus Marine data: free with attribution — this pipeline records product
  ids in run manifests and Passage displays attribution in its UI.
- ECMWF open data: CC BY 4.0.
- Regional models via Open-Meteo's bulk files: CC BY 4.0 as the catalogue
  declares, attributed to the originating service (Météo-France AROME, DWD
  ICON-EU) and to Open-Meteo in every regional run's provenance.
- Met Office UKV via Open-Meteo: CC BY-SA 4.0 for transformed tiles and
  cropped fixtures. Attribution, licence link and modifications are recorded;
  see [DATA-LICENSES.md](DATA-LICENSES.md). Reduced-cadence production was
  activated on 2026-10-02; full-cadence acceptance remains open.
- GSHHG shoreline: LGPL-3.0-or-later, with permission to use, copy, modify and
  distribute given attribution — Wessel, P., and W. H. F. Smith (1996), *A
  global, self-consistent, hierarchical, high-resolution shoreline database*,
  J. Geophys. Res., 101(B4), 8741–8743.
- EMODnet Bathymetry: CC BY 4.0 — EMODnet Bathymetry Consortium (2024):
  EMODnet Digital Bathymetry (DTM 2024). Its own metadata states **DO NOT USE
  FOR NAVIGATION**.

Every routing-index manifest records each source's product, version, access
URL, access date, licence and attribution, plus the SHA-256 of the exact
shoreline file it was compiled from.
