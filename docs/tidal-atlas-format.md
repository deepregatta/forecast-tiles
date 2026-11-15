# TCA1 — tidal-atlas packs, schema 1

**Status:** versioned contract, canonical here since 2026-11-15 (Tactician
Isomar P13). **Published 2026-11-15** on the owner's approval: build
`20261115T0944Z`, run 43700692470 of the manual `tidal-atlas` workflow,
36,533,243 bytes in seven grids, byte for byte the local dry run of the same
day (§9). Producer: this repo (`ingest-atlas`,
`src/ingest/atlas/`). Consumer: Tactician's `core/atlas` and both of its
shells. Schemas: `contracts/tidal-atlas-{latest,manifest}.schema.json`.
Golden fixture: `tests/fixtures/tidal-atlas-alderney/`, committed here and in
Tactician, pinned in both [shared-contract locks](shared-contracts.md).

## 1. What this is, and what it is not

SHOM's 2D tidal-current atlases, as grids a phone or a PC reads without HDF5.
Each gives the surface current for SHOM's mean neap tide (coefficient 45) and
mean spring tide (coefficient 95), from six hours before to six hours after
high water at the atlas's reference port. It is an **atlas**: the mean tide,
with no weather, no surge and no river flow. It is not a forecast, and
**routing never reads it**. SHOM's condition travels with every grid and
manifest: *"A n'utiliser qu'en complément des cartes et ouvrages nautiques
officiels"* — to be used only in addition to official nautical charts and
publications.

## 2. Layout in the bucket

```
tidal-atlas/latest.json                     the build in force, and its grids
tidal-atlas/{build_id}/manifest.json        written last: the build is complete
tidal-atlas/{build_id}/{grid}.tca1.gz       one per grid
status/tidal-atlas.json
```

`build_id` is UTC `YYYYMMDDTHHMMZ`. Builds are immutable; the pointer moves by
compare-and-swap and never backwards; the new and previous builds are kept.
The pointer carries every grid's entry (id, zone, title, rank, key, bytes,
SHA-256, bounds, resolution, reference port, offsets, source), so a reader
sizes a download from one read.

## 3. A grid file

One gzip stream (mtime 0). Decompressed, little-endian:

| Bytes | Content |
|---|---|
| 4 | `TCA1` |
| 1 | schema version, `1` |
| 4 | `H`, the header's length (≤ 65,536) |
| H | the header, UTF-8 JSON |
| 2 × N | i16 values, N = 2 × steps × rows × columns × 2 |

Values run: coefficient (45, then 95), offset from high water (ascending),
row (south to north), column (west to east), component (east `u`, then north
`v`), in **millimetres per second**. Both components of a node are −32768
together where the atlas has no water: land, or a bank dry at that hour (the
Manche atlas dries and floods with the tide). Nothing follows the values.

Header fields, all required and no others: `spec` (`TCA1`), `schema_version`
(1), `grid`, `zone`, `title`, `resolution_m`, `reference_port`
(`name`, SHOM's four-digit `shom_code`, `event` `high_water`), `lat0`,
`dlat`, `rows`, `lon0`, `dlon`, `columns` (node centres of a regular WGS84
grid; at least 2 × 2), `coefficients` `[45, 95]`, `offsets_min` (strictly
rising from −360 to +360), `scale_m_s` 0.001, `missing` −32768, `source`
(SHOM's file, its SHA-256 and its archive's, release date, model lineage),
`attribution`, `limitation`, `not_for_navigation` `true`.

## 4. How a reader opens a grid

Refuse, whole, a stream that is not gzip; a magic other than `TCA1`; a schema
other than 1 (say the grid needs another version of the app); a header that
is not JSON, has a field missing or unknown, or disagrees with itself (the
checks above); a payload shorter or longer than the header asks; a node with
one component missing. Never return part of a grid. `ingest-atlas check`
performs these checks; Tactician's `core/atlas` refuses the same way, and
also refuses a file whose SHA-256 is not its manifest entry's.

## 5. What the builder does

`ingest-atlas fetch` downloads each zone's archive from SHOM's download
service (`services.data.shom.fr`, the address SHOM's data.gouv.fr listings
give) and refuses one whose SHA-256 is not pinned in
`src/ingest/atlas/sources.py`. `build` reads the NetCDF4 file with h5py and
refuses one that is not timed from high water (`isBM` 0), names another
reference port, has coefficients other than 45 and 95, a time axis other than
0–720 min, irregular axes (stored coordinates may be rounded: Bretagne Nord's
are to 0.001° on a 0.0045° step, so a node may be up to a quarter of a step
off the line) or a speed over 32 m/s. It keeps every 30 minutes where the
file is finer and every step where it is coarser (Golfe normand-breton is
hourly), and rounds to 1 mm/s. SHOM's `_FillValue` −9999 and NaN are missing.

## 6. Precedence

Grids are listed finest first (`rank`). A reader drawing one point takes the
first grid that holds water there. Each grid is timed from its own reference
port. Where two grids meet, the drawn current may step: the atlases are
separate models with separate ports.

## 7. Zones, checked 2026-11-15

From data.gouv.fr's listings by the organisation Shom (one per zone, updated
2026-07-29) and the archives downloaded that day; reference port, step and
grid from each file's own attributes.

