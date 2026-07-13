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

## Status

Phase 0 — size-validation prototype. Run it with:

```sh
uv sync
uv run scripts/size_prototype.py            # all layers
uv run scripts/size_prototype.py --layers weather
```

The prototype downloads sampled forecast steps of one live cycle per layer
(byte-range subsetting against the AWS open-data mirrors), builds real tiles
with `tilekit`, and extrapolates full-run sizes against the 4 GB go/no-go
budget (8 GB R2 storage guard at ×2 run retention).

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
identically.

## Data licensing

- NOAA data: US Government work, public reuse permitted.
- Copernicus Marine data: free with attribution — this pipeline records product
  ids in run manifests and Passage displays attribution in its UI.
- ECMWF open data: CC BY 4.0.
