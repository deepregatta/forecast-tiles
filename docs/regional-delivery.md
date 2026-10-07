# Regional delivery: evidence and activation gates

AROME, ICON-EU and UKV are deployed as reduced-cadence production canaries.
Registry, GitHub, Passage and Worker allowlists enable all three; full cadence
remains off until seven-day acceptance passes. The dated activation record
below separates confirmed delivery from the remaining observation gate.

## Reproduced evidence, 2026-10-02

Primary-source GRIB probes verified **one-hour gust maxima for both models**,
including ICON-EU at +81/+84/+120 h. Three-hour output spacing after +78 h
does not change the maximum window. Recorded URLs, ETags and GRIB interval
metadata are in `tests/fixtures/openmeteo/gust-windows/`. AROME's current
official package host is `meteofrance-pnt.s3.rbx.io.cloud.ovh.net`.

```sh
uv sync --extra openmeteo
uv run python scripts/probe_openmeteo.py gust-window weather-arome --cycle 20261002T09 --arome-groups 13H18H 49H51H --output /tmp/arome-gust.json
uv run python scripts/probe_openmeteo.py gust-window weather-icon-eu --cycle 20261002T06 --output /tmp/icon-gust.json
uv run ingest weather-arome --cycle 20261002T09 --dry-run /tmp/openmeteo-phase3-arome-v2
uv run ingest weather-icon-eu --cycle 20261002T12 --dry-run /tmp/openmeteo-phase3-icon
```

| Dry run with verified gust | Tile size/count | Total gzip bytes | Largest gzip / decoded Float32 bytes | Elapsed |
|---|---|---:|---:|---:|
| AROME 09Z | 5° / 28 | 73,281,185 | 5,219,065 / 24,960,000 | 39.9 s |
| ICON-EU 12Z | 10° / 60 | 169,837,222 | 6,542,524 / 28,569,600 | 83.8 s |
| UKV 12Z | 3° / 80 | 164,039,908 | 3,166,351 / 9,504,000 | 48.2 s |

All three pass 8 MiB gzip / 32 MiB decoded tile limits. Manifests now declare gzip,
inflated and decoded sizes for preflight admission, served geometry, coverage,
capabilities, attribution and cycle-specific scheduling. Large source-object
inventories remain once per manifest; deterministic regional tile headers use
the cycle timestamp and source digest. Existing root tile generation retains
its previous behavior.

UKV uses instantaneous gust at +0–54 h and the corrected native ellipsoid,
with CC BY-SA notices. The 5° trial exceeded the gzip gate; 3° passes at
unchanged resolution. Primary evidence and independent GRIB proof are in
[ukv-discovery.md](ukv-discovery.md).

