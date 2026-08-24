# TLI1 — the conservative routing index

**Status:** versioned contract. Producer: this repo (`ingest land`). Consumer:
tactician's `core/land`, through `routing`'s `SpatialIndex` seam. Schemas:
`contracts/land-index-{tile,manifest,latest}.schema.json`. Golden fixture:
`tests/fixtures/land-index-raz/`, committed in both repos.

---

## 1. What this is, and what it is not

TLI1 answers exactly one question: **may a route segment cross this cell?**

It exists because tactician's router avoided land only against synthetic test
polygons, so its "no land crossing" invariant meant a test rectangle rather
than the Channel. It is a routing-legality artifact and nothing else:

- **It is not a chart.** No soundings, no depths, no aids to navigation, no
  coastline anyone is meant to look at. The display basemap is a separate
  raster layer (OSM/OpenSeaMap) and always was — Tactician is not a
  chartplotter, and this index does not make it one.
- **It resolves shoal areas, not individual rocks.** Its finest source is
  posted at about 115 m. Measured on the fixture region: the EMODnet DTM
  reports −56 m at Ar Men and −29 m at La Vieille, both of which are rocks with
  lighthouses on them. Neither source resolves them, so neither does the index.
  A boat pilots by chart; this index keeps a *route* off Brittany.
- **Both sources carry `DO NOT USE FOR NAVIGATION`.** That sentence is copied
  verbatim into every manifest, and it is the sentence the whole artifact is
  built under.

## 2. The design law: conservative in one direction only

Every step of the compilation **adds** blocked area. No step removes any.

> A route the index passes must be passable in the source data. A route it
> refuses may or may not have been.

That asymmetry is what makes the artifact safe to route against, and it is
checkable rather than asserted — `build_index` fails the build if the buffer
ever reduced a tile's blocked count. Concretely:

| Step | Why it only adds |
|---|---|
| Depth reduced to index cells by the **shallowest** source cell, never the mean | one rock in a 232 m cell makes the cell shallow |
| Coastline rasterised by edge-marking **and** even-odd fill | the edge pass catches an islet smaller than a cell, which a centre-in-polygon test alone would lose |
| Lakes (GSHHG level 2) **not** subtracted from land | a lake is water no sea route can reach; leaving it filled costs nothing and keeps the compilation one-directional |
| Cells with no depth value are blocked | a cell that cannot be shown passable is not treated as passable. The manifest counts them, so "refusing water for want of data" stays visible |
| The buffer is an **outward** dilation | it puts the index's edge outside the source's, never inside |

### The named parameters

They are versioned properties of a published index, recorded in its manifest,
and not tuning knobs anyone may quietly change: a different value is a
different index id.

| Parameter | Value in `nweu` | Why |
|---|---|---|
| `buffer_m` | 200 | GSHHG's full-resolution shoreline and the EMODnet DTM's 115 m posting both carry positional error of this order. The dilation is applied per axis, computed from the *narrowest* cell in the tile — a longitude cell covers fewer metres than a latitude cell, and fewer still further north — so the guarantee holds everywhere in the tile rather than on average. |
| `safety_contour_m` | 0 | The contour that dries at Lowest Astronomical Tide. Blocking it can never make a legal route illegal. Anything deeper is a draft-and-tide judgement about a particular boat on a particular day: that is a race-package constraint, and this pipeline does not decide it. |
| `nodata_is_blocked` | true | See the table above. Measured `nodata_cells` on `nweu` is what says whether this is costing anything. |
| cell size | 1/480° | About 232 m of latitude, 149 m of longitude at 50 N. Finer than the DTM's own posting is coarse, and it leaves the Raz de Sein passage about ten cells wide. |

### There is no polygon simplification

The simplification **is** the raster. A cell is the tolerance, it is always
outward, and integer cell arithmetic gives byte-identical answers on Ubuntu,
Windows and macOS — which floating-point segment/polygon intersection does not,
and tactician's validation plan §4 T1 asks for exactly that. Douglas-Peucker on
a coastline would need a proof that every simplified vertex moved seaward; the
raster gets that for free.

## 3. Layout on the bucket

```
land-index/latest.json                      mutable pointer, keyed by domain
land-index/{index_id}/manifest.json         written LAST; its presence marks the version complete
land-index/{index_id}/{tile_id}.bin.gz      gzipped TLI1 tiles
status/land-index.json                      last publish, for operations
```

`index_id` is `{domain}-{YYYYMMDD}T{HH}Z` — the same shape as a forecast run id
and under the same rule: **published versions are immutable.** A rebuild is a
new id; `publish_index` refuses to overwrite an existing id whose content
differs. Retention keeps the current version and the one before it, per domain.

`land-index/latest.json` is a pointer of its own rather than an entry in the
forecast `latest.json`, because that document is a contract both Passage and
tactician's `shore` read as a list of *forecast* layers to fetch tiles for.

### Coverage is the tile list, not the bounding box

A domain is a set of whole tiles on the same 10° grid the forecast tiles use.
`nweu` is the four covering 10°W–10°E, 40°N–60°N: a whole 2×2 block rather than
the L-shape the races alone would need, because a race area resolves to tiles by
its bounding box and the RORC Channel fleet's own box reaches a degree east of
the course into the fourth tile. A domain cut to the courses would have put a
coverage edge inside a race this index exists to serve.
The manifest's bounding box is that set's envelope and may contain tiles that
were never compiled, so **the tile list is the coverage**. A query outside a
published tile is *unknown*, never *clear*. This is why the domain is
tile-aligned rather than cut to a data provider's bounds: every published tile
is complete over its whole extent, so a tile that is present needs no second
rectangle inside it.

