"""Just enough GeoTIFF to read what the EMODnet WCS actually returns.

The alternative was a GDAL binding, which is a large binary dependency for one
job: read an uncompressed single-band float32 raster and tell us where its
corner is. What the service returns is fully described by a dozen baseline TIFF
tags plus `ModelTransformation`, so it is read here directly.

Anything else — a compression, a band count, a sample format this has not seen
— is refused by name rather than guessed at. A bathymetry raster silently
misread is a routing index that says water where there is rock.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

_TAG_TYPES = {1: "B", 2: "s", 3: "H", 4: "I", 5: "II", 11: "f", 12: "d", 16: "Q"}

TAG_WIDTH = 256
TAG_HEIGHT = 257
TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_SAMPLES_PER_PIXEL = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_OFFSETS = 273
TAG_STRIP_BYTE_COUNTS = 279
TAG_TILE_WIDTH = 322
TAG_TILE_LENGTH = 323
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTE_COUNTS = 325
TAG_SAMPLE_FORMAT = 339
TAG_MODEL_TRANSFORMATION = 34264
TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_GDAL_NODATA = 42113


class GeoTiffError(ValueError):
    pass


@dataclass
class GeoRaster:
    """A north-up float32 raster and the geographic box it covers."""

    values: np.ndarray  # (height, width) float32, row 0 northernmost
    west: float
    north: float
    dlon: float
    dlat: float  # positive; rows step south by this much

    @property
    def height(self) -> int:
        return self.values.shape[0]

    @property
    def width(self) -> int:
        return self.values.shape[1]

    def south_up(self) -> np.ndarray:
        """The same values with row 0 southernmost, which is the index's order."""
        return self.values[::-1, :]


def read_geotiff(buf: bytes) -> GeoRaster:
    if buf[:2] == b"MM":
        endian = ">"
    elif buf[:2] == b"II":
        endian = "<"
    else:
        raise GeoTiffError("not a TIFF: bad byte-order mark")
    if struct.unpack(endian + "H", buf[2:4])[0] != 42:
        raise GeoTiffError("not a baseline TIFF (BigTIFF is not supported here)")

    tags = _read_ifd(buf, endian, struct.unpack(endian + "I", buf[4:8])[0])

    def one(tag: int, name: str) -> int:
        if tag not in tags:
            raise GeoTiffError(f"missing {name} tag")
        return int(tags[tag][0])

    if one(TAG_COMPRESSION, "Compression") != 1:
        raise GeoTiffError("only uncompressed GeoTIFF is supported here")
    if one(TAG_SAMPLES_PER_PIXEL, "SamplesPerPixel") != 1:
        raise GeoTiffError("only single-band GeoTIFF is supported here")
    if one(TAG_BITS_PER_SAMPLE, "BitsPerSample") != 32:
        raise GeoTiffError("only 32-bit samples are supported here")
    if one(TAG_SAMPLE_FORMAT, "SampleFormat") != 3:
        raise GeoTiffError("only IEEE float samples are supported here")

    width = one(TAG_WIDTH, "ImageWidth")
    height = one(TAG_HEIGHT, "ImageLength")
    dtype = np.dtype(np.float32).newbyteorder(endian)

    if TAG_TILE_OFFSETS in tags:
        values = _read_tiled(buf, tags, dtype, width, height)
    elif TAG_STRIP_OFFSETS in tags:
        values = _read_stripped(buf, tags, dtype, width, height)
    else:
        raise GeoTiffError("neither strip nor tile offsets present")

    west, north, dlon, dlat = _geo_transform(tags)
    nodata = _gdal_nodata(tags)
    values = np.ascontiguousarray(values, dtype=np.float32)
    if nodata is not None:
        values[values == np.float32(nodata)] = np.nan
    return GeoRaster(values=values, west=west, north=north, dlon=dlon, dlat=dlat)


