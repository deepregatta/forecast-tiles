# TCP1 — chart archives, schema 1

**Status:** versioned contract, canonical here since 2026-11-07 (WP10 phase 6).
Producer: this repo (`ingest-charts`, `src/ingest/charts/`). Consumer:
tactician's `core/charts` and both of its shells, which draw the archives with
one MapLibre style. Schemas: `contracts/charts-{latest,manifest}.schema.json`.
Golden fixture: `tests/fixtures/chart-pack-cherbourg/`, committed here and at
`core/charts/tests/fixtures/` in tactician, pinned in both
[shared-contract locks](shared-contracts.md).

The format was drafted in tactician (`docs/chart-pack-format.md`, WP10 stage
1, 2026-11-07) with the builder that wrote the first packs; both moved here,
the builder vendored with its source path and date
([src/ingest/charts/VENDORED.md](../src/ingest/charts/VENDORED.md)).
Tactician's decisions and measurements behind it stay in its offline charts
plan.

---

## 1. What this is, and what it is not

A chart archive is **a picture of the water for the crew**. It is not a
navigation chart, and **routing never reads it**: land avoidance is the
[TLI1 routing index](land-index-format.md), and nothing here changes a routing
input. Every archive says so in its own metadata (`not_for_navigation`), and
EMODnet's "DO NOT USE FOR NAVIGATION" is carried with it.

No polar data, and nothing else private, is ever written under `charts/`.
Every archive is built from open data (§8) and is itself openly available.

## 2. Container

