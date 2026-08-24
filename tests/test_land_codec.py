"""TLI1 encodes one bit per cell and refuses to be half-read."""

import numpy as np
import pytest

from landkit.codec import (
    CELL_ORDER,
    SCHEMA_VERSION,
    decode_tile,
    encode_tile,
    pack_mask,
    row_bytes,
    unpack_mask,
)
from landkit.grid import CELLS_PER_DEG, dilation_cells, lat_cell_metres, lon_cell_metres


def make_header(mask, **overrides):
    header = {
        "spec": "TLI1",
        "schema_version": SCHEMA_VERSION,
        "index_id": "test-20260824T00Z",
        "domain": "test",
        "tile_id": "N48W005",
        "lon0": -5.0,
        "lat0": 48.0,
        "dlon": 1 / CELLS_PER_DEG,
        "dlat": 1 / CELLS_PER_DEG,
        "nlon": mask.shape[1],
        "nlat": mask.shape[0],
        "row_bytes": row_bytes(mask.shape[1]),
        "cell_order": CELL_ORDER,
        "cells": mask.size,
        "blocked_cells": int(mask.sum()),
        "composition": {
            "land_cells": int(mask.sum()),
            "shoal_cells": 0,
            "nodata_cells": 0,
            "blocked_before_buffer": int(mask.sum()),
        },
        "conservatism": {
            "buffer_m": 200.0,
            "dilation_cells": {"lon": 2, "lat": 1},
            "safety_contour_m": 0.0,
            "vertical_datum": "LAT (Lowest Astronomical Tide)",
        },
        "generated_at": "2026-08-24T00:00:00Z",
    }
    header.update(overrides)
    return header


def test_round_trip():
    rng = np.random.default_rng(11)
    mask = rng.random((37, 53)) < 0.3
    decoded = decode_tile(encode_tile(make_header(mask), mask))
    assert (decoded.blocked == mask).all()
    assert decoded.header["tile_id"] == "N48W005"


def test_rows_are_padded_to_a_whole_byte_so_a_row_can_be_indexed_alone():
    mask = np.zeros((4, 11), dtype=bool)
    mask[2, 10] = True
    packed = pack_mask(mask)
    assert row_bytes(11) == 2
    assert len(packed) == 4 * 2
    # The last row's high bits are padding and must not read as blocked.
    assert (unpack_mask(packed, 4, 11) == mask).all()


def test_bit_zero_is_the_westernmost_cell_and_row_zero_the_southernmost():
    mask = np.zeros((2, 8), dtype=bool)
    mask[0, 0] = True  # south-west corner
    assert pack_mask(mask) == b"\x01\x00"


def test_a_bumped_schema_version_fails_loudly_by_name():
    mask = np.zeros((8, 8), dtype=bool)
    buf = encode_tile(make_header(mask), mask)
    bumped = buf.replace(b'"schema_version":1', b'"schema_version":9')
    with pytest.raises(ValueError, match="schema_version 9"):
        decode_tile(bumped)


def test_a_header_that_disagrees_with_its_payload_is_refused():
    mask = np.zeros((8, 8), dtype=bool)
    mask[1, 1] = True
    with pytest.raises(ValueError, match="blocked_cells"):
        encode_tile(make_header(mask, blocked_cells=7), mask)
    with pytest.raises(ValueError, match="row_bytes"):
        encode_tile(make_header(mask, row_bytes=3), mask)
    with pytest.raises(ValueError, match="cells"):
        encode_tile(make_header(mask, cells=5), mask)


def test_not_a_tli1_tile():
    with pytest.raises(ValueError, match="not a TLI1 tile"):
        decode_tile(b"PFT1" + bytes(64))


def test_cell_of_locates_a_position_and_says_when_it_is_outside():
    mask = np.zeros((480, 480), dtype=bool)
    decoded = decode_tile(encode_tile(make_header(mask), mask))
    assert decoded.cell_of(-5.0, 48.0) == (0, 0)
    assert decoded.cell_of(-4.999, 48.001) == (0, 0)
    assert decoded.cell_of(-4.0001, 48.9999) == (479, 479)
    assert decoded.cell_of(-3.5, 48.5) is None


def test_the_dilation_always_reaches_at_least_the_buffer_it_names():
    for south, north in [(40, 50), (50, 60), (-10, 0)]:
        lon_cells, lat_cells = dilation_cells(south, north, 200.0)
        worst = max(abs(south), abs(north))
        assert lon_cells * lon_cell_metres(worst) >= 200.0
        assert lat_cells * lat_cell_metres() >= 200.0
