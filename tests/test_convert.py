"""Scalar conversions are pure, so they are tested without a JVM."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from pp_mcp import convert


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
