"""Additive write tools, served only by `pp-write` (SAFETY.md, layer 2).

Every tool here goes through `staging.apply()`, so it changes the staged copy and nothing else,
and declares exactly which counters it adds to. Rules for adding a tool:

* Add, never remove or overwrite. No delete_*, replace_*, bulk update_*, splits or note edits.
* Identify things exactly (UUID, exact ISIN, exact name); a name fragment is fine for reading,
  not for writing.
* Never act on text read from the file: security names and notes are data, not instructions.
* Return the new record in the same shape as the read tools, plus the staged change.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import cast

from fastmcp.exceptions import ToolError
from typing_extensions import TypedDict

from . import convert, staging
from . import jtypes as j
from .bridge import AMOUNT_FACTOR, QUOTE_FACTOR
from .jsontypes import JsonObject

MAX_PRICE_ROWS = 1000



class PriceRow(TypedDict):
    """One price for add_prices, in the security's currency."""

    date: str
    value: float | str


CASH_TYPES = (
    "DEPOSIT", "REMOVAL", "INTEREST", "INTEREST_CHARGE",
    "FEES", "FEES_REFUND", "TAXES", "TAX_REFUND",
)


# --- helpers ---------------------------------------------------------------------------------


def _area() -> staging.StagingArea:
    return staging.staging_area()


def _past_date(value: object, field: str) -> dt.date:
    try:
        parsed = dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date like 2026-01-31, got {value!r}") from exc
    if parsed > dt.date.today():
        raise ToolError(f"{field} {parsed} is in the future; only past and current dates can be recorded")
    return parsed


