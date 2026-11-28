"""Phase 3 write tools against a private copy of the demo fixture. Need Portfolio Performance.

Demo facts used here: one taxonomy, "Regions" (id ed06b453-…), with "Developed markets"
(id developed) holding "Europe" and "North America", and "Emerging markets". DWLD is spread
10/20/70% over Developed/Europe/North America, so it is fully assigned; EXMP and DEMW are 100%
in one region; DUSC and every account are unassigned.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from pp_engine_mcp import classification_tools as ct
from pp_engine_mcp import read_tools, staging
from pp_engine_mcp.bridge import set_config_provider

FIXTURE = Path(__file__).parent / "fixtures" / "demo.xml"


@pytest.fixture
def area(config, tmp_path, monkeypatch):
    replica = tmp_path / "replica" / "portfolio.xml"
    replica.parent.mkdir()
    shutil.copyfile(FIXTURE, replica)
    replica.chmod(0o444)
    monkeypatch.setenv("PP_FILE", str(replica))
    monkeypatch.setenv("PP_STAGED_DIR", str(tmp_path / "staged"))
    area = staging.staging_area()
    set_config_provider(lambda: staging.read_config(area))
    yield area
    set_config_provider(None)


def _nothing_staged(area) -> bool:
    return staging.status(area)["staged"] is False


def _unassigned() -> float:
    return read_tools.get_allocation("Regions")["unassigned"]


# --- add_classification ----------------------------------------------------------------------


def test_add_top_level_classification(area):
    result = ct.add_classification("Regions", "Frontier markets")

    details = result["change"]["details"]
    assert result["change"]["delta"] == {"classifications": 1}
    assert details["classification"]["path"] == "Frontier markets"
    assert read_tools.list_taxonomies()["taxonomies"][0]["classifications"] == 6


def test_add_nested_classification_by_path(area):
    result = ct.add_classification("regions", "Asia Pacific", parent="Developed markets » ")
    assert result["change"]["details"]["classification"]["path"] == "Developed markets » Asia Pacific"

    nested = ct.add_classification("Regions", "Japan", parent="Developed markets / Asia Pacific")
    assert nested["change"]["details"]["classification"]["path"] == "Developed markets » Asia Pacific » Japan"


def test_new_classification_takes_the_weight_its_siblings_leave(area):
    # Emerging markets has no children yet, so a first child gets the full target weight.
    first = ct.add_classification("Regions", "China", parent="emerging")
    assert first["change"]["details"]["classification"]["target_weight_percent"] == 100.0
    second = ct.add_classification("Regions", "India", parent="emerging")
    assert second["change"]["details"]["classification"]["target_weight_percent"] == 0.0


@pytest.mark.parametrize("taxonomy, name, parent, message", [
    ("Regions", "Europe", "Developed markets", "already exists"),
    ("Regions", "europe", "developed", "already exists"),
    ("Regio", "Asia", None, "no taxonomy is exactly"),
    ("Regions", "Asia", "Asia", "no classification in Regions"),
    ("Regions", " ", None, "must not be empty"),
    ("Regions", "Asia/Pacific", None, "must not contain"),
])
def test_add_classification_refusals(area, taxonomy, name, parent, message):
    with pytest.raises(ToolError, match=message):
        ct.add_classification(taxonomy, name, parent=parent)
    assert _nothing_staged(area)


# --- assign_classification -------------------------------------------------------------------


def test_assign_security_fills_unassigned(area):
    before = _unassigned()
    result = ct.assign_classification("Regions", "North America", security="DUSC")

    details = result["change"]["details"]
    assert result["change"]["delta"] == {"assignments": 1}
    assert details["classification"] == "Developed markets » North America"
    assert details["security"] == "Demo US Corp"
    assert details["weight_percent"] == 100.0
    assert _unassigned() < before


def test_assign_account_split_over_two_regions(area):
    ct.assign_classification("Regions", "Europe", account="Savings", weight="60")
    result = ct.assign_classification("Regions", "emerging", account="Savings", weight=40)
    assert result["change"]["details"]["assigned_in_taxonomy_percent"] == 100.0
    assert staging.status(area)["pending_changes"] == 2

    with pytest.raises(ToolError, match="would exceed 100%"):
        ct.assign_classification("Regions", "North America", account="Savings", weight="0.01%")


def test_assign_to_a_new_classification(area):
    ct.add_classification("Regions", "Frontier markets")
    result = ct.assign_classification("Regions", "Frontier markets", security="DUSC", weight="12.5")
    assert result["change"]["details"]["weight_percent"] == 12.5


@pytest.mark.parametrize("classification, kwargs, message", [
    ("North America", {"security": "DWLD", "weight": "1%"}, "already is in|already in"),
    ("Emerging markets", {"security": "DWLD", "weight": "1%"}, "would exceed 100%"),
    ("Regions", {"security": "DUSC"}, "no classification in Regions"),
    ("Mars", {"security": "DUSC"}, "no classification in Regions"),
    ("Europe", {"security": "DUSC", "account": "Savings"}, "exactly one of security or account"),
    ("Europe", {}, "exactly one of security or account"),
    ("Europe", {"security": "DUSC", "weight": 101}, "cannot exceed 100"),
    ("Europe", {"security": "DUSC", "weight": 0.15}, r'write "0.15%" for 0.15 percent, or 15.00 if'),
    ("Europe", {"security": "DUSC", "weight": 1}, 'write "1%" for 1 percent, or 100 if'),
    ("Europe", {"security": "DUSC", "weight": "101%"}, "cannot exceed 100"),
    ("Europe", {"security": "DUSC", "weight": "%"}, "must be a number"),
    ("Europe", {"security": "DUSC", "weight": 0}, "greater than zero"),
    ("Europe", {"security": "DUSC", "weight": "33.333"}, "decimal places"),
    ("Europe", {"security": "Demo"}, "no security is exactly"),
    ("Europe", {"account": "Broker"}, "no account is exactly"),
])
def test_assign_refusals(area, classification, kwargs, message):
    with pytest.raises(ToolError, match=message):
        ct.assign_classification("Regions", classification, **kwargs)
    assert _nothing_staged(area)


@pytest.mark.parametrize("weight, percent", [("15%", 15.0), (" 0.5 % ", 0.5), (1.5, 1.5), ("1%", 1.0)])
def test_weight_spellings(area, weight, percent):
    result = ct.assign_classification("Regions", "Europe", security="DUSC", weight=weight)
    assert result["change"]["details"]["weight_percent"] == percent


def test_classification_found_by_id_and_full_path(area):
    by_id = ct.assign_classification("Regions", "developed", security="DUSC", weight=50)
    assert by_id["change"]["details"]["classification"] == "Developed markets"
    by_path = ct.assign_classification("Regions", "Regions > Developed markets > Europe", security="DUSC", weight=50)
    assert by_path["change"]["details"]["classification"] == "Developed markets » Europe"
