"""Scalar conversions are pure, so they are tested without a JVM."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pp_engine_mcp import convert


def test_amount_scaling_is_exact():
    # PP stores money as hundredths; 12_345_67 is 12,345.67
    assert convert.amount(1234567) == Decimal("12345.67")
    assert convert.amount(0) == Decimal("0")
    assert convert.amount(None) is None


def test_quote_and_share_scaling():
    # 192.275 per share, 27 shares
    assert convert.quote(19227500000) == Decimal("192.275")
    assert convert.shares(2700000000) == Decimal("27")


def test_scaling_does_not_go_through_float():
    # 0.07 is not representable in binary floating point; Decimal keeps it exact
    seven_cents = convert.amount(7)
    assert seven_cents == Decimal("0.07")
    assert seven_cents is not None
    assert seven_cents * 100 == Decimal(7)


@pytest.mark.parametrize(
    "value,expected",
    [
        (convert.Money(Decimal("1234.5"), "EUR"), {"amount": 1234.5, "currency": "EUR"}),
        (dt.date(2026, 9, 5), "2026-09-05"),
        ([Decimal("1.5")], [1.5]),
    ],
)
def test_jsonable(value, expected):
    assert convert.jsonable(value) == expected


def _java_security(history, latest):
    """Stand-in for PP's Security; prices are (date, value) pairs, oldest first."""
    def row(day, value):
        return SimpleNamespace(
            getDate=lambda: SimpleNamespace(
                getYear=lambda: day.year, getMonthValue=lambda: day.month, getDayOfMonth=lambda: day.day),
            getValue=lambda: value * 100_000_000, getHigh=lambda: 0, getLow=lambda: 0, getVolume=lambda: 0,
        )

    rows = [row(*p) for p in history]
    return SimpleNamespace(
        getPrices=lambda: SimpleNamespace(size=lambda: len(rows), get=lambda i: rows[i]),
        getLatest=lambda: row(*latest) if latest else None,
    )


OLD, NEW = dt.date(2026, 1, 1), dt.date(2026, 2, 1)


@pytest.mark.parametrize("history, latest, expected", [
    ([(OLD, 1), (NEW, 2)], None, (NEW, 2)),  # history only
    ([(OLD, 1)], (NEW, 3), (NEW, 3)),        # quote is newer
    ([(NEW, 2)], (NEW, 3), (NEW, 3)),        # same day: the quote
    ([(NEW, 2)], (OLD, 3), (NEW, 2)),        # stale quote
    ([], (NEW, 3), (NEW, 3)),
    ([], None, None),
])
def test_newest_price_falls_back_to_history(history, latest, expected):
    found = convert.newest_price(_java_security(history, latest))
    assert (found and (found.date, found.value)) == expected
