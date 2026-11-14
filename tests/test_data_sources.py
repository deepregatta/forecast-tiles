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


OWNER_CONFIRMED_2026_11_12 = {
    "noaa-gfs",
    "noaa-gefs",
    "noaa-gfs-wave",
    "ecmwf-open-ifs",
    "cmems-glo12",
    "cmems-ibi",
    "dwd-icon-eu",
    "emodnet-bathymetry",
    "shom-balisage",
    "openstreetmap",
    "protomaps-basemap",
    "metoffice-ukv",
    "meteofrance-arome",
    "gshhg",
    "open-waters-seamap",
}


# Recorded on the owner's decision with no provider licence statement read.
NO_PROVIDER_PAGE_READ = {"meteofrance-arome"}


# Staged on 14 November 2026 (Tactician P8, owner-approved) and published the
# same day on the owner's approval: the same Copernicus Marine licence as
# cmems-glo12 and cmems-ibi, read again that day for each product.
STAGED_2026_11_14 = {"cmems-nws", "cmems-med"}


def test_exactly_the_sources_the_owner_confirmed_say_yes():
    said_yes = {s["id"] for s in REGISTER["sources"] if s["redistribute"] == "yes"}
    assert said_yes == OWNER_CONFIRMED_2026_11_12 | STAGED_2026_11_14
    assert {s["id"] for s in REGISTER["sources"]} == OWNER_CONFIRMED_2026_11_12 | STAGED_2026_11_14
    for source_id in OWNER_CONFIRMED_2026_11_12:
        source = entry(source_id)
        assert source["licence"], source_id
        assert "DATA-LICENSES.md" in source["recorded_in"], source_id
        assert source["notes"].endswith("Confirmed by the owner on 2026-11-12."), source_id
        if source_id in NO_PROVIDER_PAGE_READ:
            # verified_on is a date a provider's terms were read: none was, so none is recorded.
            assert source["verified_on"] is None and source["verified_from"] is None, source_id
        elif source_id == "metoffice-ukv":
            assert source["verified_on"] == "2026-11-12", source_id
        else:
            assert source["verified_on"] == "2026-11-12", source_id
            assert source["verified_from"].startswith("https://"), source_id


def test_the_staged_copernicus_products_cite_the_licence_read_and_their_staging():
    ibi = entry("cmems-ibi")
    for source_id in STAGED_2026_11_14:
        source = entry(source_id)
        assert source["licence"] == ibi["licence"], source_id
        assert source["licence_url"] == ibi["licence_url"], source_id
        assert source["share_alike"] is False, source_id
        assert source["verified_on"] == "2026-11-14", source_id
        assert source["verified_from"] == ibi["licence_url"], source_id
        assert "DATA-LICENSES.md" in source["recorded_in"], source_id
        assert source["attribution"].startswith("Generated using E.U. Copernicus Marine Service"), (
            source_id
        )
        assert source["attribution"].endswith("; modified: converted and quantized data"), source_id
        assert source["notes"].endswith("Published to the bucket from 2026-11-14."), source_id
    assert "retired on 2027-01-12" in entry("cmems-med")["notes"]


def test_share_alike_is_true_only_where_a_file_states_the_duty():
    flagged = {i for i in OWNER_CONFIRMED_2026_11_12 if entry(i)["share_alike"] is True}
    assert flagged == {"openstreetmap", "protomaps-basemap", "metoffice-ukv"}
    unstated = {"meteofrance-arome", "gshhg", "open-waters-seamap"}
    assert all(entry(i)["share_alike"] is None for i in unstated)
    rest = OWNER_CONFIRMED_2026_11_12 - flagged - unstated
    assert all(entry(i)["share_alike"] is False for i in rest)


def test_the_open_points_are_named_in_the_notes_of_the_three_added_entries():
    assert "No current Météo-France page" in entry("meteofrance-arome")["notes"]
    assert "CC0 1.0" in entry("gshhg")["notes"]
    assert "bathymetry source's terms were not read" in entry("open-waters-seamap")["notes"]


CC_BY = "https://creativecommons.org/licenses/by/4.0/"
MODIFIED = "modified: converted and quantized data"
# One wording per product: the register's attribution is the line Passage's GRIB page shows
# (credit; licence (url); modification clause), and DATA-LICENSES.md records it.
CREDIT_LINES = {
    "ecmwf-open-ifs": (
        "This service is based on data and products of the European Centre for "
        "Medium-Range Weather Forecasts (ECMWF); "
        f"ECMWF open data; CC BY 4.0 ({CC_BY}); {MODIFIED}"
    ),
    "cmems-glo12": (
        f"Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-00016; {MODIFIED}"
    ),
    "cmems-ibi": (
        f"Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-00027; {MODIFIED}"
    ),
    "dwd-icon-eu": f"Deutscher Wetterdienst ICON-EU via Open-Meteo; CC BY 4.0 ({CC_BY}); {MODIFIED}",
    "meteofrance-arome": f"Météo-France AROME via Open-Meteo; CC BY 4.0 ({CC_BY}); {MODIFIED}",
}


def test_ecmwf_and_copernicus_marine_credits_are_the_providers_own_wording():
    assert entry("ecmwf-open-ifs")["attribution"].startswith(
        "This service is based on data and products of the European Centre for Medium-Range Weather Forecasts (ECMWF); "
    )
    for source_id in ("cmems-glo12", "cmems-ibi"):
        assert entry(source_id)["attribution"].startswith(
            "Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-000"
        )


def test_each_derived_tile_credit_has_one_wording_recorded_in_the_licence_notes():
    notes = (ROOT / "DATA-LICENSES.md").read_text()
    for source_id, line in CREDIT_LINES.items():
        source = entry(source_id)
        assert source["attribution"] == line, source_id
        # a value is stated in a repository file cited in recorded_in: the prose authority
        assert line in notes, source_id
        assert "DATA-LICENSES.md" in source["recorded_in"], source_id
    assert "Credit lines for derived tiles" in notes
    assert "12 November 2026" in notes


def test_openmeteo_regional_credits_say_the_data_were_modified():
    # CC BY 4.0 asks for changes to be indicated; UKV's own line already does.
    for source_id in ("dwd-icon-eu", "meteofrance-arome", "metoffice-ukv"):
        assert "odified" in entry(source_id)["attribution"], source_id


def test_noaa_credits_stay_empty_until_wording_is_recorded():
    for source_id in ("noaa-gfs", "noaa-gfs-wave", "noaa-gefs"):
        assert entry(source_id)["attribution"] is None, source_id


def test_schema_accepts_a_yes_answer_with_a_licence_and_a_cited_file():
    # A fixture, not a recorded answer: the register's own UKV entry is a recorded yes.
    doc = copy.deepcopy(REGISTER)
    source = next(s for s in doc["sources"] if s["id"] == "metoffice-ukv")
    source.update(redistribute="yes")
    assert errors_for(doc) == []
    source.update(redistribute="no")
    assert errors_for(doc) == []


def test_ukv_entry_keeps_its_recorded_duties():
    ukv = entry("metoffice-ukv")
    assert ukv["share_alike"] is True
    assert ukv["verified_on"] == "2026-11-12"
    assert ukv["verified_from"] == "https://registry.opendata.aws/met-office-uk-deterministic/"
    assert ukv["redistribute"] == "yes"
    assert ukv["licence"] == "CC BY-SA 4.0"
    assert "DATA-LICENSES.md" in ukv["recorded_in"]