| Zone | Archive | Files | Carried grid | Reference port | Step | Grid | Built |
|---|---|---|---|---|---|---|---|
| Golfe normand-breton | 16.1 MB | 500 m | 500 m, 364 × 299 | Saint-Malo (0410) | 1 h | 0.0045° × 0.0069° | 6.28 MB |
| Bretagne Nord | 12.9 MB | 500 m (Abers), 250 m Roscoff | 500 m, 335 × 106 | Roscoff (0054) | 30 min | 0.0045° × 0.0068° | 3.66 MB |
| Finistère | 776.3 MB | 500 m, 250 m Iroise, 100 m Rade de Brest, 30 m port of Brest | 500 m, 281 × 200 | Brest (0003) | 5 min | 0.0045° × 0.0067° | 6.16 MB |
| Baie de Seine | 94.2 MB | 500 m (Cherbourg HW), 250 m Le Havre (**low water**) | 500 m, 184 × 322 | Cherbourg (0013) | 5 min | 0.0045° × 0.0069° | 5.96 MB |
| Pas de Calais | 86.5 MB | 500 m, 250 m Dunkirk, Calais, Boulogne | 500 m, 328 × 262 | Calais (0055) | 5 min | 0.0045° × 0.0071° | 5.38 MB |
| Manche | 43.8 MB | 2 km | 2 km, 154 × 295 | Cherbourg (0013) | 5 min | 0.018° × 0.028° | 3.13 MB |
| Gascogne | 23.0 MB | 2 km | 2 km, 293 × 263 | Concarneau (0160) | 30 min | 0.018° × 0.026° | 5.97 MB |

Seven grids, 36,533,243 bytes (36.5 MB). Left out: the port insets (250 m,
100 m, 30 m) — harbour scale, about 1.2 GB of source, and Le Havre's is timed
from low water, which TCA1 does not carry. A national listing ("Atlas de
courants de marée 2D au format netcdf") bundles the same seven archives. No
"Bretagne Sud" atlas is listed; Gascogne covers that coast at 2 km.

Licence, from each pack's metadata (`*_PACK.xml`, 2026-07-16): *"Utilisation
libre sous réserve de citer la source de la manière suivante : "Shom, 2026.
Atlas de courants de marée 2D au format netcdf,
https://dx.doi.org/10.17183/ATLASCOURANTS2D_NETCDF". Licence Ouverte (version
2.0 d'avril 2017), définie par la mission Etalab."* and *"A n'utiliser qu'en
complément des cartes et ouvrages nautiques officiels."* Register row
`shom-atlas-courants-2d` in `contracts/data-sources.json`.

## 8. Storage

See [storage admission](storage-admission.md#tidal-atlas): 36.5 MB a build,
73.1 MB with the previous build kept, 109.6 MB at the worst moment (two kept
and one uploading).

## 9. Publication

First build `20261115T0944Z`, published 2026-11-15 by the `tidal-atlas`
workflow (manual dispatch, `dry_run` false, run 43700692470): seven grids,
36,533,243 bytes, no previous build. The publisher's regional room check
read 10.89 GB physical in the bucket, 3.12 GB of it outside referenced
forecast runs, and a reserved peak of 16.84 GB with this upload, under the
21 GB limit. Checked from outside the same day: `tidal-atlas/latest.json`
names the build, and every grid served by
`https://forecast.deepregatta.com/` has its pointer's size and SHA-256, the
same bytes as the local dry-run build (the builder is deterministic). A real
dock fetch through Tactician's own path moved exactly what it planned on the
PC (Alderney 15,363,146 B, Raz de Sein 12,128,624 B) and on the Tab S6 Lite.
There is no schedule: a new SHOM edition is a new build, dispatched by hand.
