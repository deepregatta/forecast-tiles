"""The chart builder's rules, per cell (WP10, docs/chart-pack-format.md).

Ported from tactician `tools/charts/test_build.py` (2026-11-07): the shared
land tile, which masked tiles take it, the Latin-name rule, the cover and the
checker's refusal of a tile without one. Added with the cells: a cell is
exactly the descendants of its z6 tile, every one stored and covered, built
without the world, and its input identity changes when an input does.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pmtiles")

from pmtiles.reader import MmapSource, Reader, all_tiles  # noqa: E402
from pmtiles.tile import zxy_to_tileid  # noqa: E402

from ingest.charts import build, check, mvt, products, sources  # noqa: E402
from ingest.charts.common import (  # noqa: E402
    CELL_SETS,
    CHANNEL,
    EXTENT,
    cell_area,
    cells_over,
    parse_cell,
    tiles_in,
)

FIELDS = {"layers": {"earth": ["kind"], "cover": []}}


def earth_tile(rings_per_feature) -> bytes:
    w = mvt.LayerWriter("earth", EXTENT)
    for rings in rings_per_feature:
        w.add(mvt.POLYGON, mvt.lines_geometry(rings, closed=True), {"kind": "earth"})
    return gzip.compress(mvt.encode([w]), 6, mtime=0)


def square(x0, y0, x1, y1):
    # exterior rings run clockwise on screen (y down): positive surveyor's area
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ---- the shared land tile, the mask and names (ported) ----------------------


def test_land_tile_is_one_earth_polygon_past_every_edge():
    tile = mvt.parse(build.land_tile())
    assert [layer.name for layer in tile.layers] == ["earth"]
    (f,) = tile.layers[0].features
    assert f.type == mvt.POLYGON
    (ring,) = mvt.decode_rings(list(f.geometry))
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    assert min(xs + ys) < 0 and max(xs + ys) > EXTENT
    assert mvt.ring_area(ring) > 0, "an exterior ring"
    assert build.land_tile() == build.land_tile()


@pytest.fixture
def bay():
    # z10 tile (0, 0): land everywhere but a bay in its top-left quarter
    hole = square(0, 0, EXTENT // 4, EXTENT // 4)
    land = [square(-64, -64, EXTENT + 64, EXTENT + 64), list(reversed(hole))]
    return build.LandTest({(10, 0, 0): earth_tile([land])})


def test_masked_tiles_ashore_take_the_land_tile_and_bays_keep_content(bay):
    assert bay.wholly_land((12, 3, 3)) and bay.wholly_land((14, 15, 15))
    assert not bay.wholly_land((12, 0, 0)) and not bay.wholly_land((14, 1, 1))
    assert not bay.wholly_land((12, 4, 0)), "no z10 tile is not land"


@pytest.mark.parametrize("name", ["Baie de Seine", "Île Pelée", "Ría de Arousa", "2 Dir Q ≠"])
def test_latin_names_stay(name):
    assert build.latin_names({"name": name}) == {"name": name}


@pytest.mark.parametrize("name", ["Татарский пролив", "北部湾", "Αγκυροβόλιο", "Nism̓aakqin Park"])
def test_other_scripts_and_combining_marks_go(name):
    assert build.latin_names({"name": name, "type": "x"}) == {"type": "x"}


def test_names_are_composed():
    assert build.latin_names({"name": "Île"}) == {"name": "Île"}


def test_cover_is_one_square_past_every_edge():
    layer = mvt.parse(gzip.compress(mvt.encode([build.cover_layer()]))).layers[0]
    assert layer.name == "cover" and list(layer.keys) == []
    assert check.whole_tile(layer)
    assert check.whole_tile(mvt.parse(build.land_tile()).layers[0])
    coast = mvt.parse(earth_tile([[square(0, 0, EXTENT // 2, EXTENT)]])).layers[0]
    assert not check.whole_tile(coast)


def _area_pack(tmp_path: Path, cover: bool, overview: bool = True) -> list[str]:
    bounds = [-1.62, 49.65, -1.61, 49.66]
    half = earth_tile([[square(0, 0, EXTENT // 2, EXTENT)]])
    tiles = {zxy_to_tileid(z, 0, 0): half for z in range(0, 6)} if overview else {}
    for z in range(6, 15):
        for t in tiles_in(bounds, z):
            writers = [mvt.LayerWriter("earth", EXTENT)]
            writers[0].add(
                mvt.POLYGON,
                mvt.lines_geometry([square(0, 0, EXTENT // 2, EXTENT)], closed=True),
                {"kind": "earth"},
            )
            if cover:
                writers.insert(0, build.cover_layer())
            tiles[zxy_to_tileid(*t)] = gzip.compress(mvt.encode(writers), 6, mtime=0)
    meta = {
        "spec": "TCP1",
        "schema_version": 1,
        "kind": "area",
        "build_id": "t",
        "built": "t",
        "sources": [],
        "attribution": "t",
        "not_for_navigation": "t",
        "vector_layers": [],
        "bounds": bounds,
        "area": {"bounds": bounds, "minzoom": 6, "maxzoom": 14},
        "world_overview": {"minzoom": 0, "maxzoom": 5} if overview else None,
    }
    path = tmp_path / "t.pmtiles"
    build.write_pmtiles(path, tiles, meta, bounds, 0 if overview else 6, 14, 9)
    return check.check(path, FIELDS, quiet=True)


def test_check_refuses_an_uncovered_area_tile(tmp_path):
    assert any("without a cover" in e for e in _area_pack(tmp_path, cover=False))
    assert _area_pack(tmp_path, cover=True) == []
    # the golden fixture is built without the overview: nothing draws under it
    assert _area_pack(tmp_path, cover=False, overview=False) == []


# ---- cells -------------------------------------------------------------------


def test_stage1_cells_are_the_z6_tiles_under_the_channel_box():
    assert CELL_SETS["stage1"] == cells_over(CHANNEL.bbox)
    assert CELL_SETS["stage1"] == ["6-30-21", "6-30-22", "6-31-21", "6-31-22", "6-32-21", "6-32-22"]
    w, s, e, n = CHANNEL.bbox
    union = [cell_area(c).bbox for c in CELL_SETS["stage1"]]
    assert min(b[0] for b in union) <= w and max(b[2] for b in union) >= e
    assert min(b[1] for b in union) <= s and max(b[3] for b in union) >= n


def test_a_cell_is_exactly_its_z6_tiles_descendants():
    a, b = cell_area("6-31-21"), cell_area("6-32-21")  # neighbours on 0 degrees
    for z in (6, 10, 14):
        ta, tb = set(a.tiles(z)), set(b.tiles(z))
        assert len(ta) == 4 ** (z - 6) and not ta & tb
        assert all((x >> (z - 6), y >> (z - 6)) == (31, 21) for _, x, y in ta)
    # a box's tiles reach over the edge it shares with its neighbour; a cell's do not
    assert set(tiles_in(a.bbox, 14)) & set(b.tiles(14))


@pytest.mark.parametrize("bad", ["6-64-0", "5-1-1", "6-1", "6-01-2", "world"])
def test_cell_ids_are_z6_tiles(bad):
    with pytest.raises(ValueError):
        parse_cell(bad)


SEA10 = (10, 31 * 16 + 3, 21 * 16 + 5)  # the one z10 tile of the synthetic cell with sea in it


class FakeArchive:
    """A Protomaps or Seamap archive with one tile for every tile asked:
    land everywhere but the sea z10 tile's family, where Seamap has a buoy."""

    tweak = 0

    def __init__(self, name, url):
        self.name, self.url = name, url
        self.seamap = name.startswith("seamap-")

    @staticmethod
    def _in_sea(z, x, y):
        if z < 10:
            return (SEA10[1] >> (10 - z), SEA10[2] >> (10 - z)) == (x, y)
        return (x >> (z - 10), y >> (z - 10)) == SEA10[1:]

    def fetch(self, tiles, log=print):
        out = {}
        for z, x, y in tiles:
            if self.seamap:
                if not self._in_sea(z, x, y):
                    continue
                w = mvt.LayerWriter("seamark", EXTENT)
                w.add(
                    mvt.POINT,
                    mvt.point_geometry([(2048, 2048)]),
                    {
                        "type": "buoy_lateral",
                        "family": "minor_aid",
                        "cell_rank": self.tweak,
                        "name": "Ш1",
                    },
                )
                out[(z, x, y)] = gzip.compress(mvt.encode([w]), 6, mtime=0)
            elif self._in_sea(z, x, y):
                out[(z, x, y)] = earth_tile([[square(0, 0, EXTENT // 4, EXTENT // 4)]])
            else:
                out[(z, x, y)] = earth_tile([[square(-64, -64, EXTENT + 64, EXTENT + 64)]])
        return out

    def describe(self):
        return {"url": self.url, "attribution": "test", "version": "t", "osm_replication_time": "t"}


class FakeGrid:
    blocks, fetched, digest = 1, 0, "grid"
    elev = np.zeros((2, 2))

    def __init__(self, bbox, log=print):
        pass

    def sea_distance(self):
        pass

    def tile_sea_distance(self, z, x, y):
        return 0.0 if FakeArchive._in_sea(z, x, y) else 50.0

    def depth_layer(self, z, x, y, keep=lambda k: True):
        layer = mvt.LayerWriter("depth", keep=keep)
        if FakeArchive._in_sea(z, x, y):
            layer.add(
                mvt.POLYGON,
                mvt.lines_geometry([square(0, 0, EXTENT, EXTENT)], closed=True),
                {"kind": "band", "min": 0},
            )
        return layer


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "RemoteArchive", FakeArchive)
    monkeypatch.setattr(products, "Grid", FakeGrid)
    empty = {"type": "FeatureCollection", "features": []}
    shom = {layer: empty for layer in sources.SHOM_LAYERS}
    shom["_meta"] = {"fetched": "2026-11-07T00:00:00Z", "content_sha256": "0" * 64}
    monkeypatch.setattr(sources, "fetch_shom", lambda bbox, tag, log=print: shom)
    monkeypatch.setenv("DEEPREGATTA_CHARTS_CACHE", str(tmp_path / "cache"))

    def run(out: Path):
        return build.run(
            cell_area("6-31-21"),
            kind="cell",
            protomaps="20261106",
            seamap="2026-11-02",
            out=out / "6-31-21.pmtiles",
            build_id="20261107T1200Z",
            built="2026-11-07T12:00:00Z",
            workers=2,
        )

    return run


def test_a_cell_is_stored_covered_and_independent_of_the_world(synthetic, tmp_path):
    report = synthetic(tmp_path / "a")
    path = tmp_path / "a" / "6-31-21.pmtiles"
    depth = tmp_path / "a" / "6-31-21-depth.pmtiles"
    fields = check.style_fields()
    assert check.check(path, fields, quiet=True) == []
    assert check.check(depth, fields, quiet=True) == []
    with open(path, "rb") as f:
        r = Reader(MmapSource(f))
        md, h = r.metadata(), r.header()
        tiles = dict(all_tiles(r.get_bytes))
    assert (md["kind"], md["cell"], md["world_overview"]) == (
        "cell",
        {"id": "6-31-21", "z": 6, "x": 31, "y": 21},
        None,
    )
    assert (h["min_zoom"], h["max_zoom"]) == (6, 14)
    assert md["depth"]["archive"] == "6-31-21-depth.pmtiles"
    assert md["inputs_sha256"] == report["inputs_sha256"]
    assert not any(s["id"] in ("world", "shom") for s in md["sources"]), "no world, no SHOM marks"
    assert len(tiles) == sum(4**k for k in range(9))
    land = build.land_tile()
    assert sum(1 for d in tiles.values() if d == land) == sum(
        4**k - 4 ** (k - 4) for k in range(5, 9)
    )
    sea = tiles[SEA10]
    names = [layer.name for layer in mvt.parse(sea).layers]
    assert names[0] == "cover" and "seamark" in names and "depth" not in names
    seamark = next(layer for layer in mvt.parse(sea).layers if layer.name == "seamark")
    assert "name" not in [seamark.keys[t] for f in seamark.features for t in f.tags[::2]]
    with open(depth, "rb") as f:
        r = Reader(MmapSource(f))
        dmd = r.metadata()
    assert dmd["kind"] == "depth" and dmd["cell"] == md["cell"]
    assert dmd["build_id"] == md["build_id"] and dmd["inputs_sha256"] == md["inputs_sha256"]


def test_a_cells_input_identity_follows_its_inputs(synthetic, tmp_path, monkeypatch):
    a = synthetic(tmp_path / "a")["inputs_sha256"]
    assert synthetic(tmp_path / "b")["inputs_sha256"] == a, "same inputs, same identity"
    monkeypatch.setattr(FakeArchive, "tweak", 1)
    assert synthetic(tmp_path / "c")["inputs_sha256"] != a, "a Seamap tile changed"


def test_style_fields_is_tacticians_list_as_vendored():
    import hashlib
    import json

    here = Path(build.__file__).parent
    record = json.loads((here / "vendored.json").read_text())
    assert hashlib.sha256((here / "style-fields.json").read_bytes()).hexdigest() == record["sha256"]
    fields = json.loads((here / "style-fields.json").read_text())
    assert set(fields["layers"]) >= {"seamark", "light", "sea_area", "earth", "water", "depth"}


def test_an_inland_cell_has_no_depth_companion(synthetic, tmp_path, monkeypatch):
    # 6-32-22 of stage 1 is wholly inland France: no EMODnet sea under it
    monkeypatch.setattr(FakeGrid, "tile_sea_distance", lambda self, z, x, y: 50.0)
    monkeypatch.setattr(
        FakeGrid, "depth_layer", lambda self, z, x, y, keep=lambda k: True: mvt.LayerWriter("depth")
    )
    synthetic(tmp_path / "a")
    path = tmp_path / "a" / "6-31-21.pmtiles"
    assert not (tmp_path / "a" / "6-31-21-depth.pmtiles").exists()
    with open(path, "rb") as f:
        assert Reader(MmapSource(f)).metadata()["depth"] is None
    assert check.check(path, check.style_fields(), quiet=True) == []


# ---- phase 8: Europe, SHOM past the edge, EMODnet kept, unchanged cells -----


def test_europe_is_the_coasts_cells_and_stage_ones():
    from ingest.land.emodnet import EXTENT as DTM

    europe = set(CELL_SETS["europe"])
    # 71 coastal cells, and stage 1's open Biscay 6-30-22 and inland 6-32-22
    assert len(europe) == 73 and set(CELL_SETS["stage1"]) <= europe
    # the Faroes, Jan Mayen, North Africa, Kola and the White Sea, Anatolia's south
    assert not europe & {"6-30-17", "6-30-13", "6-32-25", "6-33-25", "6-38-16", "6-38-24"}
    # Shetland, Gibraltar, Malta, Rhodes and Cyprus, Sochi, Svalbard's absence
    assert {"6-31-18", "6-31-25", "6-34-25", "6-37-25", "6-38-25", "6-39-23"} <= europe
    for cell in europe:
        w, s, e, n = cell_area(cell).bbox
        assert DTM[0] <= w < DTM[2] and DTM[1] <= s and n <= DTM[3], cell


def test_blocks_past_the_dtms_extent_are_no_data(monkeypatch):
    from ingest.charts import depth
    from ingest.land import emodnet

    asked = []

    class Block:
        values = np.zeros((depth.CELLS, depth.CELLS), dtype=np.float32)

    def fetch(cache, w, s):
        asked.append(w)
        return Block

    monkeypatch.setattr(emodnet, "fetch_block", fetch)
    monkeypatch.setattr(emodnet, "block_path", lambda cache, w, s: Path("/nonexistent"))
    grid = depth.Grid((41.5, 43.2, 44.5, 43.8), margin=0.25, log=lambda *a: None)
    assert max(asked) == 42, "no block east of the DTM's 43E is asked for"
    assert (
        np.isnan(grid.elev[:, -depth.CELLS :]).all()
        and not np.isnan(grid.elev[:, : depth.CELLS]).any()
    )


def test_newest_seamap_tries_every_day_back():
    import datetime as dt

    have = {"2026-10-26", "2026-11-02"}
    found = sources.newest_seamap(
        dt.date(2026, 11, 7), exists=lambda url: url.rsplit("/", 1)[1][:10] in have
    )
    assert found == "2026-11-02"
    with pytest.raises(RuntimeError):
        sources.newest_seamap(dt.date(2026, 11, 7), days=3, exists=lambda url: False)


def _light(lon, lat, s1, s2):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "inspireid": f"L{lon}{lat}",
            "litchr": 2,
            "colour": "1",
            "valnmr": 20,
            "sectr1": s1,
            "sectr2": s2,
        },
    }


def test_a_light_across_a_cell_edge_draws_its_sectors_into_the_cell(
    synthetic, tmp_path, monkeypatch
):
    # a major light 1.2 NM north of 6-31-21's northern edge, its legs (1.6 NM)
    # pointing south into the cell: phase 7 found such sectors missing
    w, s, e, n = cell_area("6-31-21").bbox
    lon, lat = -2.8, n + 1.2 / 60
    light = _light(lon, lat, 330, 30)  # legs at 150 and 210 degrees
    asked = []

    def shom(bbox, tag, log=print):
        asked.append(bbox)
        bw, bs, be, bn = bbox
        out = {
            layer: {"type": "FeatureCollection", "features": []} for layer in sources.SHOM_LAYERS
        }
        if bw <= lon <= be and bs <= lat <= bn:
            out["lights"]["features"] = [light]
        out["_meta"] = {"fetched": "2026-11-07T00:00:00Z", "content_sha256": "1" * 64}
        return out

    monkeypatch.setattr(sources, "fetch_shom", shom)
    # the sea everywhere, so the edge tiles keep their content past the mask
    monkeypatch.setattr(FakeGrid, "tile_sea_distance", lambda self, z, x, y: 0.0)
    synthetic(tmp_path / "a")
    assert asked[0][3] >= n + 1.6 / 60, "SHOM is read past the edge by a light's reach"
    with open(tmp_path / "a" / "6-31-21.pmtiles", "rb") as f:
        tiles = dict(all_tiles(Reader(MmapSource(f)).get_bytes))
    from ingest.charts.common import lon_to_x, lat_to_y

    row = lat_to_y(n - 1e-6, 14)
    x = lon_to_x(lon, 14)
    lit = [
        (14, xx, row)
        for xx in range(x - 3, x + 4)
        if "light" in {layer.name for layer in mvt.parse(tiles[(14, xx, row)]).layers}
    ]
    assert lit, "the light's legs reach the cell's top row of z14 tiles"


class NoGrid:
    def __init__(self, *a, **k):
        raise AssertionError("the DTM is not read when the EMODnet product is kept")


def test_a_kept_emodnet_product_builds_the_same_cell(synthetic, tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    report = build.run(
        cell_area("6-31-21"),
        kind="cell",
        protomaps="20261106",
        seamap="2026-11-02",
        out=a / "6-31-21.pmtiles",
        build_id="20261107T1200Z",
        built="2026-11-07T12:00:00Z",
        workers=2,
        save_product=a / "6-31-21.emodnet.npz",
    )
    assert report["emodnet_product"]["built"] is True
    key = report["emodnet_product"]["key"]
    product = products.Product.load(a / "6-31-21.emodnet.npz", cell_area("6-31-21"), key)
    monkeypatch.setattr(products, "Grid", NoGrid)
    b = tmp_path / "b"
    b.mkdir()
    again = build.run(
        cell_area("6-31-21"),
        kind="cell",
        protomaps="20261106",
        seamap="2026-11-02",
        out=b / "6-31-21.pmtiles",
        build_id="20261107T1200Z",
        built="2026-11-07T12:00:00Z",
        workers=2,
        product=product,
    )
    assert again["inputs_sha256"] == report["inputs_sha256"]
    assert again["emodnet_product"] == {"key": key, "built": False}
    for name in ("6-31-21.pmtiles", "6-31-21-depth.pmtiles"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    with pytest.raises(products.ProductError):
        products.Product.load(a / "6-31-21.emodnet.npz", cell_area("6-31-21"), "0" * 64)


def test_the_monthly_path_skips_unchanged_cells_and_keeps_emodnet(synthetic, tmp_path, monkeypatch):
    """Build and publish a cell and the world to a scratch layout, then build
    them again from the published pointer, as the monthly run does: with the
    same inputs nothing is cut and the DTM is not read; with a Seamap tile
    changed the cell is rebuilt from the kept product and published with it."""
    from ingest.charts import cli
    from ingest.charts import publish as charts
    from ingest.publish import DirStore

    root = tmp_path / "r2"
    args = ["--protomaps", "20261106", "--seamap", "2026-11-02", "--workers", "2"]
    assert (
        cli.main(
            ["build", "world", "--build-id", "20261107T1200Z", "--out", str(tmp_path / "b1"), *args]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "build",
                "6-31-21",
                "--build-id",
                "20261107T1200Z",
                "--out",
                str(tmp_path / "b1"),
                *args,
            ]
        )
        == 0
    )
    assert (tmp_path / "b1" / "6-31-21.emodnet.npz").is_file()
    charts.publish_build(
        DirStore(root),
        tmp_path / "b1",
        "20261107T1200Z",
        max_bucket_bytes=10**12,
        log=lambda *a: None,
    )
    pointer = str(root / "charts" / "latest.json")

    monkeypatch.setattr(products, "Grid", NoGrid)
    b2 = tmp_path / "b2"
    for target in ("world", "6-31-21"):
        assert (
            cli.main(
                [
                    "build",
                    target,
                    "--build-id",
                    "20261207T0720Z",
                    "--out",
                    str(b2),
                    "--previous",
                    pointer,
                    *args,
                ]
            )
            == 0
        )
    assert sorted(p.name for p in b2.iterdir()) == [
        "6-31-21.unchanged.json",
        "world.unchanged.json",
    ]
    result = charts.publish_build(
        DirStore(root), b2, "20261207T0720Z", max_bucket_bytes=10**12, log=lambda *a: None
    )
    assert (
        result.uploaded == []
        and json.loads((root / "charts/latest.json").read_text())["build_id"] == "20261107T1200Z"
    )

    monkeypatch.setattr(FakeArchive, "tweak", 1)
    b3 = tmp_path / "b3"
    for target in ("world", "6-31-21"):
        assert (
            cli.main(
                [
                    "build",
                    target,
                    "--build-id",
                    "20261207T0720Z",
                    "--out",
                    str(b3),
                    "--previous",
                    pointer,
                    *args,
                ]
            )
            == 0
        )
    assert (b3 / "6-31-21.pmtiles").is_file() and not (b3 / "6-31-21.emodnet.npz").exists()
    result = charts.publish_build(
        DirStore(root), b3, "20261207T0720Z", max_bucket_bytes=10**12, log=lambda *a: None
    )
    latest = json.loads((root / "charts/latest.json").read_text())
    first = json.loads((root / "charts/20261107T1200Z/manifest.json").read_text())
    cell = latest["cells"]["6-31-21"]
    assert latest["build_id"] == "20261207T0720Z" and cell["build_id"] == "20261207T0720Z"
    assert cell["emodnet"] == first["cells"]["6-31-21"]["emodnet"], (
        "the product is kept, not copied"
    )
    assert not any(k.endswith(".npz") for k in result.uploaded)
