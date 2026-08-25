# forecast-tiles

Scheduled ingestion pipeline that turns open NOAA / Copernicus / ECMWF forecast
data into compact, immutable, provider-independent **PFT1** tiles served from
Cloudflare R2 and consumed by [Passage](https://github.com/deepregatta/passage)
entirely in the browser.

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
uv run ingest currents-ibi                   # hourly regional current field
uv run ingest land --domain nweu             # rebuild the routing index (one-shot)
```

Each run: resolve the latest **complete** provider cycle (`.idx` presence,
falling back one cycle rather than publishing a partial run) → download via
byte-range subsetting → decode/orient/quantize into a `ForecastCube` →
validate (step coverage, physical ranges, gust ≥ wind, missing fraction; any
failure aborts before upload) → 10°×10° gzipped PFT1 tiles → atomic publish
(storage guard first, tiles, `manifest.json` last, post-publish re-download
check, `latest.json`, retention delete, `status/{layer}.json`).

Scheduled GitHub Actions run each layer daily. GLO12 currents run after their
provider update; IBI runs at 15:00 UTC after its documented 14:00 UTC target
delivery — see `.github/workflows/ingest-*.yml`. `ingest weather-ecmwf` exits 0 with a log
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
5. For both Copernicus current layers also add
   `COPERNICUSMARINE_SERVICE_USERNAME` and
   `COPERNICUSMARINE_SERVICE_PASSWORD` (free Copernicus Marine account).
   Transient authentication-service connection failures are retried after 5
   and 15 minutes; invalid credentials still fail immediately.

The storage guard refuses to publish when retained runs + the new run would
exceed `MAX_BUCKET_BYTES` (default 8 GB).

## Layers

| Layer | Source | Resolution | Cadence |
|---|---|---|---|
| `weather` | NOAA GFS (wind u/v, gust hourly; vis/CAPE/temp/dew-point/precip 3-hourly) | 0.25° | 1×/day |
| `weather-ecmwf` | ECMWF open data (wind u/v, gust) | 0.25° | 1×/day |
| `ensemble` | NOAA GEFS, 31 members (wind + gust, mean + int8 anomalies; 3-hourly to 144 h, 6-hourly to 384 h) | 0.5° | 1×/day |
| `waves` | NOAA GFS-Wave (Hs, period, direction, wind-wave, swell) | 0.25° | 1×/day |
| `currents` | Copernicus Marine GLO12 (surface u/v, 6-hourly to 240 h; NOAA RTOFS fallback) | 1/12° | 1×/day |
| `currents-ibi` | Copernicus Marine IBI analysis-forecast (surface u/v, hourly through 72 h; IBI domain only) | 1/36° | 1×/day |

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
The IBI axis comes from a separate real-data size gate: 65.39 MB per full
regional run and 19.779 MB for the four Channel tiles. See
[docs/ibi-currents.md](docs/ibi-currents.md).

The PFT1 format and the manifest/latest JSON schemas are canonically specified
in the Passage repo ([`docs/forecast-tile-format.md`](https://github.com/deepregatta/passage/blob/main/docs/forecast-tile-format.md), `contracts/forecast-*.schema.json`);
this repo vendors copies plus a shared golden fixture that both CIs must decode
identically. The routing index's own spec, schemas and golden fixture are
canonical **here** and consumed by tactician's `core/land` (`docs/land-index-format.md`,
`contracts/land-index-*.schema.json`, `tests/fixtures/land-index-raz/`). The IBI shore deliverable extends the local layer-name enums with
`currents-ibi`; mirroring that enum into Passage is explicitly `OPEN:` before
Passage claims schema parity. No per-tile provenance/resolution extension has
been made; the later blended-current contract remains separate.

## Data licensing

- NOAA data: US Government work, public reuse permitted.
- Copernicus Marine data: free with attribution — this pipeline records product
  ids in run manifests and Passage displays attribution in its UI.
- ECMWF open data: CC BY 4.0.
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
