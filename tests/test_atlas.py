"""TCA1 tidal-atlas packs (docs/tidal-atlas-format.md): the codec and its
golden fixture, the NetCDF reader on synthetic SHOM-shaped files, the staged
publisher, the manual-only workflow and the licence register row."""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from test_publish import FakeStore

from ingest.atlas import build, codec, sources
from ingest.atlas import publish as atlas
from ingest.publish import DirStore, PublishError, StalePublishError, StorageGuardError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/tidal-atlas-alderney"
BIG = 10**12


def schema(name):
    return Draft202012Validator(
        json.loads((ROOT / "contracts" / name).read_text()), format_checker=FormatChecker()
    )


def header(**changes):
    h = {
        "spec": "TCA1",
        "schema_version": 1,
        "grid": "manche",
        "zone": "Manche",
        "title": "Manche, Dunkirk to Brest",
        "resolution_m": 2000,
        "reference_port": {"name": "CHERBOURG", "shom_code": "0013", "event": "high_water"},
        "lat0": 49.0,
        "dlat": 0.5,
        "rows": 3,
        "lon0": -2.0,
        "dlon": 0.5,
        "columns": 4,
        "coefficients": [45, 95],
        "offsets_min": [-360, 0, 360],
        "scale_m_s": 0.001,
        "missing": -32768,
        "source": {
            "file": "x.nc",
            "sha256": "0" * 64,
            "archive_sha256": "1" * 64,
            "release_date": "2025/02/25",
            "lineage": "Manche_3D-2010",
        },
        "attribution": sources.ATTRIBUTION,
        "limitation": sources.LIMITATION,
        "not_for_navigation": True,
    }
    h.update(changes)
    return h


def values(h=None):
    h = h or header()
    shape = (2, len(h["offsets_min"]), h["rows"], h["columns"], 2)
    return np.arange(np.prod(shape), dtype=np.int16).reshape(shape)


def body_of(data: bytes) -> bytes:
    return gzip.decompress(data)


def regzip(body: bytes) -> bytes:
    return gzip.compress(body, mtime=0)


# ---- codec -----------------------------------------------------------------


def test_a_grid_round_trips_and_its_bytes_are_deterministic():
    h, v = header(), values()
    data = codec.encode(h, v)
    assert data == codec.encode(h, v)
    again, decoded = codec.decode(data)
    assert again == h and np.array_equal(decoded, v)


def test_the_payload_order_is_coefficient_step_row_column_component():
    h, v = header(), values()
    body = body_of(codec.encode(h, v))
    length = struct.unpack_from("<I", body, 5)[0]
    payload = np.frombuffer(body[9 + length :], dtype="<i2")
    # neap, offset 0, row 2 (north), column 1, v
    index = ((((0 * 3 + 1) * 3 + 2) * 4 + 1) * 2) + 1
    assert payload[index] == v[0, 1, 2, 1, 1]


@pytest.mark.parametrize(
    "damage",
    [
        lambda b: b"TCA2" + b[4:],
        lambda b: b[:4] + bytes([2]) + b[5:],
        lambda b: b[:-2],
        lambda b: b + b"\0\0",
        lambda b: b[:5] + struct.pack("<I", 10**6) + b[9:],
        lambda b: b[:8],
    ],
    ids=["magic", "version", "short", "trailing", "header-length", "truncated-header"],
)
def test_a_damaged_grid_is_refused_whole(damage):
    body = body_of(codec.encode(header(), values()))
    with pytest.raises(codec.CodecError):
        codec.decode(regzip(damage(body)))


def test_not_gzip_is_refused():
    with pytest.raises(codec.CodecError):
        codec.decode(b"TCA1 not gzip")


@pytest.mark.parametrize(
    "changes",
    [
        {"coefficients": [45, 100]},
        {"offsets_min": [-300, 0, 360]},
        {"offsets_min": [-360, 360, 0]},
        {"rows": 1},
        {"dlat": -0.5},
        {"reference_port": {"name": "X", "shom_code": "13", "event": "low_water"}},
        {"not_for_navigation": False},
        {"scale_m_s": 0.01},
        {"lat0": 89.5},
    ],
)
def test_a_header_that_disagrees_with_the_contract_is_refused(changes):
    with pytest.raises(codec.CodecError):
        codec.encode(header(**changes), values(header(**changes)))