Every archive is one [PMTiles v3](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
file: tile type MVT (Mapbox Vector Tile 2.1), tiles gzip-compressed,
directories clustered, every layer at extent 4096. Vector tiles are drawn past
their last zoom by overzooming; nothing is stored above z14.

| Kind | Zooms | Covers | Where |
|---|---|---|---|
| `world` | z0–5 | The whole world | Published (`world.pmtiles`); ships with the app so the map is never blank |
| `cell` | z6–14 | Exactly one z6 tile: every descendant of it, nothing else | Published (`{cell}.pmtiles`), the unit of building, change detection and download |
| `depth` | z6–12 | The depth layer of one `cell` or `area` | Published beside each cell (`{cell}-depth.pmtiles`) |
| `area` | z0–14 | A race area on a device: the world at z0–5 and the area's tiles at z6–14 (z6–14 alone when built without the world, as the fixture is; `world_overview` is then null) | On disk only. Core folds the cells under a race area and the world into one; stage 1's Channel pack was built whole |

**Cells.** The world is cut into z6 tiles (5.625° of longitude, about
400 × 400 km at 50°N). A cell id is `6-{x}-{y}` (XYZ, y down). Cells never
overlap: a z14 tile belongs to exactly one cell. A cell set names the cells
built; Europe's lists the sea-touching ones. A cell with no EMODnet sea under
it (`6-32-22`, inland France, in `stage1`) has no depth companion: its `depth`
metadata and its manifest entry's `depth` are null. The `stage1` cell set is the six cells under tactician's stage 1 area
(6.5°W–2.5°E, 48.3°N–51.3°N): `6-30-21`, `6-30-22`, `6-31-21`, `6-31-22`,
`6-32-21`, `6-32-22`.

## 3. How a reader opens an archive

1. Read the PMTiles header. Refuse anything that is not tile type MVT with gzip
   tile compression.
2. Read the JSON metadata. Refuse it unless `spec` is `"TCP1"`. If
   `schema_version` is not `1`, refuse it and say the archive needs another
   version of the app; never guess at a newer schema.
3. Read `kind`. A `cell` or `area` names its depth companion in
   `depth.archive`; open that file the same way and require `kind` `"depth"`
   and the same `build_id` (and, for a cell, the same `cell` and
   `inputs_sha256`). A missing or mismatched companion means the chart draws
   without depth, not that it fails. A reader of packs on disk opens `area`
   and `world`; a `cell` is transport, folded into an `area` first.
4. Show `attribution` and `not_for_navigation` wherever the chart is drawn, and
   the source dates (below) wherever the chart's age is stated.

`ingest-charts check` (`src/ingest/charts/check.py`) performs these checks and
also checks the layers, properties and zoom ranges below, and that every tile
of an area's box or a cell is stored and, where required, covered. Tactician's
reader is `core/charts` (`Pack::open`), which refuses the same way.

## 4. Missing tiles, the mask and the cover

A `cell` stores **every descendant of its z6 tile** at z6–14, and an `area`
**every tile of its box** (`area.bounds`); the checker refuses a gap. Two
kinds of tile are deliberately thin:

- **The coastal mask.** Above z10, a tile that lies wholly more than
  `coastal_mask.sea_distance_nm` (2 NM) from the sea is not cut from the
  sources. "Sea" is EMODnet DTM cells below LAT connected to open water. Such a
  tile that is wholly land in the basemap (inside the OSM coastline of its z10
  tile) is stored as **one shared land-only tile**: a single `earth` polygon
  (`kind` `earth`) reaching 64 units past every edge, 68 bytes, which PMTiles
  stores once and run-length codes. Inland, the crew sees land without towns,
  rivers or labels above z10; the z10 tile carries those. A masked tile over
  water the basemap draws (a drying bay such as the Baie du Mont-Saint-Michel,
  which EMODnet puts above LAT) keeps its own content. Both tests read only
  the cell's own tiles and grid, so a cell's mask never depends on another
  build.
- **Beyond the box.** Beyond an `area`'s box, it holds only the world's z0–5
  tiles (or nothing, built without it).

**Land beyond the box.** A renderer draws a tile the archive lacks as an empty
one, the sea, over land too. So the style reads an `area` a second time, as the
source `overview`: its z0–5 only, overzoomed from z6 up, whose layers draw the
world's land, lakes and coast. Inside the box that z5 land is far coarser than
the area's own and would cover sea along every coast, so every z6–14 tile
carries a **`cover`** layer: one square over the whole tile, 64 units past each
edge, which the style paints in the sea colour above the overview's layers and
below everything else. The shared land tile needs none (its `earth` covers the
tile already). Renderers clip a tile's drawing to the tile, so the change from
overview to area falls on a tile edge, with no seam. Identical tiles stay
identical, so the cover costs little (0.82 MB over the stage 1 Channel).

**Every `cell` carries the cover**, in every tile but the land tile, although
it holds no world: it is always folded into an area with the world under it,
and so it never depends on the world build. An `area` carries it when built
with the world, and not without (nothing draws under it then). The checker
refuses a cell, or an area with the world, where a tile is covered by neither.

What each renderer does with a tile the archive lacks (measured 2026-11-07 in
tactician): MapLibre Native (13.7.1) draws it as an empty loaded tile and never
the ancestor; MapLibre GL JS (5.24) does the same on a 404, so tactician's PC
host answers 410 and nudges the source to draw the loaded ancestor.

## 5. Layers

Every chart tile carries all of its layers; a layer is absent from a tile only
when it has no feature there (and `cover` from the land tile and from areas
built without the world). Layer order inside a tile carries no meaning.

| Layer | From | Zooms | Content |
|---|---|---|---|
| `earth` | Protomaps | 0–14 | Land polygons, island label points |
| `water` | Protomaps | 0–14 | Lakes, rivers, streams and water label points. **Ocean polygons are removed**: the sea is the style's background under the depth bands |
| `boundaries` | Protomaps | 0–14 | Administrative boundaries |
| `places` | Protomaps | 0–14 | Place label points |
| `roads` | Protomaps | 12–14 | Only in tiles within `roads.sea_distance_nm` (1 NM) of the sea, and only the `roads.kinds` rule `streets`: no paths, tracks or service roads; piers always kept |
| `sea_area` | Open Waters Seamap | 3–14 | Named sea areas |
| `seamark` | Seamap, with SHOM merged | 2–14 | Aids, hazards, areas, routes and structures, in Seamap's schema |
| `light` | Seamap, with SHOM merged | 8–14 | Light sector arcs and legs |
| `depth` | EMODnet DTM 2024 | 6–12 | Depth bands and contours (the `depth` companion) |
| `cover` | the builder | 6–14 | One square over the whole tile, no properties (§4) |

Base layers are cut from the Protomaps daily build and Seamap layers from Open
Waters' dated weekly archive, both read by HTTP range at the dates a run names.
Their other layers (Protomaps' landuse, buildings, POIs, transit; Seamap's
land, water, waterways, wetland) are not carried.

### SHOM's marks in Seamap's layers

Where SHOM Balisage (WFS) covers a cell, its buoys, beacons, daymarks,
landmarks and lights are written into `seamark` in Seamap's own schema, and
their sectors into `light`, so Seamap's symbols draw them. In each tile:

- an OSM aid (family `major_aid` or `minor_aid`, or type `landmark`) within
  80 m of a SHOM mark is the same mark and is dropped, with its sectors; OSM
  rocks, wrecks, harbours and other features near a SHOM mark stay. A cell
  reads SHOM 0.01° past its box, so a copy just across a cell edge goes too;
- `cell_rank` (a point's place among its family in a 32 px cell, which the
  style thins by) is recomputed in every cell a SHOM mark joins: SHOM first,
  then the cell's own features in their old order. SHOM landmarks, mostly
  churches inland, rank 3 lower. Cells without SHOM keep Seamap's ranks;
- SHOM sector lines appear from z8, where Seamap's own `light` layer starts.

The merged features carry no marker of their origin; the archive's `sources`
records that SHOM contributed. A cell with no SHOM mark names no SHOM source.

### Names

Labels are Latin only: a Seamap `name` with a non-Latin letter or a combining
mark is dropped, its feature kept, since the style ships Latin glyphs only.
Protomaps names carry their own scripts and the style draws only Latin ones.

### Depth

| `kind` | Geometry | Properties |
|---|---|---|
| `band` | Polygon | `min`: the band's shallow edge in metres below LAT. Bands are 0–2, 2–5, 5–10, 10–20, 20–50 and 50 m and deeper |
| `contour` | LineString | `depth`: 0, 2, 5, 10, 20, 30, 50 or 100 m below LAT; 0 is the drying line |

Contoured per tile from EMODnet's 1/16′ grid (about 115 m), averaged to about
one cell per screen pixel below z10, with a 64-unit buffer past each tile edge.
No depth tile is stored above z12; the style overzooms z12. Every cell lies
inside EMODnet's DTM extent (`EXTENT` in `src/ingest/land/emodnet.py`), which
the builder refuses to leave; GEBCO would be another, stated source.

### Properties

A feature keeps only the properties some layer of the style reads, so the
style is the specification. Tactician derives the list from its pinned style
packages (`@openwaters/seamap` 0.3.2, `@protomaps/basemaps` 5.7.2, plus the
depth layers) in `tools/charts/style-fields.json`; this repo vendors that file
as `src/ingest/charts/style-fields.json`, pinned in its shared-contract lock.
A key the style builds at run time is kept by pattern
(`seamark:*:restriction`). Feature ids are dropped.

| Layer | Kept properties (schema 1) |
|---|---|
| `seamark` | access, category, cell_rank, color, color_pattern, depth, family, fuel, function, light, light_color, name, near_shore, radar_reflector, radar_transponder, radar_transponder_group, radio_station, restriction, seabed_depth, `seamark:*:restriction`, seamark:fog_signal:category, seamark:landmark:conspicuity, seamark:light:1:category, seamark:light:1:colour, seamark:light:category, seamark:light:colour, seamark:restriction, seamark:rock:water_level, seamark:seabed_area:surface, seamark:shoreline_construction:water_level, seamark:water_level, shape, surrounding_depth, topmark_color, topmark_color_pattern, topmark_shape, type, water_level |
| `light` | cell_rank, color, family, subtype, visibility |
| `sea_area` | area, name |
| `earth`, `water` | kind, name, name2, name3, name:en, pgf:name, pgf:name2, pgf:name3, script, script2, script3 |
| `places` | capital, kind, min_zoom, name, name2, name3, name:en, pgf:name, pgf:name2, pgf:name3, population_rank, ref, ref:en, script, script2, script3, sort_key |
| `boundaries` | kind_detail |
| `roads` | is_bridge, is_link, is_tunnel, kind, kind_detail, min_zoom, name, name2, name3, name:en, pgf:name, pgf:name2, pgf:name3, script, script2, script3 |
| `depth` | depth, kind, min |

A style that reads a property not listed here needs it added to tactician's
style, the list vendored again, and a rebuild.

## 6. Metadata

| Key | Value |
|---|---|
| `spec`, `schema_version` | `"TCP1"`, `1` |
| `kind` | `world`, `cell`, `depth` or `area` |
| `build_id`, `built` | Build identifier (UTC `YYYYMMDDTHHMMZ`) and ISO time |
| `builder` | What wrote it |
| `name`, `bounds`, `minzoom`, `maxzoom` | As in TileJSON; `bounds` is the cell's z6 tile or the area's box |
| `cell` | `cell` archives and their depth: `id`, `z` 6, `x`, `y` |
| `area` | `cell` and `area`: the `bounds`, `minzoom` 6, `maxzoom` 14 |
| `world_overview` | `area`: the folded-in world's zooms, 0–5, or null; `cell`: always null |
| `coastal_mask` | `above_zoom` 10, `sea_distance_nm`, and the rule for tiles beyond an area's box ("draw the nearest ancestor") |
| `roads` | `from_zoom` 12, `sea_distance_nm`, `kinds` |
| `depth` | `cell` and `area`: `source_layer`, `max_detail_zoom` 12, `packaging` (`"separate archive"`), `archive` (the companion's file name) |
| `inputs_sha256` | `world`, `cell` and a cell's depth: the input identity (§7) |
| `sources` | One entry per input: `protomaps` (daily `build`, `version`, `osm_replication_time`), `seamap` (archive `date`, `osm_replication_time`), `shom` (`fetched`, `content_sha256` of the records, `marks`), `emodnet` (`product`, `vertical_datum` LAT), and in an `area` with the world `world` (its `build_id`); each with its `licence` |
| `attribution` | One line to show on the chart, e.g. "Balisage © Shom 2026 · © OpenStreetMap contributors · © Protomaps · © Open Waters: Seamap · EMODnet Bathymetry Consortium (2024)" |
| `not_for_navigation` | The sentence to show beside it |
| `style_fields` | The style package versions the kept properties were derived from |
| `vector_layers` | TileJSON layer list with each field's value types and zoom range |

The dates a crew would read ("buoys 2 Nov") are `sources[seamap].date` and
`sources[shom].fetched`.

## 7. Publication

```
charts/latest.json                          mutable pointer, compare-and-swap, never backwards
charts/{build_id}/manifest.json             written LAST; its presence marks the build complete
charts/{build_id}/world.pmtiles             world overview, z0-5
charts/{build_id}/{cell}.pmtiles            one z6 cell, z6-14
charts/{build_id}/{cell}-depth.pmtiles      its depth, z6-12
status/charts.json                          last publish, for operations
```

Charts have their own pointer and prefix, so no forecast or land reader ever
sees them. Archives and manifests are immutable, uploaded as conditional
creates with `Cache-Control: public, max-age=31536000, immutable`; the pointer
and status carry `public, max-age=300, must-revalidate`. `publish_build`
refuses to overwrite a build id whose manifest differs.

**Integrity.** Each archive's manifest entry lists its `bytes`, `sha256` and
an **FNV-1a 64 digest per 1 MiB chunk** (`chunks`, `chunk_bytes` 1,048,576),
the digest PFT1 and TLI1 use per object. A client reads whole chunks and
verifies each, so it can range-read part of a cell and still prove every byte
it keeps.

**Input identity and reuse.** `inputs_sha256` is a SHA-256 over the builder's
recipe (its own code files and the style-fields list), the rules (mask, road
distance and kinds), the cell, SHOM's `content_sha256` over the cell and its
margin, the EMODnet grid it read, and every Protomaps and Seamap tile it cut.
When a run's archive has the same identity as the current build's entry for
it, the publisher uploads nothing: the new manifest keeps the entry, whose key
and `build_id` name the build that wrote it. Cells a run did not build are
carried forward the same way. Changing any recipe file therefore rebuilds and
uploads everything.

**The pointer and the manifest** carry, for the world and each cell, its key,
bytes, `sha256`, chunk digests, `build_id`, `inputs_sha256`, bounds, zooms and
source revisions, a cell also its `z`, `x`, `y` and its `depth` object. The
manifest adds totals and the checks passed. `previous_build_id` names the
build this one followed.

**Retention** keeps every object the new and the previous manifests name. In
older builds, everything else is deleted; a build's manifest goes once nothing
kept still points into it. A build without a manifest (an upload in flight or
interrupted) is left alone, as is anything newer than the previous build.

**Guards, before any upload:** forecast runs, land indexes and charts share the
bucket's `MAX_BUCKET_BYTES` guard (retained forecast and land bytes plus the
charts kept after the publish); the chart bytes must also leave the regional
forecasts' reserved peak under the same limit (`check_regional_room`, the
calculation of `ingest.capacity.check_regional_capacity`); and with
`CAPACITY_ENFORCE=1` the whole upload is reserved under the storage-admission
owner `charts` ([storage admission](storage-admission.md)). `MAX_BUCKET_BYTES`
is 20 GB since 2026-11-07 (Davi), leaving about 5.9 GB for charts beside the
forecasts' reservation; the `charts` ceiling is 5.5 GB.

