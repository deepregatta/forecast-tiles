import copy
import hashlib

import pytest

from ingest.forecast_recovery import permit_failed_cycle
from ingest.paid_work import Paused
from test_publish import FakeStore

IDENTITY = hashlib.sha256(b"weather:2026-10-05T00:00Z").hexdigest()


class Guard:
    def __init__(self):
        self.doc = {
            "paused": False,
            "max_seconds": 100000,
            "max_starts": 50,
            "channels": {
                "weather": {"min_interval_seconds": 60, "daily_starts": 10},
            },
            "usage": {
                "starts": 4,
                "seconds": 57600,
                "channels": {
                    "weather": {
                        "seen": {IDENTITY: "failed", "other": "keep"},
                        "days": {},
                        "last": 1,
                    },
                    "other": {"seen": {"unrelated": "keep"}},
                },
            },
        }
        self.writes = 0
        self.clock = lambda: 1791183600

    def read(self):
        return copy.deepcopy(self.doc), '"etag"'

    def validate(self, doc):
        if doc["paused"]:
            raise Paused("operator pause")

    def write(self, doc, etag):
        assert etag == '"etag"'
        self.writes += 1
        self.doc = copy.deepcopy(doc)
        return True


def test_single_identity_is_removed_without_refunding_any_charge_or_policy():
    guard = Guard()
    before = copy.deepcopy(guard.doc)
    permit_failed_cycle(FakeStore(), guard, "weather", "2026-10-05T00:00Z")
    del before["usage"]["channels"]["weather"]["seen"][IDENTITY]
    assert guard.doc == before and guard.writes == 1


@pytest.mark.parametrize("landed", [True, False])
def test_uncertain_recovery_put_is_never_retried(landed):
    guard = Guard()
    write = guard.write

    def uncertain(doc, etag):
        if landed:
            write(doc, etag)
        else:
            guard.writes += 1
        raise Paused("reservation outcome unknown")

    guard.write = uncertain
    if landed:
        permit_failed_cycle(FakeStore(), guard, "weather", "2026-10-05T00:00Z")
    else:
        with pytest.raises(Paused):
            permit_failed_cycle(FakeStore(), guard, "weather", "2026-10-05T00:00Z")
    assert guard.writes == 1


@pytest.mark.parametrize("block", ["paused", "lease", "quota", "superseded", "complete"])
def test_recovery_block_preserves_all_state_before_any_write(block):
    guard, store = Guard(), FakeStore()
    if block == "paused":
        guard.doc["paused"] = True
    elif block == "lease":
        guard.doc["usage"]["channels"]["weather"]["active"] = {"until": guard.clock() + 1}
    elif block == "quota":
        guard.doc["max_starts"] = 4
    elif block == "superseded":
        store.objects["latest.json"] = b'{"layers":{"weather":{"cycle":"2026-10-05T06:00Z"}}}'
    else:
        store.objects["forecast-runs/weather-20261005T00Z/manifest.json"] = b"{}"
    before = copy.deepcopy(guard.doc)
    with pytest.raises(Paused):
        permit_failed_cycle(store, guard, "weather", "2026-10-05T00:00Z")
    assert guard.doc == before and guard.writes == 0
