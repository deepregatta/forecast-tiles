# forecast-tiles

Scheduled ingestion pipeline that turns open NOAA / Copernicus / ECMWF forecast
data into compact, immutable, provider-independent **PFT1** tiles served from
Cloudflare R2 and consumed by [Passage](https://github.com/deepregatta/passage)
entirely in the browser.

```
NOAA GFS / GEFS / GFS-Wave · Copernicus GLO12 · ECMWF open data
        │  scheduled GitHub Actions (this repo)
        ▼
normalize → validate → quantize (int16/int8) → 10°×10° tiles → gzip
        │
        ▼
Cloudflare R2:  forecast-runs/{run_id}/…  (immutable)  +  latest.json
```

## Running the pipeline

```sh
uv sync
uv run ingest weather                        # latest complete GFS cycle -> R2
uv run ingest weather --cycle 20260713T06    # explicit cycle
uv run ingest weather --dry-run /tmp/tiles   # write the R2 layout locally instead
uv run ingest ensemble|waves|currents|weather-ecmwf
```

Each run: resolve the latest **complete** provider cycle (`.idx` presence,
falling back one cycle rather than publishing a partial run) → download via
byte-range subsetting → decode/orient/quantize into a `ForecastCube` →
validate (step coverage, physical ranges, gust ≥ wind, missing fraction; any
failure aborts before upload) → 10°×10° gzipped PFT1 tiles → atomic publish
(storage guard first, tiles, `manifest.json` last, post-publish re-download
check, `latest.json`, retention delete, `status/{layer}.json`).

Scheduled GitHub Actions run each layer 4×/day (currents 1×/day) — see
`.github/workflows/ingest-*.yml`. `ingest weather-ecmwf` exits 0 with a log
line when ECMWF hasn't published a full-horizon cycle yet.

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
5. For the currents layer also add `COPERNICUSMARINE_SERVICE_USERNAME` and
   `COPERNICUSMARINE_SERVICE_PASSWORD` (free Copernicus Marine account).

The storage guard refuses to publish when retained runs + the new run would
exceed `MAX_BUCKET_BYTES` (default 8 GB).

## Layers

| Layer | Source | Resolution | Cadence |
|---|---|---|---|
| `weather` | NOAA GFS (wind u/v, gust hourly; vis/CAPE/temp/dew-point/precip 3-hourly) | 0.25° | 4×/day |
| `weather-ecmwf` | ECMWF open data (wind u/v, gust) | 0.25° | 4×/day |
| `ensemble` | NOAA GEFS, 31 members (wind + gust, mean + int8 anomalies; 3-hourly to 144 h, 6-hourly to 384 h) | 0.5° | 4×/day |
| `waves` | NOAA GFS-Wave (Hs, period, direction, wind-wave, swell) | 0.25° | 4×/day |
| `currents` | Copernicus Marine GLO12 (surface u/v, 6-hourly to 240 h; NOAA RTOFS fallback) | 1/12° | 1×/day |

Time axes reflect the Phase 0 size measurement — see
[docs/phase0-results.md](docs/phase0-results.md) (verdict: GO at 3.26 GB per
full generation, 6.53 GB at ×2 run retention against the 8 GB storage guard).

The PFT1 format and the manifest/latest JSON schemas are canonically specified
in the passage repo (`docs/forecast-tiles-spec.md`, `contracts/forecast-*.schema.json`);
this repo vendors copies plus a shared golden fixture that both CIs must decode
identically. `contracts/` here is a verbatim copy of the passage repo's
`contracts/forecast-{tile,manifest,latest}.schema.json` — update both together.

## Data licensing

- NOAA data: US Government work, public reuse permitted.
- Copernicus Marine data: free with attribution — this pipeline records product
  ids in run manifests and Passage displays attribution in its UI.
- ECMWF open data: CC BY 4.0.
