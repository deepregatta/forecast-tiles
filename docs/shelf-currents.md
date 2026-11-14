# NW Shelf and Mediterranean currents (staged)

Added 2026-11-14 for Tactician's per-point current rule (its Isomar plan, P8):
at each point the finest current model that covers it, the NW Shelf model,
then IBI ([ibi-currents.md](ibi-currents.md)), then the Mediterranean model.
Two layers, built exactly as `currents-ibi` is: the Copernicus Marine Toolbox,
the product's own crop, surface `uo`/`vo` times 1.943844 to knots, 16-bit
quantization at 0.01 kt, PFT1 tiles. Code: `src/ingest/sources/shelf.py`.

**Staged, not published.** `shelf.PUBLICATION_ENABLED` is `False`, so
`ingest currents-nws` or `ingest currents-med` without `--dry-run` exits 1
before reaching a provider or a store. The only workflow,
`.github/workflows/ingest-currents-shelf.yml`, is manual-dispatch only, dry run
by default, has no schedule and carries no bucket credentials. The dispatcher
does not know either layer. Nothing has been written to the bucket.

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
about 281 MB (NW Shelf) and 105 MB (Mediterranean), 386 MB together, beside
IBI's two runs of about 207 MB. Under the 20 GB `MAX_BUCKET_BYTES` the
regional calculation reserves 14.13 GB and charts 5.5 GB
([storage-admission.md](storage-admission.md)), which leaves about 0.37 GB:
the new layers do not fit beside a full chart rebuild without raising the
guard or the charts ceiling. That is part of the owner's decision to publish.

## To publish (the owner's steps, not done)

1. Approve publication of both layers to the public bucket (redistribution:
   the register's rows and DATA-LICENSES.md carry the credit lines).
2. Decide the storage room above (raise `MAX_BUCKET_BYTES` or the charts
   ceiling), and add the two layers to `ROOT_LAYERS` in
   `src/ingest/capacity.py` once they have a run in the bucket (the regional
   peak calculation requires every root layer to be measured).
3. If `PAID_WORK_ENFORCE` is on, give each layer a channel in the paid-work
   ledger.
4. Set `PUBLICATION_ENABLED = True`, add the R2 secrets and a fallback cron to
   the workflow (or a workflow per layer, as IBI's), and the dispatcher
   timetable entries (NW Shelf finished 10:07 UTC, Mediterranean 13:06 UTC on
   13 Nov). Run one dispatch by hand and record it here.
5. Passage reads neither layer; its schemas already accept the names.

Unexercised: publication, the scheduled ingest, the GitHub runner's memory for
the NW Shelf build (3.9 GB here), and any read of these layers from the bucket.
