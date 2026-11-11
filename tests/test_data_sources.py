"""The data-source register covers every published layer and obeys its schema.

`contracts/data-sources.json` is the machine-readable licence register; the
prose duties (UKV ShareAlike above all) stay in DATA-LICENSES.md. A value in
the register must be one a file in this repository already states, and that
file is cited in `recorded_in`. Everything else is `unconfirmed`.
"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ingest.cli import LAYERS

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts/data-sources.schema.json").read_text())
REGISTER = json.loads((ROOT / "contracts/data-sources.json").read_text())
# Published outputs that are not scheduled layers: the TCP1 chart pack.
NON_LAYER_USERS = {"charts"}


def validator():
    return Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def errors_for(doc):
    return sorted(validator().iter_errors(doc), key=lambda e: list(e.path))


def entry(source_id):
    return next(s for s in REGISTER["sources"] if s["id"] == source_id)


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)


def test_register_validates_against_the_schema():
    assert errors_for(REGISTER) == []


def test_every_layer_is_used_by_an_entry():
    used = {layer for source in REGISTER["sources"] for layer in source["used_by"]}
    missing = [layer for layer in LAYERS if layer not in used]
    assert missing == [], f"layers with no data-sources.json entry: {missing}"


def test_every_used_by_id_is_a_layer_or_the_chart_pack():
    known = set(LAYERS) | NON_LAYER_USERS
    unknown = {
        (source["id"], user)
        for source in REGISTER["sources"]
        for user in source["used_by"]
        if user not in known
    }
    assert unknown == set()


def test_entry_ids_are_unique():
    ids = [source["id"] for source in REGISTER["sources"]]
    assert len(ids) == len(set(ids))


def test_every_cited_file_exists_in_this_repository():
    for source in REGISTER["sources"]:
        for path in source["recorded_in"]:
            assert (ROOT / path).is_file(), f"{source['id']}: {path} not found"


def test_a_yes_or_no_entry_names_a_licence_and_cites_a_file():
    for source in REGISTER["sources"]:
        if source["redistribute"] == "unconfirmed":
            continue
        assert source["licence"], source["id"]
        assert source["recorded_in"], source["id"]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda s: s.update(redistribute="yes", licence=None), "licence"),
        (lambda s: s.update(redistribute="maybe"), "redistribute"),
        (lambda s: s.update(verified_on="2026-10-02", verified_from=None), "verified_from"),
        (lambda s: s.update(verified_on="2 October 2026"), "verified_on"),
        (lambda s: s.update(attribution="Someone", recorded_in=[]), "recorded_in"),
        (lambda s: s.update(used_by=[]), "used_by"),
        (lambda s: s.update(unexpected="x"), "unexpected"),
        (lambda s: s.pop("share_alike"), "share_alike"),
    ],
)
def test_schema_rejects_a_weakened_entry(mutate, expected):
    doc = copy.deepcopy(REGISTER)
    mutate(next(s for s in doc["sources"] if s["id"] == "noaa-gfs"))
    found = errors_for(doc)
    assert found, "the weakened entry was accepted"
    assert any(expected in e.message or expected in list(map(str, e.path)) for e in found)


def test_schema_rejects_unknown_top_level_properties_and_an_empty_list():
    assert errors_for({**REGISTER, "extra": 1})
    assert errors_for({"schema_version": 1, "sources": []})
    assert errors_for({"schema_version": 2, "sources": REGISTER["sources"]})


def test_schema_accepts_a_yes_answer_with_a_licence_and_a_cited_file():
    # A fixture, not real data: the register itself holds no "yes" until the owner confirms one.
    doc = copy.deepcopy(REGISTER)
    source = next(s for s in doc["sources"] if s["id"] == "metoffice-ukv")
    source.update(redistribute="yes")
    assert errors_for(doc) == []
    source.update(redistribute="no")
    assert errors_for(doc) == []


def test_ukv_entry_keeps_its_recorded_duties():
    ukv = entry("metoffice-ukv")
    assert ukv["share_alike"] is True
    assert ukv["verified_on"] == "2026-10-02"
    assert ukv["verified_from"] == "https://registry.opendata.aws/met-office-uk-deterministic/"
    assert ukv["redistribute"] == "unconfirmed"
    assert "DATA-LICENSES.md" in ukv["recorded_in"]
