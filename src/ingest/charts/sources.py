"""The builder's inputs, read by HTTP range and cached under the charts cache.

- Protomaps daily build and Open Waters' dated Seamap archive: only the
  directories and tiles the pack needs are read, never a planet. Fetched tiles
  are kept in a SQLite cache per archive, so a rebuild reads nothing twice.
- SHOM Balisage over WFS, paged, as GeoJSON per layer.

Grown from spikes/chart-pilots/tools/extract_pmtiles.py and fetch_marks.py
(2026-11-06). Vendored from tactician `tools/charts/sources.py` (main at
eaf6d88, 2026-11-07); the cache folder comes from `common.cache_dir()`, and
Open Waters' archive is read only at a date a run names (`seamap_url`): no
run probes for the newest one by itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from pmtiles.tile import deserialize_directory, deserialize_header, find_tile, zxy_to_tileid

from ingest.charts.common import USER_AGENT, cache_dir

PROTOMAPS_BUILDS = "https://build-metadata.protomaps.dev/builds.json"
PROTOMAPS_URL = "https://build.protomaps.com/{date}.pmtiles"
SEAMAP_URL = "https://tiles.openwaters.io/seamap/{date}.pmtiles"
SHOM_WFS = "https://services.data.shom.fr/INSPIRE/wfs"
SHOM_LAYERS = [
    "lights",
    "boylat",
    "boycar",
    "boyisd",
    "boysaw",
    "boyspp",
    "bcnlat",
    "bcncar",
    "bcnisd",
    "bcnspp",
    "topmar",
    "daymar",
    "lndmrk",
]

_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers["User-Agent"] = USER_AGENT
    return _local.s


def newest_protomaps() -> str:
    builds = session().get(PROTOMAPS_BUILDS, timeout=60).json()
    return max(b["key"] for b in builds if b["key"].endswith(".pmtiles")).removesuffix(".pmtiles")


class RemoteArchive:
    """A remote PMTiles v3 archive, read by range with a local tile cache."""

    def __init__(self, name: str, url: str):
        self.name, self.url = name, url
        sources = cache_dir() / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(sources / f"{name}.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v BLOB)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS dirs (offset INTEGER, length INTEGER, data BLOB, PRIMARY KEY (offset, length))"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS tiles (tid INTEGER PRIMARY KEY, data BLOB)")
        self.db.commit()
        self._dirs: dict = {}
        self._lock = threading.Lock()
        head = self._meta("head")
        if head is None:
            head = self._get(0, 16384)
            self._set_meta("head", head)
        self.header = deserialize_header(head[:127])
        self._head = head
        md = self._meta("metadata")
        if md is None:
            raw = self._get(self.header["metadata_offset"], self.header["metadata_length"])
            md = _decompress(raw, self.header["internal_compression"])
            self._set_meta("metadata", md)
        self.metadata = json.loads(md)

    def _meta(self, k):
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))
        self.db.commit()

    def _get(self, offset: int, length: int) -> bytes:
        for attempt in range(5):
            try:
                r = session().get(
                    self.url,
                    headers={"Range": f"bytes={offset}-{offset + length - 1}"},
                    timeout=120,
                )
                r.raise_for_status()
                if len(r.content) != length:
                    raise IOError(f"short read {len(r.content)} of {length}")
                return r.content
            except Exception:  # noqa: BLE001 - retried, then re-raised
                if attempt == 4:
                    raise
        raise AssertionError

    def _directory(self, offset: int, length: int):
        key = (offset, length)
        if key in self._dirs:
            return self._dirs[key]
        if offset == self.header["root_offset"] and offset + length <= len(self._head):
            raw = self._head[offset : offset + length]
        else:
            with self._lock:
                row = self.db.execute(
                    "SELECT data FROM dirs WHERE offset = ? AND length = ?", key
                ).fetchone()
            if row:
                raw = row[0]
            else:
                raw = self._get(offset, length)
                with self._lock:
                    self.db.execute(
                        "INSERT OR REPLACE INTO dirs VALUES (?, ?, ?)", (offset, length, raw)
                    )
        entries = deserialize_directory(raw)
        self._dirs[key] = entries
        return entries

    def entries(self, tiles) -> dict:
        """{(z, x, y): (offset, length)} for the tiles that exist. Directories
        are walked level by level, each distinct leaf read once."""
        h = self.header
        out = {}
        pending = {zxy_to_tileid(*t): ((h["root_offset"], h["root_length"]), t) for t in tiles}
        for _ in range(4):
            if not pending:
                break
            keys = sorted({k for k, _ in pending.values()})
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda k: self._directory(*k), keys))
            nxt = {}
            for tid, (key, t) in pending.items():
                e = find_tile(self._dirs[key], tid)
                if e is None:
                    continue
                if e.run_length > 0:
                    out[t] = (h["tile_data_offset"] + e.offset, e.length)
                else:
                    nxt[tid] = ((h["leaf_directory_offset"] + e.offset, e.length), t)
            pending = nxt
        self.db.commit()
        return out

    def fetch(self, tiles, log=print) -> dict:
        """{(z, x, y): bytes} for the tiles that exist, from the cache or by range."""
        want = self.entries(tiles)
        have = {}
        missing = []
        for t, e in want.items():
            row = self.db.execute(
                "SELECT data FROM tiles WHERE tid = ?", (zxy_to_tileid(*t),)
            ).fetchone()
            if row:
                have[t] = row[0]
            else:
                missing.append((t, e))
        if missing:
            # tiles of a clustered archive lie in tile-id order: merge neighbours
            # into ranges of up to 8 MB, skipping gaps under 64 kB
            missing.sort(key=lambda p: p[1][0])
            spans, cur = [], None
            for t, (off, ln) in missing:
                if cur and off - cur["end"] < 65536 and off + ln - cur["start"] < 8 << 20:
                    cur["end"] = max(cur["end"], off + ln)
                    cur["tiles"].append((t, off, ln))
                else:
                    cur = {"start": off, "end": off + ln, "tiles": [(t, off, ln)]}
                    spans.append(cur)
            total = sum(s["end"] - s["start"] for s in spans)
            log(
                f"  {self.name}: {len(missing)} tiles to read in {len(spans)} ranges, {total / 1e6:.1f} MB"
            )

            def read(span):
                return span, self._get(span["start"], span["end"] - span["start"])

            done = 0
            with ThreadPoolExecutor(6) as pool:
                for span, blob in pool.map(read, spans):
                    for t, off, ln in span["tiles"]:
                        data = blob[off - span["start"] : off - span["start"] + ln]
                        self.db.execute(
                            "INSERT OR REPLACE INTO tiles VALUES (?, ?)", (zxy_to_tileid(*t), data)
                        )
                        have[t] = data
                    done += 1
                    if done % 200 == 0:
                        self.db.commit()
                        log(f"  {self.name}: {done}/{len(spans)} ranges")
            self.db.commit()
        return have

    def describe(self) -> dict:
        return {
            "url": self.url,
            "attribution": self.metadata.get("attribution"),
            "version": self.metadata.get("version") or self.metadata.get("planetiler:version"),
            "osm_replication_time": self.metadata.get("planetiler:osm:osmosisreplicationtime"),
        }


def _decompress(raw: bytes, compression) -> bytes:
    import gzip

    return gzip.decompress(raw) if getattr(compression, "value", compression) == 2 else raw


def fetch_shom(bbox, tag: str, log=print) -> dict:
    """SHOM Balisage WFS layers over bbox, cached by tag. Returns
    {layer: FeatureCollection, "_meta": {...}}."""
    folder = cache_dir() / "sources" / f"shom-{tag}-{'_'.join(f'{v:g}' for v in bbox)}"
    folder.mkdir(parents=True, exist_ok=True)
    w, s, e, n = bbox
    out = {}
    for layer in SHOM_LAYERS:
        path = folder / f"{layer}.geojson"
        if path.exists():
            out[layer] = json.loads(path.read_text())
            continue
        feats, start = [], 0
        while True:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"BALISAGE_BDD_WFS:{layer}",
                "outputFormat": "application/json",
                "bbox": f"{s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326",
                "count": 5000,
                "startIndex": start,
                "sortBy": "inspireid",
            }
            r = session().get(SHOM_WFS, params=params, timeout=180)
            r.raise_for_status()
            fc = r.json()
            feats += fc["features"]
            if len(fc["features"]) < 5000:
                break
            start += 5000
        fc = {"type": "FeatureCollection", "features": feats}
        path.write_text(json.dumps(fc))
        out[layer] = fc
        log(f"  shom {layer}: {len(feats)}")
    digest = hashlib.sha256()
    for layer in SHOM_LAYERS:
        for f in sorted(
            out[layer]["features"], key=lambda f: f["properties"].get("inspireid") or ""
        ):
            digest.update(json.dumps(f, sort_keys=True).encode())
    meta_path = folder / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
    else:
        meta = {"fetched": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    meta.update(
        content_sha256=digest.hexdigest(), records={k: len(v["features"]) for k, v in out.items()}
    )
    meta_path.write_text(json.dumps(meta, indent=1))
    out["_meta"] = meta
    return out
