# forecast-tiles

Public repo. Scheduled ingestion pipeline turning open NOAA / Copernicus / ECMWF forecast data into compact, immutable **PFT1** tiles on Cloudflare R2, consumed by [Passage](https://github.com/deepregatta/passage) entirely in the browser. Pipeline detail and tile spec: [README.md](README.md), `docs/`.

Current capabilities, configuration versus dated delivery evidence, verification and maintained documentation links: [README → Operational status and verification](README.md#operational-status-and-verification). Recheck live evidence when a task requires it; enabled configuration alone does not prove delivery.

## Layout

- `src/` — Python pipeline (uv-managed): resolve latest complete provider cycle → byte-range download → decode/orient/quantize into a `ForecastCube` → 10°×10° tiles → gzip → R2. `src/landkit/` + `src/ingest/land/` are the second, non-scheduled pipeline: the **conservative routing index** (`docs/land-index-format.md`), consumed by tactician's `core/land`.
- `src/ingest/sources/openmeteo/` — regional models from Open-Meteo's bulk files (registry, catalog, reader, footprints, adapter); AROME, ICON-EU and UKV have enabled production gates for two-cycle/day canaries. They publish to `latest-regional.json`, never root `latest.json`. Runtime allowlists and capacity controls also apply; manual workflow dry runs default on. Full cadence remains disabled in checked-in Worker config; dated delivery/acceptance evidence is in `docs/regional-delivery.md` and `docs/regional-canary-status.json`.
- `currents` uses CMEMS GLO12 only; `currents-ibi` uses CMEMS IBI. Not-ready cycles wait/skip; outages fail with the provider cause. No RTOFS fallback is implemented.
- `contracts/` — tile/manifest/latest JSON Schemas. The `forecast-*` ones are vendored from passage's `contracts/`; keep in sync when the spec changes. `contracts/shared-contracts.lock.json` pins canonical paths/revisions/digests; [shared-contract checks](docs/shared-contracts.md) run in the local/CI Python gate. The `land-index-*` ones are **canonical here** — tactician consumes them.
- `scripts/`, `tests/` — tooling and pytest suite.
- Root forecast layers have one GitHub Actions workflow each; regionals share `ingest-openmeteo.yml` with per-layer concurrency. `dispatcher/` is a Cloudflare Worker (TypeScript, Cron Triggers only), configured to dispatch with `--wait-minutes`. Root fallback crons run at :37 and regional catch-up at :47 every six hours. Deployment/delivery checkpoints are linked in README → Dispatcher; current operation needs fresh evidence.

## Commands

```bash
uv sync --all-extras                         # prepare Python verification dependencies
(cd dispatcher && npm ci)                    # prepare pinned dispatcher dependencies
bash scripts/verify.sh                       # installed Python + dispatcher gates; excludes live R2 writes
uv run ingest weather --dry-run /tmp/tiles   # local run, no R2 writes
uv sync --extra currents                    # CMEMS GLO12 and IBI dependency
uv run ingest currents-ibi --dry-run /tmp/ibi
uv run ingest land --domain nweu             # routing index; one-shot, not a cron
uv sync --extra openmeteo                    # regional dependency; production gates are separate
uv run ingest weather-arome --dry-run /tmp/arome
```

## Conventions

- Commit and push to main after edits — don't wait to be asked.
- Published runs are immutable: never mutate an existing `forecast-runs/{run_id}/` layout; changes ship as new runs + `latest.json`. The same rule holds for `land-index/{index_id}/`, and `publish_index` enforces it.
- The routing index is **conservative in one direction only**: every compilation step adds blocked area and none removes any. Its buffer, safety contour and cell size are named, versioned manifest parameters, never inline literals. It is routing legality, never a chart.
- This repo is **public** — no credentials, no private product details in code or docs.