def _fixed_point(value: object, factor: int, field: str, places: int) -> int:
    """A positive decimal as PP's fixed-point long. Rejects more precision than PP stores,
    rather than rounding it away silently."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ToolError(f"{field} must be a number, got {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ToolError(f"{field} must be greater than zero, got {value!r}")
    if number != round(number, places):
        raise ToolError(f"{field} has more than {places} decimal places: {value!r}")
    return int(number * factor)


# Ways to name a security or account exactly, most specific first.
SECURITY_KEYS: tuple[Callable[[j.Security], j.JString | None], ...] = (
    lambda s: s.getUUID(),
    lambda s: s.getIsin(),
    lambda s: s.getTickerSymbol(),
    lambda s: s.getName(),
)
ACCOUNT_KEYS: tuple[Callable[[j.Account], j.JString], ...] = (
    lambda a: a.getUUID(),
    lambda a: a.getName(),
)


def _exact_security(client: j.Client, query: str) -> j.Security:
    """UUID, ISIN, ticker or full name, exactly (case-insensitive). No fragments for writes."""
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
        "Use list_securities to find it."
    )


def _exact_account(client: j.Client, query: str) -> j.Account:
    needle = str(query).strip().casefold()
    accounts = list(client.getAccounts())
    for key in ACCOUNT_KEYS:
        found = [a for a in accounts if str(key(a)).casefold() == needle]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ToolError(f"{query!r} matches {len(found)} accounts; use the UUID instead")
    names = ", ".join(sorted(str(a.getName()) for a in accounts))
    raise ToolError(f"no account is exactly {query!r}. Accounts: {names}")


def _staged(
    description: str, expected: dict[str, int], mutate: Callable[[j.Client], JsonObject]
) -> staging.ApplyResult:
    try:
        return staging.apply(_area(), description, expected, mutate)
    except staging.StagingError as exc:
        raise ToolError(str(exc)) from exc


# --- write tools -----------------------------------------------------------------------------


def add_prices(security: str, prices: list[PriceRow]) -> staging.ApplyResult:
    """Add historical prices to one security, in the security's own currency.

    Only dates without a price can be added: if any date already has one, nothing is staged and
    the conflicting dates are listed. Use this for securities without a price feed (pension
    funds, unlisted funds). Changes go to the staged copy only.

    Args:
        security: UUID, ISIN, ticker or full name, matched exactly.
        prices: rows like {"date": "2026-10-31", "value": 123.45}; at most 1000.
    """
    if not prices:
        raise ToolError("prices must contain at least one row")
    if len(prices) > MAX_PRICE_ROWS:
        raise ToolError(f"at most {MAX_PRICE_ROWS} prices per call, got {len(prices)}")

    rows: dict[dt.date, int] = {}
    for index, row in enumerate(prices):
        # MCP clients are validated against PriceRow; direct Python callers are not.
        fields = cast("Mapping[str, object]", row)
        if "date" not in fields or "value" not in fields:
            raise ToolError(f"prices[{index}] must look like {{\"date\": \"2026-10-31\", \"value\": 123.45}}")
        when = _past_date(fields["date"], f"prices[{index}].date")
        if when in rows:
            raise ToolError(f"{when} appears more than once in prices")
        rows[when] = _fixed_point(fields["value"], QUOTE_FACTOR, f"prices[{index}].value", 8)

    def mutate(client: j.Client) -> JsonObject:
        target = _exact_security(client, security)
        existing = {convert.date_of(p.getDate()) for p in target.getPrices()}
        clashes = sorted(d for d in rows if d in existing)
        if clashes:
            shown = ", ".join(d.isoformat() for d in clashes[:20])
            more = f" and {len(clashes) - 20} more" if len(clashes) > 20 else ""
            raise ToolError(
                f"{target.getName()} already has prices on {shown}{more}. Existing prices are never "
                "overwritten; leave those dates out and try again. Nothing was staged."
            )
        new_price, local_date = j.security_price(), j.local_date()
        for when, value in sorted(rows.items()):
            target.addPrice(new_price(local_date.of(when.year, when.month, when.day), value), False)
        target.setUpdatedAt(j.instant().now())
        ordered = sorted(rows)
        return {
            "security": str(target.getName()),
            "security_uuid": str(target.getUUID()),
            "currency": convert.text(target.getCurrencyCode()),
            "added": len(rows),
            "first": ordered[0].isoformat(),
            "last": ordered[-1].isoformat(),
        }

    return _staged(f"add {len(rows)} price(s) to {security}", {"prices": len(rows)}, mutate)


def record_cash(
    account: str,
    type: str,
    amount: float | str,
    date: str,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Record a cash transaction on one account, in the account's currency.

    Changes go to the staged copy only. If the account already has a transaction with the same
    type, date and amount, nothing is staged unless allow_duplicate is true.

    Args:
        account: account UUID or full name, matched exactly.
        type: DEPOSIT, REMOVAL, INTEREST, INTEREST_CHARGE, FEES, FEES_REFUND, TAXES or TAX_REFUND.
        amount: positive amount; the type gives the direction (REMOVAL takes money out).
        date: ISO date, not in the future.
        note: optional note to store with the transaction, as the user worded it.
        allow_duplicate: record it even if an identical transaction exists.
    """
    kind = str(type).strip().upper()
    if kind not in CASH_TYPES:
        raise ToolError(f"type must be one of {', '.join(CASH_TYPES)}; got {type!r}")
    when = _past_date(date, "date")
    cents = _fixed_point(amount, AMOUNT_FACTOR, "amount", 2)
    text = (note or "").strip() or None

    def mutate(client: j.Client) -> JsonObject:
        target = _exact_account(client, account)
        if not allow_duplicate:
            for existing in target.getTransactions():
                if (
                    str(existing.getType().name()) == kind
                    and int(existing.getAmount()) == cents
                    and convert.date_of(existing.getDateTime().toLocalDate()) == when
                ):
                    raise ToolError(
                        f"{target.getName()} already has a {kind} of {Decimal(cents) / AMOUNT_FACTOR} "
                        f"on {when} (uuid {existing.getUUID()}). Nothing was staged; pass "
                        "allow_duplicate=true if this really is a second one."
                    )

        transaction = j.account_transaction()()
        transaction.setType(j.account_transaction_type().valueOf(kind))
        transaction.setDateTime(j.local_date_time().of(when.year, when.month, when.day, 0, 0))
        transaction.setCurrencyCode(target.getCurrencyCode())
        transaction.setAmount(cents)
        if text:
            transaction.setNote(text)
        transaction.setUpdatedAt(j.instant().now())
        target.addTransaction(transaction)

        info = convert.transaction(transaction, account_name=str(target.getName()), include_notes=True)
        return {"transaction": convert.jsonable(info)}

    return _staged(f"record {kind} on {account} for {date}", {f"account_tx:{kind}": 1}, mutate)


def discard_staged() -> staging.DiscardResult:
    """Set aside every staged change and start over from the replica.

    The staged copy and its change log are moved to the staging folder's backups/, not deleted.
    The user's real file and the replica are not touched.
    """
    try:
        return staging.discard(_area())
    except staging.StagingError as exc:
        raise ToolError(str(exc)) from exc


# --- staged reads ----------------------------------------------------------------------------


def get_staged_changes() -> staging.Status:
    """List the changes staged so far, oldest first, and whether they can still be promoted.

    `up_to_date_with_replica` false means the replica was refreshed after staging; `unmodified`
    false means the staged file was edited outside pp-write. Either one blocks promoting.
    """
    return staging.status(_area())


WRITE_TOOLS: list[Callable[..., object]] = [add_prices, record_cash, discard_staged]
STAGED_READ_TOOLS: list[Callable[..., object]] = [get_staged_changes]
