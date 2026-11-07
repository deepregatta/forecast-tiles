"""Mapbox Vector Tile 2.1 at the protobuf level: keep layers and properties
without decoding geometry, and encode the few features the builder adds.

Geometry of kept features passes through untouched, so trimming a tile costs a
protobuf parse and re-serialise, not a geometry round trip.

Vendored unchanged from tactician `tools/charts/mvt.py` (main at eaf6d88,
2026-11-07), only its import made package-relative.
"""

from __future__ import annotations

import gzip
import math
from fnmatch import fnmatchcase

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from ingest.charts.common import EXTENT


def _messages():
    F = descriptor_pb2.FieldDescriptorProto
    fd = descriptor_pb2.FileDescriptorProto(
        name="tcp1_vector_tile.proto", package="tcp1vt", syntax="proto2"
    )
    tile = fd.message_type.add(name="Tile")
    value = tile.nested_type.add(name="Value")
    for num, name, typ in [
        (1, "string_value", F.TYPE_STRING),
        (2, "float_value", F.TYPE_FLOAT),
        (3, "double_value", F.TYPE_DOUBLE),
        (4, "int_value", F.TYPE_INT64),
        (5, "uint_value", F.TYPE_UINT64),
        (6, "sint_value", F.TYPE_SINT64),
        (7, "bool_value", F.TYPE_BOOL),
    ]:
        value.field.add(name=name, number=num, type=typ, label=F.LABEL_OPTIONAL)
    feature = tile.nested_type.add(name="Feature")
    feature.field.add(name="id", number=1, type=F.TYPE_UINT64, label=F.LABEL_OPTIONAL)
    feature.field.add(
        name="tags",
        number=2,
        type=F.TYPE_UINT32,
        label=F.LABEL_REPEATED,
        options=descriptor_pb2.FieldOptions(packed=True),
    )
    feature.field.add(name="type", number=3, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL)
    feature.field.add(
        name="geometry",
        number=4,
        type=F.TYPE_UINT32,
        label=F.LABEL_REPEATED,
        options=descriptor_pb2.FieldOptions(packed=True),
    )
    layer = tile.nested_type.add(name="Layer")
    layer.field.add(
        name="version", number=15, type=F.TYPE_UINT32, label=F.LABEL_REQUIRED, default_value="1"
    )
    layer.field.add(name="name", number=1, type=F.TYPE_STRING, label=F.LABEL_REQUIRED)
    layer.field.add(
        name="features",
        number=2,
        type=F.TYPE_MESSAGE,
        type_name=".tcp1vt.Tile.Feature",
        label=F.LABEL_REPEATED,
    )
    layer.field.add(name="keys", number=3, type=F.TYPE_STRING, label=F.LABEL_REPEATED)
    layer.field.add(
        name="values",
        number=4,
        type=F.TYPE_MESSAGE,
        type_name=".tcp1vt.Tile.Value",
        label=F.LABEL_REPEATED,
    )
    layer.field.add(
        name="extent", number=5, type=F.TYPE_UINT32, label=F.LABEL_OPTIONAL, default_value="4096"
    )
    tile.field.add(
        name="layers",
        number=3,
        type=F.TYPE_MESSAGE,
        type_name=".tcp1vt.Tile.Layer",
        label=F.LABEL_REPEATED,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fd)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("tcp1vt.Tile"))


Tile = _messages()
POINT, LINESTRING, POLYGON = 1, 2, 3


