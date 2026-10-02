# Regional delivery: evidence and activation gates

AROME and ICON-EU implementation is available for dry runs. R2 production is
disabled in the registry; workflow and Worker allowlists default empty. UKV is
not registered. Do not activate a model until its outstanding gates are closed.

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

Both pass 8 MiB gzip / 32 MiB decoded tile limits. Manifests now declare gzip,
inflated and decoded sizes for preflight admission, served geometry, coverage,
capabilities, attribution and cycle-specific scheduling. Large source-object
inventories remain once per manifest; deterministic regional tile headers use
the cycle timestamp and source digest. Existing root tile generation retains
its previous behavior.

The isolated live [R2 conditional check](https://github.com/deepregatta/forecast-tiles/actions/runs/37032148191)
passed three tests in 37.06 s: immutable creation, conditional replacement and
interleaved/stale publisher behavior. Its temporary prefix was cleaned. The
manual `r2-audit` workflow reads both catalogues and reports abandoned uploads
and damaged references without deleting anything.

Passage's real headless desktop Chrome loaded actual regional dry-run tiles
through `HttpTileTransport` and the updated engine, for Brest–Cherbourg route
points and 12–18 UTC. The root was a **small synthetic fixture**, not a full
production GFS workload. These are sampled JS heap plus backing-storage
measurements, not process RSS or proof of the absolute peak.

| Model | Cold regional transfer / elapsed | Warm transfer / elapsed | Sampled added heap + backing storage |
|---|---:|---:|---:|
| AROME | 3,845,933 B / 194 ms | 0 B / 0 ms | 54,931,889 B (52.4 MiB) |
| ICON-EU | 4,888,594 B / 228 ms | 0 B / 1 ms | 63,754,930 B (60.8 MiB) |

Each route used one regional tile. Browser regressions cover desktop and mobile
viewports; viewport emulation does not establish physical-phone memory usage.
Passage's `scripts/serve-regional-bench.mjs` reproduces the scratch server;
repeat with a representative root workload and a physical phone before activation.

## Activation sequence

1. Complete representative desktop/phone cold/warm, boundary-route and memory
   measurements against the root-only baseline. Audit Tactician separately;
   tile decoding does not prove that consumer's model/export support.
2. Run the live read-only audit. Reconcile damaged references and abandoned
   uploads before measuring a worst-case simultaneous **existing-layer** peak.
   Keep private account figures in GitHub variables, outside this public repo.
3. Configure positive `REGIONAL_EXISTING_PEAK_BYTES` (root current/previous plus
   simultaneous root uploads, including their manifests) and
   `REGIONAL_HEADROOM_BYTES`. Admission adds all nonreferenced bucket bytes
   (routing data, pointers, metadata and orphans), three capped runs per enabled
   regional, and headroom. Both reserved and physical upload peaks must fit the
   unchanged 8,000,000,000-byte guard. Run caps include tiles plus manifest.
4. Deploy Passage with `VITE_REGIONAL_MODELS` naming only the tested model,
   open that model's registry production gate, and set the same model in GitHub
   `OPENMETEO_ENABLED_LAYERS` and Worker `REGIONAL_MODELS`. Direct CLI publication
   also needs `REGIONAL_ENABLED_LAYERS`. Worker deployment is a maintainer task.
   No flags have been enabled by this implementation.
5. Start with AROME 03/15Z or ICON-EU 00/12Z. Workflow `canary` defaults true;
   `OPENMETEO_CANARY=true` restricts CLI automatic selection and cadence too.
   Leave GitHub `OPENMETEO_FULL_CADENCE` and Worker `REGIONAL_FULL_CADENCE` false
   until seven days meet the plan's 95% timeliness/no-invalid-run criteria and
   replacement, outage and rollback are exercised. The :47 catch-up and all
   dispatched jobs share `ingest-${layer}`, cancel false, max parallel one.

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
