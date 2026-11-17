"""Phase 2 write tools: securities, trades, dividends, transfers and deliveries.

Same rules as write_tools: staged only, additive only, exact identification, every change
declares the counters it adds to. Amounts follow Portfolio Performance's own dialogs:

* A trade's `total` is what is booked on the cash account: gross + fees + taxes for a buy,
  gross - fees - taxes for a sell. Give either `price` (per share, in the security's currency)
  or `total`, not both.
* Fees and taxes are in the account's currency.
* When the security trades in another currency than the account, `exchange_rate` (account
  currency per 1 unit of the security's currency) is required, and the gross is stored in both
  currencies as PP does.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from fastmcp.exceptions import ToolError

from . import convert, staging
from . import jtypes as j
from . import write_common as w
from .bridge import AMOUNT_FACTOR, QUOTE_FACTOR, SHARE_FACTOR
from .jsontypes import JsonObject

ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(frozen=True)
class _Amounts:
    total: int         # booked on the account, in its currency (cents)
    gross: int         # shares x price, in the account's currency (cents)
    forex: int | None  # the same gross in the security's currency, when that differs


def _amounts(
    *,
    adds_shares: bool,
    shares_raw: int,
    price: object | None,
    total: object | None,
    fees: int,
    taxes: int,
    rate: Decimal | None,
) -> _Amounts:
    if (price is None) == (total is None):
        raise ToolError(
            "give exactly one of price (per share, in the security's currency) or total "
            "(the amount booked on the cash account)"
        )
    if price is not None:
        in_security = w.round_cents(Decimal(shares_raw) / SHARE_FACTOR * w.decimal(price, "price"))
        forex = in_security if rate is not None else None
        gross = w.round_cents(Decimal(in_security) / AMOUNT_FACTOR * rate) if rate is not None else in_security
        booked = gross + fees + taxes if adds_shares else gross - fees - taxes
    else:
        booked = w.cents(total, "total")
        gross = booked - fees - taxes if adds_shares else booked + fees + taxes
        forex = w.round_cents(Decimal(gross) / AMOUNT_FACTOR / rate) if rate is not None else None
    if gross <= 0:
        raise ToolError("fees and taxes are as large as the total; check the amounts")
    if booked <= 0:
        raise ToolError("fees and taxes exceed the gross value; check the amounts")
    return _Amounts(total=booked, gross=gross, forex=forex)


def _add_units(
    add: Callable[[j.Unit], None],
    currency: j.JString,
    security: j.Security,
    fees: int,
    taxes: int,
    amounts: _Amounts,
    rate: Decimal | None,
) -> None:
    if fees:
        add(w.unit("FEE", fees, currency))
    if taxes:
        add(w.unit("TAX", taxes, currency))
    if rate is not None and amounts.forex is not None:
        security_currency = security.getCurrencyCode()
        assert security_currency is not None  # a rate is only accepted when the currencies differ
        add(w.gross_value_unit(amounts.gross, currency, amounts.forex, security_currency, rate))


def _matching(
    transactions: j.JList[j.PortfolioTransaction], kind: str, security: j.Security, shares_raw: int, on: dt.date
) -> list[j.PortfolioTransaction]:
    return [
        t for t in transactions
        if str(t.getType().name()) == kind
        and w.same(t.getSecurity(), security)
        and int(t.getShares()) == shares_raw
        and w.day_of(t) == on
    ]


# --- securities ------------------------------------------------------------------------------


def add_security(
    name: str,
    currency: str,
    isin: str | None = None,
    ticker: str | None = None,
    wkn: str | None = None,
) -> staging.ApplyResult:
    """Add a new security (no price feed; add prices with add_prices). Changes go to the staged
    copy only. Refused if a security with the same ISIN, ticker or name already exists.

    Args:
        name: full name as it should appear in Portfolio Performance.
        currency: ISO currency code the security trades in, e.g. EUR or USD.
        isin: optional 12-character ISIN.
        ticker: optional ticker symbol, e.g. VWRL.AS.
        wkn: optional German WKN.
    """
    clean_name = name.strip()
    if not clean_name:
        raise ToolError("name must not be empty")
    code = currency.strip().upper()
    clean_isin = isin.strip().upper() if isin and isin.strip() else None
    if clean_isin is not None and not ISIN.match(clean_isin):
        raise ToolError(f"isin must be 12 characters like IE00B3RBWM25, got {isin!r}")
    clean_ticker = ticker.strip() if ticker and ticker.strip() else None
    clean_wkn = wkn.strip() if wkn and wkn.strip() else None

    def mutate(client: j.Client) -> JsonObject:
        if j.currency_unit().getInstance(code) is None:
            raise ToolError(f"{currency!r} is not a currency Portfolio Performance knows")
        for existing in client.getSecurities():
            clashes = [
                label for label, mine, theirs in (
                    ("ISIN", clean_isin, convert.text(existing.getIsin())),
                    ("ticker", clean_ticker, convert.text(existing.getTickerSymbol())),
                    ("name", clean_name, str(existing.getName())),
                )
                if mine is not None and theirs is not None and mine.casefold() == theirs.casefold()
            ]
            if clashes:
                raise ToolError(
                    f"{existing.getName()} (uuid {existing.getUUID()}) already has this {', '.join(clashes)}. "
                    "Nothing was staged."
                )
        security = j.security()(clean_name, code)
        if clean_isin:
            security.setIsin(clean_isin)
        if clean_ticker:
            security.setTickerSymbol(clean_ticker)
        if clean_wkn:
            security.setWkn(clean_wkn)
        security.setUpdatedAt(j.instant().now())
        client.addSecurity(security)
        return {"security": convert.json_object(convert.security(security))}

    return w.staged(f"add security {clean_name} ({code})", {"securities": 1}, mutate)


# --- trades ----------------------------------------------------------------------------------


def _trade(
    kind: str,
    portfolio: str,
    security: str,
    shares: float | str,
    date: str,
    price: float | str | None,
    total: float | str | None,
    fees: float | str | None,
    taxes: float | str | None,
    account: str | None,
    exchange_rate: float | str | None,
    note: str | None,
    allow_duplicate: bool,
) -> staging.ApplyResult:
    when = w.past_date(date, "date")
    shares_raw = w.shares(shares)
    fee_cents = w.optional_cents(fees, "fees")
    tax_cents = w.optional_cents(taxes, "taxes")
    text = w.note_text(note)

    def mutate(client: j.Client) -> JsonObject:
        target = w.exact_portfolio(client, portfolio)
        held = w.exact_security(client, security)
        cash = w.exact_account(client, account) if account else target.getReferenceAccount()
        if cash is None:
            raise ToolError(f"{target.getName()} has no reference account; pass account")
        currency = cash.getCurrencyCode()
        rate = w.exchange_rate(exchange_rate, currency, held, required=True)
        amounts = _amounts(
            adds_shares=kind == "BUY", shares_raw=shares_raw, price=price, total=total,
            fees=fee_cents, taxes=tax_cents, rate=rate,
        )
        if kind == "SELL":
            w.require_shares(target, held, shares_raw, when)
        w.refuse_duplicate(
            _matching(target.getTransactions(), kind, held, shares_raw, when),
            f"a {kind} of {w.shares_text(shares_raw)} {held.getName()} in {target.getName()} on {when}",
            allow_duplicate,
        )

        entry = j.buy_sell_entry()(target, cash)
        entry.setType(j.portfolio_transaction_type().valueOf(kind))
        entry.setDate(w.java_datetime(when))
        entry.setSecurity(held)
        entry.setShares(shares_raw)
        entry.setCurrencyCode(currency)
        entry.setAmount(amounts.total)
        if text:
            entry.setNote(text)
        leg = entry.getPortfolioTransaction()
        _add_units(leg.addUnit, currency, held, fee_cents, tax_cents, amounts, rate)
        entry.insert()
        return {"transaction": w.describe(leg, account=cash, portfolio=target)}

    return w.staged(
        f"record {kind} of {shares} {security} in {portfolio} on {date}",
        {f"portfolio_tx:{kind}": 1, f"account_tx:{kind}": 1},
        mutate,
    )


def record_buy(
    portfolio: str,
    security: str,
    shares: float | str,
    date: str,
    price: float | str | None = None,
    total: float | str | None = None,
    fees: float | str | None = None,
    taxes: float | str | None = None,
    account: str | None = None,
    exchange_rate: float | str | None = None,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Record a purchase: shares into a portfolio, paid from a cash account. Staged copy only.

    Give exactly one of price or total. Refused if the portfolio already has a buy of the same
    security, shares and date, unless allow_duplicate is true.

    Args:
        portfolio: securities account UUID or full name, matched exactly.
        security: UUID, ISIN, ticker or full name, matched exactly.
        shares: number of shares bought.
        date: ISO date, not in the future.
        price: price per share, in the security's currency.
        total: amount debited from the cash account (gross + fees + taxes).
        fees: fees, in the account's currency.
        taxes: taxes, in the account's currency.
        account: cash account; defaults to the portfolio's reference account.
        exchange_rate: account currency per 1 unit of the security's currency; required when
            they differ, refused when they are the same.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if an identical buy exists.
    """
    return _trade("BUY", portfolio, security, shares, date, price, total, fees, taxes,
                  account, exchange_rate, note, allow_duplicate)