def _read_ifd(buf: bytes, endian: str, offset: int) -> dict[int, tuple]:
    count = struct.unpack(endian + "H", buf[offset : offset + 2])[0]
    tags: dict[int, tuple] = {}
    for index in range(count):
        entry = offset + 2 + index * 12
        tag, typ, n = struct.unpack(endian + "HHI", buf[entry : entry + 8])
        raw = buf[entry + 8 : entry + 12]
        fmt = _TAG_TYPES.get(typ)
        if fmt is None:
            continue
        if fmt == "s":
            data = raw[:n] if n <= 4 else buf[struct.unpack(endian + "I", raw)[0] :][:n]
            tags[tag] = (data.split(b"\x00")[0].decode("ascii", "replace"),)
            continue
        size = struct.calcsize(endian + fmt) * n
        if size <= 4:
            data = raw[:size]
        else:
            (pointer,) = struct.unpack(endian + "I", raw)
            data = buf[pointer : pointer + size]
        tags[tag] = struct.unpack(endian + fmt * n, data)
    return tags


def _read_stripped(buf: bytes, tags: dict, dtype: np.dtype, width: int, height: int) -> np.ndarray:
    rows_per_strip = int(tags.get(TAG_ROWS_PER_STRIP, (height,))[0])
    out = np.empty((height, width), dtype=np.float32)
    for index, offset in enumerate(tags[TAG_STRIP_OFFSETS]):
        first = index * rows_per_strip
        rows = min(rows_per_strip, height - first)
        if rows <= 0:
            break
        chunk = np.frombuffer(buf, dtype=dtype, count=rows * width, offset=int(offset))
        out[first : first + rows] = chunk.reshape(rows, width)
    return out


def _read_tiled(buf: bytes, tags: dict, dtype: np.dtype, width: int, height: int) -> np.ndarray:
    tile_w = int(tags[TAG_TILE_WIDTH][0])
    tile_h = int(tags[TAG_TILE_LENGTH][0])
    across = (width + tile_w - 1) // tile_w
    down = (height + tile_h - 1) // tile_h
    out = np.empty((down * tile_h, across * tile_w), dtype=np.float32)
    offsets = tags[TAG_TILE_OFFSETS]
    if len(offsets) != across * down:
        raise GeoTiffError(f"{len(offsets)} tile offsets for a {across}x{down} tile grid")
    for index, offset in enumerate(offsets):
        chunk = np.frombuffer(buf, dtype=dtype, count=tile_w * tile_h, offset=int(offset))
        row, col = divmod(index, across)
        out[row * tile_h : (row + 1) * tile_h, col * tile_w : (col + 1) * tile_w] = chunk.reshape(
            tile_h, tile_w
        )
    return out[:height, :width]


def _geo_transform(tags: dict) -> tuple[float, float, float, float]:
    if TAG_MODEL_TRANSFORMATION in tags:
        matrix = tags[TAG_MODEL_TRANSFORMATION]
        if len(matrix) != 16:
            raise GeoTiffError("ModelTransformation is not a 4x4 matrix")
        if matrix[1] or matrix[4]:
            raise GeoTiffError("rotated rasters are not supported here")
        return float(matrix[3]), float(matrix[7]), float(matrix[0]), float(-matrix[5])
    if TAG_MODEL_PIXEL_SCALE in tags and TAG_MODEL_TIEPOINT in tags:
        scale = tags[TAG_MODEL_PIXEL_SCALE]
        tie = tags[TAG_MODEL_TIEPOINT]
        if tie[0] or tie[1]:
            raise GeoTiffError("only a raster-origin tiepoint is supported here")
        return float(tie[3]), float(tie[4]), float(scale[0]), float(scale[1])
    raise GeoTiffError("no ModelTransformation or PixelScale/Tiepoint georeferencing")


def _gdal_nodata(tags: dict) -> float | None:
    """GDAL writes its no-data value as an ASCII tag. Services that omit it
    leave missing cells as NaN instead, so absence is normal rather than an
    error — but a stated sentinel read as a depth would be a rock read as
    water, so it is honoured when it is there."""
    raw = tags.get(TAG_GDAL_NODATA)
    if raw is None:
        return None
    try:
        return float(raw[0])
    except (TypeError, ValueError) as error:
        raise GeoTiffError(f"unreadable GDAL_NODATA tag {raw!r}") from error