def unzip(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def parse(data: bytes):
    t = Tile()
    t.ParseFromString(unzip(data))
    return t


def value_of(v):
    for field in (
        "string_value",
        "float_value",
        "double_value",
        "int_value",
        "uint_value",
        "sint_value",
        "bool_value",
    ):
        if v.HasField(field):
            return getattr(v, field)
    return None


def props(layer, feature) -> dict:
    keys, values = layer.keys, layer.values
    tags = feature.tags
    return {keys[tags[i]]: value_of(values[tags[i + 1]]) for i in range(0, len(tags) - 1, 2)}


class LayerWriter:
    """Builds one output layer: interned keys and values, features in order."""

    def __init__(self, name: str, extent: int = EXTENT, keep=None):
        self.name = name
        self.extent = extent
        self.keep = keep
        self.keys: dict[str, int] = {}
        self.values: dict[tuple, int] = {}
        self.features = []  # (id or None, type, geometry list, tags list)

    def _key(self, k):
        i = self.keys.get(k)
        if i is None:
            i = self.keys[k] = len(self.keys)
        return i

    def _value(self, v):
        if isinstance(v, bool):
            key = ("b", v)
        elif isinstance(v, int):
            key = ("i", v)
        elif isinstance(v, float):
            key = ("i", int(v)) if v.is_integer() and abs(v) < 2**53 else ("d", v)
        else:
            key = ("s", str(v))
        i = self.values.get(key)
        if i is None:
            i = self.values[key] = len(self.values)
        return i

    def add(self, geom_type: int, geometry, properties: dict, fid=None):
        tags = []
        for k, v in properties.items():
            if v is None or (self.keep is not None and not self.keep(k)):
                continue
            tags += (self._key(k), self._value(v))
        self.features.append((fid, geom_type, geometry, tags))

    def __len__(self):
        return len(self.features)

    def byte_size(self) -> int:
        t = Tile()
        self.to_layer(t.layers.add())
        return t.ByteSize()

    def to_layer(self, layer):
        layer.version = 2
        layer.name = self.name
        layer.extent = self.extent
        layer.keys.extend(self.keys)
        for kind, v in self.values:
            val = layer.values.add()
            if kind == "s":
                val.string_value = v
            elif kind == "b":
                val.bool_value = v
            elif kind == "d":
                val.double_value = v
            elif v < 0:
                val.sint_value = v
            else:
                val.uint_value = v
        for fid, gtype, geometry, tags in self.features:
            f = layer.features.add()
            if fid is not None:
                f.id = fid
            f.type = gtype
            f.tags.extend(tags)
            f.geometry.extend(geometry)


def keeper(allowed: list[str]):
    """A predicate for property keys: exact names, or patterns with '*'."""
    exact = {k for k in allowed if "*" not in k}
    patterns = [k for k in allowed if "*" in k]

    def keep(k: str) -> bool:
        return k in exact or any(fnmatchcase(k, p) for p in patterns)

    return keep


def copy_layer(src_layer, out: LayerWriter, keep_key, drop=None, keep_ids=False):
    """Copy features from a parsed layer into a writer, keeping only allowed
    properties. Geometry is copied as its encoded command integers. `drop`
    (geometry type, kept properties) -> True leaves a feature out."""
    keys, values = src_layer.keys, src_layer.values
    kept_keys = [keep_key(k) for k in keys]
    for f in src_layer.features:
        tags = f.tags
        p = {}
        for i in range(0, len(tags) - 1, 2):
            if kept_keys[tags[i]]:
                p[keys[tags[i]]] = value_of(values[tags[i + 1]])
        if drop is not None and drop(f.type, p):
            continue
        out.add(f.type, list(f.geometry), p, f.id if keep_ids and f.HasField("id") else None)


def encode(writers: list[LayerWriter]) -> bytes:
    t = Tile()
    for w in writers:
        if len(w):
            w.to_layer(t.layers.add())
    return t.SerializeToString()


# ---- geometry commands ----


def zz(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def unzz(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def point_geometry(points) -> list[int]:
    out = [(1 & 0x7) | (len(points) << 3)]
    cx = cy = 0
    for x, y in points:
        out += (zz(x - cx), zz(y - cy))
        cx, cy = x, y
    return out


def decode_points(geometry) -> list[tuple[int, int]]:
    """The positions of a point feature (MoveTo commands only)."""
    pts, i, x, y = [], 0, 0, 0
    while i < len(geometry):
        cmd, count = geometry[i] & 0x7, geometry[i] >> 3
        i += 1
        if cmd == 1:
            for _ in range(count):
                x += unzz(geometry[i])
                y += unzz(geometry[i + 1])
                i += 2
                pts.append((x, y))
        else:
            break
    return pts


def lines_geometry(lines, closed: bool = False) -> list[int]:
    """Encode line strings (closed=False) or polygon rings (closed=True). Each
    item is a list of integer (x, y); consecutive duplicates are dropped."""
    out = []
    cx = cy = 0
    for line in lines:
        pts = [line[0]]
        for p in line[1:]:
            if p != pts[-1]:
                pts.append(p)
        if closed and len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        if len(pts) < (3 if closed else 2):
            continue
        x, y = pts[0]
        out += ((1 & 0x7) | (1 << 3), zz(x - cx), zz(y - cy))
        cx, cy = x, y
        out.append((2 & 0x7) | ((len(pts) - 1) << 3))
        for x, y in pts[1:]:
            out += (zz(x - cx), zz(y - cy))
            cx, cy = x, y
        if closed:
            out.append((7 & 0x7) | (1 << 3))
    return out


def decode_rings(geometry) -> list[list[tuple[int, int]]]:
    """The rings of a polygon feature (or the parts of a line), in order."""
    rings, ring, i, x, y = [], [], 0, 0, 0
    while i < len(geometry):
        cmd, count = geometry[i] & 0x7, geometry[i] >> 3
        i += 1
        if cmd == 1:
            if ring:
                rings.append(ring)
            ring = []
            for _ in range(count):
                x += unzz(geometry[i])
                y += unzz(geometry[i + 1])
                i += 2
                ring.append((x, y))
        elif cmd == 2:
            for _ in range(count):
                x += unzz(geometry[i])
                y += unzz(geometry[i + 1])
                i += 2
                ring.append((x, y))
        elif cmd == 7:
            pass
    if ring:
        rings.append(ring)
    return rings


def ring_area(ring) -> float:
    """Surveyor's formula in tile coordinates (y down): positive = exterior."""
    a = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        a += x0 * y1 - x1 * y0
    return a / 2.0


def tile_point(lon: float, lat: float, z: int, x: int, y: int) -> tuple[float, float]:
    n = 1 << z
    fx = (lon + 180.0) / 360.0 * n - x
    r = math.radians(lat)
    fy = (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n - y
    return fx * EXTENT, fy * EXTENT
