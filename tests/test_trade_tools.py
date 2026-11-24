"""Phase 2 write tools against a private copy of the demo fixture. Need Portfolio Performance.

Demo facts used here: portfolio "Broker" settles on "Broker cash" (EUR) and buys DWLD monthly;
"Pension" holds 60 DWLD and 40 DEMW since 2024-01-15; EXMP was bought 2023 and sold out on
2025-03-10; DUSC trades in USD, held in "US broker" on "US cash"; RTRD was never held.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from pp_engine_mcp import analytics_tools, read_tools, staging, trade_tools
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


def _balance(name: str) -> float:
    return next(a["balance"]["amount"] for a in read_tools.list_accounts()["accounts"] if a["name"] == name)


def _nothing_staged(area) -> bool:
    return staging.status(area)["staged"] is False


# --- add_security ----------------------------------------------------------------------------


def test_add_security_then_buy_it(area):
    added = trade_tools.add_security("Demo Bond Fund", "eur", isin="XX0000000009", ticker="DBND")
    assert added["change"]["delta"] == {"securities": 1}
    assert added["change"]["details"]["security"]["isin"] == "XX0000000009"
    assert added["change"]["details"]["security"]["currency"] == "EUR"

    bought = trade_tools.record_buy("Broker", "DBND", 10, "2026-11-03", price=50)
    assert bought["change"]["details"]["transaction"]["security"] == "Demo Bond Fund"


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "Other", "currency": "EUR", "isin": "XX0000000001"}, "already has this ISIN"),
    ({"name": "Demo Global World ETF", "currency": "EUR"}, "already has this name"),
    ({"name": "Other", "currency": "EUR", "ticker": "dwld"}, "already has this ticker"),
    ({"name": "Other", "currency": "XYZ"}, "not a currency"),
    ({"name": "Other", "currency": "EUR", "isin": "NOTANISIN"}, "12 characters"),
    ({"name": "  ", "currency": "EUR"}, "must not be empty"),
])
def test_add_security_refusals(area, kwargs, message):
    with pytest.raises(ToolError, match=message):
        trade_tools.add_security(**kwargs)
    assert _nothing_staged(area)


# --- buy / sell ------------------------------------------------------------------------------


def test_buy_by_price_books_gross_plus_costs(area):
    before = _balance("Broker cash")
    result = trade_tools.record_buy("Broker", "DWLD", 10, "2026-11-03", price="100", fees="1.50", taxes="0.25")

    assert result["change"]["delta"] == {"portfolio_tx:BUY": 1, "account_tx:BUY": 1}
    row = result["change"]["details"]["transaction"]
    assert row["amount"]["amount"] == 1001.75
    assert row["gross"]["amount"] == 1000.0
    assert row["fees"]["amount"] == 1.5 and row["taxes"]["amount"] == 0.25
    assert (row["account"], row["portfolio"], row["shares"]) == ("Broker cash", "Broker", 10.0)
    assert _balance("Broker cash") == pytest.approx(before - 1001.75)

    listed = read_tools.get_transactions(type="BUY", start="2026-11-03", end="2026-11-03")
    assert listed["total_matching"] == 1, "one trade, one row"


def test_buy_by_total_derives_the_gross(area):
    row = trade_tools.record_buy("Broker", "DWLD", 5, "2026-11-03", total=500, fees=2)["change"]["details"]["transaction"]
    assert row["amount"]["amount"] == 500.0
    assert row["gross"]["amount"] == 498.0


def test_sell_by_price_books_gross_minus_costs(area):
    before = _balance("Broker cash")
    row = trade_tools.record_sell("Broker", "DWLD", 5, "2026-11-03", price=100, fees=1)["change"]["details"]["transaction"]
    assert row["type"] == "SELL"
    assert row["amount"]["amount"] == 499.0
    assert _balance("Broker cash") == pytest.approx(before + 499.0)


def test_foreign_currency_buy_needs_and_stores_a_rate(area):
    with pytest.raises(ToolError, match="pass exchange_rate as EUR per 1 USD"):
        trade_tools.record_buy("Broker", "DUSC", 10, "2026-11-03", price=50)
    with pytest.raises(ToolError, match="only for a security in another currency"):
        trade_tools.record_buy("Broker", "DWLD", 10, "2026-11-03", price=50, exchange_rate="0.9")

    row = trade_tools.record_buy(
        "Broker", "DUSC", 10, "2026-11-03", price=50, fees=1, exchange_rate="0.9"
    )["change"]["details"]["transaction"]
    assert row["amount"] == {"amount": 451.0, "currency": "EUR"}  # 10 x USD 50 x 0.9 + 1
    assert row["gross"]["amount"] == 450.0

    by_total = trade_tools.record_buy(
        "Broker", "DUSC", 3, "2026-11-04", total="137.77", exchange_rate="0.9183"
    )["change"]["details"]["transaction"]
    assert by_total["amount"]["amount"] == 137.77


@pytest.mark.parametrize("kwargs, message", [
    ({}, "exactly one of price"),
    ({"price": 1, "total": 1}, "exactly one of price"),
    ({"total": 5, "fees": 5}, "as large as the total"),
    ({"price": 0}, "greater than zero"),
    ({"price": 1, "fees": -1}, "zero or more"),
])
def test_buy_amount_refusals(area, kwargs, message):
    with pytest.raises(ToolError, match=message):
        trade_tools.record_buy("Broker", "DWLD", 1, "2026-11-03", **kwargs)
    assert _nothing_staged(area)


def test_cannot_sell_more_than_held(area):
    with pytest.raises(ToolError, match="cannot take out 100000"):
        trade_tools.record_sell("Broker", "DWLD", 100000, "2026-11-03", price=1)
    with pytest.raises(ToolError, match="holds 0 of Example Corp"):
        trade_tools.record_sell("Broker", "EXMP", 1, "2026-11-03", price=1)
    assert _nothing_staged(area)


def test_back_dated_sale_cannot_break_a_later_one(area):
    """20 EXMP were held on 2024-01-01, but all 20 are sold on 2025-03-10."""
    with pytest.raises(ToolError, match="as few as 0 later"):
        trade_tools.record_sell("Broker", "EXMP", 20, "2024-01-01", price=100)
    assert _nothing_staged(area)


def test_duplicate_trades_need_consent(area):
    trade_tools.record_buy("Broker", "DWLD", 1, "2026-11-03", price=100)
    with pytest.raises(ToolError, match="already a BUY of 1 Demo Global World ETF"):
        trade_tools.record_buy("Broker", "DWLD", 1, "2026-11-03", price=101)
    trade_tools.record_buy("Broker", "DWLD", 1, "2026-11-03", price=101, allow_duplicate=True)
    assert staging.status(area)["pending_changes"] == 2


# --- dividends -------------------------------------------------------------------------------


def test_dividend_defaults_to_shares_held_everywhere(area, handle):
    from pp_engine_mcp.write_common import held

    world = next(s for s in handle.client.getSecurities() if str(s.getTickerSymbol()) == "DWLD")
    expected = held(handle.client.getPortfolios(), world, dt.date(2026, 11, 3))
    assert expected > 0

    row = trade_tools.record_dividend(
        "Broker cash", "DWLD", 85, "2026-11-03", tax=15
    )["change"]["details"]["transaction"]
    assert row["type"] == "DIVIDENDS"
    assert row["amount"]["amount"] == 85.0 and row["taxes"]["amount"] == 15.0
    assert row["gross"]["amount"] == 100.0
    assert row["shares"] == expected / 100_000_000

    with pytest.raises(ToolError, match="already a dividend of Demo Global World ETF"):
        trade_tools.record_dividend("Broker cash", "DWLD", 1, "2026-11-03")


def test_dividend_on_something_never_held_needs_shares(area):
    with pytest.raises(ToolError, match="no portfolio holds Retired Demo Fund"):
        trade_tools.record_dividend("Broker cash", "RTRD", 5, "2026-11-03")
    row = trade_tools.record_dividend("Broker cash", "RTRD", 5, "2026-11-03", shares=10)["change"]["details"]["transaction"]
    assert row["shares"] == 10.0


def test_foreign_dividend_with_rate(area):
    row = trade_tools.record_dividend(
        "Broker cash", "DUSC", 17, "2026-11-03", tax=3, shares=40, exchange_rate="0.9"
    )["change"]["details"]["transaction"]
    assert row["gross"]["amount"] == 20.0


# --- transfers -------------------------------------------------------------------------------


def test_cash_transfer_moves_money_both_ways(area):
    broker, savings = _balance("Broker cash"), _balance("Savings")
    result = trade_tools.record_cash_transfer("Broker cash", "Savings", 250, "2026-11-03")
    assert result["change"]["delta"] == {"account_tx:TRANSFER_IN": 1, "account_tx:TRANSFER_OUT": 1}
    assert _balance("Broker cash") == pytest.approx(broker - 250)
    assert _balance("Savings") == pytest.approx(savings + 250)

    with pytest.raises(ToolError, match="already a transfer of EUR 250.00"):
        trade_tools.record_cash_transfer("Broker cash", "Savings", 250, "2026-11-03")


@pytest.mark.parametrize("source, target, message", [
    ("Savings", "Savings", "the same account"),
    ("US cash", "Savings", "not supported yet"),
])
def test_cash_transfer_refusals(area, source, target, message):
    with pytest.raises(ToolError, match=message):
        trade_tools.record_cash_transfer(source, target, 1, "2026-11-03")
    assert _nothing_staged(area)


def test_share_transfer_between_portfolios(area):
    result = trade_tools.record_share_transfer("DWLD", 10, "Pension", "Broker", "2026-10-30")
    assert result["change"]["delta"] == {"portfolio_tx:TRANSFER_IN": 1, "portfolio_tx:TRANSFER_OUT": 1}
    assert result["change"]["details"]["out"]["portfolio"] == "Pension"
    assert result["change"]["details"]["in"]["portfolio"] == "Broker"

    with pytest.raises(ToolError, match="cannot take out 51"):
        trade_tools.record_share_transfer("DWLD", 51, "Pension", "Broker", "2026-10-31")


# --- deliveries ------------------------------------------------------------------------------


def test_inbound_then_outbound_delivery(area):
    inbound = trade_tools.record_delivery("Pension", "DEMW", 3, "2026-11-03", "inbound", price=30, fees=1)
    row = inbound["change"]["details"]["transaction"]
    assert row["type"] == "DELIVERY_INBOUND" and row["amount"]["amount"] == 91.0

    with pytest.raises(ToolError, match="cannot take out 44"):
        trade_tools.record_delivery("Pension", "DEMW", 44, "2026-11-03", "out", total=1)
    outbound = trade_tools.record_delivery("Pension", "DEMW", 43, "2026-11-04", "out", total=1000)
    assert outbound["change"]["delta"] == {"portfolio_tx:DELIVERY_OUTBOUND": 1}


def test_delivery_direction_is_checked(area):
    with pytest.raises(ToolError, match="inbound or outbound"):
        trade_tools.record_delivery("Pension", "DEMW", 1, "2026-11-03", "sideways", price=1)


# --- the staged file stays a valid portfolio -------------------------------------------------


def test_everything_together_still_computes(area):
    trade_tools.add_security("Demo Bond Fund", "EUR", isin="XX0000000009")
    trade_tools.record_buy("Broker", "XX0000000009", 10, "2026-11-03", price=50)
    trade_tools.record_buy("Broker", "DUSC", 10, "2026-11-03", price=50, exchange_rate="0.9")
    trade_tools.record_sell("Broker", "DWLD", 5, "2026-11-03", price=100)
    trade_tools.record_dividend("Broker cash", "DWLD", 10, "2026-11-03", tax=2)
    trade_tools.record_cash_transfer("Savings", "Broker cash", 100, "2026-11-03")
    trade_tools.record_share_transfer("DWLD", 1, "Pension", "Broker", "2026-10-30")
    trade_tools.record_delivery("Pension", "DEMW", 1, "2026-11-03", "inbound", price=30)
    assert staging.status(area)["pending_changes"] == 8

    # Reload the staged file and run PP's own calculations over it.
    assert read_tools.get_holdings()["count"] > 0
    performance = analytics_tools.get_security_performance(period="YTD", include_closed=True)
    assert any(row["security"] == "Demo Bond Fund" for row in performance["securities"])
    assert analytics_tools.get_performance(period="YTD")["ttwror"] is not None
