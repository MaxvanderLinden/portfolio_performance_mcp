"""Phase 1 write tools, against a private copy of the demo fixture. Need Portfolio Performance.

The demo has daily prices up to 2026-10-30, so early November 2026 is free for new prices.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from pp_mcp import read_tools, staging, write_tools
from pp_mcp.bridge import set_config_provider

FIXTURE = Path(__file__).parent / "fixtures" / "demo.xml"


@pytest.fixture
def area(config, tmp_path, monkeypatch):
    """A private read-only replica, an empty staging folder, and read tools that see staging
    the way they do in pp-write."""
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


# --- add_prices ------------------------------------------------------------------------------


def test_add_prices_stages_and_reads_back(area):
    replica_sha = staging.sha256(area.replica)

    result = write_tools.add_prices("XX0000000001", [
        {"date": "2026-11-03", "value": 101.25},
        {"date": "2026-11-04", "value": "101.5"},
    ])

    assert result["change"]["delta"] == {"prices": 2}
    assert result["change"]["details"]["security"] == "Demo Global World ETF"
    assert staging.sha256(area.replica) == replica_sha

    prices = read_tools.get_prices("XX0000000001", start="2026-11-03", end="2026-11-04")
    history = {row["date"]: row["value"] for row in prices["prices"]}
    assert history["2026-11-03"] == 101.25
    assert history["2026-11-04"] == 101.5


def test_add_prices_never_overwrites(area):
    with pytest.raises(ToolError, match="already has prices on 2026-10-30"):
        write_tools.add_prices("DWLD", [
            {"date": "2026-11-03", "value": 100},
            {"date": "2026-10-30", "value": 100},
        ])
    assert staging.status(area)["staged"] is False


@pytest.mark.parametrize("security, rows, message", [
    ("World", [{"date": "2026-11-03", "value": 1}], "no security is exactly"),
    ("DWLD", [{"date": "2999-01-01", "value": 1}], "in the future"),
    ("DWLD", [{"date": "2026-11-03", "value": 0}], "greater than zero"),
    ("DWLD", [{"date": "2026-11-03", "value": "1.123456789"}], "decimal places"),
    ("DWLD", [{"date": "2026-11-03", "value": 1}, {"date": "2026-11-03", "value": 2}], "more than once"),
    ("DWLD", [{"day": "2026-11-03", "value": 1}], "must look like"),
    ("DWLD", [], "at least one"),
])
def test_add_prices_rejects_bad_input(area, security, rows, message):
    with pytest.raises(ToolError, match=message):
        write_tools.add_prices(security, rows)
    assert staging.status(area)["staged"] is False


# --- record_cash -----------------------------------------------------------------------------


def _balance(name: str) -> float:
    accounts = read_tools.list_accounts()["accounts"]
    return next(a["balance"]["amount"] for a in accounts if a["name"] == name)


def test_record_deposit(area):
    result = write_tools.record_cash("Broker cash", "deposit", 250, "2026-11-03", note="bonus")

    transaction = result["change"]["details"]["transaction"]
    assert transaction["type"] == "DEPOSIT"
    assert transaction["amount"] == {"amount": 250.0, "currency": "EUR"}
    assert transaction["datetime"].startswith("2026-11-03")
    assert transaction["note"] == "bonus"
    assert result["change"]["delta"] == {"account_tx:DEPOSIT": 1}


def test_removal_lowers_the_balance(area):
    before = _balance("Savings")
    write_tools.record_cash("Savings", "REMOVAL", "12.34", "2026-11-03")
    assert _balance("Savings") == pytest.approx(before - 12.34)


def test_duplicates_need_explicit_consent(area):
    write_tools.record_cash("Broker cash", "FEES", 5, "2026-11-03")
    with pytest.raises(ToolError, match="already a FEES of EUR 5.00"):
        write_tools.record_cash("Broker cash", "FEES", 5, "2026-11-03")
    assert staging.status(area)["pending_changes"] == 1

    write_tools.record_cash("Broker cash", "FEES", 5, "2026-11-03", allow_duplicate=True)
    assert staging.status(area)["pending_changes"] == 2


@pytest.mark.parametrize("account, kind, amount, message", [
    ("Broker cash", "BUY", 10, "type must be one of"),
    ("Broker", "DEPOSIT", 10, "no account is exactly"),
    ("Broker cash", "DEPOSIT", -10, "greater than zero"),
    ("Broker cash", "DEPOSIT", "10.001", "decimal places"),
])
def test_record_cash_rejects_bad_input(area, account, kind, amount, message):
    with pytest.raises(ToolError, match=message):
        write_tools.record_cash(account, kind, amount, "2026-11-03")
    assert staging.status(area)["staged"] is False


def test_interest_with_tax_withheld(area):
    result = write_tools.record_cash("Savings", "INTEREST", "7.50", "2026-11-03", tax="2.50")

    transaction = result["change"]["details"]["transaction"]
    assert transaction["amount"] == {"amount": 7.5, "currency": "EUR"}
    assert transaction["taxes"] == {"amount": 2.5, "currency": "EUR"}
    assert transaction["gross"] == {"amount": 10.0, "currency": "EUR"}


def test_fee_linked_to_a_security(area):
    result = write_tools.record_cash("Broker cash", "FEES", 3, "2026-11-03", security="XX0000000001")
    transaction = result["change"]["details"]["transaction"]
    assert transaction["security"] == "Demo Global World ETF"
    assert transaction["security_isin"] == "XX0000000001"


@pytest.mark.parametrize("kind, extra, message", [
    ("DEPOSIT", {"security": "DWLD"}, "cannot be linked to a security"),
    ("FEES", {"tax": 1}, "only be given for INTEREST"),
    ("INTEREST", {"tax": 0}, "greater than zero"),
    ("FEES", {"security": "World"}, "no security is exactly"),
])
def test_record_cash_rejects_bad_links(area, kind, extra, message):
    with pytest.raises(ToolError, match=message):
        write_tools.record_cash("Broker cash", kind, 1, "2026-11-03", **extra)
    assert staging.status(area)["staged"] is False


# --- staging housekeeping --------------------------------------------------------------------


def test_staged_changes_lists_everything_in_order(area):
    write_tools.add_prices("DWLD", [{"date": "2026-11-03", "value": 1}])
    write_tools.record_cash("Broker cash", "INTEREST", 1, "2026-11-03")

    status = write_tools.get_staged_changes()
    assert status["pending_changes"] == 2
    assert [c["delta"] for c in status["changes"]] == [{"prices": 1}, {"account_tx:INTEREST": 1}]
    assert status["up_to_date_with_replica"] and status["unmodified"]


def test_discard_sets_changes_aside_without_deleting(area):
    replica_sha = staging.sha256(area.replica)
    write_tools.record_cash("Broker cash", "DEPOSIT", 1, "2026-11-03")

    result = write_tools.discard_staged()

    assert result["discarded"] and result["changes_set_aside"] == 1
    moved = Path(result["moved_to"])
    assert (moved / staging.STAGED_NAME).exists() and (moved / staging.MANIFEST_NAME).exists()
    assert write_tools.get_staged_changes()["staged"] is False
    assert staging.sha256(area.replica) == replica_sha
    assert write_tools.discard_staged()["discarded"] is False

    # and the next write starts again from the replica
    write_tools.record_cash("Broker cash", "DEPOSIT", 1, "2026-11-03")
    assert write_tools.get_staged_changes()["pending_changes"] == 1
