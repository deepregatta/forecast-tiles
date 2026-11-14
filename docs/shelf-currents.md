# NW Shelf and Mediterranean currents (staged)

Added 2026-11-14 for Tactician's per-point current rule (its Isomar plan, P8):
at each point the finest current model that covers it, the NW Shelf model,
then IBI ([ibi-currents.md](ibi-currents.md)), then the Mediterranean model.
Two layers, built exactly as `currents-ibi` is: the Copernicus Marine Toolbox,
the product's own crop, surface `uo`/`vo` times 1.943844 to knots, 16-bit
quantization at 0.01 kt, PFT1 tiles. Code: `src/ingest/sources/shelf.py`.

**Published from 2026-11-14** on the owner's approval (staged the same morning).
Each layer has its own workflow, as IBI has (`ingest-currents-nws.yml`,
`ingest-currents-med.yml`), with the root layers' fallback cron
`37 2,8,14,20 * * *`, the bucket secrets by their existing names, a `dry_run`
input that builds into the runner's scratch directory, and a dispatcher slot
after each product's measured ready time (10:15 and 13:15 UTC). Setting
`shelf.PUBLICATION_ENABLED = False` stages them again: the CLI then refuses any
run without `--dry-run`. The first runs are recorded under
[First publication](#first-publication-2026-11-14).

## The products (read 2026-11-14)

Read from the live Copernicus catalogue (`copernicusmarine.describe`), each
dataset's public STAC item and each product's page on
`data.marine.copernicus.eu`.

| | `currents-nws` | `currents-med` |
|---|---|---|
| Product | `NWSHELF_ANALYSISFORECAST_PHY_004_013` (Met Office) | `MEDSEA_ANALYSISFORECAST_PHY_006_013` (CMCC) |
| Dataset | `cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i`, version 202511 | `cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m`, version 202511 |
| DOI | 10.48670/moi-00054 | 10.48670/mds-00359 |
| Grid | 0.0135135° × 0.0303030° (the product's "1.5 km"): 1240 rows 46.0–62.7432° N, 958 columns 16.0° W–13.0° E | 1/24° (about 4.2 km): 380 rows 30.1875–45.9792° N, 1287 columns 17.2917° W–36.2917° E |
| Values | **hourly instantaneous**, on the hour | hourly means, stamped on the hour |
| Bulletin | daily, 7 days: the 13 Nov bulletin finished 10:07 UTC and ends 20 Nov 00:00, 168 h after 13 Nov 00Z | daily, 10 days: the 13 Nov bulletin finished 13:06 UTC and ends 22 Nov 23:00, 239 h after 13 Nov 00Z |
| Published horizon | 0–120 h, 121 steps (IBI's) | 0–120 h, 121 steps |
| Tiling | **5°** (a 10° tile would be 740 × 330 cells, twice an IBI tile) | 10° |

Against the Tactician plan's numbers: the NW Shelf domain is 46°–62.74° N as
planned, but its grid is not square (0.0135° by 0.0303°, so 1.5 km north–south
and about 2.2 km east–west at 50° N) and its values are instants, where IBI's
are hourly means: at the seam the two are not the same kind of hourly value.
The Mediterranean model is about 4.2 km (1/24°), not 4.5 km, runs 10 days, and
its domain reaches west past Gibraltar to 17.29° W, so it overlaps IBI over the
western Mediterranean and an Atlantic margin, where IBI, finer, is used.

**The Mediterranean dataset version is retiring.** The catalogue marks version
202511 of `cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m` retired on
**2027-01-12 13:00 UTC** (the toolbox warns on every read). Before then, verify
its successor and set `CMEMS_MED_DATASET_ID` and `CMEMS_MED_DATASET_VERSION`.

## When a bulletin is ready

An explicit `--cycle` waits, as IBI's does, on the dataset's public STAC item:
data reaching the cycle's last native hour, no update running, the last update
finished on or after the cycle. An undated run takes the bulletin the item's
`end_datetime` closes. It does not read the ARCO store's time axis, as IBI's
undated run does: on 2026-11-14 at 08:54 UTC the NW Shelf store's axis already
ran to 21 Nov 00:00 while its finite values stopped at 20 Nov 00:00 (the item's
end), so a cycle read from the axis came out a day ahead and the settled check
refused it. `build_cube` then applies IBI's catalogue checks before and after
its read.

## Measured build (2026-11-13T00Z bulletins, this machine)

`uv run ingest currents-nws --dry-run DIR` and the same for `currents-med`,
with the toolbox's configured credentials, 14 November 2026:

| Layer | Tiles | Run bytes | Largest tile | Missing fraction | Wall time | Peak memory |
|---|---:|---:|---|---:|---:|---:|
| `currents-nws` | 24 (5°) | 140,645,942 | `N55W015` 11.8 MB | 0.311 | 57.5 s | 3.9 GB |
| `currents-med` | 11 (10°) | 52,455,689 | `N30E010` 9.3 MB | 0.518 | 29.0 s | 1.9 GB |
| `currents-ibi` (same day, for comparison) | 11 (10°) | 103,283,777 | `N30W020` 21.2 MB | 0.402 | 64.6 s | 3.3 GB |

The allowances in `MAX_MISSING` are set a few points above: 0.36 and 0.56. A
Channel race (48.9–51.0° N, 4.7° W–1.2° E) reads four NW Shelf tiles, 19.5 MB,
where it read four IBI tiles, 30.7 MB. The largest decoded NW Shelf tile is
59 MB of float32, under IBI's 125 MB.

## Storage

Retention keeps the current and previous run per layer. Two runs of each are
about 281 MB (NW Shelf) and 105 MB (Mediterranean), 386 MB together. The owner
raised `MAX_BUCKET_BYTES` from 20 GB to **21 GB** for them on 2026-11-14; the
recomputed table is in [storage-admission.md](storage-admission.md) (19.82 GB
of reserved peak with a chart rebuild, 1.18 GB to spare). At $0.015 per
GB-month for R2 Standard storage (developers.cloudflare.com/r2/pricing, read
2026-11-14) the extra gigabyte costs 1.5 cents a month.

## Publication steps, and who did them

1. Owner's approval to publish both layers and to raise the limit to 21 GB
   (2026-11-14). Done.
2. The limit: code defaults and every workflow's fallback are 21 GB. The
   repository variable `MAX_BUCKET_BYTES`, which overrides them, still reads
   20 GB, and `REGIONAL_EXISTING_PEAK_BYTES` still 11.015 GB: raising them to
   21,000,000,000 and 11,652,000,000 is a settings change for the owner.
   Publication does not wait on it: the bucket's referenced runs were 7.37 GB.
3. `ROOT_LAYERS` in `src/ingest/capacity.py` gains both layers in the commit
   after their first runs are in the bucket (the capacity profile measures
   every root layer and fails on one with no run). The spending ledger needs
   no channel while `PAID_WORK_ENFORCE` is `0`, as it is; give each layer one
   (the `paid-work-channel` workflow) before turning it on.
4. Workflows, fallback cron and dispatcher timetable: done in code. The
   dispatcher Worker runs only once redeployed (`cd dispatcher && npx wrangler
   deploy`, after `npx wrangler login`): an owner step. Until then the
   fallback cron starts each layer up to about six hours after Copernicus.
5. Passage reads neither layer; its schemas accept the names.

Unexercised: publication, the scheduled ingest, the GitHub runner's memory for
the NW Shelf build (3.9 GB here), and any read of these layers from the bucket.
