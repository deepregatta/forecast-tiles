"""The production entry point must stop before provider I/O when paused."""

from types import SimpleNamespace

import pytest

from ingest import cli
from ingest.paid_work import Guard, Paused


def test_pause_precedes_provider_resolution(monkeypatch):
    monkeypatch.setenv("PAID_WORK_ENFORCE", "1")

    def pause():
        raise Paused("operator pause")

    monkeypatch.setattr(Guard, "from_env", lambda: SimpleNamespace(check=pause))
    monkeypatch.setattr(cli, "_resolve", lambda *_: pytest.fail("provider contacted"))
    monkeypatch.setattr(cli, "make_r2_store_from_env", lambda: pytest.fail("data store touched"))
    assert cli.main(["weather", "--cycle", "20261003T00", "--force"]) == 0


def test_unknown_state_precedes_production(monkeypatch):
    monkeypatch.setenv("PAID_WORK_ENFORCE", "1")
    monkeypatch.delenv("R2_ENDPOINT", raising=False)
    monkeypatch.delenv("R2_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(cli, "_resolve", lambda *_: pytest.fail("provider contacted"))
    assert cli.main(["weather"]) == 0
