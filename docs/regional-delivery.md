# Regional delivery: evidence and activation gates

AROME, ICON-EU and UKV implementation is available for dry runs. R2 production
is disabled in the registry; workflow and Worker allowlists default empty. Do not activate a model until its outstanding gates are closed.

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
   also needs `REGIONAL_ENABLED_LAYERS`. Worker deployment is a maintainer task.
   No flags have been enabled by this implementation.
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
and application record remain outside this public repository. Regional model
allowlists remain absent/empty, all three registry production gates remain
false, and no consumer or Worker deployment was performed.

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
  recorded above; model enablement remains off.
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
Production activation and seven-day canaries remain pending; final phone
verification is recorded above.

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

AROME is the first registry-eligible canary; GitHub, consumer and Worker
allowlists still control actual activation. Keep two cycles/day until the
seven-day criteria pass. Subsequent models activate individually after the
first live publication/consumer checks; later expansion is a separate decision.
