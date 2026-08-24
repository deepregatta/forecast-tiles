# CMEMS IBI hourly analysis-forecast currents

Measured and implemented 2026-08-24 for the first shore deliverable of
Tactician WP2.

## What this layer is

`currents-ibi` publishes the surface `uo` / `vo` fields from Copernicus Marine
product `IBI_ANALYSISFORECAST_PHY_005_001`. The current catalogue selection is
`cmems_mod_ibi_phy_anfc_0.027deg-2D_PT1H-m`; the ingest resolves that identity
from the live catalogue and accepts `CMEMS_IBI_DATASET_ID` as an explicit
operational override because Copernicus dataset ids can be renamed.

These are **hourly-mean ocean-model currents including tide**. Hourly sampling
resolves the tidal cycle over the IBI grid. They are **not tidal-stream
predictions**: there is no SHOM atlas, tidal-coefficient scaling, or coastal
blend in this layer.

The domain and resolution were vendored from
`oscar/analysis/src/coachregatta_analysis/environment_fetcher.py`, RegionalModel
`IBI`, on 2026-08-24:

- `IBI_MIN_LAT = 26.0`
- `IBI_MAX_LAT = 56.0`
- `IBI_MIN_LON = -19.0`
- `IBI_MAX_LON = 5.0`
- `IBI_NOMINAL_RESOLUTION_DEG = 1 / 36`
- measured provider grid step: `0.02777863000000025°`

The Copernicus Product User Manual states one daily bulletin with a one-day
hindcast and ten-day forecast, target delivery 14:00 UTC. The workflow runs at
15:00 UTC to leave one hour for catalogue/store propagation.

## Measured size and chosen axis

Resolution is fixed; horizon is the size lever. The four Channel tiles are
`N40W010`, `N40E000`, `N50W010`, and `N50E000`. On 2026-08-24 the existing
weather + ECMWF + ensemble + waves payload for those tiles measured about
18.7 MB gzip in the live bucket.

Candidate IBI axes were measured against the real 2026-08-23T00Z bulletin with
the production PFT1 codec and gzip level 9:

| Last forecast hour | Steps | Four Channel tiles |
|---:|---:|---:|
| 24 h | 25 | 6.563 MB |
| 36 h | 37 | 9.784 MB |
| 48 h | 49 | 13.105 MB |
| 60 h | 61 | 16.423 MB |
| **72 h** | **73** | **19.779 MB** |
| 96 h | 97 | 26.568 MB |
| 120 h | 121 | 33.479 MB |
| 144 h | 145 | 40.356 MB |
| 168 h | 169 | 47.249 MB |
| 192 h | 193 | 54.273 MB |
| 216 h | 217 | 61.357 MB |
| 239 h | 240 | 68.076 MB |

Named publication constants:

- `IBI_PUBLISHED_STEP_HOURS = 1`
- `IBI_PUBLISHED_HORIZON_H = 72`
- `IBI_PUBLISHED_STEP_COUNT = 73`
- `IBI_CHANNEL_LAYER_BUDGET_BYTES = 20_000_000`

The selected 0–72 h axis is the longest measured axis below the 20 MB IBI
Channel budget, deliberately anchored to the existing ~19 MB four-layer load.
The resulting combined first download is about 38.5 MB: roughly 31 seconds at
10 Mbit/s or 62 seconds at 5 Mbit/s before protocol overhead. That remains a
dock-WiFi-sized load while preserving three full tidal days. `OPEN:` measure
real dock throughput before extending the horizon; resolution is not a
fallback lever.

The complete IBI-domain dry run measured:

- `IBI_RUN_BYTES = 65_390_442` across 11 non-empty tiles;
- `IBI_MISSING_FRACTION = 0.4182418174` for both current components;
- `IBI_MAX_MISSING_FRACTION = 0.45`, deliberately coastal rather than the
  global-ocean layer's 0.80 threshold;
- 101.5 seconds for source → PFT1 size measurement, and 115.0 seconds for the
  full validate → tiles → local atomic publish path.

At measurement time the live bucket retained 6.005 GB. Two IBI runs add
130.781 MB, projecting 6.136 GB retained and about 1.864 GB headroom under
`MAX_BUCKET_BYTES = 8_000_000_000`.

Local command:

```sh
uv run --extra currents scripts/size_prototype.py --layers currents-ibi
uv run ingest currents-ibi --dry-run /tmp/forecast-tiles-ibi
```

The dry-run manifest, `latest.json`, and a Channel tile validated against the
vendored contracts. A sampled point near 49.72°N, 1.97°W varied from 0.071 to
6.610 kt over the axis; its first twelve hourly magnitudes were 0.42, 1.79,
2.72, 3.06, 2.86, 2.13, 0.97, 0.50, 1.95, 2.68, 2.63, and 1.95 kt. This is a
clear consecutive-hour tidal-cycle signal, not a ground-truth validation
against an atlas.

## Contract boundary

PFT1 schema version 1 already carries the uniform run resolution in the
manifest, exact `dlat` / `dlon` in every tile, and source provenance in both.
No per-tile metadata extension is needed for this single-source uniform grid.
The layer-name enums are extended with `currents-ibi` so its manifest, latest
pointer, and tile headers validate without overwriting GLO12's `currents`
identity.

`OPEN:` Passage's canonical copies still need the same layer-name enum before
that consumer can claim schema parity. No Passage consumer change is part of
this shore deliverable. The future SHOM blend will require per-tile source and
resolution metadata that schema version 1 cannot express; do not add that
metadata until the blending contract is designed and versioned.

## First-publication characterisation

`OPEN:` complete this section from the first successful GitHub Actions run:

- provider cycle and bucket `published_at`, with measured latency;
- served horizon (must remain 72 h / 73 hourly steps);
- cadence observed independently of the documented daily 14:00 UTC target;
- outage behaviour confirmed from the resolver/publish logs.

The designed outage state is already deterministic: catalogue resolution,
authentication, missing-step, validation, or storage-guard failure occurs
before `latest.json` is changed. The previous immutable run stays published.
`CMEMS_IBI_DATASET_ID` is an operator override for a verified catalogue rename,
not a silent fallback to an arbitrary dataset.