def test_an_unknown_header_field_is_refused():
    h = header()
    h["extra"] = 1
    with pytest.raises(codec.CodecError):
        codec.check_header(h)


def test_a_node_missing_one_component_only_is_refused():
    v = values()
    v[1, 1, 1, 1, 0] = codec.MISSING
    with pytest.raises(codec.CodecError):
        codec.encode(header(), v)


def test_the_alderney_golden_decodes_to_its_expected_values():
    expected = json.loads((FIXTURE / "alderney.expected.json").read_text())
    data = (FIXTURE / expected["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected["sha256"]
    h, v = codec.decode(data)
    assert h == expected["header"]
    assert int((v[..., 0] == codec.MISSING).sum()) == expected["missing_nodes"] > 0
    for probe in expected["probes"]:
        c = codec.COEFFICIENTS.index(probe["coefficient"])
        s = h["offsets_min"].index(probe["offset_min"])
        assert list(v[c, s, probe["row"], probe["column"]]) == [probe["u"], probe["v"]]
    wet = v[..., 0] != codec.MISSING
    assert int(v[..., 0][wet].astype("i8").sum()) == expected["sum_u"]
    assert int(v[..., 1][wet].astype("i8").sum()) == expected["sum_v"]
    assert h["attribution"] == sources.ATTRIBUTION


# ---- the NetCDF reader -----------------------------------------------------

h5py = pytest.importorskip("h5py")


def shom_file(path, *, minutes=5, port=b"CHERBOURG", code=b"0013", is_bm=0, jitter=0.0):
    grid = sources.BY_ID["manche"]
    time_axis = np.arange(0, 721, minutes, dtype=np.int16)
    rows, cols = 4, 5
    lat = 49.0 + 0.018 * np.arange(rows)
    lon = -2.0 + 0.028 * np.arange(cols)
    lat[1] += jitter
    with h5py.File(path, "w") as f:
        for k, v in {
            "Portref": port,
            "code_du_port_de_reference": code,
            "isBM": np.array([is_bm], dtype=np.int8),
            "grid_type": b"regular",
            "sysgeo": b"WGS84",
            "Release_date": b"2025/02/25",
            "source": b"Manche_3D-2010",
            "comment": "Atlas de courant de mar\xe9e".encode("latin-1"),
        }.items():
            f.attrs[k] = v
        f["coeff"] = np.array([45, 95], dtype=np.int16)
        f["time"] = time_axis
        f["latitude"] = lat
        f["longitude"] = lon
        u = np.zeros((2, len(time_axis), 1, rows, cols), dtype=np.float32)
        u[:] = (np.arange(len(time_axis)) * 0.0101)[None, :, None, None, None]
        u[1] *= 2
        vv = -u.copy()
        u[:, :, 0, 0, 0] = -9999.0  # land
        vv[:, 10, 0, 3, 4] = np.nan  # a bank dried at one hour, one component
        for name, data in (("U", u), ("V", vv)):
            f[name] = data
            f[name].attrs["units"] = b"m/s"
    return grid


def test_a_shom_file_becomes_half_hourly_millimetre_steps(tmp_path):
    path = tmp_path / "manche.nc"
    grid = shom_file(path)
    grid = sources.Grid(**{**grid.__dict__, "file_sha256": ""})
    h, v = build.read(path, grid)
    assert h["offsets_min"] == list(range(-360, 361, 30))
    assert h["reference_port"] == {"name": "CHERBOURG", "shom_code": "0013", "event": "high_water"}
    assert h["rows"] == 4 and h["columns"] == 5
    # step 1 kept is native index 6 (30 min): 6 x 10.1 mm/s, springs doubled
    assert list(v[0, 1, 2, 2]) == [61, -61] and list(v[1, 1, 2, 2]) == [121, -121]
    assert (v[:, :, 0, 0] == codec.MISSING).all()
    # the NaN at native index 10 is not on a kept half hour; at index 12 it would be
    assert v[0, 2, 3, 4, 1] != codec.MISSING


def test_one_missing_component_makes_the_node_missing(tmp_path):
    path = tmp_path / "manche.nc"
    grid = shom_file(path, minutes=30)
    grid = sources.Grid(**{**grid.__dict__, "file_sha256": ""})
    _, v = build.read(path, grid)
    assert list(v[0, 10, 3, 4]) == [codec.MISSING, codec.MISSING]


def test_an_hourly_file_keeps_its_hours(tmp_path):
    path = tmp_path / "gnb.nc"
    grid = shom_file(path, minutes=60)
    grid = sources.Grid(**{**grid.__dict__, "file_sha256": ""})
    h, _ = build.read(path, grid)
    assert h["offsets_min"] == list(range(-360, 361, 60))


@pytest.mark.parametrize(
    "kwargs, words",
    [
        ({"is_bm": 1}, "low water"),
        ({"port": b"BREST", "code": b"0003"}, "reference port"),
        ({"jitter": 0.009}, "not regular"),
    ],
)
def test_a_file_unlike_shoms_is_refused(tmp_path, kwargs, words):
    path = tmp_path / "x.nc"
    grid = shom_file(path, **kwargs)
    grid = sources.Grid(**{**grid.__dict__, "file_sha256": ""})
    with pytest.raises(build.BuildError, match=words):
        build.read(path, grid)


def test_a_file_that_is_not_the_pinned_one_is_refused(tmp_path):
    path = tmp_path / "x.nc"
    grid = shom_file(path)
    with pytest.raises(build.BuildError, match="pinned"):
        build.read(path, grid)


# ---- the grids carried -----------------------------------------------------


def test_the_carried_grids_are_whole_zone_views_timed_from_high_water():
    ids = [g.id for g in sources.GRIDS]
    assert ids == [
        "golfe-normand-breton",
        "bretagne-nord",
        "finistere",
        "baie-de-seine",
        "pas-de-calais",
        "manche",
        "gascogne",
    ]
    resolutions = [g.resolution_m for g in sources.GRIDS]
    assert resolutions == sorted(resolutions), "finest first"
    for g in sources.GRIDS:
        assert len(g.archive_sha256) == 64 and len(g.file_sha256) == 64
        assert g.url.startswith("https://services.data.shom.fr/")
        assert "-250M-" not in g.file and "-100M-" not in g.file and "-30M-" not in g.file


# ---- the staged publisher --------------------------------------------------


def built(tmp_path, ids=("manche", "gascogne")):
    out = tmp_path / "build"
    out.mkdir(parents=True, exist_ok=True)
    for grid in ids:
        h = header(grid=grid, title=sources.BY_ID[grid].title, zone=sources.BY_ID[grid].zone)
        (out / f"{grid}.tca1.gz").write_bytes(codec.encode(h, values(h)))
    return out


def test_publication_is_switched_off():
    assert atlas.PUBLICATION_ENABLED is False


def test_a_staged_atlas_refuses_the_bucket(tmp_path):
    with pytest.raises(PublishError, match="staged"):
        atlas.publish_build(FakeStore(), built(tmp_path), "20261115T0900Z", max_bucket_bytes=BIG)


def test_a_dry_run_writes_a_schema_valid_pointer_and_manifest(tmp_path):
    root = tmp_path / "r2"
    root.mkdir()
    result = atlas.publish_build(
        DirStore(root),
        built(tmp_path),
        "20261115T0900Z",
        max_bucket_bytes=BIG,
        now="2026-11-15T09:00:00Z",
    )
    assert result.uploaded == [
        "tidal-atlas/20261115T0900Z/manche.tca1.gz",
        "tidal-atlas/20261115T0900Z/gascogne.tca1.gz",
    ]
    latest = json.loads((root / "tidal-atlas/latest.json").read_text())
    manifest = json.loads((root / "tidal-atlas/20261115T0900Z/manifest.json").read_text())
    assert list(schema("tidal-atlas-latest.schema.json").iter_errors(latest)) == []
    assert list(schema("tidal-atlas-manifest.schema.json").iter_errors(manifest)) == []
    assert [g["rank"] for g in manifest["grids"]] == [0, 1]
    assert manifest["attribution"] == sources.ATTRIBUTION
    assert manifest["limitation"] == sources.LIMITATION


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(atlas, "PUBLICATION_ENABLED", True)


def test_manifest_last_then_pointer_and_never_backwards(tmp_path, enabled):
    store = FakeStore()
    atlas.publish_build(
        store, built(tmp_path), "20261115T0900Z", max_bucket_bytes=BIG, regional_check=False
    )
    keys = [k for op, k in store.ops if op == "put"]
    manifest = "tidal-atlas/20261115T0900Z/manifest.json"
    assert keys.index(manifest) > keys.index("tidal-atlas/20261115T0900Z/gascogne.tca1.gz")
    assert keys.index("tidal-atlas/latest.json") > keys.index(manifest)
    with pytest.raises(StalePublishError):
        atlas.publish_build(
            store, built(tmp_path), "20261114T0900Z", max_bucket_bytes=BIG, regional_check=False
        )


def test_a_published_build_is_immutable(tmp_path, enabled):
    store = FakeStore()
    atlas.publish_build(
        store, built(tmp_path), "20261115T0900Z", max_bucket_bytes=BIG, regional_check=False
    )
    with pytest.raises(PublishError, match="immutable"):
        atlas.publish_build(
            store,
            built(tmp_path / "other", ids=("manche",)),
            "20261115T0900Z",
            max_bucket_bytes=BIG,
            regional_check=False,
        )


def test_retention_keeps_the_current_and_previous_builds(tmp_path, enabled):
    store = FakeStore()
    for build_id in ("20261101T0000Z", "20261108T0000Z", "20261115T0000Z"):
        atlas.publish_build(
            store, built(tmp_path), build_id, max_bucket_bytes=BIG, regional_check=False
        )
    assert atlas.atlas_build_ids(store) == ["20261108T0000Z", "20261115T0000Z"]


def test_the_storage_guard_refuses_before_any_upload(tmp_path, enabled):
    store = FakeStore()
    with pytest.raises(StorageGuardError):
        atlas.publish_build(
            store, built(tmp_path), "20261115T0900Z", max_bucket_bytes=1000, regional_check=False
        )
    assert not store.list_keys("tidal-atlas/")


def test_a_grid_that_does_not_decode_refuses_the_build(tmp_path):
    out = built(tmp_path)
    (out / "manche.tca1.gz").write_bytes(b"not a grid")
    with pytest.raises(codec.CodecError):
        atlas.collect(out)


# ---- the workflow and the register -----------------------------------------


def test_the_workflow_is_manual_only_and_holds_no_bucket_credentials():
    text = (ROOT / ".github/workflows/tidal-atlas.yml").read_text()
    trigger = text.split("\non:\n", 1)[1].split("\nconcurrency:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "schedule" not in trigger and "push" not in trigger
    assert "secrets." not in text
    assert "--dry-run" in text
    dispatcher = (ROOT / "dispatcher").rglob("*.ts")
    assert not any(
        "tidal-atlas" in p.read_text() for p in dispatcher if "node_modules" not in p.parts
    )


def test_the_register_credits_shom_for_the_atlas():
    register = json.loads((ROOT / "contracts/data-sources.json").read_text())
    row = next(s for s in register["sources"] if s["id"] == "shom-atlas-courants-2d")
    assert row["used_by"] == ["tidal-atlas"]
    assert sources.ATTRIBUTION in row["attribution"]
    assert row["licence"] == "Licence Ouverte 2.0"
    # the licence allows it; the owner decides on publication
    assert row["redistribute"] == "unconfirmed"
