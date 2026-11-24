"""Turn Portfolio Performance's Java objects into plain Python values.

Two rules here:

* Money, shares and quotes are fixed-point longs in the Java model (factors verified in
  bridge.py). They become `Decimal`, never float, so nothing is lost before the boundary;
  `jsonable()` converts at the edge for tool responses.
* Enum *names* (BUY, DIVIDENDS) are the machine-readable value. `str()` on a PP enum yields the
  user's UI language — "Aankoop" in Dutch — which is fine to show but must never be
  something callers match on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from typing import cast

from . import jtypes as j
from .bridge import AMOUNT_FACTOR, QUOTE_FACTOR, SHARE_FACTOR
from .jsontypes import JsonObject, JsonValue


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:,.2f}"


@dataclass(frozen=True)
class Price:
    date: dt.date
    value: Decimal


@dataclass(frozen=True)
class LatestPrice:
    date: dt.date
    value: Decimal
    high: Decimal | None
    low: Decimal | None
    volume: int | None


@dataclass(frozen=True)
class SecurityInfo:
    uuid: str
    name: str
    isin: str | None
    ticker: str | None
    wkn: str | None
    currency: str | None
    feed: str | None
    retired: bool
    price_count: int
    first_price: dt.date | None
    latest: LatestPrice | None
    note: str | None = None


@dataclass(frozen=True)
class AccountInfo:
    uuid: str
    name: str
    currency: str
    transaction_count: int
    note: str | None = None


@dataclass(frozen=True)
class PortfolioInfo:
    uuid: str
    name: str
    reference_account: str | None
    transaction_count: int
    note: str | None = None


@dataclass(frozen=True)
class TransactionInfo:
    uuid: str
    type: str          # stable enum name, e.g. BUY
    type_label: str    # localised label as shown in the app
    datetime: dt.datetime
    amount: Money
    gross: Money | None
    security: str | None
    security_isin: str | None
    shares: Decimal | None
    fees: Money | None
    taxes: Money | None
    account: str | None = None
    portfolio: str | None = None
    note: str | None = None


# --- scalar conversions (pure, JVM-free, unit-testable) ------------------------------------


def scaled(value: int | None, factor: int) -> Decimal | None:
    if value is None:
        return None
    return Decimal(int(value)) / factor


def amount(value: int | None) -> Decimal | None:
    return scaled(value, AMOUNT_FACTOR)


def shares(value: int | None) -> Decimal | None:
    return scaled(value, SHARE_FACTOR)


def quote(value: int | None) -> Decimal | None:
    return scaled(value, QUOTE_FACTOR)


# --- Java object conversions ----------------------------------------------------------------


def text(value: j.JString | None) -> str | None:
    """Java String (or null) to str. convertStrings=False means these arrive as Java objects."""
    if value is None:
        return None
    result = str(value)
    return result or None


def money(value: j.Money | None) -> Money | None:
    if value is None:
        return None
    return Money(amount=Decimal(int(value.getAmount())) / AMOUNT_FACTOR, currency=str(value.getCurrencyCode()))


def money_of(value: j.Money | None) -> Money:
    """`money()` for a getter that is documented to never return null (e.g. getMonetaryAmount(),
    ClientSnapshot.getMonetaryAssets()). Fails loudly rather than silently if that ever changes."""
    converted = money(value)
    assert converted is not None, "expected a non-null Money from Portfolio Performance"
    return converted


def date(value: j.LocalDate | None) -> dt.date | None:
    if value is None:
        return None
    return dt.date(int(value.getYear()), int(value.getMonthValue()), int(value.getDayOfMonth()))


def date_of(value: j.LocalDate | None) -> dt.date:
    """`date()` for a getter that is documented to never return null."""
    converted = date(value)
    assert converted is not None, "expected a non-null LocalDate from Portfolio Performance"
    return converted


def datetime(value: j.LocalDateTime | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime(
        int(value.getYear()),
        int(value.getMonthValue()),
        int(value.getDayOfMonth()),
        int(value.getHour()),
        int(value.getMinute()),
        int(value.getSecond()),
    )


def datetime_of(value: j.LocalDateTime | None) -> dt.datetime:
    """`datetime()` for a getter that is documented to never return null."""
    converted = datetime(value)
    assert converted is not None, "expected a non-null LocalDateTime from Portfolio Performance"
    return converted


def price(value: j.SecurityPrice) -> Price:
    # A SecurityPrice row always has both fields; the None branches in date()/quote() exist for
    # other callers (e.g. a missing LatestSecurityPrice), not for this one.
    found_date = date(value.getDate())
    found_value = quote(value.getValue())
    assert found_date is not None and found_value is not None
    return Price(date=found_date, value=found_value)


def latest_price(value: j.LatestSecurityPrice | None) -> LatestPrice | None:
    if value is None:
        return None
    volume = int(value.getVolume())
    high, low = int(value.getHigh()), int(value.getLow())
    found_date = date(value.getDate())
    found_value = quote(value.getValue())
    assert found_date is not None and found_value is not None
    return LatestPrice(
        date=found_date,
        value=found_value,
        high=quote(high) if high > 0 else None,
        low=quote(low) if low > 0 else None,
        volume=volume if volume > 0 else None,
    )


def newest_price(value: j.Security) -> LatestPrice | None:
    """The latest quote, or the last historical price when there is no quote or it is older (a
    security without a feed has a price history but no separate quote)."""
    latest = latest_price(value.getLatest())
    prices = value.getPrices()
    if not prices.size():
        return latest
    last = price(prices.get(prices.size() - 1))
    if latest is not None and latest.date >= last.date:
        return latest
    return LatestPrice(date=last.date, value=last.value, high=None, low=None, volume=None)


def security(value: j.Security, *, include_notes: bool = False) -> SecurityInfo:
    prices = value.getPrices()
    return SecurityInfo(
        uuid=str(value.getUUID()),
        name=str(value.getName()),
        isin=text(value.getIsin()),
        ticker=text(value.getTickerSymbol()),
        wkn=text(value.getWkn()),
        currency=text(value.getCurrencyCode()),
        feed=text(value.getFeed()),
        retired=bool(value.isRetired()),
        price_count=prices.size(),
        first_price=date(prices.get(0).getDate()) if prices.size() else None,
        latest=newest_price(value),
        note=text(value.getNote()) if include_notes else None,
    )


def account(value: j.Account, *, include_notes: bool = False) -> AccountInfo:
    return AccountInfo(
        uuid=str(value.getUUID()),
        name=str(value.getName()),
        currency=str(value.getCurrencyCode()),
        transaction_count=value.getTransactions().size(),
        note=text(value.getNote()) if include_notes else None,
    )


def portfolio(value: j.Portfolio, *, include_notes: bool = False) -> PortfolioInfo:
    reference = value.getReferenceAccount()
    return PortfolioInfo(
        uuid=str(value.getUUID()),
        name=str(value.getName()),
        reference_account=str(reference.getName()) if reference is not None else None,
        transaction_count=value.getTransactions().size(),
        note=text(value.getNote()) if include_notes else None,
    )


def _unit_totals(value: j.Transaction) -> tuple[Money | None, Money | None]:
    """Sum the FEE and TAX units attached to a transaction."""
    fees: Money | None = None
    taxes: Money | None = None
    for unit in value.getUnits().toList():
        name = str(unit.getType().name())
        converted = money(unit.getAmount())
        if converted is None:
            continue
        if name == "FEE":
            fees = converted if fees is None else Money(fees.amount + converted.amount, fees.currency)
        elif name == "TAX":
            taxes = converted if taxes is None else Money(taxes.amount + converted.amount, taxes.currency)
    return fees, taxes


def transaction(
    value: j.Transaction,
    *,
    account_name: str | None = None,
    portfolio_name: str | None = None,
    include_notes: bool = False,
) -> TransactionInfo:
    """Convert an AccountTransaction or PortfolioTransaction (their getters overlap)."""
    held = value.getSecurity()
    fees, taxes = _unit_totals(value)
    share_count = shares(int(value.getShares()))

    # Every transaction has a date and a monetary amount; the None branches in datetime()/money()
    # exist for genuinely optional fields elsewhere (e.g. a transaction with no linked security).
    found_datetime = datetime(value.getDateTime())
    found_amount = money(value.getMonetaryAmount())
    assert found_datetime is not None and found_amount is not None

    return TransactionInfo(
        uuid=str(value.getUUID()),
        type=str(value.getType().name()),
        type_label=str(value.getType()),
        datetime=found_datetime,
        amount=found_amount,
        gross=money(value.getGrossValue()),
        security=str(held.getName()) if held is not None else None,
        security_isin=text(held.getIsin()) if held is not None else None,
        shares=share_count if share_count else None,
        fees=fees,
        taxes=taxes,
        account=account_name,
        portfolio=portfolio_name,
        note=text(value.getNote()) if include_notes else None,
    )


# --- boundary -------------------------------------------------------------------------------


def jsonable(value: object) -> JsonValue:
    """Recursively convert dataclasses, Decimals and dates for JSON tool responses.

    Anything else that is not already JSON is a bug (a Java object leaking out), so it raises
    rather than passing through."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {str(key): jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return [jsonable(item) for item in items]
    raise TypeError(f"cannot represent {type(value).__name__} as JSON")


def json_object(value: object) -> JsonObject:
    """`jsonable()` for something that must become a JSON object (a dataclass or a mapping)."""
    converted = jsonable(value)
    if not isinstance(converted, dict):
        raise TypeError(f"expected a JSON object, got {type(converted).__name__}")
    return converted