def record_sell(
    portfolio: str,
    security: str,
    shares: float | str,
    date: str,
    price: float | str | None = None,
    total: float | str | None = None,
    fees: float | str | None = None,
    taxes: float | str | None = None,
    account: str | None = None,
    exchange_rate: float | str | None = None,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Record a sale: shares out of a portfolio, proceeds into a cash account. Staged copy only.

    Give exactly one of price or total. Refused if the portfolio would hold fewer shares than
    sold, on that date or later, or if an identical sale exists (unless allow_duplicate).

    Args:
        portfolio: securities account UUID or full name, matched exactly.
        security: UUID, ISIN, ticker or full name, matched exactly.
        shares: number of shares sold.
        date: ISO date, not in the future.
        price: price per share, in the security's currency.
        total: amount credited to the cash account (gross - fees - taxes).
        fees: fees, in the account's currency.
        taxes: taxes, in the account's currency.
        account: cash account; defaults to the portfolio's reference account.
        exchange_rate: account currency per 1 unit of the security's currency; required when
            they differ, refused when they are the same.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if an identical sale exists.
    """
    return _trade("SELL", portfolio, security, shares, date, price, total, fees, taxes,
                  account, exchange_rate, note, allow_duplicate)


# --- dividends -------------------------------------------------------------------------------


def record_dividend(
    account: str,
    security: str,
    amount: float | str,
    date: str,
    tax: float | str | None = None,
    fees: float | str | None = None,
    shares: float | str | None = None,
    exchange_rate: float | str | None = None,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Record a dividend paid into a cash account. Staged copy only.

    Refused if the account already has a dividend of this security on this date, unless
    allow_duplicate is true.

    Args:
        account: account UUID or full name, matched exactly.
        security: UUID, ISIN, ticker or full name, matched exactly.
        amount: net amount credited to the account, after tax and fees.
        date: payment date, ISO, not in the future.
        tax: tax withheld, in the account's currency.
        fees: fees deducted, in the account's currency.
        shares: shares the dividend was paid on; defaults to the shares held in all portfolios
            on the payment date.
        exchange_rate: optional, account currency per 1 unit of the security's currency, to
            also store the gross in the security's currency.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if a dividend of this security on this date exists.
    """
    when = w.past_date(date, "date")
    net = w.cents(amount, "amount")
    tax_cents = w.optional_cents(tax, "tax")
    fee_cents = w.optional_cents(fees, "fees")
    shares_raw = w.shares(shares) if shares is not None else None
    text = w.note_text(note)

    def mutate(client: j.Client) -> JsonObject:
        cash = w.exact_account(client, account)
        paid_on = w.exact_security(client, security)
        currency = cash.getCurrencyCode()
        rate = w.exchange_rate(exchange_rate, currency, paid_on, required=False)
        count = shares_raw if shares_raw is not None else w.held(client.getPortfolios(), paid_on, when)
        if count <= 0:
            raise ToolError(
                f"no portfolio holds {paid_on.getName()} on {when}. Pass shares if the dividend is "
                "still right (e.g. the shares were sold after the ex-date). Nothing was staged."
            )
        w.refuse_duplicate(
            (t for t in cash.getTransactions()
             if str(t.getType().name()) == "DIVIDENDS" and w.same(t.getSecurity(), paid_on) and w.day_of(t) == when),
            f"a dividend of {paid_on.getName()} on {when} in {cash.getName()}",
            allow_duplicate,
        )

        transaction = j.account_transaction()()
        transaction.setType(j.account_transaction_type().valueOf("DIVIDENDS"))
        transaction.setDateTime(w.java_datetime(when))
        transaction.setCurrencyCode(currency)
        transaction.setAmount(net)
        transaction.setSecurity(paid_on)
        transaction.setShares(count)
        gross = net + tax_cents + fee_cents
        forex = w.round_cents(Decimal(gross) / AMOUNT_FACTOR / rate) if rate is not None else None
        _add_units(transaction.addUnit, currency, paid_on, fee_cents, tax_cents,
                   _Amounts(total=net, gross=gross, forex=forex), rate)
        if text:
            transaction.setNote(text)
        transaction.setUpdatedAt(j.instant().now())
        cash.addTransaction(transaction)
        return {"transaction": w.describe(transaction, account=cash)}

    return w.staged(f"record dividend of {security} into {account} on {date}", {"account_tx:DIVIDENDS": 1}, mutate)


# --- transfers and deliveries ----------------------------------------------------------------


def record_cash_transfer(
    from_account: str,
    to_account: str,
    amount: float | str,
    date: str,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Move money between two of the user's accounts in the same currency. Staged copy only.

    Args:
        from_account: account the money leaves, UUID or full name, matched exactly.
        to_account: account the money arrives in, UUID or full name, matched exactly.
        amount: positive amount, in the accounts' currency.
        date: ISO date, not in the future.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if the same transfer on the same date exists.
    """
    when = w.past_date(date, "date")
    amount_cents = w.cents(amount, "amount")
    text = w.note_text(note)

    def mutate(client: j.Client) -> JsonObject:
        source = w.exact_account(client, from_account)
        target = w.exact_account(client, to_account)
        if w.same(source, target):
            raise ToolError("from_account and to_account are the same account")
        currency = source.getCurrencyCode()
        if str(currency) != str(target.getCurrencyCode()):
            raise ToolError(
                f"{source.getName()} is in {currency} and {target.getName()} in {target.getCurrencyCode()}; "
                "transfers between currencies are not supported yet"
            )
        w.refuse_duplicate(
            (t for t in source.getTransactions()
             if str(t.getType().name()) == "TRANSFER_OUT" and int(t.getAmount()) == amount_cents
             and w.day_of(t) == when
             and (cross := t.getCrossEntry()) is not None and w.same(cross.getCrossOwner(t), target)),
            f"a transfer of {w.money_text(amount_cents, currency)} from {source.getName()} to "
            f"{target.getName()} on {when}",
            allow_duplicate,
        )
        entry = j.account_transfer_entry()(source, target)
        entry.setDate(w.java_datetime(when))
        entry.setCurrencyCode(currency)
        entry.setAmount(amount_cents)
        if text:
            entry.setNote(text)
        entry.insert()
        return {
            "out": w.describe(entry.getSourceTransaction(), account=source),
            "in": w.describe(entry.getTargetTransaction(), account=target),
        }

    return w.staged(
        f"transfer {amount} from {from_account} to {to_account} on {date}",
        {"account_tx:TRANSFER_OUT": 1, "account_tx:TRANSFER_IN": 1},
        mutate,
    )


def _price_on(security: j.Security, on: dt.date) -> int | None:
    """The last quote on or before `on` (raw, ×10⁸)."""
    best: tuple[dt.date, int] | None = None
    for price in security.getPrices():
        day = convert.date_of(price.getDate())
        if day <= on and (best is None or day >= best[0]):
            best = (day, int(price.getValue()))
    return best[1] if best else None


def record_share_transfer(
    security: str,
    shares: float | str,
    from_portfolio: str,
    to_portfolio: str,
    date: str,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Move shares between two of the user's portfolios, valued at that day's price. Staged
    copy only. Refused if the source would hold fewer shares than moved.

    Args:
        security: UUID, ISIN, ticker or full name, matched exactly.
        shares: number of shares moved.
        from_portfolio: portfolio the shares leave, UUID or full name, matched exactly.
        to_portfolio: portfolio the shares arrive in, UUID or full name, matched exactly.
        date: ISO date, not in the future; a price on or before it must exist.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if the same transfer on the same date exists.
    """
    when = w.past_date(date, "date")
    shares_raw = w.shares(shares)
    text = w.note_text(note)

    def mutate(client: j.Client) -> JsonObject:
        moved = w.exact_security(client, security)
        source = w.exact_portfolio(client, from_portfolio)
        target = w.exact_portfolio(client, to_portfolio)
        if w.same(source, target):
            raise ToolError("from_portfolio and to_portfolio are the same portfolio")
        w.require_shares(source, moved, shares_raw, when)
        w.refuse_duplicate(
            _matching(source.getTransactions(), "TRANSFER_OUT", moved, shares_raw, when),
            f"a transfer of {w.shares_text(shares_raw)} {moved.getName()} out of {source.getName()} on {when}",
            allow_duplicate,
        )
        quote = _price_on(moved, when)
        if quote is None:
            raise ToolError(f"{moved.getName()} has no price on or before {when}; add one with add_prices first")
        value = w.round_cents(Decimal(shares_raw) / SHARE_FACTOR * Decimal(quote) / QUOTE_FACTOR)
        currency = moved.getCurrencyCode() or client.getBaseCurrency()

        entry = j.portfolio_transfer_entry()(source, target)
        entry.setDate(w.java_datetime(when))
        entry.setSecurity(moved)
        entry.setShares(shares_raw)
        entry.setCurrencyCode(currency)
        entry.setAmount(value)
        if text:
            entry.setNote(text)
        entry.insert()
        return {
            "out": w.describe(entry.getSourceTransaction(), portfolio=source),
            "in": w.describe(entry.getTargetTransaction(), portfolio=target),
        }

    return w.staged(
        f"transfer {shares} {security} from {from_portfolio} to {to_portfolio} on {date}",
        {"portfolio_tx:TRANSFER_OUT": 1, "portfolio_tx:TRANSFER_IN": 1},
        mutate,
    )


DIRECTIONS = {"IN": "DELIVERY_INBOUND", "INBOUND": "DELIVERY_INBOUND",
              "OUT": "DELIVERY_OUTBOUND", "OUTBOUND": "DELIVERY_OUTBOUND"}


def record_delivery(
    portfolio: str,
    security: str,
    shares: float | str,
    date: str,
    direction: str,
    price: float | str | None = None,
    total: float | str | None = None,
    fees: float | str | None = None,
    taxes: float | str | None = None,
    exchange_rate: float | str | None = None,
    note: str | None = None,
    allow_duplicate: bool = False,
) -> staging.ApplyResult:
    """Shares arriving in or leaving a portfolio without a cash payment (a move from another
    broker, a gift, a spin-off). Staged copy only.

    The value is what PP uses as cost basis (inbound) or proceeds (outbound): give exactly one
    of price or total. An outbound delivery is refused if the portfolio would hold fewer shares.

    Args:
        portfolio: securities account UUID or full name, matched exactly.
        security: UUID, ISIN, ticker or full name, matched exactly.
        shares: number of shares.
        date: ISO date, not in the future.
        direction: inbound (into the portfolio) or outbound.
        price: value per share, in the security's currency.
        total: total value in the portfolio's currency (its reference account's), incl. fees and
            taxes for inbound, after them for outbound.
        fees: fees, in the portfolio's currency.
        taxes: taxes, in the portfolio's currency.
        exchange_rate: portfolio currency per 1 unit of the security's currency; required when
            they differ.
        note: optional note, as the user worded it.
        allow_duplicate: record it even if an identical delivery exists.
    """
    kind = DIRECTIONS.get(direction.strip().upper())
    if kind is None:
        raise ToolError(f"direction must be inbound or outbound, got {direction!r}")
    when = w.past_date(date, "date")
    shares_raw = w.shares(shares)
    fee_cents = w.optional_cents(fees, "fees")
    tax_cents = w.optional_cents(taxes, "taxes")
    text = w.note_text(note)

    def mutate(client: j.Client) -> JsonObject:
        target = w.exact_portfolio(client, portfolio)
        moved = w.exact_security(client, security)
        reference = target.getReferenceAccount()
        if reference is None:
            raise ToolError(f"{target.getName()} has no reference account, so its currency is unknown")
        currency = reference.getCurrencyCode()
        rate = w.exchange_rate(exchange_rate, currency, moved, required=True)
        amounts = _amounts(
            adds_shares=kind == "DELIVERY_INBOUND", shares_raw=shares_raw, price=price, total=total,
            fees=fee_cents, taxes=tax_cents, rate=rate,
        )
        if kind == "DELIVERY_OUTBOUND":
            w.require_shares(target, moved, shares_raw, when)
        w.refuse_duplicate(
            _matching(target.getTransactions(), kind, moved, shares_raw, when),
            f"a {kind} of {w.shares_text(shares_raw)} {moved.getName()} in {target.getName()} on {when}",
            allow_duplicate,
        )

        transaction = j.portfolio_transaction()()
        transaction.setType(j.portfolio_transaction_type().valueOf(kind))
        transaction.setDateTime(w.java_datetime(when))
        transaction.setSecurity(moved)
        transaction.setShares(shares_raw)
        transaction.setCurrencyCode(currency)
        transaction.setAmount(amounts.total)
        _add_units(transaction.addUnit, currency, moved, fee_cents, tax_cents, amounts, rate)
        if text:
            transaction.setNote(text)
        transaction.setUpdatedAt(j.instant().now())
        target.addTransaction(transaction)
        return {"transaction": w.describe(transaction, portfolio=target)}

    return w.staged(
        f"record {kind} of {shares} {security} in {portfolio} on {date}",
        {f"portfolio_tx:{kind}": 1},
        mutate,
    )


TRADE_TOOLS: list[Callable[..., object]] = [
    add_security,
    record_buy,
    record_sell,
    record_dividend,
    record_cash_transfer,
    record_share_transfer,
    record_delivery,
]
