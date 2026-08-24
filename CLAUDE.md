# forecast-tiles

Public repo. Scheduled ingestion pipeline turning open NOAA / Copernicus / ECMWF forecast data into compact, immutable **PFT1** tiles on Cloudflare R2, consumed by [Passage](https://github.com/deepregatta/passage) entirely in the browser. Pipeline detail and tile spec: [README.md](README.md), `docs/`.

## Layout

- `src/` — Python pipeline (uv-managed): resolve latest complete provider cycle → byte-range download → decode/orient/quantize into a `ForecastCube` → 10°×10° tiles → gzip → R2. `src/landkit/` + `src/ingest/land/` are the second, non-scheduled pipeline: the **conservative routing index** (`docs/land-index-format.md`), consumed by tactician's `core/land`.
- `contracts/` — tile/manifest/latest JSON Schemas. The `forecast-*` ones are vendored from passage's `contracts/`; keep in sync when the spec changes. The `land-index-*` ones are **canonical here** — tactician consumes them.
- `scripts/`, `tests/` — tooling and pytest suite.
- Production runs are GitHub Actions cron jobs, one per layer.

## Commands

```bash
uv sync
uv run pytest
uv run ingest weather --dry-run /tmp/tiles   # local run, no R2 writes
uv run ingest weather|ensemble|waves|currents|weather-ecmwf
uv run ingest land --domain nweu             # routing index; one-shot, not a cron
```

## Conventions

- Commit and push to main after edits — don't wait to be asked.
- Published runs are immutable: never mutate an existing `forecast-runs/{run_id}/` layout; changes ship as new runs + `latest.json`. The same rule holds for `land-index/{index_id}/`, and `publish_index` enforces it.
- The routing index is **conservative in one direction only**: every compilation step adds blocked area and none removes any. Its buffer, safety contour and cell size are named, versioned manifest parameters, never inline literals. It is routing legality, never a chart.
- This repo is **public** — no credentials, no private product details in code or docs.
