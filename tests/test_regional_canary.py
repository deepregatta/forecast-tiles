from copy import deepcopy
from datetime import timedelta

import pytest

from ingest.regional_canary import score_canary, utc

START = "2026-10-02T21:00Z"
END = "2026-10-09T21:00Z"


def receipts(layer="weather-arome"):
    result = score_canary([], layer, START, END)
    return [
        {
            "layer": layer,
            "destination": "r2",
            "cycle": r["cycle"],
            "finished_at": (utc(r["deadline"]) - timedelta(minutes=1)).isoformat(),
            "outcome": "published",
            "pointer_commit_confirmed": True,
            "validation": {"ok": True, "failure_count": 0},
            "source": {"downloads_complete": True},
            "exit_code": 0,
        }
        for r in result["cycles"]
    ]


@pytest.mark.parametrize("layer", ["weather-arome", "weather-icon-eu", "weather-ukv"])
def test_complete_window_needs_all_fourteen_timely_cycles(layer):
    result = score_canary(receipts(layer), layer, START, END)
    assert result["expected_cycles"] == 14 and result["timeliness_passed"]
    assert result["on_time_fraction"] == 1
    late = receipts(layer)
    late[0]["finished_at"] = END
    result = score_canary(late, layer, START, END)
    assert result["counts"]["late"] == 1 and not result["timeliness_passed"]
    assert result["on_time_fraction"] == pytest.approx(13 / 14)


def test_missing_or_skip_only_artifact_keeps_delivery_timing_unknown():
    records = receipts()
    records[0] = {**records[0], "outcome": "already_published", "pointer_commit_confirmed": None}
    result = score_canary(records, "weather-arome", START, END)
    assert result["counts"]["unknown"] == 1 and result["on_time_fraction"] is None
    assert not result["timeliness_passed"]


def test_unavailable_source_is_a_miss_even_with_exit_zero():
    records = receipts()
    records[0] = {
        **records[0],
        "cycle": None,
        "requested_cycle": records[0]["cycle"],
        "outcome": "source_unavailable",
        "pointer_commit_confirmed": None,
    }
    result = score_canary(records, "weather-arome", START, END)
    assert result["counts"]["failed"] == 1 and not result["timeliness_passed"]


def test_invalid_publication_and_incomplete_observation_cannot_pass():
    records = receipts()
    assert not score_canary(records, "weather-arome", START, "2026-10-03T21:00Z")[
        "timeliness_passed"
    ]
    invalid = deepcopy(records)
    invalid[0]["validation"]["failure_count"] = 1
    result = score_canary(invalid, "weather-arome", START, END)
    assert result["counts"]["invalid_published"] == 1 and not result["timeliness_passed"]


def test_bootstrap_and_scratch_receipts_do_not_fill_scheduled_cycles():
    records = receipts()
    bootstrap = {**records[0], "cycle": "2026-10-02T15:00Z"}
    scratch = [{**r, "destination": "scratch"} for r in records]
    result = score_canary([bootstrap, *scratch], "weather-arome", START, END)
    assert result["counts"]["unknown"] == 14 and result["on_time_fraction"] is None


def test_invalid_extra_cycle_published_in_window_blocks_promotion():
    records = receipts()
    extra = deepcopy(records[0])
    extra["cycle"] = "2026-10-03T09:00Z"
    extra["finished_at"] = "2026-10-03T15:00Z"
    extra["validation"]["ok"] = False
    result = score_canary([*records, extra], "weather-arome", START, END)
    assert result["counts"]["on_time"] == 14
    assert not result["timeliness_passed"]
    assert len(result["invalid_publications"]) == 1


def test_extra_confirmed_publication_without_finish_remains_unknown():
    records = receipts()
    extra = {**records[0], "cycle": "2026-10-03T09:00Z", "finished_at": None}
    result = score_canary([*records, extra], "weather-arome", START, END)
    assert result["counts"]["on_time"] == 14
    assert result["unknown_publications"] and not result["timeliness_passed"]
