"""Tool-level tests against the demo fixture (tests/fixtures/demo.xml)."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from pp_mcp import read_tools as rt


@pytest.fixture(autouse=True)
def _loaded(handle):
    """Every tool goes through the shared client; loading it here keeps the JVM on one thread."""
    return handle


def test_every_tool_response_is_json_serialisable():
    for payload in (
        rt.list_securities(),
        rt.list_accounts(),
        rt.list_portfolios(),
        rt.get_holdings(),
        rt.list_taxonomies(),
    ):
        json.dumps(payload)  # no Decimal, date or Java object may escape


def test_securities_exclude_notes_and_retired_by_default():
    listed = rt.list_securities()
    assert listed["count"] == len(listed["securities"])
    assert all(s["note"] is None for s in listed["securities"])
    assert listed["count"] <= rt.list_securities(include_retired=True)["count"]


def test_holdings_shares_sum_to_one():
    holdings = rt.get_holdings()
    assert holdings["count"] > 0
    assert sum(h["share_of_total"] for h in holdings["holdings"]) == pytest.approx(1.0, abs=1e-4)


def test_holdings_reconcile_with_accounts_and_portfolios():
    total = rt.get_holdings()["total_assets"]["amount"]
    cash = sum(a["balance"]["amount"] for a in rt.list_accounts()["accounts"])
    invested = sum(p["value"]["amount"] for p in rt.list_portfolios()["portfolios"])
    assert cash + invested == pytest.approx(total, abs=0.01)


def test_transaction_type_filter():
    dividends = rt.get_transactions(type="dividends", limit=5)
    assert dividends["total_matching"] > 0
    assert {t["type"] for t in dividends["transactions"]} == {"DIVIDENDS"}
    assert dividends["truncated"] is (dividends["total_matching"] > 5)


def test_transaction_date_range_is_inclusive():
    everything = rt.get_transactions(limit=1000)
    oldest = min(t["datetime"][:10] for t in everything["transactions"])
    window = rt.get_transactions(start=oldest, end=oldest, limit=10)
    assert window["total_matching"] >= 1
    assert all(t["datetime"][:10] == oldest for t in window["transactions"])


def test_transactions_are_capped_and_say_so():
    capped = rt.get_transactions(limit=3)
    assert capped["returned"] == 3
    assert capped["truncated"] is True
    assert "of" in capped["showing"]


def test_prices_include_the_latest_quote():
    listed = rt.list_securities()["securities"]
    with_latest = next(s for s in listed if s["latest"])
    prices = rt.get_prices(with_latest["isin"] or with_latest["name"], limit=1)
    assert prices["summary"]["last"]["date"] == with_latest["latest"]["date"]
    assert prices["summary"]["count"] >= prices["returned"]


def test_price_range_filters():
    prices = rt.get_prices("XX0000000001", start="2026-01-01", end="2026-01-31")
    assert prices["summary"]["count"] > 0
    assert all(row["date"].startswith("2026-01") for row in prices["prices"])


def test_unknown_security_is_an_error_not_a_guess():
    with pytest.raises(ToolError, match="no security matches"):
        rt.get_prices("definitely not a security")


def test_ambiguous_security_lists_candidates():
    with pytest.raises(ToolError, match="matches several"):
        rt.get_prices("world")


def test_bad_date_is_rejected():
    with pytest.raises(ToolError, match="ISO date"):
        rt.get_transactions(start="01-2026")


def test_allocation_reconciles_with_total_assets():
    allocation = rt.get_allocation("Regions")
    assert allocation["classified"] + allocation["unassigned"] == pytest.approx(
        allocation["total_assets"], abs=0.01
    )
    assert allocation["allocation"]
    assert "_exact" not in json.dumps(allocation)


def test_allocation_needs_an_unambiguous_taxonomy():
    with pytest.raises(ToolError, match="no unique taxonomy"):
        rt.get_allocation("does not exist")