The isolated live [R2 conditional check](https://github.com/deepregatta/forecast-tiles/actions/runs/37032148191)
passed three tests in 37.06 s: immutable creation, conditional replacement and
interleaved/stale publisher behavior. Its temporary prefix was cleaned. The
manual `r2-audit` workflow reads both catalogues and reports abandoned uploads
and damaged references without deleting anything.

Hosted producer CI and the [ICON-EU canary-profile scratch dry run](https://github.com/deepregatta/forecast-tiles/actions/runs/37038233836)
passed. The [live read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37038229448)
found no damaged references or unreferenced forecast runs at that snapshot.
The [GFS 12Z production run](https://github.com/deepregatta/forecast-tiles/actions/runs/37033855422)
also succeeded under the CAS publisher. Passage's
[coordinated CI](https://github.com/deepregatta/passage/actions/runs/37038497599)
passed. These checks do not start regional production or establish a historical
whole-bucket upload peak.

Passage's real headless desktop Chrome loaded actual regional dry-run tiles
through `HttpTileTransport`, for Brest–Cherbourg points and 12–18 UTC. Root
bytes were copied read-only from `forecast.deepregatta.com`
(`latest.updated_at=2026-10-02T17:27:15Z`). Each run fetched production GFS,
GEFS, GFS-Wave, global-current and short ECMWF tiles, exercising ensemble,
wave, wind/current grid and hazard comparison calls before adding the regional.
Seven root manifests loaded; this route did not fetch full ECMWF or IBI tiles.
This supersedes the earlier synthetic-root result for the desktop gate.

| Workload | Cold regional transfer / total elapsed | Warm regional transfer / total elapsed | Sampled heap + backing-storage increase from fresh page |
|---|---:|---:|---:|
| Root only | 0 B / 326 ms | 0 B / 7 ms | 44,410,068 B (42.35 MiB) |
| Root + AROME | 3,845,933 B / 484 ms | 0 B / 15 ms | 100,149,996 B (95.51 MiB) |
| Root + ICON-EU | 4,888,594 B / 517 ms | 0 B / 15 ms | 111,394,470 B (106.23 MiB) |
| Root + UKV | 5,682,390 B / 506 ms | 0 B / 15 ms | 94,981,105 B (90.58 MiB) |

AROME/ICON-EU used one regional tile; UKV used two. Time includes root
fetch/decode/sampling. CDP `Runtime.getHeapUsage` sampling used a 25 ms pause
between round trips, with a fresh page/GC between models. Increases include
the root workload; they are
not process RSS or proof of the absolute peak. Each stayed below 128 MiB and
the transfer gates. Passage's scratch benchmark accepts `--root-kind live`,
`--ukv` and `?workload=full`. Mobile viewport regressions are separate
from physical-phone evidence.

### Final physical-phone checks, 2026-10-02

Samsung Galaxy A53 (SM-A536B), Android 16, Chrome 154.0.8037.92 ran the same
root workload through USB loopback. Browser HTTP caching was disabled only for
the scratch tab; CDP network events confirmed cold tile responses came from
the server. Warm automatic repeats made **zero tile requests and zero decodes**.
This measures phone execution, not mobile-network latency.

| Workload | Cold regional bytes / total call time | Warm regional bytes / total call time | Sampled heap + backing-storage increase | Added above matching root-only workload |
|---|---:|---:|---:|---:|
| Root only | 0 / 1,148 ms | 0 / 21 ms | 38,956,850 B (37.15 MiB) | — |
| Root + all three enabled | 3,845,933 / 1,568 ms | 0 / 20 ms | 96,369,191 B (91.90 MiB) | 54.75 MiB |
| Root + AROME | 3,845,933 / 1,549 ms | 0 / 31 ms | 96,139,386 B (91.69 MiB) | 54.53 MiB |
| Root + ICON-EU | 4,888,594 / 1,992 ms | 0 / 26 ms | 104,280,528 B (99.45 MiB) | 62.30 MiB |
| Root + UKV | 5,682,390 / 2,338 ms | 0 / 28 ms | 66,098,192 B (63.04 MiB) | 25.88 MiB |

The combined selection admits AROME alongside the root and omits ICON-EU/UKV
before transfer. An earlier combined run exposed warm cache churn. Automatic
comparison now preflights retained bytes against the shared 64 MiB cache
remaining after root loading and each model's transient peak against the
128 MiB regional ceiling. Admission follows allowlist order; named requests
and export remain explicit. Regression tests cover refusal before a second
model downloads. The final desktop combined check also reused AROME warm.

The boundary route at 49.9–50.1°N crosses AROME's 5° and ICON-EU's 10° tile
lines. Named grids returned finite vectors for every requested point: 36 for
AROME/UKV and 16 for ICON-EU. Automatic comparison preserves the root and omits
AROME/ICON-EU on this route; the combined selection admits only UKV.

| Boundary workload, including explicit grid reads where applicable | Automatic cold regional bytes | Named cold / warm regional bytes | Sampled increase | Added above boundary root-only workload |
|---|---:|---:|---:|---:|
| Root only | 0 | — | 80,184,013 B (76.47 MiB) | — |
| Root + all three enabled | 2,835,417 | — | 94,005,449 B (89.65 MiB) | 13.18 MiB |
| Root + AROME | 0 | 8,508,522 / 0 | 129,950,478 B (123.93 MiB) | 47.46 MiB |
| Root + ICON-EU | 0 | 10,381,719 / 10,381,719 | 203,370,904 B (193.95 MiB) | 117.48 MiB |
| Root + UKV | 2,835,417 | 0 / 0 | 92,391,457 B (88.11 MiB) | 11.64 MiB |

The larger explicit ICON-EU mosaic evicts a prior tile under serialized transient
admission and therefore downloads/decodes both tiles again warm, as on desktop.
A smaller 49.90–49.95°N request fetched one 4,888,594 B tile and reused it with
zero warm requests/decodes. The ICON-EU memory row includes that extra check.
Explicit mosaics remain subject to request budgets and may need a smaller
region for warm reuse. AROME retained both boundary tiles; UKV retained its
already-admitted tile. No automatic regional transfer exceeded 20 MiB, and
all sampled additional working sets stayed below 128 MiB.

Every workload used a fresh scratch document, reset its own navigation history
and collected garbage before measuring. Initial backing storage was 54,326 B
in every final run. CDP heap sampling used a 25 ms pause between round trips;
observed intervals were 27–564 ms, with 20–64 samples per workload. These are
sampled heap plus backing-storage increases, including root work, rather than
process RSS or proof of an absolute transient peak. Boundary/mask regressions
remain separate evidence. The scratch tab, wake lock and USB mappings were
removed after verification; other phone tabs were preserved.

### Decoder timing and repeated reads

Scratch-only wrappers timed the actual consumer's synchronous `decodeTile`
function and asynchronous `gunzip`, without changing production functions.
Synchronous elapsed time is a main-thread decoder CPU proxy; gzip elapsed time
also includes asynchronous work and is not a CPU measurement. A fresh desktop
Chrome 154 scratch browser used the same bytes and disabled HTTP cache.

| Cold regional decoding on the three-point route | Phone count / synchronous elapsed | Desktop count / synchronous elapsed |
|---|---:|---:|
| Combined selection (AROME admitted) | 1 / 51.0 ms | 1 / 27.1 ms |
| AROME selected | 1 / 64.5 ms | 1 / 26.6 ms |
| ICON-EU selected | 1 / 68.3 ms | 1 / 30.6 ms |
| UKV selected | 2 / 132.1 ms | 2 / 29.5 ms |

All automatic warm repeats on both devices performed zero gzip operations and
zero PFT1 decodes. On the phone, the explicit boundary cold grids decoded
AROME twice in 138.9 ms and ICON-EU twice in 164.3 ms. The larger ICON-EU warm
mosaic decoded twice again in 207.0 ms; the smaller retained grid decoded zero
times warm. UKV's boundary tile decoded once during automatic loading and
needed no further decode for either named read.

## Activation sequence

1. The final individual, combined and boundary physical-phone checks above
   close the remaining phone verification gate for these workloads. Keep the
   larger explicit ICON-EU warm-eviction limitation visible. Audit Tactician
   separately; tile decoding does not prove that consumer's model/export support.
2. Run the live read-only audit. Reconcile damaged references and abandoned
   uploads before measuring a worst-case simultaneous **existing-layer** peak.
   Keep private account figures in GitHub variables, outside this public repo.
3. Configure positive `REGIONAL_EXISTING_PEAK_BYTES` (root current/previous plus
   simultaneous root uploads, including their manifests) and
   `REGIONAL_HEADROOM_BYTES`. Admission adds all nonreferenced bucket bytes
   (routing data, pointers, metadata and orphans), three capped runs per enabled
   regional, and headroom. Both reserved and physical upload peaks must fit the
   reviewed guard. Its code fallback is still 8,000,000,000 bytes; the approved
   repository capacity override is applied as recorded below. Run caps include manifests.
4. Deploy Passage with `VITE_REGIONAL_MODELS` naming only the tested model,
   open that model's registry production gate, and set the same model in GitHub
   `OPENMETEO_ENABLED_LAYERS` and Worker `REGIONAL_MODELS`. Direct CLI publication
   also needs `REGIONAL_ENABLED_LAYERS`. The owner authorized proceeding with
   production deployment; the dated record below captures the applied flags.
5. Start with AROME 03/15Z or ICON-EU/UKV 00/12Z. Workflow `canary` defaults true;
   `OPENMETEO_CANARY=true` restricts CLI automatic selection and cadence too.
   Leave GitHub `OPENMETEO_FULL_CADENCE` and Worker `REGIONAL_FULL_CADENCE` false
   until seven days meet the plan's 95% timeliness/no-invalid-run criteria and
   replacement, outage and rollback are exercised. The :47 catch-up and all
   dispatched jobs share `ingest-${layer}`, cancel false, max parallel one.

### Guard increase approval and application, 2026-10-02

The owner approved the prepared capacity/cost proposal. A
[fresh read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37055878696)
passed and confirmed that the calculated reservation, including nonreferenced
objects, still fits the approved guard. `MAX_BUCKET_BYTES`,
`REGIONAL_EXISTING_PEAK_BYTES` and `REGIONAL_HEADROOM_BYTES` are applied in
GitHub and their exact values were verified by read-back. The private proposal
and application record remain outside this public repository. At this capacity
checkpoint model allowlists were empty and production remained disabled. The
subsequent owner-authorized activation is recorded below.

All forecast ingestion workflows accept one GitHub `MAX_BUCKET_BYTES` variable,
so the approved override is consistent across root and regional jobs. The code
fallback remains 8,000,000,000 bytes. This configuration decision closes the
approval gate; representative-cycle capacity refreshes and the other activation
gates still apply.

`uv run python scripts/capacity_profile.py` performs a read-only whole-bucket
inventory, retaining only aggregate byte counts and public model IDs in its
output. It measures the larger current/previous root run per layer, calculates
three simultaneous runs for every existing layer, adds 10% size variation,
three capped regional runs, all nonreferenced bytes and 500 MB headroom, and
rounds a proposed guard up to a whole decimal GB. Unknown/damaged or missing
root references fail closed. The output distinguishes measured sizes from a
**calculated overlap envelope**; it is not an observed historical peak. Refresh
this profile across representative cycles before enabling publication. It now
includes all three registered caps: AROME 110 MB, ICON-EU 200 MB and UKV
200 MB. Use `--layers` for a narrower explicitly proposed activation.

Use that report's `proposed_existing_peak_bytes`, `headroom_bytes` and
`proposed_guard_bytes` as a reviewable configuration proposal. Raising a software
guard does not itself add stored bytes or trigger a charge. Under current
[R2 Standard pricing](https://developers.cloudflare.com/r2/pricing/), storage is
$0.015 per billable GB-month, using average daily peaks and rounding up whole
GB-months. The 10 GB-month storage, one million Class A and ten million Class B
free allowances are shared across the account. Class A/B cost $4.50/$0.36 per
billable million, rounded up; egress is free. Check account-wide use before
claiming unused allowance. Keep account-specific figures outside public docs.

## Verification checkpoint, 2026-10-02

- Producer: `uv run pytest -q` passed 287 tests (three separately gated live
  R2 tests skipped); Ruff lint/format passed. Dispatcher passed 60 tests and
  TypeScript checks. [Producer CI](https://github.com/deepregatta/forecast-tiles/actions/runs/37047416532)
  passed for `ca5d8ec`.
- The [hosted UKV scratch-only run](https://github.com/deepregatta/forecast-tiles/actions/runs/37047448665)
  passed for 12Z: 80 tiles / 164.0 MB, 44.0 s. This is a dry run, not a
  regional R2 publication or seven-day canary.
- The [fresh read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37047445147)
  passed with all three measured caps included and supported the prepared
  configuration/cost proposal. The later approval and verified application are
  recorded above. This checkpoint predates model activation.
- Passage: 450 engine and 534 viewer tests, lint, build and 72 desktop/mobile
  browser regressions passed locally. [Passage CI](https://github.com/deepregatta/passage/actions/runs/37047493679)
  passed for `e9531a8`, including its Python and hosted browser jobs. Contracts
  are byte-identical between repositories.

## Attempt evidence and freshness

`ingest-openmeteo` uploads one `LAYER-attempt-RUN_ID-RUN_ATTEMPT` JSON artifact
per matrix job, on success or failure, retained for 30 days. A killed runner or
an earlier setup failure can leave no report: treat that as unknown, even if
an upload step warns instead of failing. Download artifacts during each canary
review and retain the reviewed evidence outside immutable forecast runs.

Locally, keep the report beside the scratch layout:

```sh
uv run ingest weather-arome --cycle 20261002T15 --dry-run /tmp/regional-attempt-tiles --attempt-report /tmp/regional-attempt.json
```

The report separates `scratch_published`, `published`, `already_published`,
upstream unavailability/timeouts, source identity/validation failures and
publication failures. Exit 0 after an unavailable catch-up source is **not**
a canary success. Only a returned publisher result sets
`pointer_commit_confirmed: true`; an interrupted or uncertain result requires
pointer/manifest read-back. The previous successful pointer entry is recorded
separately, and failed attempts do not overwrite it.

Evidence includes public metadata/object keys, ETags, completed download bytes,
metadata creation lag when a valid timestamp exists, download/decode/conversion
and phase times, largest gzip/Float32-decoded/inflated tile sizes, scratch free
space, largest observed source file and Linux process peak RSS in KiB. Output
gzip bytes count tiles only; manifest and pointer overhead are separate. A
partial download is not counted as a completed object. Temporary-file peak
comes from file sizes observed after each download, not a filesystem sampler.
RSS is the process-wide high-water mark, not an incremental array measurement.
`metadata_lag_s` comes from the source document's `created_at`, not a measurement
of when every uploaded object first became publicly available. Keep that
distinction when selecting a freshness threshold or scoring canary delivery.
Reports omit credentials, destination paths and error-message text. Reporting
does not change tile bytes; a report-write error cannot undo a confirmed publish.

Use recent source completion measurements to supply the stale threshold.
For example, a **measured** 180-minute lag for the selected model is passed as:

```sh
uv run python scripts/regional_health.py weather-arome --dir /tmp/regional-attempt-tiles --canary --source-lag-minutes 180
```

Omit `--dir` to read the live regional pointer. Omit `--canary` only for an
activated full-cadence model; the tool selects 03/15Z for canary AROME and
00/12Z for canary ICON-EU/UKV. It flags stale once the second selected cycle
after the pointer's current cycle plus the supplied source lag has passed.
`--now` accepts an explicit timestamp for a reproducible check. Exit codes are
0 fresh, 1 stale, 2 unknown. Unknown lag is not zero: refresh measurements before
running the check. This checks pointer freshness only; manifest integrity,
95% delivery within the configured window and seven days of live observation
still require their separate canary evidence. The command makes no writes and
sends no notifications.

Local verification after adding this evidence: `uv run pytest -q` passed
312 tests with the three separately gated live R2 tests skipped; Ruff lint and
format passed. The 25 new checks cover scratch evidence, preserved pointers on
upstream/decode/validation/publication failures, bounded wait reporting,
unknown timing, report-write failure and full/canary stale thresholds across
UTC rollovers. [Hosted CI](https://github.com/deepregatta/forecast-tiles/actions/runs/37050369572)
passed for the initial report implementation (`aee52a8`): 311 Python tests,
three live R2 skips, Ruff and 60 dispatcher tests/typecheck. The additional
local check preserves parser rejection's actual nonzero exit code.

Two scratch-only jobs verify artifact delivery on that implementation:
[15Z not yet available](https://github.com/deepregatta/forecast-tiles/actions/runs/37050378762)
correctly recorded `source_unavailable` despite a zero exit code;
[the complete 03Z run](https://github.com/deepregatta/forecast-tiles/actions/runs/37050497359)
recorded `scratch_published`, 16 validation checks, 28 tiles / 73,854,633 bytes,
largest gzip / decoded tile 5,157,931 / 24,960,000 bytes, 46.91 s total,
2.72 s download / 0.37 s decode / 32.68 s encode and 1,318,624 KiB process peak
RSS. The downloaded JSON was checked against the job log. Those original
artifacts called metadata creation time/lag `source_completed_at` /
`completion_lag_s`; the fields are now named `metadata_created_at` /
`metadata_lag_s` to avoid claiming measured upload availability.
This scratch checkpoint predates production activation. Final phone
verification and subsequent production activation are recorded separately.

## Disable, rollback and partial uploads

Stop dispatch, catch-up and in-flight writers for the named model; disable its
consumer selection. Never use normal ingest to roll back or `--force` regionals.
Test the operator command on a scratch directory first:

```sh
uv run python scripts/regional_control.py disable weather-arome --expected-current weather-arome-20261002T09Z --dir /tmp/openmeteo-phase3-arome-v2
# Remove --dir only for an explicitly intended live operator action.
uv run python scripts/regional_control.py restore-previous weather-arome --expected-current CURRENT_RUN --dir /tmp/scratch
```

The tool validates the retained previous manifest and sampled tile hashes,
changes only that layer using CAS, refuses a newer same-layer update, and
never prunes objects or writes root `latest.json`. Restored entries have no
faulty current run as their fallback.

All regional tiles and manifests use `If-None-Match: *`. A complete existing
manifest refuses before upload; a partial prefix collision fails safely without
overwrite, pointer commit or retention. Deterministic bytes are not permission
to resume automatically. Inspect unreferenced partial data after all writers
stop; cleanup remains separately age-gated via `scripts/audit_runs.py`, then
retry. The read-only audit workflow never requests cleanup.

## Canary activation authorized, 2026-10-02

The owner authorized proceeding after final phone verification. A
[fresh read-only capacity audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37061907290)
measured newer current/previous root runs and still fits the approved guard.
The existing-layer reservation was refreshed from that profile without raising
the guard. Account figures remain outside this public repository.

AROME, ICON-EU and UKV were enabled individually, with the first production
publication and Passage export checked before proceeding to the next model.
GitHub `OPENMETEO_ENABLED_LAYERS`, Passage `VITE_REGIONAL_MODELS` and Worker
`REGIONAL_MODELS` contain all three. GitHub `OPENMETEO_FULL_CADENCE` and Worker
`REGIONAL_FULL_CADENCE` remain false. Keep two cycles/day until seven-day
acceptance passes; later expansion requires a separate decision.

### Production publication and consumer proof

| Model / bootstrap cycle | Confirmed publication | Tiles / gzip bytes | Observation start (UTC) |
|---|---|---:|---|
| AROME 15Z | [37062808393](https://github.com/deepregatta/forecast-tiles/actions/runs/37062808393) | 28 / 73,435,104 | 2026-10-02 20:49:47 |
| ICON-EU 12Z | [37064779481](https://github.com/deepregatta/forecast-tiles/actions/runs/37064779481) | 60 / 169,837,222 | 2026-10-02 21:09:26 |
| UKV 12Z | [37066152404](https://github.com/deepregatta/forecast-tiles/actions/runs/37066152404) | 80 / 164,039,908 | 2026-10-02 21:22:06 |

Each downloaded attempt reports `published`, confirmed pointer commit, complete
source downloads and successful validation. These late manual bootstrap cycles
establish production delivery but do **not** count toward scheduled timeliness.
Exact observation timestamps and the remaining gates are recorded in
[regional-canary-status.json](regional-canary-status.json).

Passage production runs commit `b8108c1`, with all three model flags; its
[CI](https://github.com/deepregatta/passage/actions/runs/37064077941) passed
450 engine, 535 viewer, 72 browser and 512 Python tests, plus lint/build checks.
The production Pages deployment is `8bfc2445-9c67-4f19-b727-ad5b423c598d`.
The Worker version is `d1c8d08a-ad6f-4705-b141-21307134b868`; its existing GitHub
secret was preserved and all five cron expressions were verified.

A live export exposed a shorter-horizon UI gap: AROME/UKV can have less than
48 hours remaining when published, while the minimum selectable period was
two days. Passage now offers an explicit **Next 1 day** period with English and
French labels and a regional horizon hint. Whole-window coverage checks remain
in force. The new regression failed before the fix; 29 focused unit tests and
six desktop/mobile GRIB map tests then passed locally.

The live production browser downloaded all three one-day wind exports for
48.39–48.44°N, 4.50–4.45°W. Independent ecCodes parsing verified 75 GRIB2 messages
per file, the pinned 15Z/12Z source times and u/v/gust parameter identities.
AROME and ICON-EU gusts use template 8 with one-hour maxima; UKV uses template 0
with instantaneous gusts. Actual regional transfers were 3,852,701 / 4,888,594 /
2,835,417 bytes, each with HTTP 200. Originating service, distributor and
CC BY / CC BY-SA attribution were visible in Passage.

The [same-cycle AROME retry](https://github.com/deepregatta/forecast-tiles/actions/runs/37064565029)
reported `already_published` without new tile output. The expanded
[isolated live R2 drill](https://github.com/deepregatta/forecast-tiles/actions/runs/37063250426)
passed four checks in 57.58 s, including injected partial-upload failure,
refused same-cycle overwrite, fresh-cycle replacement, previous-run restoration
and regional-only disable. Root bytes remained unchanged and the throwaway
prefix was cleaned. This exercises real R2 under an isolated prefix, rather
than rolling back a production pointer.

The [post-activation read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37066523436)
passed after all three publications; its calculated overlap envelope fits the
approved guard. Public read-back retained all seven root layers and confined
all three new entries to `latest-regional.json`. Continue checking scheduled
replacement, capacity and existing-layer delivery throughout observation.
Seven-day acceptance cannot finish before 9 October at each model's start time;
full cadence remains off. A twice-daily Codex follow-up at 09:45 and 22:45
Europe/Paris collects original attempt artifacts and reviews the recorded gates.
These checks follow the morning/evening delivery deadlines; timeliness uses
publication receipts rather than the review time. It stays quiet while evidence is
healthy or unchanged and reports failures, required action or completed
acceptance. It cannot waive the observation period or promote unknown evidence.

### Read-only seven-day canary scoring

`scripts/regional_canary.py` scores downloaded R2 attempt artifacts for each
model's two-cycle profile. Its configured deadline is the registry's readiness
lag plus bounded source wait plus a ten-minute ingestion allowance. This is
an explicit service window, not a claim that metadata creation proves file
availability. Manual bootstrap/backfill cycles whose scheduled time predates
observation are excluded.

```sh
uv run python scripts/regional_canary.py weather-arome --started-at 2026-10-02T20:49:47.207117Z --attempts /tmp/regional-canary-attempts/weather-arome
```

Seven days contain fourteen scheduled cycles per model, so the 95% criterion
requires all fourteen on time in this initial window. The first confirmed
publisher finish establishes delivery timing. `already_published` without the
original delivery artifact cannot prove timeliness; missing artifacts remain
unknown. An upstream-unavailable exit-zero attempt is a miss, and invalid
publication (including an extra, unscheduled cycle within observation) or an
incomplete observation cannot pass. Exit 0 confirms timing
criteria only; consumer/root-delivery and operator-drill evidence are separate.
Exit 1 denotes a completed failed window, and 2 an incomplete/unknown window.
The scorer makes no writes and never promotes cadence.

Latest activation verification: `uv run pytest -q` passed **321 tests**, with
four separately gated live-R2 tests skipped locally; Ruff lint/format passed.
Dispatcher typecheck and all **60 tests** passed. The real R2 workflow separately
passed those four tests. The canary scorer's regression rejects an invalid
extra-cycle publication even when all fourteen scheduled receipts are on time,
and refuses confirmed publications whose finish timing is missing.


## Morning observation incident, 2026-10-03

AROME 03Z and UKV 00Z delivered on time in their first scheduled canary slots:
[AROME](https://github.com/deepregatta/forecast-tiles/actions/runs/37100880530)
finished at 05:50:40 UTC and
[UKV](https://github.com/deepregatta/forecast-tiles/actions/runs/37095967210)
at 04:30:43 UTC. Both pointers retained their bootstrap runs as previous.

[ICON-EU 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37093177955)
failed at 03:26 UTC before downloads or uploads: its 03:24:37 metadata revision
omitted the required surface u/v/gust variables. A later metadata revision,
created at 03:41:38, listed them and the complete 93-step axis. The resolver
had treated marker existence as readiness, so the job stopped instead of
using its configured 45-minute wait. The prior valid ICON-EU pointer remained
served; no invalid forecast was published.

The repair makes incomplete metadata inventory/axis retryable during cycle
resolution. Explicit-cycle waits remain bounded; automatic catch-up selects
the newest genuinely complete registered cycle. Wrong reference time, geometry,
file metadata and final identity changes remain fatal. Five new regressions
failed before the repair, covering early metadata, incomplete-axis lookback,
wrong reference time, wait-until-complete and bounded timeout.

This is a delivery miss, not a timely success. The original observation starts
are unchanged. Even thirteen later timely ICON-EU cycles would give 13/14,
below 95%, so its initial window cannot pass. Recovery delivery is scored by its
actual finish timestamp and cannot erase the failed attempt. Full cadence
remains off; a new acceptance window requires an explicit recorded decision.

The [morning read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37107528945)
passed, with no incomplete/superseded runs or damaged referenced manifests/tiles.
Its overlap envelope fits the approved guard. The existing-layer reservation
was kept at its higher reviewed value. All seven root entries remain intact;
six have advanced since activation, while IBI's next dispatch is still due.
GitHub and Worker full-cadence flags were read back as false, and Passage still
enables all three regional models.


### Recovery and approved separate ICON-EU recheck

The [readiness repair CI](https://github.com/deepregatta/forecast-tiles/actions/runs/37107968933)
passed for `f4e717c`. Local verification passed **326 tests**, with four gated
live-R2 tests skipped, and Ruff lint/format. The five new readiness regressions
failed before the fix.

The [explicit ICON-EU recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37108056371)
published the original 00Z cycle, with all 13 validation checks passing and a
confirmed finish at **2026-10-03 08:00:06.350599 UTC**. Its pointer retained the
October 2 12Z run as previous. Live Passage downloaded the recovered 00Z tile
with HTTP 200 and exported 75 independently inspected GRIB2 messages: wind
components and template-8 one-hour maximum gusts, all from the correct cycle.
DWD ICON-EU and CC BY 4.0 attribution remained visible. This confirms recovery,
not on-time delivery: the original cycle is now scored **late**, with both the
failed source-check attempt and the recovered publication retained.

On October 3 the owner explicitly approved a **separate seven-day ICON-EU
recheck**, starting at that confirmed recovery finish and ending
**2026-10-10 08:00:06.350599 UTC (10:00 Paris)**. The original October 2 start,
original seven-day window and first miss remain in the acceptance record and
continue to be scored separately. AROME and UKV keep their original starts.
The recheck contains fourteen scheduled slots, starting with October 3 12Z;
the recovered manual 00Z publication is its bootstrap and contributes no
timely scheduled cycle. All fourteen recheck slots must arrive on time to
reach 95%. No cadence promotion is authorized by the recheck alone: the
consumer, root delivery, capacity and operator evidence gates still apply.

The [post-recovery read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37108440311)
passed. All referenced runs and current/previous pointers are intact, and the
complete overlap envelope still fits the unchanged approved guard with the
higher reviewed existing-layer reservation retained. GitHub and Worker
full-cadence flags remain false. The heartbeat remains twice daily, quiet while
healthy, and now scores the approved recheck as well as the original window.


Passage `5a5cc04` corrects the gust notice to describe the selected forecast's
maximum window without attributing every model to ECMWF. Live production
verification passed in English and French, with DWD/CC BY 4.0 attribution and
the recovered 00Z export intact. Local validation passed **84 focused tests**,
**450 engine + 535 viewer tests**, lint and the Pages production build. The
Pages deployment `b8e605d2-2c0e-4245-8a2c-973e8deca6b5` succeeded.

The [Passage CI run](https://github.com/deepregatta/passage/actions/runs/37108941963)
has an **overall failed result**: its 72 browser tests and 512 Python tests
passed, but the JavaScript job stopped at required `npm audit`, before its
test/build steps. The [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm),
reviewed October 2, affects all published braces versions through 3.0.3 and
currently lists no patched version. It is present through the existing
Tailwind 3 build tooling. `npm audit --omit=dev` reports zero runtime dependency
vulnerabilities; this does not waive the required full audit. Resolving this
validation gate and obtaining passing hosted CI remain required before
full-cadence promotion.


## Evening observation, 2026-10-03

All three evening cycles delivered within their recorded deadlines:
[ICON-EU 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37133219853)
at 15:49:30 UTC,
[UKV 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37143537260)
at 18:19:37 UTC and
[AROME 15Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37145435478)
at 18:59:59 UTC. Their current/previous pointers now retain the morning runs.
AROME and UKV have two on-time slots each; ICON-EU's approved recheck has one.
ICON-EU's original window still records its first late cycle alongside the
new on-time cycle. Seven elapsed days remain mandatory; no window has passed.

UKV's [first 12Z attempt](https://github.com/deepregatta/forecast-tiles/actions/runs/37136218148)
was paused by the newly enabled spending control. Its zero exit code did not
publish data. The original paused report is retained separately from the
successful scheduled catch-up, which finished before the 18:25 UTC deadline.
This verifies recovery under the spending guard without changing that policy.
Already-published catch-up reports likewise contribute no new delivery time.

Actions history was paginated back through the earliest original observation
start. All 23 completed attempt reports were downloaded in original form;
workflow/artifact identities and report hashes are retained in the acceptance
record. Both catalogues and all 20 current/previous public manifests passed
the canonical schemas and run/layer identity checks. Existing root dispatches
continued publishing, including IBI 00Z, GFS/GEFS/waves 12Z and ECMWF 12Z.
The [evening read-only whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37152807217)
passed with no incomplete/superseded runs or damaged references. Its physical
and complete overlap envelopes fit the unchanged approved guard; the higher
reviewed existing-layer reservation was retained.

Passage's current production deployment contains all three regional models.
Its [latest CI](https://github.com/deepregatta/passage/actions/runs/37152627848)
still fails only at the previously reported dependency audit, while browser
and Python jobs pass. This gate remains open. GitHub and Worker full cadence
are still false; observation and quiet twice-daily monitoring continue.

## Control-read delivery incident, 2026-10-04

The [scheduled ICON-EU 00Z attempt](https://github.com/deepregatta/forecast-tiles/actions/runs/37174061949)
exited zero but did **not** publish. Required source variables became ready
at 03:42:29 UTC, after the resolver correctly waited for their inventory.
The subsequent spending-control read returned `control state unavailable`;
the attempt was recorded as paused at 03:43:50 UTC, before any tile upload.
The original log does not expose the exception type, so its precise transport
cause remains unknown. A fresh control read showed valid admission policy;
no owner pause, allowance change or lease clearing was used for recovery.

The [guarded manual recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37187079023)
published at **2026-10-04 07:54:46.640741 UTC**, with all thirteen validation
checks passing and a confirmed pointer commit. It retained October 3 12Z as
previous. Its 60 tiles stayed within the registered compressed and decoded
limits. This recovery ran on `176beb7`, before the read repair described below.
The 04:20 UTC deadline was missed: this slot is **late** in both ICON-EU
windows, never counted as a timely delivery.

ICON-EU's original window now contains one on-time and two late deliveries,
with a maximum possible **12/14** on time. Its owner-approved October 3–10
recheck contains one on-time and one late delivery, with a maximum **13/14**.
Neither can reach 95%. Both starts, both windows and every attempt are retained.
A further separate recheck was requested after repair verification; the
subsequent owner approval and new window are recorded below. AROME and UKV each have three on-time deliveries
and retain their original windows. Seven elapsed days remain mandatory.

The same generic control-read failure also paused
[GEFS October 3 18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37164416190)
at 01:37:53 UTC and
[ECMWF October 4 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37185512740)
at 07:35:54 UTC after their provider waits. These green workflows were not
publications. GEFS 00Z subsequently published; its missing 18Z slot remains
a historical delivery gap, and the older cycle must not replace current 00Z.
The [explicit guarded ECMWF recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37187443477)
confirmed publication to R2 at **08:15:58 UTC**, with twelve validation checks
passing and 648 tiles. Fresh root readback and schema checks verify current
October 4 00Z and previous October 3 12Z. Its paused scheduled attempt remains
in the cumulative record. This recovery used the pre-repair source;
subsequent scheduled delivery must demonstrate the repair in operation.

Forecast `dba89e2` adds at most three idempotent control GET/stream attempts for
transient transport failures and HTTP 408/429/500/502/503/504 responses, closing
each body before retry and waiting one then two seconds. Missing, unauthorized,
malformed or oversized control remains paused. Exception diagnostics expose
only the class. Uncertain admission PUTs are never retried; owner pause,
spending allowances, leases, capacity guard and reservations are unchanged.
The canonical Oscar copy (`9f6d009c`) and Passage vendor (`1c324e4`) carry the
same implementation, verified by AST; the write method is unchanged by AST.

Eight regressions failed before the repair. After it, local verification
passed **386 forecast Python tests**, shared-contract checks, Ruff, dispatcher
typecheck and **60 dispatcher tests**; **1,535 Oscar Python tests** (four
skipped) and Ruff; and **537 Passage Python tests** and Ruff. Oscar's local
image check could not run because Docker is unavailable, but its
[hosted image build and deterministic offline smoke](https://github.com/deepregatta/oscar/actions/runs/37187803132)
passed, and Oscar's full hosted CI passed.
[Forecast repair CI](https://github.com/deepregatta/forecast-tiles/actions/runs/37187844842)
passed. [Passage repair CI](https://github.com/deepregatta/passage/actions/runs/37187841701)
still fails the required dependency audit; passing Python, shared-contract
and browser jobs do not waive that failure. Detailed final job states are in
the status record. No Oscar production deployment is claimed by the
shared-library sync.

Actions history was paginated through the earliest original start. Thirty
original attempt reports are retained with artifact identities and hashes;
no reports are missing. Both catalogues and all 20 current/previous manifests
pass canonical schemas and identity checks. The seven root layers remain
present, with the delivery regressions above explicitly retained.
The [read-only whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37186842467)
passed with intact references and no abandoned runs at its checkpoint.
The measured physical and complete overlap envelopes fit the unchanged
approved guard; the retained existing-layer reservation still covers the
refreshed profile. Exact account figures remain private.
The [audit repeated after both recoveries](https://github.com/deepregatta/forecast-tiles/actions/runs/37188468153)
also passed, with intact references and no incomplete or superseded runs;
physical usage and complete overlap still fit without any policy change.

Passage production deployment `5d765a3b-7bf0-4d24-945a-f13b46f501a1` succeeded
for `1c324e4`, retaining all three regional models. Current export and
attribution checks must be repeated before promotion. GitHub and Worker
full-cadence flags remain false. The heartbeat remains twice daily.

## Approved further ICON-EU recheck, 2026-10-04

The owner approved the separate recheck after the control-read repair passed
its relevant local and hosted gates, and directed autonomous continuation of
the authorized Release 1 acceptance work. The selected ICON-EU window is now
`post-control-read-repair`, from **2026-10-04 09:05:12 UTC** to
**2026-10-11 09:05:12 UTC (11:05:12 Europe/Paris)**. The original October 2
window and the October 3 readiness-repair window remain intact and continue
to be scored separately, with all failed/paused attempts and late recoveries.
The historical `recheck_started_at` still refers to the October 3 window;
the selected window and `current_recheck_started_at` identify the new one.

The new window has fourteen scheduled slots, starting with October 4 12Z
(dispatch 15:25 UTC, delivery deadline 16:20 UTC). The October 4 00Z manual
recovery predates this schedule, used pre-repair source and contributes no
timely cycle. All fourteen slots must be on time, and all seven days must
elapse before this gate can pass. AROME and UKV retain their original starts.

Routine recovery and further separately recorded rechecks after verified
fixes proceed within the owner's direction to continue autonomously, retaining
all earlier evidence. Duration, timeliness, required validation, invalid-run,
capacity and consumer gates remain mandatory. Full cadence stays disabled
until every gate passes; later model expansion remains an owner decision.


## Required Passage audit resolved, 2026-10-04

Passage [`97eb093`](https://github.com/deepregatta/passage/commit/97eb0938359aa45b3b26f91e10d99832a7a9408d)
removes the Tailwind 3 build chain through the unpatched `braces` advisory
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).
Tailwind CSS and its PostCSS plugin are pinned to 4.3.3. Compatibility rules
retain the existing typography, divider sides, control corners, native date
metrics, hover borders and focus outlines. Browser link assertions follow the
configured demo origin, allowing isolated local checks without occupying an
existing application's port. Committed screenshot and demo fixtures remain
unchanged.

A clean install and full `npm audit` report **zero vulnerabilities**; lint,
**513 engine tests**, **595 viewer tests**, **106 desktop/mobile browser checks**
and the production build pass locally. All four required jobs in
[Passage CI](https://github.com/deepregatta/passage/actions/runs/37192475185)
pass, including the full audit. The original failed audit and later failed
checkpoints remain in the cumulative record; no audit waiver was used.

Production deployment `8fc54c6b-c9ac-4f6c-84bc-6cdfa76bea85` succeeded for the same commit,
retaining all three regional models. Fresh live Passage exports for October 4
AROME 03Z, ICON-EU 00Z and UKV 00Z each contain **75 GRIB2 messages across 25
hourly times** over 48–49°N, 5–4°W. Independent ecCodes decoding confirms cycle
identity and u/v/gust parameters: AROME and ICON-EU gusts use template 8 with
one-hour maximum intervals; UKV uses instantaneous template 0. Export sizes
are 120,360 / 30,048 / 113,460 bytes respectively; exact SHA-256 receipts are
in the status record. Each regional tile request returned HTTP 200. Credits,
UKV CC BY-SA terms and gust semantics pass in English and French, and the
390px mobile page has no horizontal overflow or uncaught page errors.

This resolves the required dependency gate. Delivery observation continues
through the selected ICON-EU window ending October 11 at 09:05:12 UTC;
AROME/UKV starts, all earlier ICON-EU windows, misses, capacity policy and
full-cadence gates are unchanged. Current consumer and required hosted checks
must still pass at promotion.

A final history refresh paginated two pages back to the original start and
retained **33 original attempt reports** with no missing reports. The three
additional artifacts from [37190024725](https://github.com/deepregatta/forecast-tiles/actions/runs/37190024725)
are already-published skips for AROME 03Z and ICON-EU/UKV 00Z; they contribute
no publication or timely credit. All five scored observation windows retain
their previous counts, including both failed ICON-EU windows.


## Evening observation, 2026-10-04

All three scheduled evening regional cycles published from the read-repaired
source, with the morning runs retained as previous:
[ICON-EU 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37212977200)
confirmed at 15:50:53.885183 UTC (deadline 16:20 UTC),
[UKV 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37216130136)
at 16:32:59.100236 UTC (deadline 18:25 UTC), and
[AROME 15Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37225691350)
at 18:58:57.429638 UTC (deadline 20:25 UTC). Their original reports confirm
successful validation and pointer commits; publication time comes from the
confirmed attempt finish, rather than manifest creation or workflow status.

AROME and UKV now each have four timely slots in their original windows.
ICON-EU's selected October 4 post-control-read-repair window has its first timely
slot and thirteen pending slots. Its original window retains two late slots
and its October 3 recheck retains one; their maximum possible results remain
12/14 and 13/14. All three ICON-EU windows were scored separately. No seven-day
window has elapsed or passed. The selected window still ends on October 11 at
09:05:12 UTC and requires fourteen timely scheduled deliveries.

Actions history was paginated through the earliest original observation start.
All 42 original attempt reports are retained with workflow/artifact identities
and hashes: nine new reports contain three confirmed publications and six
already-published skips. Those skips receive no additional delivery credit.
No new unknown timing, missing report or invalid publication was found. Both
catalogues and all twenty current/previous manifests passed their canonical
schemas and run/layer identity checks.

Root delivery also continued after the read repair. Publication job logs confirm
[IBI 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37193203236)
at 09:54:07 UTC, GFS and waves 06Z/12Z, ECMWF-short 06Z,
[GEFS 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37223760432)
at 18:42:13.7479606 UTC, and
[ECMWF 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37227933791)
at 19:47:40.9074511 UTC. Seven root layers remain present, with valid previous
runs. The historical GEFS 18Z gap, ECMWF 00Z pause/manual recovery and ICON-EU
misses remain in the cumulative record; later delivery does not erase them.
No new control-read pause appeared in the reviewed root or regional jobs.

The [evening read-only whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37233479169)
passed with intact references and no incomplete/superseded runs. Physical usage
plus upload/headroom and the complete retained overlap envelope fit the unchanged
approved guard. The existing-layer reservation remains sufficient and unchanged.
Exact account figures remain private.

Passage production deployment `aa040084-8a56-4799-857c-ad8c729f523a` succeeded
on `0915e4c821419cee25ef14c342f9c5ef838bc824`, with all three regional models
still enabled. Its [required CI](https://github.com/deepregatta/passage/actions/runs/37233008589)
passed all four jobs. The morning independent exports and attribution checks
remain dated evidence; fresh consumer checks are still required before promotion.
GitHub and Worker full cadence both read back false. Observation continues with
all starts, two-cycle profiles, prior failures and validation history preserved.

## Morning delivery and root recovery, 2026-10-05

All three morning regional cycles published on time, with validated original
attempt reports and confirmed pointer commits:
[ICON-EU 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37259419851)
at 03:50:47.735218 UTC,
[UKV 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37262749968)
at 04:28:23.755924 UTC, and
[AROME 03Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37269237462)
at 05:49:53.543334 UTC. AROME and UKV each have five timely slots in their
original windows; ICON-EU's selected October 4 window has two. Its earlier
windows still retain the two original misses and one recheck miss, with maxima
12/14 and 13/14. All five windows were scored separately with the recorded
starts and ingestion allowance. No seven-day window has elapsed or passed.

Actions history was paginated back through the earliest observation start.
All 48 original reports and their hashes are retained: six new reports contain
three confirmed publications and three already-published skips. The skips have
no additional timely credit. No missing report, unknown timing or invalid
regional publication was found. Both catalogues and all twenty current/previous
manifests passed canonical schemas and run/layer identity checks.

Four overnight root jobs failed before publication. The original logs identify
authenticated `latest.json` GET `ReadTimeoutError` for
[waves October 4 18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37242187750)
and [GFS October 5 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37263491912),
and conditional immutable tile PUT HTTP 502/500 for
[GEFS October 4 18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37246834210)
and [ECMWF October 5 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37277248286),
respectively. The waves fallback exited zero with a duplicate-work pause and
did not publish. Subsequent scheduled
[waves 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37265940484)
and [GEFS 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37271553252)
published at 05:34:53.0014776 and 06:45:31.1845428 UTC. Both missed 18Z cycles
remain historical gaps; they must never replace the newer current runs.

Repairs `8f7e2ec08fa0041eb4254981f237910b4b01267e` and
`b7eecc863b2bae89e3118580eaf9dd6cd5eea8fe` bound authenticated object GET/stream
retries to three and settle uncertain root data-object creates by authenticated
read-back. A create retries only after proven absence; existing bytes must
match. Partial unpublished root tiles are reused only when their complete
encoded payload and nondiagnostic headers match, preserving their original
gzip and provenance. Regional immutable behavior and uncertain admission PUT
policy are unchanged. The manual recovery workflow removes one reviewed failed
cycle's duplicate identity by guarded CAS while retaining every charged counter,
lease, frequency limit, operator pause and capacity control. Local verification
passed 101 focused regressions, 475 Python tests, 60 dispatcher tests and the
required contract, Ruff, TypeScript and actionlint checks. Both required hosted
jobs passed for [each](https://github.com/deepregatta/forecast-tiles/actions/runs/37281415298)
[repair](https://github.com/deepregatta/forecast-tiles/actions/runs/37282251025).

[Guarded GFS 00Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37281551578)
passed all 27 validation checks and confirmed publication at
**08:21:45.1911938 UTC** in its original job log. Fresh pointer and manifest
read-back confirms current October 5 00Z and previous October 4 18Z.
[ECMWF's first guarded recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37282373142)
stopped before ledger mutation or ingestion because its unchanged frequency
limit permits a new start only after **13:35:29.751419 UTC**. A single same-thread
follow-up is scheduled for **15:40 Europe/Paris** today. It must reread control
and the pointer, refuse a superseded cycle, and verify the retained partial tile
samples against their original bytes and new manifest hashes after publication.
Manual recovery supplies no regional scheduled-timeliness credit. Actual
scheduled root delivery after these repairs remains a live acceptance gate.

The [morning audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37279751813)
and [post-GFS-recovery read-only audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37283528665)
passed with intact references. The unreferenced partial GEFS October 4 18Z and
ECMWF October 5 00Z prefixes remain included in physical and overlap accounting;
no cleanup was performed. Physical usage plus regional upload/headroom and the
complete overlap envelope fit the unchanged approved guard. The existing-layer
reservation remains sufficient and unchanged. Exact account figures stay private.

Passage production deployment `f2fb16d8-5789-46d4-9ce1-a3ec37ee8f17` succeeded
on `b78c44201ca1483ddebaf67b4c76c564a09b2b60`, with all three regional models
enabled and [all four required hosted jobs passing](https://github.com/deepregatta/passage/actions/runs/37235960339).
October 4 independent exports remain dated consumer evidence; fresh exports and
attribution checks are still required before promotion. GitHub
`OPENMETEO_FULL_CADENCE` and Worker `REGIONAL_FULL_CADENCE` both read back false.
The separate twice-daily acceptance heartbeat, every earlier window and every
failure remain preserved.


## ECMWF recovery follow-up, 2026-10-05

The single follow-up reread authenticated producer control and the root pointer.
The unchanged frequency interval had elapsed, the failed 00Z identity remained
charged, allowances and leases permitted work, and current main's required hosted
checks passed. [ECMWF 00Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37318831827) on
`1aa3cbcd34e56b6003efcbb37bcb17cbedd12720` passed all twelve validation checks and confirmed
publication at **2026-10-05T13:57:49.8815175Z** in its original job log. Fresh canonical pointer
and manifest checks confirm current `weather-ecmwf-20261005T00Z` and previous
`weather-ecmwf-20261004T12Z`. All three original partial tile samples remain
byte-identical; their SHA-256, manifest FNV64 hashes and sizes match. The original
HTTP 500 failure and the earlier frequency-blocked recovery remain recorded.
This manual recovery supplies no regional scheduled-timeliness credit.

Subsequent actual scheduled publication on the repaired source is also confirmed
for [waves 06Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37300226442)
at 11:22:38.4589069 UTC,
[GEFS 06Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37308339113)
at 12:42:39.5116910 UTC,
[ECMWF-short 06Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37308343002)
at 12:42:18.9584472 UTC, and
[IBI 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37292201033)
at 12:48:21.3844656 UTC. Publication lines and canonical current/previous
manifests establish delivery rather than green workflow status alone.

[GFS 06Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37296543107)
exited zero with a recompute-frequency pause at 10:37:18.2099738 UTC, within
the unchanged interval after the morning manual 00Z recovery. It did not publish
and is retained as a scheduled delivery gap. The valid 00Z remains current;
another late 06Z manual start could obstruct the upcoming 12Z slot and is not
used here. Subsequent actual scheduled GFS and ECMWF delivery remains an
acceptance gate. No policy, charged counter, lease, allowance, frequency limit,
capacity guard or reservation was relaxed or refunded.

The [during-recovery whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37319476767) and
[post-publication audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37321005605) passed read-only with intact references.
Physical usage plus upload/headroom and the complete retained overlap fit the
unchanged approved guard; the existing-layer reservation remains sufficient and
unchanged. The historical unreferenced partial GEFS 18Z prefix remains included
in accounting; no cleanup occurred. Exact account figures remain private.

Both catalogues and all twenty current/previous manifests passed their canonical
schemas and identities. All 51 original regional attempt reports and hashes are
retained after pagination through the earliest observation start. The three new
morning catch-up reports are already-published skips and add no timely credit.
All five acceptance windows were scored separately with their recorded starts
and ingestion allowance: AROME/UKV still have five timely original slots each,
and selected ICON-EU has two. The two earlier ICON-EU windows and all misses
remain intact; no seven-day window has elapsed or passed. No missing report,
unknown timing or invalid regional publication was found. Current Passage
production and all four required hosted checks remain healthy; independent
exports are still dated October 4 evidence and must be refreshed before promotion.
GitHub and Worker full cadence both remain false. This one-time recovery
follow-up is complete; the separate twice-daily acceptance heartbeat continues.


## Evening observation and ECMWF policy pause, 2026-10-05

All three evening regional cycles published on time with successful validation
and pointer commits in their original attempt reports:
[ICON-EU 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37332782559)
at 15:46:02.268740 UTC,
[UKV 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37339495226)
at 16:29:33.763671 UTC, and
[AROME 15Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37358429529)
at 19:02:50.880503 UTC. AROME and UKV each have six timely original slots;
selected ICON-EU has three. Its two earlier retained windows still cannot pass,
with maxima 12/14 and 13/14. All five windows were scored separately with their
recorded starts and ingestion allowance; none has seven elapsed days or passed.

Actions history was paginated through the earliest original observation start.
All 54 original reports and their hashes remain retained, including three new
confirmed evening publications. No missing report, unknown timing or invalid
regional publication was found. Both catalogues and all twenty current/previous
manifests passed canonical schemas and run/layer identity checks.

Natural scheduled root delivery resumed for
[GFS 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37340793767)
at 16:55:53.9635746 UTC. Original job logs also confirm
[waves 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37345241605)
at 17:26:37.9558942 UTC and
[GEFS 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37354652607)
at 18:43:40.2474801 UTC. The earlier GFS 06Z pause and every other gap remain
recorded; subsequent publication does not erase a missed attempt.

[ECMWF 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37362767519)
exited zero with a recompute-frequency pause at **19:35:53.9122773 UTC** and did
not publish. The unchanged interval from the 13:42 UTC manual recovery permitted
another start only after **19:42:22.536649 UTC**. Evening authenticated control
review confirms that frequency is now eligible and no 12Z identity or lease was
charged, but today's unchanged daily allowance is exhausted by the original
failed 00Z start and its successful recovery. No retry was dispatched and no
counter, allowance, frequency limit, pause or lease was overridden or refunded.
An overnight manual 12Z start would use allowance needed by the next day's
registered cycles, so it is not used. Verify the next actual scheduled ECMWF
delivery, retain this 12Z gap and refuse an older publication after supersession.
Current October 5 00Z and previous October 4 12Z remain valid.

The [fresh evening read-only whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37372268647) is queued on GitHub.
Current guard and reservation configuration read back unchanged. Fresh physical
usage, complete overlap and reservation sufficiency remain unknown pending
execution. The last completed whole-bucket proof is the
[October 5 afternoon audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37321005605);
its dated result remains retained and supplies no fresh evening measurement.
The historical unreferenced GEFS 18Z prefix must remain included in accounting;
no operator cleanup occurred. Exact account figures remain private.
Current forecast and Passage required hosted checks pass before this record update;
Passage production and all three model flags remain healthy. Consumer exports
are still dated October 4 evidence and must be refreshed before promotion.
GitHub and Worker full cadence both read back false. The twice-daily acceptance
heartbeat remains active; the completed one-time recovery follow-up stays paused.


### Evening capacity and hosted verification completed

The first attempt of [audit 37372268647](https://github.com/deepregatta/forecast-tiles/actions/runs/37372268647)
ended after GitHub failed to assign a hosted runner. Its job was cancelled with
zero executed steps, so it supplied no R2 measurement. Original check/job
metadata and runner diagnostics remain retained with hashes. One bounded retry,
attempt 2 on the same verified source, succeeded: the whole-bucket audit found
intact references, physical usage plus regional upload/headroom and complete
retained overlap fitting the unchanged approved guard. The existing-layer
reservation remains sufficient and unchanged. The historical incomplete GEFS
18Z prefix remains included in accounting; no operator cleanup occurred.
Exact account figures remain private. The prior queued/unknown checkpoint and
the failed first attempt remain historical evidence rather than being replaced.

Both required [hosted checks for the delivery-record commit](https://github.com/deepregatta/forecast-tiles/actions/runs/37373815519)
`7d14399fd4f3f9a0d18aee95d4f21927ffd303d5` passed. Final pagination retained
the same 54 original reports and hashes, with no further completed regional
attempts, and all twenty current/previous manifests remain canonical and valid.
The ECMWF 12Z miss and unchanged daily-allowance gate remain recorded; subsequent
scheduled delivery still needs confirmation. No seven-day window has passed,
and GitHub/Worker full cadence remains false. The twice-daily acceptance
heartbeat continues with no additional follow-up required for this audit.


## Morning observation and scheduled ECMWF resumption, 2026-10-06

The original validated attempt reports confirm timely scheduled replacements for
[ICON-EU 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37408947790)
at 03:45:47.996319 UTC,
[UKV 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37412892316)
at 04:31:24.968082 UTC, and
[AROME 03Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37420185290)
at 05:50:11.752264 UTC. AROME and UKV each have seven timely original slots;
selected ICON-EU has four. All five windows were scored with their unchanged
starts and ingestion allowance. Neither seven elapsed days nor acceptance has
been reached. ICON-EU's two earlier windows and misses remain retained, with
unchanged maxima 12/14 and 13/14.

Three pages of Actions history reached the earliest observation start. All 60
original attempt reports and hashes are retained: six new reports comprise the
three confirmed publications and three already-published catch-up skips. No
missing report, unknown timing or invalid regional publication was found.
Both catalogues and all twenty current/previous manifests passed canonical
schemas and identities. Six current ECMWF/regional tile samples match manifest
sizes and compressed-byte hashes and decode as PFT1.

[Scheduled ECMWF October 6 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37429121228)
confirmed actual publication at **2026-10-06T07:46:15.1256209Z** in its original
job log. Fresh readback confirms current `weather-ecmwf-20261006T00Z` and previous
`weather-ecmwf-20261005T00Z`. The earlier October 5 12Z miss remains recorded,
including its [fallback attempt](https://github.com/deepregatta/forecast-tiles/actions/runs/37377017734)
at 21:37:15.4703219 UTC, which exited zero at the unchanged daily-start limit
without publication. No midnight manual retry consumed the next day's allowance,
and no older cycle was published. Every charged counter, lease, frequency limit,
allowance, operator pause, guard and reservation remains unchanged by this review.

Original logs confirm nine overnight root publications across GFS, waves, GEFS,
ECMWF-short, currents and ECMWF. Six already-published root fallback skips and
the ECMWF daily-limit pause remain separately classified with original log
hashes. Subsequent publication does not erase any historical missed attempt.
All seven root layers remain present with valid current/previous references.

The [fresh read-only whole-bucket audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37431873022) passed on attempt 1 with intact
references. Physical usage plus regional upload/headroom and the complete
retained overlap fit the unchanged approved guard; the existing-layer reservation
remains sufficient and unchanged. The incomplete GEFS October 4 18Z prefix stays
included in accounting; no operator cleanup occurred. Exact account figures
remain private, and the previous hosted-runner failure remains historical evidence.

Both forecast hosted jobs and all four Passage hosted jobs passed on the reviewed
source commits. Passage production remains healthy with all three regional models;
independent consumer exports retain their October 4 evidence date and must be
refreshed before promotion. GitHub and Worker full cadence both read back false.
The twice-daily acceptance heartbeat continues; no owner decision is pending.


## Evening delivery stop from expired billing review, 2026-10-06

Authenticated control and every new original job diagnostic confirm that the
required provider-cost review expired at **2026-10-06T09:03:45.229649Z**, after
its unchanged 72-hour lifetime. Production enforcement remains enabled and
`paused=false`; the stale-review check stops admission before cycle/source
discovery. Twenty-three reviewed root attempts and nine regional attempts exited
zero at this gate without publishing. A green workflow therefore supplies no
new-delivery evidence. Root and regional pointers have not advanced since the
morning review. All seven root layers, three regional layers and twenty
current/previous manifests remain canonical and readable; three regional tile
samples match manifest sizes/hashes and decode as PFT1.

The three registered evening slots are confirmed misses:
[ICON-EU 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37487471311)
at 15:26:35.5100704 UTC,
[UKV 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37494357097)
at 16:16:39.4094583 UTC, and
[AROME 15Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37513849095)
at 18:46:39.8385757 UTC. All three original job logs report
`paused: billing review is stale`. Their requested cycles identify the slots;
no source cycle or publication was resolved. The other six new regional reports
are cycle-unspecified catch-up pauses and receive no inferred cycle or timing
credit. Three pages of history reached the earliest original observation start.
The temporary artifact cache was absent; all sixty earlier original GitHub
reports were restored byte-for-byte against their committed hashes. All 69
reports, prior classifications and original job diagnostics remain retained.

The scorer previously treated compact CLI `requested_cycle` values as naive ISO
timestamps and crashed on these paused attempts. Repair `d8d0aaf` uses the existing
UTC CLI cycle parser for that exact identity format; ordinary timestamps still
require explicit timezones and cycle-unspecified catches remain unknown.
All five unchanged windows now score successfully. AROME and UKV originals each
have seven timely slots and one failed slot, with maximum **13/14**. Selected
ICON-EU has four timely and one failed slot, maximum **13/14**; its earlier
retained windows now have maxima **11/14** and **12/14**. None can reach 95%.
All original observation/bootstrap times, profile hours, earlier windows and
misses remain intact. No new recheck begins before verified restoration, and no
late/manual recovery receives timely credit or changes historical scoring.

Required [CI 37529836233](https://github.com/deepregatta/forecast-tiles/actions/runs/37529836233)
failed in an existing attempt-equivalence test because two acquisition timestamps
crossed a wall-clock second; 476 other Python tests and dispatcher passed.
Repair `26d8239` fixes only that test's acquisition clock. All 477 local Python
tests, 60 dispatcher tests, lint/contracts and
[both required hosted jobs](https://github.com/deepregatta/forecast-tiles/actions/runs/37530055537)
passed. The original failed check and diagnostic hash remain retained; no waiver
or production timestamp change occurred.

The [fresh read-only capacity audit](https://github.com/deepregatta/forecast-tiles/actions/runs/37529174262) passed on attempt 1 with intact
references. Physical usage plus regional upload/headroom and complete retained
overlap fit the unchanged approved guard. Existing-layer reservation and
headroom remain sufficient and unchanged. Its prior configuration baseline was
restored from the original morning AROME job log. The historical incomplete GEFS
October 4 18Z prefix remains accounted for; no cleanup occurred. Exact account
figures remain private. Capacity evidence cannot replace the required billing
review: the [recovery policy](paid-work.md#operator-pause-and-recovery) requires
current account-wide costs, billing period, conversion/tax treatment and
outstanding-work review before a conditional provider-decision update.

At the stopped checkpoint, billing API reads returned HTTP 403 and the browser
required owner sign-in. Before the separately recorded renewal, no control write,
producer restart, allowance/counter refund, gate bypass or cadence change occurred. Passage's current
production and four hosted jobs remain healthy, with consumer exports still dated
October 4 and requiring refresh before promotion. GitHub and Worker full cadence
both remain false. The twice-daily heartbeat continues, keeping this unresolved
review gate visible and retaining every miss rather than restarting history.


## Conditional billing review renewal, 2026-10-06

Owner sign-in enabled a real account-wide R2 Class A/Class B/storage billing
review in the provider dashboard. Its billing period matches the existing
control; reported billing, conversion/tax treatment and outstanding work support
the existing reviewed decision. The existing budget alert remains intact.
Exact account figures and the financial receipt remain private; provider costs
are delayed and this remains a producer guard rather than a global billing cap.

The new manual [review-paid-work workflow](../.github/workflows/review-paid-work.yml)
changes only `reviewed_at`, using one conditional write and exact authenticated
read-back. It defaults to dry run, requires a completed review within fifteen
minutes and the exact previous timestamp, and refuses closed provider decisions,
operator pause or expired periods. Conflicts/uncertain writes stop without another
PUT. Twelve focused regressions, all 489 local Python and 60 dispatcher tests,
lint/contracts and [both hosted gates](https://github.com/deepregatta/forecast-tiles/actions/runs/37531294917)
passed on `ffecd433b50af16eeb8e7ea32ece9f1fd5594bbf`. The vendored shared guard is
unchanged; no credential or provider plan change occurred.

[Renewal 37531498219](https://github.com/deepregatta/forecast-tiles/actions/runs/37531498219)
confirmed one CAS and exact authenticated read-back at
**2026-10-06T21:06:38.467282Z**. Every other field, including provider decision,
operator pause, period, cumulative/day counters, charged identities, leases,
runtime/frequency limits, capacity guard and reservations, remains intact.
The guard's 72-hour review lifetime is unchanged. No cost decision was inferred
from workflow success or refreshed automatically without evidence.

Six ordinary bounded guarded recovery jobs were dispatched for GEFS 12Z, full
ECMWF 12Z, IBI October 6, and the three missed regional canary cycles. The paused
attempts never charged those identities, so no duplicate removal or counter
refund was needed. Manual recovery gives no timely scheduled slot. GFS, waves
and short ECMWF are left for their approaching scheduled cycles because admission
for older cycles now would block those slots under existing frequency limits.
All original misses remain recorded; actual recovery publication, immutable
read-back and later scheduled delivery are separate gates. Full cadence stays
false. Future reviews must use fresh provider evidence before the unchanged
72-hour expiry; a timestamp-only renewal is never a substitute for that review.


## Regional restoration and separate rechecks, 2026-10-06

Original publication logs and the original immutable attempt reports confirm
[AROME 15Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531680691)
at 21:10:01.5939606 UTC,
[UKV 12Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531688268)
at 21:11:27.3648129 UTC, and
[ICON-EU 12Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531684498)
at 21:11:55.4611924 UTC. Each report passes its scientific checks and confirms
the pointer commit. Fresh public read-back retains the morning runs as previous;
all twenty root/regional current/previous manifests validate, and all three
recovered regional sample tiles match manifest bytes/FNV64 and decompress as PFT1.
The cached pointer was checked again with cache bypass to identify the new
immutable runs. Seventy-two original reports and hashes are retained, with
history paginated back to the earliest original observation start.

The three recoveries are late in all earlier relevant windows. Current scores
therefore retain one October 6 late slot for AROME/UKV and the selected October 4
ICON-EU window, plus every earlier ICON-EU miss. The stopped checkpoint and all
twenty-three root/nine regional paused attempts remain in incident/review history.
No publication or recovery erases a miss.

Under the owner's recorded autonomous routine-recheck authorization, each model
has a **separate** `post-billing-review-restoration` window starting
**2026-10-06T21:14:59.217473+00:00** and ending **2026-10-13T21:14:59.217473+00:00**. All original
bootstrap/observation times, two-cycle profiles, historical ICON-EU recheck start,
prior windows and misses remain intact. The first expected cycles are October 7
00Z for ICON-EU/UKV and 03Z for AROME. All fourteen slots are still pending; the
three manual late recoveries precede this schedule and earn zero timely credit.
Seven elapsed days and all fourteen timely deliveries remain mandatory.

Fresh live Passage exports from the recovered October 6 cycles each contain
**75 independently decoded GRIB2 messages**, with correct reference cycle,
u/v/gust parameter numbers and 25 gust messages. AROME/ICON-EU retain template 8
preceding-hour maxima; UKV retains template 0 instantaneous gusts. English/French
source/license attribution and gust notices match each model, including UKV's
CC BY-SA attribution and modifications. The actual downloaded files were decoded
with ecCodes; the first browser download watcher timed out, but the UI Saved state
and original file proved successful export without a duplicate download.
Observed 1905px desktop and 375px mobile layouts expose all model/download controls
without horizontal page overflow. Temporary viewport, original language/model/period
were restored. Responsive emulation is separate from the historical physical-phone
evidence. The October 4 export checkpoint remains retained in export history;
new exports will still need freshness verification at promotion.

[IBI recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531677012)
confirmed publication at 21:10:19.0860175 UTC. GEFS/full ECMWF recoveries and
subsequent scheduled GFS/waves/short-ECMWF delivery remain separately verified
gates. The twice-daily heartbeat retains its schedule and quiet notification
policy. It now requires a fresh real account-wide billing review once review age
reaches 36 hours, before the unchanged 72-hour expiry, and permits the narrow
conditional renewal only with actual evidence. Unavailable evidence/authentication
remains a gate requiring action; no automatic fabricated timestamp refresh.
GitHub and Worker full cadence both remain false.


## In-progress root-recovery capacity checkpoint, 2026-10-06

[Read-only audit 37533093940](https://github.com/deepregatta/forecast-tiles/actions/runs/37533093940)
passed its inventory/reference checks while GEFS and full ECMWF recovery were
still running. Physical storage plus regional upload/headroom fits the unchanged
approved guard, and the measured existing-layer reservation remains sufficient.
The **complete conservative overlap envelope does not fit in this snapshot**:
it counts all unreferenced objects, including the two active incomplete root
uploads, alongside retained/upload reservations. Inventory workflow success does
not waive this comparison. The original snapshot and job-log hash remain retained.
No guard, reservation, headroom, control or deletion changed. Refresh after actual
publication and ordinary publisher retention; full-cadence promotion remains closed
until the complete envelope fits. Exact account figures stay private.


## All bounded recoveries confirmed; overlap gate restored, 2026-10-06

[GEFS 12Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531669714)
confirmed publication from its original log at 21:23:31.7657237 UTC, and
[full ECMWF 12Z recovery](https://github.com/deepregatta/forecast-tiles/actions/runs/37531673632)
at 21:26:03.2907488 UTC. All six bounded recovery workflows now passed with
confirmed publication logs; root and regional immutable current/previous
read-back agree. All seven root layers remain present. Three recovered root
sample tiles match manifest sizes/FNV64 and decode as PFT1; their original
previous manifests remain byte-identical. None of these manual recoveries
provides timely regional acceptance credit.

Authenticated ledger read-back preserves every policy/period/pause/limit field,
every prior charged identity and daily counter. The six recoveries were charged
exactly once for their ordinary reserved runtimes, with every recovery lease
released. No counter refund, duplicate removal, uncertain-write retry or gate
bypass occurred.

[Post-publication read-only audit 37533695166](https://github.com/deepregatta/forecast-tiles/actions/runs/37533695166)
passes intact references, physical plus uploads/headroom, complete retained
overlap including all unreferenced objects, measured root variation/reservation
sufficiency and the calculated envelope. The approved guard, existing-layer
reservation and headroom are unchanged. The earlier in-progress overlap miss
and its original snapshot remain retained; publication/reference transition and
ordinary publisher retention restored the fit, without manual cleanup or policy
changes. Historical incomplete GEFS October 4 18Z remains accounted for. Exact
account figures stay private.

The remaining live gates are seven elapsed days and fourteen timely slots per
selected recheck, actual subsequent scheduled replacement/delivery, continued
capacity and consumer freshness, and passing current required hosted checks.
GFS, waves and short ECMWF are still to be observed at their approaching
scheduled slots. Their older missed cycles will never overwrite newer data.
Both full-cadence flags remain false, and the twice-daily heartbeat continues
autonomously with the pre-expiry real billing-review procedure.


## First scheduled recheck cycles and short-ECMWF fallback collision, 2026-10-07

The selected post-billing-review-restoration windows each have **one timely
scheduled cycle and thirteen pending**, with no new regional miss, unknown or
invalid publication. Original reports, publication logs, fresh pointers and
immutable tile samples confirm
[ICON-EU 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37566725078)
at 03:49:53.3121912 UTC,
[UKV 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37570652060)
at 04:34:22.2184378 UTC, and
[AROME 03Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37577968577)
at 05:49:59.8549610 UTC. All three source/scientific validations pass with zero
failures and pointer commits confirmed. Three other new reports from
[catch-up 37550362178](https://github.com/deepregatta/forecast-tiles/actions/runs/37550362178)
are already-published skips for the preceding cycles and supply no new timing
credit. Three pages of Actions history reach the earliest original observation;
all 78 original reports are retained and prior receipts verified against their
recorded hashes. Every original start, bootstrap, two-cycle profile, failed
window and miss stays intact. The selected windows still end **October 13 at
21:14:59.217473 UTC** and require all fourteen timely cycles plus seven elapsed
days. Original AROME/UKV windows now score 8 timely, 1 late and 5 pending;
ICON-EU original scores 6 timely, 3 late and 5 pending, its October 3 recheck
6 timely/2 late/6 pending and October 4 recheck 5 timely/1 late/8 pending.
Those historical windows remain incapable of reaching 95%.

Scheduled existing delivery resumed: GFS
[October 6 18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37540549658)
and [October 7 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37571435891),
waves [18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37544139538)
and [00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37574261783),
GEFS [18Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37551066766)
and [00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37580586630),
[full ECMWF 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37586794698)
at 07:51:26.9783547 UTC and
[GLO12 00Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37577965870)
at 07:43:40.6413421 UTC have original publication-log confirmation. The earlier
GLO12 catch-up 37549695664 exited zero because its next-day source axis was not
ready; it receives no publication credit. Sixteen completed root attempts are
retained individually: nine publications, five already-published skips, one
source-unavailable outcome and one frequency pause. IBI's October 6 guarded
recovery remains current; its October 7 scheduled slot was not yet due.

Short ECMWF encountered a new retained miss. The
[delayed fallback 37549904334](https://github.com/deepregatta/forecast-tiles/actions/runs/37549904334)
selected the older **October 6 06Z** complete cycle at 00:06:22 UTC and published
it at 00:17:18.4699931 UTC. The
[explicit October 6 18Z job](https://github.com/deepregatta/forecast-tiles/actions/runs/37551068948)
observed readiness at 00:27:44.1283607 UTC but stopped at the unchanged six-hour
recompute frequency limit. Green workflow conclusions do not erase this 18Z
miss. Both original logs and hashes are retained. No older cycle was written
over a newer pointer, and no invalid run was published.

Repair **869eac1ff391e38b492236b10ea9b0afd7ac9267** makes only the scheduled
short-ECMWF fallback name the newest **started** 06Z/18Z cycle in UTC, checking
once. An unavailable successor skips before admission instead of publishing its
older complete predecessor. Manual defaults/explicit cycles, dispatcher waits,
operator pause, paid-work checks, charged counters, six-hour frequency limit,
two-start daily allowance, leases and capacity controls remain unchanged.
Midnight/year/timezone boundary and real CLI/source-readiness regressions prove
that a complete predecessor cannot acquire or build while the named successor
is unavailable. All **502 Python tests, 60 dispatcher tests**, contract/lint
checks, workflow actionlint and
[both required hosted jobs](https://github.com/deepregatta/forecast-tiles/actions/runs/37589977374)
passed. Actual scheduled short-ECMWF replacement after the fix remains a separate
observation gate. A manual 18Z recovery now would consume the approaching
October 7 06Z frequency/daily allowance, so it is left for the **12:15 UTC**
scheduled dispatch, without an override, refund or replay.

Fresh read-back retains all seven root and three regional layers and validates
twenty canonical current/previous manifests. All seven root and three regional
sampled tiles match manifest bytes/hashes and decode as PFT1; previous manifests
match retained bytes where prior samples exist. The
[read-only whole-bucket audit 37589462950](https://github.com/deepregatta/forecast-tiles/actions/runs/37589462950)
passes physical plus upload/headroom, complete retained overlap including all
unreferenced objects, root variation and existing-layer reservation sufficiency.
Approved guard, reservation and headroom stay unchanged. The historical
incomplete GEFS October 4 18Z prefix and October 6 in-progress overlap miss remain
accounted for; no manual cleanup occurred. Exact account figures stay private.

Authenticated control read-back confirms the real **October 6 21:06:38.467282
UTC** billing-review timestamp is unchanged and about eleven hours old. Every
policy/period/pause/limit and prior charged identity/daily counter is preserved;
charged starts/runtime only increase through normal admissions. No review
renewal is due or performed. The next real account-wide review is due at the
36-hour checkpoint **October 8 09:06:38.467282 UTC**, before the unchanged
72-hour expiry **October 9 21:06:38.467282 UTC**. Authentication was refreshed
through Wrangler's standard read-only account command; no credentials or
provider billing settings changed.

Worker and GitHub full cadence remain false. Passage's existing deployment and
all four required hosted checks remain healthy. Its independent October 6
consumer exports stay dated in `current_exports`, and October 4 history stays
retained; no fresh export or physical-device claim is made here. Refresh current
exports, bilingual attribution/gust semantics and hosted validation again before
promotion. Regional observation continues with unchanged selected windows while
actual repaired short-ECMWF scheduled delivery remains to be verified.


## Evening scheduled replacement checkpoint, 2026-10-07

Each selected regional recheck now has **two timely scheduled cycles and twelve
pending**, with no new miss, unknown or invalid publication. Confirmed original
job logs and reports record
[ICON-EU 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37643887782)
at 15:45:28.9101957 UTC,
[UKV 12Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37650685324)
at 16:32:54.2344171 UTC and
[AROME 15Z](https://github.com/deepregatta/forecast-tiles/actions/runs/37669351274)
at 19:04:19.1028605 UTC. All three scientific validations pass with zero failures
and pointer commits confirmed. Fresh current/previous manifests show replacement
of the morning cycles, and sampled current tiles match manifest hashes/bytes
and decode as PFT1. The morning manifests remain byte-identical as previous.
Four pages of Actions history reach the earliest original observation. All 87
original reports are retained and prior receipt hashes rechecked; six other new
reports from catch-ups 37599338544 and 37679834767 were already-published skips
and supply no new timing credit. Original AROME/UKV windows score 9 timely,
1 late and 4 pending; ICON-EU original scores 7 timely/3 late/4 pending, its
October 3 recheck 7 timely/2 late/5 pending and October 4 recheck 6 timely/1 late/
7 pending. These historical failed windows, all misses and original starts
remain intact. No selected window is reset: **October 13 21:14:59.217473 UTC**,
seven elapsed days and all fourteen timely cycles are still mandatory.

The repaired short-ECMWF scheduled delivery is now verified.
[Explicit October 7 06Z job 37619795811](https://github.com/deepregatta/forecast-tiles/actions/runs/37619795811)
waited normally for readiness, observed the 144-hour axis after seven checks
at 12:28:46.0428534 UTC and confirmed publication at **12:41:22.1449797 UTC**.
Fresh root read-back holds `weather-ecmwf-short-20261007T06Z`, with the retained
October 6 06Z as previous; its sampled immutable tile and previous manifest
pass hash/byte checks. The repaired scheduled fallback
[37598855320](https://github.com/deepregatta/forecast-tiles/actions/runs/37598855320)
requested October 7 06Z at 09:11 UTC and
[37679592926](https://github.com/deepregatta/forecast-tiles/actions/runs/37679592926)
requested October 7 18Z at 20:07 UTC. Both correctly skipped unavailable
successors before admission. Neither built or charged a preceding complete
cycle. The prior October 6 18Z frequency miss and original logs remain retained;
closing the subsequent-delivery gate does not erase that history, grant regional
timing credit or relax a control. No manual older-cycle replay occurred.

Twenty-three new completed root attempts are retained separately: nine actual
publications, twelve already-published skips and the two expected
source-unavailable short fallbacks, with no new pause. Original logs confirm
GFS, waves and GEFS October 7 06Z and 12Z, full ECMWF October 7 12Z, short ECMWF
October 7 06Z and IBI October 7 00Z. IBI
[job 37602882406](https://github.com/deepregatta/forecast-tiles/actions/runs/37602882406)
confirmed publication at 12:33:41.1204563 UTC; the full ECMWF
[12Z job 37673833432](https://github.com/deepregatta/forecast-tiles/actions/runs/37673833432)
confirmed publication at 19:47:49.2188496 UTC. Root retains all seven existing
layers and the regional catalogue all three. Twenty canonical current/previous
manifests validate; all seven root and three regional current tile samples
match immutable manifest bytes/hashes and decode as PFT1. Every retained
manifest that was sampled in the morning remains byte-identical.

The [evening whole-bucket audit 37684535798](https://github.com/deepregatta/forecast-tiles/actions/runs/37684535798)
passes physical plus uploads/headroom, complete retained overlap including all
unreferenced objects, root variation and reservation sufficiency. Approved guard,
existing-layer reservation and headroom remain unchanged. The historical
incomplete GEFS October 4 18Z prefix and earlier in-progress overlap miss remain
accounted for; no manual cleanup or policy change occurred. Exact account figures
remain private. Authenticated control read-back finds the real October 6 billing
review about 24 hours old, with every policy/period/pause/limit and earlier
charged identity/daily counter preserved. No renewal is due or performed. A
fresh actual account-wide review is due from **October 8 09:06:38.467282 UTC**,
before the unchanged **October 9 21:06:38.467282 UTC** expiry.

Both full-cadence flags remain false. Current forecast source and Passage's
production deployment retain passing required hosted checks; no software fix
or new consumer export was needed in this review. Independent October 6 exports
and October 4 history remain dated, with physical-device evidence separate.
Refresh consumer exports/attribution and required hosted validation before
promotion. All selected windows continue observing, with no owner action needed.
