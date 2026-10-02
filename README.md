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
the live R2 check of conditional writes is still to run.

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

## Running the pipeline

```sh
uv sync
uv run ingest weather                        # latest complete GFS cycle -> R2
uv run ingest weather --cycle 20260713T06    # explicit cycle
uv run ingest weather --dry-run /tmp/tiles   # write the R2 layout locally instead
uv run ingest ensemble|waves|currents|weather-ecmwf
uv run ingest weather-ecmwf-short             # ECMWF's 06Z/18Z runs, to 144 h
uv run ingest currents-ibi                   # hourly regional current field
uv run ingest weather --force                # re-publish the cycle latest.json already has (repairs only)
uv run ingest weather --cycle 20260930T06 --wait-minutes 90   # wait for the provider, then publish
uv run ingest land --domain nweu             # rebuild the routing index (one-shot)
uv sync --extra openmeteo                    # regional models (dry runs only so far)
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
validate (step coverage, physical ranges, gust ≥ wind, missing fraction,
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

Since 2026-10-01 the [dispatcher](#dispatcher) starts each layer's workflow
at its provider's usual publication time, for every provider cycle, and the
run waits for its cycle. Each workflow also keeps a fallback `schedule`
(`37 2,8,14,20 * * *`) for a missed dispatch; GitHub starts those hours late,
and a cycle already published exits in about a minute. See
`.github/workflows/ingest-*.yml`. `ingest weather-ecmwf` exits 0 with a log
line when ECMWF hasn't published a full-horizon cycle yet, `ingest
weather-ecmwf-short` likewise for a 06Z/18Z cycle to 144 h, and `ingest
currents` and `ingest currents-ibi` do the same until Copernicus has finished
writing the bulletin (the public STAC item of the dataset says so; GLO12
never falls back to RTOFS for that).

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
exceed `MAX_BUCKET_BYTES` (default 8 GB).

## Layers

| Layer | Source | Resolution | Cadence (since 2026-10-01) |
|---|---|---|---|
| `weather` | NOAA GFS (wind u/v, gust hourly; vis/CAPE/temp/dew-point/precip 3-hourly) | 0.25° | every cycle (00/06/12/18Z) |
| `weather-ecmwf` | ECMWF open data (wind u/v; gust = the maximum over the 1, 3 or 6 h before each step, published with its per-step window as the variable's `statistic`) | 0.25° | 00Z and 12Z (the full 240 h cycles) |
| `weather-ecmwf-short` | the same ECMWF open data, 06Z and 18Z cycles, which stop at 144 h: 3-hourly to 144 h (49 steps, the first part of `weather-ecmwf`'s axis; gust windows 1 h to +90 h, 3 h to +144 h) | 0.25° | 06Z and 18Z (since Passage plan Phase 5C) |
| `ensemble` | NOAA GEFS, 31 members (wind + gust, mean + int8 anomalies; 3-hourly to 144 h, 6-hourly to 384 h) | 0.5° | every cycle |
| `waves` | NOAA GFS-Wave (Hs, period, direction, wind-wave, swell) | 0.25° | every cycle |
| `currents` | Copernicus Marine GLO12 (surface u/v, 6-hourly to 240 h; NOAA RTOFS fallback) | 1/12° | 1×/day, soon after Copernicus |
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

### Regional layers (dry runs only)

Phases 1–4 of the [Open-Meteo bulk plan](docs/open-meteo-bulk-implementation-plan.md)
add regional deterministic models from Open-Meteo's public AWS files
(`src/ingest/sources/openmeteo/`). Each run is three whole `.om` files from a
complete `data_run/` cycle, decoded locally with `omfiles` (the optional
`openmeteo` extra). The registry defines each layer; none is enabled for R2
yet, so the CLI accepts them only with `--dry-run`. They publish to their own
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
regional-only disable/rollback are implemented. Production remains disabled:
see [regional delivery evidence and activation gates](docs/regional-delivery.md).

The `ingest-openmeteo` workflow normalizes manual and scheduled catch-up jobs
into per-layer concurrency groups; manual dry runs default on. The Worker has
AROME/ICON-EU entries but `REGIONAL_MODELS` is empty, so current dispatches are
unchanged. When individually activated, default canary profiles select AROME
03/15Z and ICON-EU 00/12Z. Full cadence needs both the GitHub
`OPENMETEO_FULL_CADENCE` variable and Worker `REGIONAL_FULL_CADENCE` setting.
Maintainer deployment and the plan's seven-day gates are still required.

Regional jobs save a separate attempt JSON artifact for 30 days, including
failure category, prior successful run, source identities/bytes and completion
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

**Timetable** (UTC; `dispatcher/src/timetable.ts`, crons in
`dispatcher/wrangler.toml`):

| Cron | Layer | Cycle dispatched | Provider ready (measured) | `wait_minutes` |
|---|---|---|---|---|
| `25 4,10,16,22 * * *` | `weather` | fire time − 4 h 25 | cycle + 4 h 37–4 h 41 | 90 |
| `0 5,11,17,23 * * *` | `waves` | fire time − 5 h | + 5 h 10–5 h 25 | 90 |
| `15 0,6,12,18 * * *` | `ensemble` | fire time − 6 h 15 (00:15 → previous day 18Z) | + 6 h 29–6 h 31 | 90 |
| the same, 00:15 and 12:15 only | `weather-ecmwf-short` | fire time − 6 h 15 (00:15 → previous day 18Z) | + 6 h 27 (the four 06Z/18Z cycles of 29–30 Sep; ECMWF releases a cycle's files at one minute) | 120 |
| `20 7,19 * * *` | `weather-ecmwf` | fire time − 7 h 20 | + 7 h 34 | 120 |
| `45 5,9 * * *` | 05:45 `currents`, 09:45 `currents-ibi` | that day's 00Z | GLO12 06:10–09:05 (29 Sep–1 Oct); IBI 09:54–11:36 | 240 (`currents`, whose workflow allows 300 min), 180 (`currents-ibi`) |

That is 18 dispatches from 16 fires a day on 5 cron expressions, all of the
Workers Free plan's 5 Cron Triggers per account. Two layers share the 00:15
and 12:15 fires; each is dispatched on its own, so one failing does not stop
the other. If the account needs a trigger for
another Worker, replace them with the single `*/5 * * * *`: the timetable
stays in code, and a tick with no layer due contacts nothing.

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

**Status:** deployed 2026-09-29 21:24 UTC in dry-run, with the token set.
The dry run logged all 16 slots of 2026-09-30, each for the timetable's
cycle, 27–48 s after the minute. Dispatching is live since 2026-10-01
(Passage plan, Phase 5B).

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
`CADENCE_HOURS` set to the provider cadences, and every workflow's
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
- *Local check:* `cd dispatcher && npm ci && npm test`.

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
  see [DATA-LICENSES.md](DATA-LICENSES.md). Production remains gated.
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
