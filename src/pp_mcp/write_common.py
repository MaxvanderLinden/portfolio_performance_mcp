"""Helpers shared by the write tools: input parsing, exact lookups, holdings checks, currency.

Only imported by the write server (through write_tools, trade_tools and classification_tools).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TypeVar

import jpype
from fastmcp.exceptions import ToolError

from . import convert, staging
from . import jtypes as j
from .bridge import AMOUNT_FACTOR, SHARE_FACTOR
from .jsontypes import JsonObject

# Far beyond any real portfolio, and well within what PP's 64-bit fixed-point fields can hold.
MAX_NUMBER = Decimal(10) ** 10

OwnerT = TypeVar("OwnerT", bound=j.Identified)

# Portfolio transaction types that add shares; every other type removes them.
SHARES_IN = frozenset({"BUY", "TRANSFER_IN", "DELIVERY_INBOUND"})


def area() -> staging.StagingArea:
    return staging.staging_area()


def staged(
    description: str, expected: dict[str, int], mutate: Callable[[j.Client], JsonObject]
) -> staging.ApplyResult:
    try:
        return staging.apply(area(), description, expected, mutate)
    except staging.StagingError as exc:
        raise ToolError(str(exc)) from exc


# --- input parsing ---------------------------------------------------------------------------


def past_date(value: object, field: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date like 2026-01-31, got {value!r}") from exc
    if parsed > dt.date.today():
        raise ToolError(f"{field} {parsed} is in the future; only past and current dates can be recorded")
    return parsed


def decimal(value: object, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ToolError(f"{field} must be a number, got {value!r}") from exc
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise ToolError(f"{field} must be {'zero or more' if allow_zero else 'greater than zero'}, got {value!r}")
    if number > MAX_NUMBER:
        raise ToolError(f"{field} is too large: {value!r}")
    return number


def fixed_point(value: object, factor: int, field: str, places: int, *, allow_zero: bool = False) -> int:
    """A decimal as PP's fixed-point long. Rejects more precision than PP stores, rather than
    rounding it away silently."""
    number = decimal(value, field, allow_zero=allow_zero)
    if number != round(number, places):
        raise ToolError(f"{field} has more than {places} decimal places: {value!r}")
    return int(number * factor)


def cents(value: object, field: str) -> int:
    return fixed_point(value, AMOUNT_FACTOR, field, 2)


def optional_cents(value: object | None, field: str) -> int:
    """Fees and taxes: absent means zero."""
    return 0 if value is None else fixed_point(value, AMOUNT_FACTOR, field, 2, allow_zero=True)


def shares(value: object, field: str = "shares") -> int:
    return fixed_point(value, SHARE_FACTOR, field, 8)


def round_cents(amount: Decimal) -> int:
    if amount > MAX_NUMBER:
        raise ToolError("the resulting amount is too large; check shares, price and exchange_rate")
    return int((amount * AMOUNT_FACTOR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money_text(amount_cents: int, currency: object) -> str:
    return f"{currency} {Decimal(amount_cents) / AMOUNT_FACTOR:,.2f}"


def shares_text(raw: int) -> str:
    return f"{(Decimal(raw) / SHARE_FACTOR).normalize():f}"


def note_text(note: str | None) -> str | None:
    return (note or "").strip() or None


# --- exact lookups (no fragments for writes) -------------------------------------------------

SECURITY_KEYS: tuple[Callable[[j.Security], j.JString | None], ...] = (
    lambda s: s.getUUID(),
    lambda s: s.getIsin(),
    lambda s: s.getTickerSymbol(),
    lambda s: s.getName(),
)
OWNER_KEYS: tuple[Callable[[j.Identified], j.JString], ...] = (
    lambda o: o.getUUID(),
    lambda o: o.getName(),
)


def exact_security(client: j.Client, query: str) -> j.Security:
    """UUID, ISIN, ticker or full name, exactly (case-insensitive)."""
    needle = str(query).strip().casefold()
    if not needle:
        raise ToolError("security must not be empty")
    securities = list(client.getSecurities())
    for key in SECURITY_KEYS:
        found = [s for s in securities if (convert.text(key(s)) or "").casefold() == needle]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ToolError(f"{query!r} matches {len(found)} securities; use the UUID instead")
    raise ToolError(
        f"no security is exactly {query!r} (UUID, ISIN, ticker or full name). "
        "Use list_securities to find it, or add_security to create it."
    )


def _exact_owner(items: list[OwnerT], query: str, kind: str) -> OwnerT:
    needle = str(query).strip().casefold()
    for key in OWNER_KEYS:
        found = [item for item in items if str(key(item)).casefold() == needle]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ToolError(f"{query!r} matches {len(found)} {kind}s; use the UUID instead")
    names = ", ".join(sorted(str(item.getName()) for item in items))
    raise ToolError(f"no {kind} is exactly {query!r}. {kind.capitalize()}s: {names}")


def exact_account(client: j.Client, query: str) -> j.Account:
    return _exact_owner(list(client.getAccounts()), query, "account")


def exact_portfolio(client: j.Client, query: str) -> j.Portfolio:
    return _exact_owner(list(client.getPortfolios()), query, "portfolio")


def same(a: j.Identified | None, b: j.Identified | None) -> bool:
    return a is not None and b is not None and str(a.getUUID()) == str(b.getUUID())


def same_id(a: j.Classification, b: j.Classification) -> bool:
    return str(a.getId()) == str(b.getId())


# --- Java values -----------------------------------------------------------------------------


def java_datetime(day: dt.date) -> j.LocalDateTime:
    return j.local_date_time().of(day.year, day.month, day.day, 0, 0)


def day_of(transaction: j.Transaction) -> dt.date:
    return convert.date_of(transaction.getDateTime().toLocalDate())


def unit(kind: str, amount_cents: int, currency: j.JString | str) -> j.Unit:
    return j.unit()(j.unit_type().valueOf(kind), j.money().of(currency, amount_cents))


def gross_value_unit(
    gross_cents: int, account_currency: j.JString, forex_cents: int, security_currency: j.JString, rate: Decimal
) -> j.Unit:
    """The GROSS_VALUE unit PP attaches when a security trades in another currency than the
    account: the gross in both currencies and the rate between them."""
    try:
        return j.unit()(
            j.unit_type().valueOf("GROSS_VALUE"),
            j.money().of(account_currency, gross_cents),
            j.money().of(security_currency, forex_cents),
            j.big_decimal()(str(rate)),
        )
    except jpype.JException as exc:
        raise ToolError(f"Portfolio Performance rejected the currency conversion: {exc.message()}") from exc


def exchange_rate(
    value: object | None, account_currency: j.JString, security: j.Security, *, required: bool
) -> Decimal | None:
    """`value` is units of the account currency per one unit of the security's currency."""
    security_currency = str(security.getCurrencyCode() or account_currency)
    if security_currency == str(account_currency):
        if value is not None:
            raise ToolError(
                f"exchange_rate is only for a security in another currency; {security.getName()} "
                f"and the account are both in {account_currency}"
            )
        return None
    if value is None:
        if required:
            raise ToolError(
                f"{security.getName()} is in {security_currency} and the account in {account_currency}: "
                f"pass exchange_rate as {account_currency} per 1 {security_currency}"
            )
        return None
    return decimal(value, "exchange_rate")


