"""PFT1 tile codec — the Python reference implementation.

Layout (little-endian throughout):

    bytes 0-3    magic b"PFT1"
    bytes 4-7    u32 header_len (unpadded JSON byte length)
    bytes 8-..   UTF-8 JSON header, zero-padded to a 4-byte boundary
    then         payload: per-variable arrays in header order,
                 each C-order [member?][time][lat][lon]

Decoded value = raw * scale + offset; the per-dtype sentinel decodes to NaN
(null on the TypeScript side). The canonical spec lives in the passage repo
(docs/forecast-tiles-spec.md); this module and the TS codec must round-trip
the shared golden fixture identically.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import orjson

MAGIC = b"PFT1"

# dtype name -> (numpy dtype, sentinel for missing)
DTYPES: dict[str, tuple[type, int]] = {
    "i16": (np.int16, -32768),
    "i8": (np.int8, -128),
}

_HEADER_REQUIRED = (
    "spec",
    "schema_version",
    "layer",
    "model",
    "run_id",
    "cycle",
    "tile_id",
    "lat0",
    "lon0",
    "dlat",
    "dlon",
    "nlat",
    "nlon",
    "time_axes",
    "member_count",
    "variables",
)


@dataclass
class DecodedTile:
    header: dict
    arrays: dict[str, np.ndarray]  # float32, NaN where missing

    def times_utc(self, axis: str) -> list[str]:
        ax = self.header["time_axes"][axis]
        base = np.datetime64(ax["base"].replace("Z", ""))
        return [str(base + np.timedelta64(h, "h")) + "Z" for h in ax["offsets_h"]]


def quantize(values: np.ndarray, dtype: str, scale: float, offset: float = 0.0) -> np.ndarray:
    """Quantize float values to the integer dtype; NaN becomes the sentinel."""
    np_dtype, sentinel = DTYPES[dtype]
    info = np.iinfo(np_dtype)
    raw = np.round((np.asarray(values, dtype=np.float64) - offset) / scale)
    # sentinel is reserved: clamp the representable range to exclude it
    raw = np.clip(raw, info.min + 1, info.max)
    raw = np.where(np.isnan(values), sentinel, raw)
    return raw.astype(np_dtype)


def _expected_shape(header: dict, var: dict) -> tuple[int, ...]:
    n_time = len(header["time_axes"][var["axis"]]["offsets_h"])
    spatial = (n_time, header["nlat"], header["nlon"])
    if var.get("per_member"):
        return (header["member_count"], *spatial)
    return spatial


def encode_tile(header: dict, arrays: dict[str, np.ndarray]) -> bytes:
    """Encode a tile. `header["variables"]` lists specs in payload order:
    {name, axis, dtype, scale, offset?, missing?, per_member?}.

    Arrays may be float (quantized here, NaN = missing) or already the target
    integer dtype (passed through). byte_offset/byte_length are filled in.
    """
    for key in _HEADER_REQUIRED:
        if key not in header:
            raise ValueError(f"header missing required key: {key}")
    if header["spec"] != "PFT1":
        raise ValueError(f"unsupported spec: {header['spec']}")

    header = dict(header)
    variables = [dict(v) for v in header["variables"]]
    payload_parts: list[bytes] = []
    offset_bytes = 0
    for var in variables:
        np_dtype, sentinel = DTYPES[var["dtype"]]
        var.setdefault("offset", 0.0)
        var.setdefault("missing", sentinel)
        values = arrays[var["name"]]
        expected = _expected_shape(header, var)
        if tuple(values.shape) != expected:
            raise ValueError(f"{var['name']}: shape {tuple(values.shape)} != expected {expected}")
        if values.dtype == np_dtype:
            raw = values
        elif np.issubdtype(values.dtype, np.floating):
            raw = quantize(values, var["dtype"], var["scale"], var["offset"])
        else:
            raise ValueError(f"{var['name']}: dtype {values.dtype} is neither float nor {np_dtype}")
        encoded = np.ascontiguousarray(raw).astype(np_dtype).tobytes()
        var["byte_offset"] = offset_bytes
        var["byte_length"] = len(encoded)
        # pad every array to a 4-byte boundary so typed-array views stay aligned
        pad = (-len(encoded)) % 4
        offset_bytes += len(encoded) + pad
        payload_parts.append(encoded + b"\x00" * pad)

    header["variables"] = variables
    header_json = orjson.dumps(header)
    pad = (-(8 + len(header_json))) % 4
    return b"".join(
        [MAGIC, struct.pack("<I", len(header_json)), header_json, b"\x00" * pad, *payload_parts]
    )


def decode_tile(buf: bytes) -> DecodedTile:
    if buf[:4] != MAGIC:
        raise ValueError("not a PFT1 tile")
    (header_len,) = struct.unpack("<I", buf[4:8])
    header = orjson.loads(buf[8 : 8 + header_len])
    payload_start = 8 + header_len + ((-(8 + header_len)) % 4)

    arrays: dict[str, np.ndarray] = {}
    for var in header["variables"]:
        np_dtype, _ = DTYPES[var["dtype"]]
        start = payload_start + var["byte_offset"]
        raw = np.frombuffer(
            buf,
            dtype=np.dtype(np_dtype).newbyteorder("<"),
            count=var["byte_length"] // np.dtype(np_dtype).itemsize,
            offset=start,
        )
        raw = raw.reshape(_expected_shape(header, var))
        values = raw.astype(np.float32) * var["scale"] + var.get("offset", 0.0)
        values[raw == var["missing"]] = np.nan
        arrays[var["name"]] = values
    return DecodedTile(header=header, arrays=arrays)
