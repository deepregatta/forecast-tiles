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
