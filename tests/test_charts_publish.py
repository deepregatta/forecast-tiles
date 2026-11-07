"""The chart publish protocol: guards first, archives under an immutable build
id, manifest last, a pointer moved by compare-and-swap and never backwards,
unchanged archives reused rather than copied, and retention of the current and
previous builds' objects (docs/chart-pack-format.md, "Publication")."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("pmtiles")

from jsonschema import Draft202012Validator  # noqa: E402
from pmtiles.tile import zxy_to_tileid  # noqa: E402
from test_publish import FakeStore  # noqa: E402

from ingest.charts import build, check, mvt  # noqa: E402
from ingest.charts import publish as charts  # noqa: E402
from ingest.charts.common import cell_area  # noqa: E402
from ingest.publish import (  # noqa: E402
    CACHE_IMMUTABLE,
    CACHE_MUTABLE,
    PublishError,
    StalePublishError,
    StorageGuardError,
    fnv64,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts"
BIG = 10**12


def validator(name):
    return Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


def _tile(payload: bytes) -> bytes:
    w = mvt.LayerWriter("earth", 4096)
    w.add(mvt.POINT, mvt.point_geometry([(1, 1)]), {"kind": payload.hex()})
    return gzip.compress(mvt.encode([w]), 6, mtime=0)


def _meta(kind, build_id, digest, bounds, minzoom, maxzoom, **extra):
    return {
        "spec": "TCP1",
        "schema_version": 1,
        "kind": kind,
        "build_id": build_id,
        "built": "2026-11-07T12:00:00Z",
        "sources": [
            {"id": "protomaps", "build": "20261106"},
            {"id": "seamap", "date": "2026-11-02"},
            {"id": "emodnet", "product": "EMODnet Digital Bathymetry (DTM) 2024"},
        ],
        "attribution": "t",
        "not_for_navigation": "t",
        "vector_layers": [],
        "bounds": list(bounds),
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "inputs_sha256": digest,
        **extra,
    }


def make_build(
    folder: Path, build_id: str, cells: dict[str, str], world: str | None, big=None, inland=()
):
    """Small archives shaped as a build writes them: {cell: inputs digest}.
    `big` names a cell whose archive spans more than one 1 MiB chunk."""
    folder.mkdir(parents=True, exist_ok=True)
    if world is not None:
        meta = _meta("world", build_id, world, (-180, -85, 180, 85), 0, 5)
        build.write_pmtiles(
            folder / "world.pmtiles", {0: _tile(world.encode())}, meta, meta["bounds"], 0, 5, 2
        )
    for cell, digest in cells.items():
        a = cell_area(cell)
        x, y = a.cell
        c = {"id": cell, "z": 6, "x": x, "y": y}
        n = 300 if cell == big else 1
        tiles = {
            zxy_to_tileid(6, x, y) + i: _tile(
                (digest + str(i)).encode() + os.urandom(4096 if cell == big else 0)
            )
            for i in range(n)
        }
        meta = _meta(
            "cell",
            build_id,
            digest,
            a.bbox,
            6,
            14,
            cell=c,
            depth=None
            if cell in inland
            else {"archive": f"{cell}-depth.pmtiles", "packaging": "separate archive"},
        )
        build.write_pmtiles(folder / f"{cell}.pmtiles", tiles, meta, a.bbox, 6, 14, 9)
        if cell in inland:
            continue
        dmeta = _meta("depth", build_id, digest, a.bbox, 6, 12, cell=c)
        build.write_pmtiles(
            folder / f"{cell}-depth.pmtiles",
            {zxy_to_tileid(6, x, y): _tile(b"d" + digest.encode())},
            dmeta,
            a.bbox,
            6,
            12,
            9,
        )
    return folder


@pytest.fixture(autouse=True)
def no_reader_checks(monkeypatch):
    # the reader checks have their own tests; these archives are deliberately tiny
    monkeypatch.setattr(check, "check", lambda path, fields, quiet=False: [])


def publish(store, folder, build_id, **kw):
    kw.setdefault("max_bucket_bytes", BIG)
    kw.setdefault("regional_check", False)
    kw.setdefault("now", f"{build_id[:4]}-{build_id[4:6]}-{build_id[6:8]}T12:00:00Z")
    return charts.publish_build(store, folder, build_id, log=lambda *a: None, **kw)


def test_first_build_uploads_everything_manifest_last(tmp_path):
    store = FakeStore()
    folder = make_build(
        tmp_path / "b1",
        "20261107T1200Z",
        {"6-31-21": "a" * 64, "6-31-22": "b" * 64},
        "e" * 64,
        big="6-31-21",
    )
    result = publish(store, folder, "20261107T1200Z")
    puts = [k for op, k in store.ops if op == "put"]
    data_keys = [k for k in puts if k.endswith(".pmtiles")]
    assert len(data_keys) == 5 and result.reused == []
    manifest_key = "charts/20261107T1200Z/manifest.json"
    assert puts.index(manifest_key) > max(puts.index(k) for k in data_keys)
    assert puts.index(charts.LATEST_KEY) > puts.index(manifest_key)
    for k in data_keys + [manifest_key]:
        assert store.meta[k][1] == CACHE_IMMUTABLE
    assert store.meta[charts.LATEST_KEY][1] == CACHE_MUTABLE
    manifest = json.loads(store.objects[manifest_key])
    latest = json.loads(store.objects[charts.LATEST_KEY])
    validator("charts-manifest.schema.json").validate(manifest)
    validator("charts-latest.schema.json").validate(latest)
    assert latest["cells"] == manifest["cells"] and latest["world"] == manifest["world"]
    assert latest["previous_build_id"] is None
    entry = manifest["cells"]["6-31-21"]
    data = store.objects[entry["key"]]
    assert len(entry["chunks"]) == (len(data) + (1 << 20) - 1) >> 20 >= 2
    assert entry["chunks"] == [fnv64(data[i : i + (1 << 20)]) for i in range(0, len(data), 1 << 20)]
    assert entry["depth"]["key"] == "charts/20261107T1200Z/6-31-21-depth.pmtiles"
    assert (entry["x"], entry["y"], entry["sources"]["seamap"]["date"]) == (31, 21, "2026-11-02")


def test_unchanged_archives_are_reused_and_unbuilt_cells_carried(tmp_path):
    store = FakeStore()
    publish(
        store,
        make_build(
            tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64, "6-31-22": "b" * 64}, "e" * 64
        ),
        "20261107T1200Z",
    )
    store.ops.clear()
    # the next run rebuilds 6-31-21 unchanged and the world changed; 6-31-22 is not built
    result = publish(
        store,
        make_build(tmp_path / "b2", "20261114T1200Z", {"6-31-21": "a" * 64}, "d" * 64),
        "20261114T1200Z",
    )
    uploaded = [k for op, k in store.ops if op == "put" and k.endswith(".pmtiles")]
    assert uploaded == ["charts/20261114T1200Z/world.pmtiles"]
    assert sorted(result.reused) == ["6-31-21", "6-31-22"]
    m = json.loads(store.objects["charts/20261114T1200Z/manifest.json"])
    assert m["cells"]["6-31-21"]["key"] == "charts/20261107T1200Z/6-31-21.pmtiles"
    assert m["cells"]["6-31-21"]["build_id"] == "20261107T1200Z"
    assert m["cells"]["6-31-22"]["key"].startswith("charts/20261107T1200Z/")
    assert m["previous_build_id"] == "20261107T1200Z"
    assert m["totals"]["bytes_this_build"] == m["world"]["bytes"]
    validator("charts-manifest.schema.json").validate(m)


def test_the_pointer_never_moves_backwards(tmp_path):
    store = FakeStore()
    publish(
        store,
        make_build(tmp_path / "b2", "20261114T1200Z", {"6-31-21": "a" * 64}, "e" * 64),
        "20261114T1200Z",
    )
    store.ops.clear()
    with pytest.raises(StalePublishError):
        publish(
            store,
            make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "c" * 64}, "e" * 64),
            "20261107T1200Z",
        )
    assert not [k for op, k in store.ops if op == "put"]
    assert json.loads(store.objects[charts.LATEST_KEY])["build_id"] == "20261114T1200Z"


def test_a_published_build_is_immutable_and_a_retry_is_idempotent(tmp_path):
    store = FakeStore()
    folder = make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64}, "e" * 64)
    publish(store, folder, "20261107T1200Z")
    before = dict(store.objects)
    publish(store, folder, "20261107T1200Z")  # the same bytes: nothing changes
    assert {k: v for k, v in store.objects.items() if k != charts.STATUS_KEY} == {
        k: v for k, v in before.items() if k != charts.STATUS_KEY
    }
    other = make_build(tmp_path / "b1x", "20261107T1200Z", {"6-31-21": "f" * 64}, "e" * 64)
    with pytest.raises(PublishError):
        publish(store, other, "20261107T1200Z")


def test_retention_keeps_the_current_and_previous_builds_objects(tmp_path):
    store = FakeStore()
    ids = ["20261101T1200Z", "20261108T1200Z", "20261115T1200Z", "20261122T1200Z"]
    # 6-30-21 never changes: every build reuses build 1's object
    publish(
        store,
        make_build(tmp_path / "1", ids[0], {"6-30-21": "9" * 64, "6-31-21": "1" * 64}, "1" * 64),
        ids[0],
    )
    publish(
        store,
        make_build(tmp_path / "2", ids[1], {"6-30-21": "9" * 64, "6-31-21": "2" * 64}, "2" * 64),
        ids[1],
    )
    r3 = publish(
        store,
        make_build(tmp_path / "3", ids[2], {"6-30-21": "9" * 64, "6-31-21": "3" * 64}, "3" * 64),
        ids[2],
    )
    keys = set(store.objects)
    # build 1's changed objects went; its reused cell and manifest stay
    assert (
        f"charts/{ids[0]}/6-31-21.pmtiles" not in keys
        and f"charts/{ids[0]}/world.pmtiles" not in keys
    )
    assert f"charts/{ids[0]}/6-30-21.pmtiles" in keys and f"charts/{ids[0]}/manifest.json" in keys
    assert f"charts/{ids[1]}/6-31-21.pmtiles" in keys, "the previous build is kept"
    assert f"charts/{ids[0]}/6-31-21-depth.pmtiles" in r3.deleted_keys
    # once nothing kept names build 1, it goes whole
    publish(
        store,
        make_build(tmp_path / "4", ids[3], {"6-30-21": "6" * 64, "6-31-21": "4" * 64}, "4" * 64),
        ids[3],
    )
    publish(
        store,
        make_build(tmp_path / "5", "20261129T1200Z", {"6-30-21": "7" * 64}, "5" * 64),
        "20261129T1200Z",
    )
    assert not [k for k in store.objects if k.startswith(f"charts/{ids[0]}/")]
    assert not [k for k in store.objects if k.startswith(f"charts/{ids[1]}/")]
    latest = json.loads(store.objects[charts.LATEST_KEY])
    for e in [latest["world"], *latest["cells"].values()]:
        assert e["key"] in store.objects
        assert "depth" not in e or e["depth"]["key"] in store.objects


def test_an_incomplete_older_build_is_left_alone(tmp_path):
    store = FakeStore()
    store.objects["charts/20261001T0000Z/6-31-21.pmtiles"] = b"partial"
    publish(
        store,
        make_build(tmp_path / "1", "20261101T1200Z", {"6-31-21": "a" * 64}, "e" * 64),
        "20261101T1200Z",
    )
    publish(
        store,
        make_build(tmp_path / "2", "20261108T1200Z", {"6-31-21": "b" * 64}, "e" * 64),
        "20261108T1200Z",
    )
    publish(
        store,
        make_build(tmp_path / "3", "20261115T1200Z", {"6-31-21": "c" * 64}, "e" * 64),
        "20261115T1200Z",
    )
    assert store.objects["charts/20261001T0000Z/6-31-21.pmtiles"] == b"partial"


def test_the_storage_guard_refuses_before_any_upload(tmp_path):
    store = FakeStore()
    folder = make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64}, "e" * 64)
    with pytest.raises(StorageGuardError):
        publish(store, folder, "20261107T1200Z", max_bucket_bytes=1000)
    assert not [k for op, k in store.ops if op == "put"]


def test_charts_never_starve_the_regional_forecasts(tmp_path, monkeypatch):
    store = FakeStore()
    folder = make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64}, "e" * 64)
    monkeypatch.delenv("REGIONAL_EXISTING_PEAK_BYTES", raising=False)
    with pytest.raises(StorageGuardError, match="regional capacity unknown"):
        publish(store, folder, "20261107T1200Z", regional_check=True)
    caps = 110_000_000 + 2 * 200_000_000
    monkeypatch.setenv("REGIONAL_ENABLED_LAYERS", "weather-arome,weather-icon-eu,weather-ukv")
    monkeypatch.setenv("REGIONAL_HEADROOM_BYTES", "500")
    monkeypatch.setenv("REGIONAL_EXISTING_PEAK_BYTES", "1000")
    size = sum(p.stat().st_size for p in folder.glob("*.pmtiles"))
    limit = 1000 + 3 * caps + 500 + size
    with pytest.raises(StorageGuardError, match="reserved peak"):
        publish(store, folder, "20261107T1200Z", regional_check=True, max_bucket_bytes=limit - 1)
    assert not [k for op, k in store.ops if op == "put"]
    publish(store, folder, "20261107T1200Z", regional_check=True, max_bucket_bytes=limit)


def test_a_build_is_refused_whole(tmp_path):
    store = FakeStore()
    folder = make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64}, "e" * 64)
    with pytest.raises(PublishError, match="build 20261107T1200Z, publishing"):
        publish(store, folder, "20261108T1200Z")
    (folder / "6-31-21-depth.pmtiles").unlink()
    with pytest.raises(PublishError, match="depth companion"):
        publish(store, folder, "20261107T1200Z")
    with pytest.raises(PublishError, match="not UTC"):
        publish(store, folder, "latest")
    with pytest.raises(PublishError, match="needs the world"):
        publish(
            store,
            make_build(tmp_path / "nw", "20261107T1200Z", {"6-31-22": "a" * 64}, None),
            "20261107T1200Z",
        )
    assert not [k for op, k in store.ops if op == "put"]


def test_the_chunk_digest_is_fnv1a_per_mebibyte():
    data = bytes(range(256)) * 8192 + b"tail"  # 2 MiB and 4 bytes
    assert charts.chunk_digests(data) == [
        fnv64(data[: 1 << 20]),
        fnv64(data[1 << 20 : 2 << 20]),
        fnv64(b"tail"),
    ]


def test_a_chart_build_reserves_under_the_charts_owner_before_uploading(tmp_path):
    from test_storage_admission import policy

    from ingest.publish import PreconditionFailed
    from ingest.storage_admission import KEY, Admission, encode

    store = FakeStore()
    doc = policy()
    doc["owners"]["charts"] = {
        "mode": "managed",
        "prefixes": ["charts/"],
        "ceiling_bytes": 30_000_000,
    }
    doc["baseline_bytes"]["charts"] = 0
    store.objects[KEY] = encode(doc)
    admission = Admission(store, conflicts=(PreconditionFailed,))
    folder = make_build(tmp_path / "b1", "20261107T1200Z", {"6-31-21": "a" * 64}, "e" * 64)
    publish(store, folder, "20261107T1200Z", capacity_admission=admission)
    puts = [k for op, k in store.ops if op == "put"]
    assert puts.index(KEY) < min(i for i, k in enumerate(puts) if k.endswith(".pmtiles"))
    (r,) = admission.read()[0]["reservations"].values()
    assert r["writer"] == "charts" and r["state"] == "finished"
    # without a charts owner, the ledger refuses the upload and nothing is written
    store = FakeStore()
    del doc["owners"]["charts"], doc["baseline_bytes"]["charts"]
    store.objects[KEY] = encode(doc)
    with pytest.raises(StorageGuardError, match="shared capacity"):
        publish(
            store,
            folder,
            "20261107T1200Z",
            capacity_admission=Admission(store, conflicts=(PreconditionFailed,)),
        )
    assert not [k for k in store.objects if k.startswith("charts/")]


def test_an_inland_cell_publishes_without_depth(tmp_path):
    store = FakeStore()
    folder = make_build(
        tmp_path / "b1",
        "20261107T1200Z",
        {"6-31-21": "a" * 64, "6-32-22": "b" * 64},
        "e" * 64,
        inland=("6-32-22",),
    )
    publish(store, folder, "20261107T1200Z")
    m = json.loads(store.objects["charts/20261107T1200Z/manifest.json"])
    validator("charts-manifest.schema.json").validate(m)
    validator("charts-latest.schema.json").validate(json.loads(store.objects[charts.LATEST_KEY]))
    assert m["cells"]["6-32-22"]["depth"] is None and (
        m["cells"]["6-32-22"]["x"],
        m["cells"]["6-32-22"]["y"],
    ) == (32, 22)
    assert m["cells"]["6-31-21"]["depth"]["key"].endswith("6-31-21-depth.pmtiles")
