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
- measured provider grid: float64 coordinates on a regular `0.02777863°`
  lattice (not the 1/36° lines: the domain's first row is 26.16535726°N and
  first column 18.99950521°W; checked 2026-09-24,
  `tests/fixtures/cmems-coordinates.npz`). Tiles keep these coordinates; the
  step is taken across the whole axis, so manifests from 2026-09-24 carry
  `resolution_deg` 0.02777863 where earlier ones carried the two-point
  difference 0.02777863000000025.

The Copernicus Product User Manual states one daily bulletin with a one-day
hindcast and ten-day forecast, target delivery 14:00 UTC. Measured delivery is
earlier; see [When CMEMS publishes](#when-cmems-publishes).

## When CMEMS publishes

Measured 2026-09-28 from Copernicus's public metadata: the native-file
listing (`mdl-native-10`), the ARCO store's object `Last-Modified` times
(`mdl-arco-time-032/…/timeChunked.zarr`), and the dataset's STAC item. None of
these needs credentials.

- **What a bulletin covers.** The bulletin issued on day D is one native file
  per valid day: `…_R{D}_HC01.nc` for D−1 (a hindcast that replaces the
  previous forecast of that day) and `…_R{D}_FC01.nc` … `FC10.nc` for D to D+9,
  hourly means stamped on the hour. So it carries 240 forecast hours, lead
  0–239 h from D 00:00. The weekly analysis (`AN01`–`AN07`) rewrites the week
  before on another schedule. On 2026-09-28 the dataset ended 2026-10-07
  23:00.
- **Cycle label.** `resolve()` labels the bulletin D 00Z (its last hour minus
  239 h). That is `FC01`'s first hour, so the label and the GRIB reference
  time are correct. The data only exist from about 10:00 on day D.
- **When.** The native files land at 09:40–09:45 UTC (six of six, 24–29 Sep).
  The ARCO store, which the ingest reads through `open_dataset`, is rewritten
  in place after them. Its hindcast-day chunks were written at 09:54, 10:40,
  10:14, 09:57, 11:33 and 11:05 UTC (24–29 Sep). The catalogue's
  `arco_updated_date` says the data finished at **11:36:46** on 28 Sep and
  **11:08:40** on 29 Sep.
- **One update, watched every 2 minutes (2026-09-29).** Native files 09:44:59.
  At 09:47:43 the zarr metadata was rewritten and the time axis reached the
  new last hour (2026-10-08 23:00), so `resolve()` would already have
  returned 29 Sep 00Z. `arco_updating_start_date` first named only that
  appended day (2026-10-08T00:00), widened to 2026-09-28T00:00 by 09:56, and
  cleared when the update finished at 11:08:40 (chunks written at 11:05).
  For 80 minutes the store advertised the new bulletin while its values for
  29 Sep onward were still the previous one's.
- **Mid-update reads.** While the store is rewritten, the catalogue part sets
  `arco_updating_start_date` to the first data instant that "may not be up to
  date". The toolbox only warns about it for `subset`, and not for
  `open_dataset`, and the range it names can be too narrow (above).
  `build_cube` therefore reads the part's update state before and after its
  read and raises `CycleNotAvailableError` when any update is in progress,
  whatever range it names, when the last finished update predates the
  cycle's day, or when the state changed during the read. The CLI then exits
  0 ("not available yet") and a later slot publishes. Each manifest's
  `provenance.provider_updated_at` records when Copernicus finished the
  bulletin.
- **Our lag before 2026-09-29.** The workflow ran daily at 15:00 UTC and GitHub
  started it 0.75–9.3 h late. Over the 31 scheduled runs from 29 Aug to 28 Sep
  it published the same-day bulletin at D+17:16 to D+21:11 (median D+18:29),
  7–10 h after Copernicus had it.

## Horizon: 0–120 h

From 2026-09-29 the layer publishes lead 0–120 h (121 hourly steps). Before
that it published 0–72 h (73 steps), chosen on 2026-08-24 as the longest axis
under a 20 MB budget for the four Channel tiles
([Measured size](#measured-size-and-chosen-axis)).

72 h ended too early. A run labelled D 00Z is served from its publication
until the next bulletin's, so it is up to 24 h plus the publication delay old
when it is replaced. With the 15:00 cron that was D+17 h to D+45 h, so a 72 h
run offered only **27–55 h of forecast ahead of now**. At 20:00 UTC on
2026-09-28 the served run was `currents-ibi-20260927T00Z`, 44 h old, and a
Passage GRIB file for "next 3 days" stopped at 30 Sep 00:00, 28 h ahead.

Always covering the next 72 h needs a horizon of 72 h plus the worst-case run
age:

| Publication (after D 00Z) | Worst-case age | Horizon needed for 3 days | Ahead of now with 72 h | with 120 h |
|---|---:|---:|---:|---:|
| 17–21 h (15:00 cron, measured) | 45 h | 117 h | 27–55 h | 75–103 h |
| ≈ 10–16 h (hourly slots from 07:50) | ≈ 40 h | ≈ 112 h | 32–62 h | 80–110 h |

So 0–120 h keeps the next 3 days in the served run at any moment with the
hourly slots. It still covers about 56 h ahead if one day's ingest is missed.
The four Channel tiles (`N40W010`, `N40E000`, `N50W010`, `N50E000`) measured
21.6 MB at 0–72 h on 2026-09-28 (compressibility varies: 19.8 MB on
2026-08-23). At 0–120 h they are about 36.6 MB: the August measurement below
(33.5 MB) scaled by the same ratio. A full run is about 114 MB, so two
retained runs add about 94 MB to the bucket, which held 6.07 GB on 2026-09-28
against the 8 GB guard.

Who downloads them: Passage's GRIB page (a Channel IBI file reads the whole
tiles, and the browser decodes one about 125 MB tile at a time: 360 × 360 ×
121 × 2 float32) and Tactician's race package. Passage's briefing reads the
GLO12 `currents` layer and never these tiles.

The ingest reads 121 × 1078 × 871 values per component, about 454 MB of
float32 each, within the runner's memory.

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

Named publication constants (from 2026-09-29, see
[Horizon: 0–120 h](#horizon-0120-h)):

- `IBI_PUBLISHED_STEP_HOURS = 1`
- `IBI_PUBLISHED_HORIZON_H = 120`
- `IBI_PUBLISHED_STEP_COUNT = 121`
- Channel payload about 36.6 MB (the 2026-08-24 budget of
  `IBI_CHANNEL_LAYER_BUDGET_BYTES = 20_000_000` is superseded)

Original 2026-08-24 decision, kept for the record: the 0–72 h axis was the
longest measured axis below the 20 MB IBI Channel budget, deliberately
anchored to the existing ~19 MB four-layer load.
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

The first public run succeeded on 2026-08-24:

- workflow: [GitHub Actions run 32710089975](https://github.com/deepregatta/forecast-tiles/actions/runs/32710089975),
  exact source commit `f60ed48`;
- provider cycle: `2026-08-23T00:00Z`;
- bucket `published_at`: `2026-08-24T09:11:51Z`;
- measured cycle-to-bucket latency: 33 h 11 min 51 s;
- served horizon: 72 h / 73 consecutive hourly steps;
- public payload: 65,390,314 bytes, 11 non-empty tiles, 8 validation checks;
- pipeline duration: 87.5 seconds; complete Actions job: 1 min 37 s;
- public Channel tile `N40W010`: 9,951,379 bytes, FNV-1a
  `2cd3db23d91016d1`, contract-valid and decoded successfully.

The latency is the age of the first manually triggered bucket publication, not
a claim that the provider itself took 33 hours. At 09:11 UTC the rolling
catalogue still exposed the 2026-08-23 bulletin; the run occurred before the
documented 14:00 UTC target for the next bulletin. Closed 2026-09-28: each of
the 36 scheduled runs published the bulletin of the day it was scheduled for, and Copernicus's own
update times are measured in [When CMEMS publishes](#when-cmems-publishes).

Outage behaviour is fail-stale, not synthetic fallback: catalogue-resolution
failure raises `IBI catalogue resolution failed` before a cube exists;
authentication receives the shared 5/15-minute coarse retries; missing steps,
validation failure, or the storage guard also abort before `latest.json` is
changed. The previous immutable IBI run therefore stays published. This path
is covered offline; the successful first publication did not manufacture a
production outage. `CMEMS_IBI_DATASET_ID` is an operator override for a
verified catalogue rename, not a silent fallback to an arbitrary dataset.
