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
from decimal import Decimal
from typing import cast

from fastmcp.exceptions import ToolError
from typing_extensions import TypedDict

from . import classification_tools, convert, staging, trade_tools
from . import jtypes as j
from . import write_common as w
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
# Types Portfolio Performance lets you attach to a security (e.g. a custody fee for one fund).
SECURITY_CASH_TYPES = frozenset(CASH_TYPES) - {"DEPOSIT", "REMOVAL"}


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
        when = w.past_date(fields["date"], f"prices[{index}].date")
        if when in rows:
            raise ToolError(f"{when} appears more than once in prices")
        rows[when] = w.fixed_point(fields["value"], QUOTE_FACTOR, f"prices[{index}].value", 8)

    def mutate(client: j.Client) -> JsonObject:
        target = w.exact_security(client, security)
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

    return w.staged(f"add {len(rows)} price(s) to {security}", {"prices": len(rows)}, mutate)


def record_cash(
    account: str,
    type: str,
    amount: float | str,
    date: str,
    note: str | None = None,
    allow_duplicate: bool = False,
    security: str | None = None,
    tax: float | str | None = None,
) -> staging.ApplyResult:
    """Record a cash transaction on one account, in the account's currency.

    Changes go to the staged copy only. If the account already has a transaction with the same
    type, date and amount, nothing is staged unless allow_duplicate is true.

    Args:
        account: account UUID or full name, matched exactly.
        type: DEPOSIT, REMOVAL, INTEREST, INTEREST_CHARGE, FEES, FEES_REFUND, TAXES or TAX_REFUND.
        amount: positive amount booked on the account; the type gives the direction (REMOVAL
            takes money out). For INTEREST with tax withheld, this is the net amount received.
        date: ISO date, not in the future.
        note: optional note to store with the transaction, as the user worded it.
        allow_duplicate: record it even if an identical transaction exists.
        security: optional security this relates to (UUID, ISIN, ticker or full name, exactly),
            e.g. a custody fee for one fund. Not for DEPOSIT or REMOVAL.
        tax: INTEREST only: tax withheld, so the gross interest is amount + tax.
    """
    kind = str(type).strip().upper()
    if kind not in CASH_TYPES:
        raise ToolError(f"type must be one of {', '.join(CASH_TYPES)}; got {type!r}")
    when = w.past_date(date, "date")
    cents = w.fixed_point(amount, AMOUNT_FACTOR, "amount", 2)
    text = (note or "").strip() or None
    if security is not None and kind not in SECURITY_CASH_TYPES:
        raise ToolError(f"a {kind} cannot be linked to a security")
    if tax is not None and kind != "INTEREST":
        raise ToolError("tax can only be given for INTEREST (as tax withheld)")
    tax_cents = w.fixed_point(tax, AMOUNT_FACTOR, "tax", 2) if tax is not None else None

    def mutate(client: j.Client) -> JsonObject:
        target = w.exact_account(client, account)
        linked = w.exact_security(client, security) if security is not None else None
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
        if linked is not None:
            transaction.setSecurity(linked)
        if tax_cents is not None:
            withheld = j.money().of(target.getCurrencyCode(), tax_cents)
            transaction.addUnit(j.unit()(j.unit_type().valueOf("TAX"), withheld))
        if text:
            transaction.setNote(text)
        transaction.setUpdatedAt(j.instant().now())
        target.addTransaction(transaction)

        info = convert.transaction(transaction, account_name=str(target.getName()), include_notes=True)
        return {"transaction": convert.jsonable(info)}

    return w.staged(f"record {kind} on {account} for {date}", {f"account_tx:{kind}": 1}, mutate)


def discard_staged() -> staging.DiscardResult:
    """Set aside every staged change and start over from the replica.

    The staged copy and its change log are moved to the staging folder's backups/, not deleted.
    The user's real file and the replica are not touched.
    """
    try:
        return staging.discard(w.area())
    except staging.StagingError as exc:
        raise ToolError(str(exc)) from exc


# --- staged reads ----------------------------------------------------------------------------


def get_staged_changes() -> staging.Status:
    """List the changes staged so far, oldest first, and whether they can still be promoted.

    `up_to_date_with_replica` false means the replica was refreshed after staging; `unmodified`
    false means the staged file was edited outside pp-write. Either one blocks promoting.
    """
    return staging.status(w.area())


WRITE_TOOLS: list[Callable[..., object]] = [
    add_prices, record_cash, *trade_tools.TRADE_TOOLS,
    *classification_tools.CLASSIFICATION_TOOLS, discard_staged,
]
STAGED_READ_TOOLS: list[Callable[..., object]] = [get_staged_changes]