## 4. Tile binary format

Little-endian throughout, deliberately the same shape as PFT1 so the two
decoders read alike.

```
bytes 0-3    magic b"TLI1"
bytes 4-7    u32 header_len (unpadded JSON byte length)
bytes 8-..   UTF-8 JSON header, zero-padded to a 4-byte boundary
then         payload: nlat rows of row_bytes, row 0 southernmost,
             bit 0 of each byte westernmost
```

`row_bytes = ceil(nlon / 8)`, so a row can be indexed without unpacking the
rows before it. A set bit means **blocked**.

Cell (row, col) covers the half-open box

```
lon ∈ [lon0 + col·dlon, lon0 + (col+1)·dlon)
lat ∈ [lat0 + row·dlat, lat0 + (row+1)·dlat)
```

`lon0`/`lat0` are the tile's **south-west corner**, matching the tile id. Note
this is an *area* convention: PFT1's `lat0`/`lon0` name a sample point, TLI1's
name a cell edge. A consumer that confused the two would be half a cell out
everywhere.

The header is documented by `contracts/land-index-tile.schema.json`. The fields
that are not geometry are there so the artifact can state what it is made of
without a second file: `composition` counts land, shoal, no-data and
cross-source disagreement cells; `conservatism` repeats the buffer, the
dilation actually applied to *this* tile, the safety contour and its datum.

### Version checking is by name and fails loud

A consumer reads `spec` and `schema_version` before anything else and refuses a
document it does not know, reporting both the found and the expected value. It
must never half-read a bumped version: the payload's meaning is exactly what
the header says it is.

## 5. Sources

| Source | Role | Licence | Attribution |
|---|---|---|---|
| **GSHHG 2.3.7**, full-resolution shoreline (`gshhs_f.b`) | land polygons, level 1 | LGPL-3.0-or-later, with permission to use, copy, modify and distribute given attribution | Wessel, P., and W. H. F. Smith (1996), *A global, self-consistent, hierarchical, high-resolution shoreline database*, J. Geophys. Res., 101(B4), 8741–8743 |
| **EMODnet Digital Bathymetry (DTM) 2024**, `emodnet:mean` via WCS | depth, for the selected safety contour | CC-BY-4.0 | EMODnet Bathymetry Consortium (2024): EMODnet Digital Bathymetry (DTM 2024) |

Both are public; neither needs credentials, and none appear anywhere in this
pipeline. Every manifest records the product, version, access URL, access date,
licence, attribution and — for the shoreline archive — the SHA-256 of the exact
file the index was compiled from.

**Why EMODnet and not GEBCO.** EMODnet's European DTM is posted at 1/16
arc-minute (~115 m) against GEBCO's 15 arc-seconds (~450 m), which is the
difference between resolving the Chaussée de Sein and averaging it away; and it
is referenced to **LAT**, which is the datum a "does this dry?" question is
actually asked against. GEBCO remains the fallback for a domain outside
EMODnet's coverage, and that would be a different, stated source.

**Why the shoreline as well as the bathymetry.** They are independent surveys
and the index is their union. On the fixture region they agree on 98.1 % of
cells: the DTM contributes drying rock the shoreline has no polygon for, the
shoreline contributes coastal detail the DTM smooths. `source_agreement` is
measured into every manifest, because two sources that suddenly stopped
agreeing would mean a compilation fault long before it meant a survey.

## 6. Rebuilding

This is a **one-shot artifact, not a cron job.** Shorelines and bathymetry
change on the timescale of survey programmes. Rebuild when a source publishes a
new release, when the domain is extended, or when a conservatism parameter is
deliberately changed — and each rebuild ships as a new immutable version.

```sh
uv sync
uv run ingest land --domain nweu --dry-run /tmp/land        # local, no R2 writes
uv run ingest land --domain nweu                            # publish to R2
```

The first run downloads the GSHHG archive (119 MB, checked against its recorded
SHA-256) and one DTM block per degree square of the domain into `--cache-dir`
(default `.land-cache`, git-ignored). The cache is the reason a second build is
minutes rather than an hour; deleting it re-fetches from the sources.

`--buffer-m` and `--safety-contour-m` override the recorded defaults. They are
there for evaluating a change, not for publishing an unrecorded one: whatever
is used lands in the manifest.

The GitHub Actions workflow `land-index.yml` is `workflow_dispatch` only — there
is no schedule, on purpose.

## 7. Golden fixture

`tests/fixtures/land-index-raz/` is a complete one-degree index over the Raz de
Sein — Île de Sein, the Chaussée rocks and the legal passage between them —
built by the same code path as a production domain, at a fixed timestamp so it
is reproducible. It is a *real* index of a small domain rather than a synthetic
one, so the consumer's contract test reads exactly what the producer publishes.

Both repositories commit the same bytes and both CIs decode them: this repo's
`tests/test_land_fixture.py` and tactician's `core/land` contract test. Neither
vendors the other's implementation.

Regenerate with:

```sh
uv run python scripts/make_land_fixture.py
```

A fixture that legitimately changes — a new source release, a changed
conservatism parameter — is a recorded decision in both repos, not a silent
regeneration.
