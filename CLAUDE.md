# forecast-tiles

Public repo. Scheduled ingestion pipeline turning open NOAA / Copernicus / ECMWF forecast data into compact, immutable **PFT1** tiles on Cloudflare R2, consumed by [Passage](https://github.com/deepregatta/passage) entirely in the browser. Pipeline detail and tile spec: [README.md](README.md), `docs/`.

## Layout

- `src/` — Python pipeline (uv-managed): resolve latest complete provider cycle → byte-range download → decode/orient/quantize into a `ForecastCube` → 10°×10° tiles → gzip → R2.
- `contracts/` — tile/manifest/latest JSON Schemas, vendored from passage's `contracts/`; keep in sync when the spec changes.
- `scripts/`, `tests/` — tooling and pytest suite.
- Production runs are GitHub Actions cron jobs, one per layer.

## Commands

```bash
uv sync
uv run pytest
uv run ingest weather --dry-run /tmp/tiles   # local run, no R2 writes
uv run ingest weather|ensemble|waves|currents|weather-ecmwf
```

## Conventions

- Commit and push to main after edits — don't wait to be asked.
- Published runs are immutable: never mutate an existing `forecast-runs/{run_id}/` layout; changes ship as new runs + `latest.json`.
- This repo is **public** — no credentials, no private product details in code or docs.
