"""TCA1 grid files: one SHOM atlas grid, both coefficients, every kept hour.

A grid file is one gzip stream (mtime 0, so the same grid always gives the
same bytes). Decompressed, all integers little-endian:

    b"TCA1"  u8 schema_version = 1
    u32 H    H bytes of UTF-8 JSON: the header (below)
    i16 ×    2 coefficients × steps × rows × columns × 2 components

in that order: the neap coefficient (45) first, offsets from high water
ascending, rows south to north, columns west to east, then east (u) and
north (v) velocity in millimetres per second. Both components of a node
are `MISSING` (-32768) together where the atlas has no water (land, or a
bank dried at that hour). Nothing follows the values.

The header names the grid (`lat0`, `dlat`, `rows`, `lon0`, `dlon`,
`columns`: node centres), the reference port and its event, the offsets,
the scale and the source. `decode` refuses anything it does not recognise,
a short or long payload, and a header that disagrees with itself; it never
returns part of a grid. Tactician's reader is `core/atlas`, which refuses
the same way.
"""

from __future__ import annotations

import gzip
import json
import struct

import numpy as np

MAGIC = b"TCA1"
SCHEMA_VERSION = 1
SPEC = "TCA1"
MISSING = -32768
SCALE_M_S = 0.001
COEFFICIENTS = (45, 95)
MAX_HEADER_BYTES = 65536
# The atlases give six hours either side of the reference high water.
MAX_OFFSET_MIN = 360

HEADER_FIELDS = {
    "spec",
    "schema_version",
    "grid",
    "zone",
    "title",
    "resolution_m",
    "reference_port",
    "lat0",
    "dlat",
    "rows",
    "lon0",
    "dlon",
    "columns",
    "coefficients",
    "offsets_min",
    "scale_m_s",
    "missing",
    "source",
    "attribution",
    "limitation",
    "not_for_navigation",
}


class CodecError(ValueError):
    pass


def check_header(header: dict) -> None:
    if not isinstance(header, dict) or set(header) != HEADER_FIELDS:
        missing = sorted(HEADER_FIELDS - set(header or {}))
        extra = sorted(set(header or {}) - HEADER_FIELDS)
        raise CodecError(f"TCA1 header fields differ: missing {missing}, unknown {extra}")
    if header["spec"] != SPEC or header["schema_version"] != SCHEMA_VERSION:
        raise CodecError("not a TCA1 schema 1 header")
    for key in ("rows", "columns", "resolution_m"):
        if type(header[key]) is not int or header[key] < 1:
            raise CodecError(f"TCA1 header {key} must be a positive integer")
    if header["rows"] < 2 or header["columns"] < 2:
        raise CodecError("TCA1 grid needs at least two rows and two columns")
    for key in ("lat0", "dlat", "lon0", "dlon"):
        if not isinstance(header[key], float):
            raise CodecError(f"TCA1 header {key} must be a number")
    if not (header["dlat"] > 0 and header["dlon"] > 0):
        raise CodecError("TCA1 steps must be positive")
    top = header["lat0"] + header["dlat"] * (header["rows"] - 1)
    east = header["lon0"] + header["dlon"] * (header["columns"] - 1)
    if not (-90 <= header["lat0"] < top <= 90 and -180 <= header["lon0"] < east <= 180):
        raise CodecError("TCA1 grid leaves the globe")
    if list(header["coefficients"]) != list(COEFFICIENTS):
        raise CodecError("TCA1 coefficients must be [45, 95]")
    offsets = header["offsets_min"]
    if (
        not isinstance(offsets, list)
        or len(offsets) < 2
        or any(type(o) is not int for o in offsets)
        or offsets != sorted(set(offsets))
        or offsets[0] != -MAX_OFFSET_MIN
        or offsets[-1] != MAX_OFFSET_MIN
    ):
        raise CodecError("TCA1 offsets must rise strictly from -360 to +360 minutes")
    if header["scale_m_s"] != SCALE_M_S or header["missing"] != MISSING:
        raise CodecError("TCA1 scale and missing value are fixed")
    port = header["reference_port"]
    if (
        not isinstance(port, dict)
        or set(port) != {"name", "shom_code", "event"}
        or port["event"] != "high_water"
        or not isinstance(port["name"], str)
        or not isinstance(port["shom_code"], str)
        or not port["shom_code"].isdigit()
    ):
        raise CodecError("TCA1 reference port must name a SHOM port and its high water")
    if header["not_for_navigation"] is not True:
        raise CodecError("TCA1 grids are never for navigation")


def value_count(header: dict) -> int:
    return 2 * len(header["offsets_min"]) * header["rows"] * header["columns"] * 2


def encode(header: dict, values: np.ndarray) -> bytes:
    """`values`: int16, shape (2, steps, rows, columns, 2)."""
    check_header(header)
    shape = (2, len(header["offsets_min"]), header["rows"], header["columns"], 2)
    if values.dtype != np.int16 or values.shape != shape:
        raise CodecError(f"values must be int16 {shape}, got {values.dtype} {values.shape}")
    missing = values == MISSING
    if (missing[..., 0] != missing[..., 1]).any():
        raise CodecError("a node is missing one component only")
    text = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    if len(text) > MAX_HEADER_BYTES:
        raise CodecError("TCA1 header too long")
    body = MAGIC + struct.pack("<BI", SCHEMA_VERSION, len(text)) + text
    body += values.astype("<i2").tobytes()
    return gzip.compress(body, compresslevel=9, mtime=0)


def decode(data: bytes) -> tuple[dict, np.ndarray]:
    try:
        body = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise CodecError(f"not a gzip stream: {exc}") from exc
    if len(body) < 9 or body[:4] != MAGIC:
        raise CodecError("not a TCA1 grid")
    version, length = struct.unpack_from("<BI", body, 4)
    if version != SCHEMA_VERSION:
        raise CodecError(f"TCA1 schema {version} needs another version of this reader")
    if length > MAX_HEADER_BYTES or 9 + length > len(body):
        raise CodecError("TCA1 header runs past the file")
    try:
        header = json.loads(body[9 : 9 + length])
    except ValueError as exc:
        raise CodecError(f"TCA1 header is not JSON: {exc}") from exc
    check_header(header)
    payload = body[9 + length :]
    if len(payload) != 2 * value_count(header):
        raise CodecError(
            f"TCA1 payload is {len(payload)} bytes, header asks for {2 * value_count(header)}"
        )
    shape = (2, len(header["offsets_min"]), header["rows"], header["columns"], 2)
    values = np.frombuffer(payload, dtype="<i2").reshape(shape).astype(np.int16)
    missing = values == MISSING
    if (missing[..., 0] != missing[..., 1]).any():
        raise CodecError("a node is missing one component only")
    return header, values
