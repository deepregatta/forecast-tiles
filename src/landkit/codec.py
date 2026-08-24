"""TLI1 routing-index tile codec — the Python reference implementation.

TLI1 answers one question and no other: **may a route cross this cell?** It is
a routing-legality artifact, not a chart. It carries no depths, no soundings,
no aids to navigation and no coastline the crew is meant to look at; the
display basemap is a separate layer entirely.

Layout (little-endian throughout, deliberately the same shape as PFT1 so the
two decoders read alike):

    bytes 0-3    magic b"TLI1"
    bytes 4-7    u32 header_len (unpadded JSON byte length)
    bytes 8-..   UTF-8 JSON header, zero-padded to a 4-byte boundary
    then         payload: the blocked bitmap, `nlat` rows of `row_bytes`,
                 row 0 southernmost, bit 0 of each byte westernmost

A set bit means **blocked**: a route segment touching that cell is illegal.
The bitmap is a raster on purpose. A polygon index would need floating-point
segment intersection at query time, and this artifact has to give byte-identical
answers on Ubuntu, Windows and macOS (validation-plan section 4 T1); integer
cell arithmetic does, and a raster is conservative by construction because a
cell is set whenever *any* source geometry touches it.

The canonical spec is `docs/land-index-format.md` in this repo; the consumer is
tactician's `core/land`, which checks the version by name and refuses anything
else rather than half-reading it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import orjson

MAGIC = b"TLI1"

#: The tile-header contract version. A consumer that does not know it refuses
#: the file rather than guessing at the payload's meaning.
SCHEMA_VERSION = 1

#: Bit order inside a row byte, stated in the header so a decoder never has to
#: infer it. Row 0 is the southernmost, matching the tile id's SW-corner name.
CELL_ORDER = "south_to_north_row_major_lsb_first"

_HEADER_REQUIRED = (
    "spec",
    "schema_version",
    "index_id",
    "tile_id",
    "lon0",
    "lat0",
    "dlon",
    "dlat",
    "nlon",
    "nlat",
    "row_bytes",
    "cell_order",
    "cells",
    "blocked_cells",
    "composition",
    "conservatism",
    "generated_at",
)


@dataclass
class DecodedLandTile:
    header: dict
    #: (nlat, nlon) bool, True = blocked. Row 0 is the southernmost.
    blocked: np.ndarray

    def cell_of(self, lon: float, lat: float) -> tuple[int, int] | None:
        """The (row, column) containing a position, or None if outside."""
        head = self.header
        col = int(np.floor((lon - head["lon0"]) / head["dlon"]))
        row = int(np.floor((lat - head["lat0"]) / head["dlat"]))
        if 0 <= row < head["nlat"] and 0 <= col < head["nlon"]:
            return row, col
        return None


def row_bytes(nlon: int) -> int:
    """Rows are padded to a whole byte so a decoder can index a row without
    unpacking the ones before it."""
    return (nlon + 7) // 8


def pack_mask(blocked: np.ndarray) -> bytes:
    """Pack a (nlat, nlon) bool array, LSB-first within each row's bytes."""
    if blocked.dtype != np.bool_:
        raise ValueError(f"blocked mask must be bool, got {blocked.dtype}")
    if blocked.ndim != 2:
        raise ValueError(f"blocked mask must be 2-D, got shape {blocked.shape}")
    return np.packbits(blocked, axis=-1, bitorder="little").tobytes()


def unpack_mask(payload: bytes, nlat: int, nlon: int) -> np.ndarray:
    expected = nlat * row_bytes(nlon)
    if len(payload) != expected:
        raise ValueError(f"payload is {len(payload)} bytes, expected {expected}")
    raw = np.frombuffer(payload, dtype=np.uint8).reshape(nlat, row_bytes(nlon))
    return np.unpackbits(raw, axis=-1, bitorder="little", count=nlon).astype(bool)


def encode_tile(header: dict, blocked: np.ndarray) -> bytes:
    for key in _HEADER_REQUIRED:
        if key not in header:
            raise ValueError(f"header missing required key: {key}")
    if header["spec"] != "TLI1":
        raise ValueError(f"unsupported spec: {header['spec']}")
    if header["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {header['schema_version']}")
    if tuple(blocked.shape) != (header["nlat"], header["nlon"]):
        raise ValueError(
            f"mask shape {tuple(blocked.shape)} != header ({header['nlat']}, {header['nlon']})"
        )
    if header["row_bytes"] != row_bytes(header["nlon"]):
        raise ValueError("header row_bytes disagrees with nlon")
    if header["cell_order"] != CELL_ORDER:
        raise ValueError(f"unsupported cell_order: {header['cell_order']}")
    if header["cells"] != header["nlat"] * header["nlon"]:
        raise ValueError("header cells disagrees with nlat * nlon")
    counted = int(blocked.sum())
    if header["blocked_cells"] != counted:
        raise ValueError(f"header blocked_cells {header['blocked_cells']} != counted {counted}")

    payload = pack_mask(blocked)
    header_json = orjson.dumps(header)
    pad = (-(8 + len(header_json))) % 4
    return b"".join(
        [MAGIC, struct.pack("<I", len(header_json)), header_json, b"\x00" * pad, payload]
    )


def decode_tile(buf: bytes) -> DecodedLandTile:
    if buf[:4] != MAGIC:
        raise ValueError("not a TLI1 tile")
    (header_len,) = struct.unpack("<I", buf[4:8])
    header = orjson.loads(buf[8 : 8 + header_len])
    if header.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"TLI1 schema_version {header.get('schema_version')} != {SCHEMA_VERSION}")
    start = 8 + header_len + ((-(8 + header_len)) % 4)
    blocked = unpack_mask(buf[start:], header["nlat"], header["nlon"])
    counted = int(blocked.sum())
    if counted != header["blocked_cells"]:
        raise ValueError(
            f"payload has {counted} blocked cells, header says {header['blocked_cells']}"
        )
    return DecodedLandTile(header=header, blocked=blocked)