# --- holdings --------------------------------------------------------------------------------


def _deltas(portfolio: j.Portfolio, security: j.Security) -> list[tuple[dt.date, int]]:
    events: list[tuple[dt.date, int]] = []
    for transaction in portfolio.getTransactions():
        if not same(transaction.getSecurity(), security):
            continue
        sign = 1 if str(transaction.getType().name()) in SHARES_IN else -1
        events.append((day_of(transaction), sign * int(transaction.getShares())))
    return sorted(events)


def held(portfolios: Iterable[j.Portfolio], security: j.Security, on: dt.date) -> int:
    """Raw shares (×10⁸) held at the end of `on`, across the given portfolios."""
    return sum(delta for p in portfolios for day, delta in _deltas(p, security) if day <= on)


def require_shares(portfolio: j.Portfolio, security: j.Security, wanted: int, on: dt.date) -> None:
    """Refuse to take out more shares than the portfolio holds on `on`, or on any later date
    (a back-dated sale must not make a later one go negative)."""
    # Net per day: the order of trades within a day is not recorded, so only day ends count.
    by_day: Counter[dt.date] = Counter()
    for day, delta in _deltas(portfolio, security):
        by_day[day] += delta
    events = sorted(by_day.items())
    balance = sum(delta for day, delta in events if day <= on)
    lowest = balance
    for day, delta in events:
        if day > on:
            balance += delta
            lowest = min(lowest, balance)
    if lowest < wanted:
        raise ToolError(
            f"{portfolio.getName()} holds {shares_text(held([portfolio], security, on))} of "
            f"{security.getName()} on {on}"
            + (f" (and as few as {shares_text(lowest)} later)" if lowest < held([portfolio], security, on) else "")
            + f"; cannot take out {shares_text(wanted)}. Nothing was staged."
        )


def refuse_duplicate(matches: Iterable[j.Transaction], what: str, allow: bool) -> None:
    if allow:
        return
    for existing in matches:
        raise ToolError(
            f"there is already {what} (uuid {existing.getUUID()}). Nothing was staged; pass "
            "allow_duplicate=true if this really is a second one."
        )


def describe(
    transaction: j.Transaction, *, account: j.Account | None = None, portfolio: j.Portfolio | None = None
) -> JsonObject:
    info = convert.transaction(
        transaction,
        account_name=str(account.getName()) if account is not None else None,
        portfolio_name=str(portfolio.getName()) if portfolio is not None else None,
        include_notes=True,
    )
    return convert.json_object(info)