## 8. Sources and licences

| Source | Terms | Shown on the chart |
|---|---|---|
| OpenStreetMap (basemap, seamarks) | ODbL. An archive is a derivative database and stays openly available under ODbL, which the public objects satisfy | © OpenStreetMap contributors |
| Open Waters Seamap | Tiles CC BY 4.0, being corrected to ODbL | © Open Waters: Seamap |
| Protomaps | OSM data, ODbL | © Protomaps, © OpenStreetMap |
| SHOM Balisage | Licence Ouverte 2.0 | Balisage © Shom, with the year fetched |
| EMODnet Bathymetry | CC BY 4.0, "do not use for navigation" | EMODnet Bathymetry Consortium (2024) |

Open Waters publishes its dated weekly archive for offline use. Until the owner
has told Open Waters about the range reads, a run reads one dated archive the
owner approved, and there is no schedule.

## 9. Versioning

`schema_version` increases when a schema 1 reader or style would draw an
archive wrongly: a layer or property renamed, removed or given another
meaning, or a change to the missing-tile rule. Adding a property to a layer's
kept list because the style now reads it does not; nor do new sources, new
metadata keys or measurements. Nor did adding `cover` (2026-11-07), nor the
`cell` kind and `inputs_sha256` (2026-11-07): a schema 1 reader of packs on
disk refuses a `cell` as a kind it does not open, and draws everything else as
before.

## 10. Golden fixture

`tests/fixtures/chart-pack-cherbourg/` holds `cherbourg.pmtiles` (kind
`area`, one tile per zoom z6–14 over Cherbourg harbour, z14 tile 8118/5580,
without the world) and its companion `cherbourg-depth.pmtiles`, built from the
inputs pinned in `cherbourg.json` with build id `fixture-20261107`, and
`cherbourg.expected.json`, the reading a TCP1 reader should produce from them.
`scripts/make_chart_fixture.py` regenerates them; from the same cached inputs
it gives the same bytes (checked 2026-11-07 against the bytes tactician's
builder first wrote). `tests/test_chart_fixture.py` reads them back. The fixture
is an `area` because that is what a device opens; the `cell` kind is covered by
`tests/test_charts_build.py` on a synthetic cell.
