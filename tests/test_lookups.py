"""Lookups and input checks that need no JVM, with stand-ins for PP's objects."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from pp_engine_mcp import read_tools, trade_tools
from pp_engine_mcp import write_common as w

SHARE = 10**8


def _item(uuid, name, ticker=None):
    return SimpleNamespace(
        getUUID=lambda: uuid, getId=lambda: uuid, getName=lambda: name,
        getIsin=lambda: None, getTickerSymbol=lambda: ticker,
    )


def test_a_shared_ticker_is_an_error_not_a_guess():
    items = [_item("1", "Alpha", "ABC"), _item("2", "Beta", "ABC"), _item("3", "ABC Fund", "X")]
    handle = SimpleNamespace(client=SimpleNamespace(getSecurities=lambda: items))
    with pytest.raises(ToolError, match="Alpha .*Beta"):
        read_tools.resolve_security(handle, "ABC")


def test_owners_resolve_by_uuid_and_exact_name_before_fragment():
    items = [_item("u-1", "Broker"), _item("u-2", "Broker cash")]
    assert read_tools.resolve_owner(items, "u-2", "account") is items[1]
    assert read_tools.resolve_owner(items, "broker", "account") is items[0]
    assert read_tools.resolve_owner(items, "cash", "account") is items[1]
    with pytest.raises(ToolError, match="must not be empty"):
        read_tools.resolve_owner(items[:1], " ", "account")


def test_exact_taxonomy_name_beats_a_fragment():
    items = [_item("t-1", "Regions"), _item("t-2", "Regions (MSCI)")]
    client = SimpleNamespace(getTaxonomies=lambda: items)
    assert read_tools.resolve_taxonomy(client, "Regions") is items[0]


@pytest.mark.parametrize("value", ["1e400", "1e30", "1e15", 10**11])
def test_absurd_numbers_are_refused(value):
    with pytest.raises(ToolError, match="too large"):
        w.cents(value, "amount")


def test_extra_precision_is_refused_at_any_length():
    for value in ("1.000000001", "1.00000000000000000000000000000000001", "1e-400"):
        with pytest.raises(ToolError, match="decimal places"):
            w.shares(value)
    assert w.shares("1.50000000000000000") == 150_000_000


@pytest.mark.parametrize("price, rate, message", [
    ("1e10", None, "too large"),
    ("0.00001", None, "rounds to zero"),
])
def test_derived_amounts_are_bounded(price, rate, message):
    shares = SHARE * 1000 if message == "too large" else SHARE
    with pytest.raises(ToolError, match=message):
        trade_tools._amounts(adds_shares=True, shares_raw=shares, price=price, total=None, fees=0, taxes=0, rate=rate)


def test_only_day_ends_count_for_later_holdings(monkeypatch):
    first, later = dt.date(2026, 1, 1), dt.date(2026, 2, 1)
    events = sorted([(first, 10 * SHARE), (later, 10 * SHARE), (later, -10 * SHARE)])
    monkeypatch.setattr(w, "_deltas", lambda portfolio, security: events)
    portfolio = _item("p", "Broker")
    w.require_shares(portfolio, _item("s", "Fund"), 10 * SHARE, dt.date(2026, 1, 15))
    with pytest.raises(ToolError, match="cannot take out 11"):
        w.require_shares(portfolio, _item("s", "Fund"), 11 * SHARE, dt.date(2026, 1, 15))
