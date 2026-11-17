"""Read-only tools over the portfolio replica.

Every tool here answers from PP's own snapshot classes, so values agree with the application.
Two conventions, both about keeping responses useful to a model:

* Anything that could be large is capped and says so (`truncated`, `total_matching`), rather than
  returning four thousand price rows.
* Free text stored in the file (notes) is opt-in per call. See SAFETY.md, layer 7.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

from fastmcp.exceptions import ToolError

from . import convert
from . import jtypes as j
from .bridge import ClientHandle, get_client
from .jsontypes import JsonObject

MAX_PRICE_ROWS = 250
MAX_TRANSACTION_ROWS = 100

NamedT = TypeVar("NamedT", bound=j.Named)

TRADE_TYPES = {"BUY", "SELL"}


# --- helpers ---------------------------------------------------------------------------------


def _java_date(value: dt.date) -> j.LocalDate:
    return j.local_date().of(value.year, value.month, value.day)


def parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date like 2026-01-31, got {value!r}") from exc


def _snapshot(handle: ClientHandle, on: dt.date | None = None) -> j.ClientSnapshot:
    when = _java_date(on) if on else j.local_date().now()
    return j.client_snapshot().create(handle.client, handle.converter, when)


# How a security can be named, most specific first.
SECURITY_KEYS: tuple[Callable[[j.Security], j.JString | None], ...] = (
    lambda s: s.getUUID(),
    lambda s: s.getIsin(),
    lambda s: s.getTickerSymbol(),
)


def resolve_security(handle: ClientHandle, query: str) -> j.Security:
    """Accept a UUID, ISIN, ticker or (part of a) name. Ambiguity is an error, not a guess."""
    needle = query.strip().casefold()
    if not needle:
        raise ToolError("security must not be empty")

    securities = list(handle.client.getSecurities())
    for key in SECURITY_KEYS:
        exact = [s for s in securities if (convert.text(key(s)) or "").casefold() == needle]
        if len(exact) == 1:
            return exact[0]

    by_name = [s for s in securities if str(s.getName()).casefold() == needle]
    if len(by_name) == 1:
        return by_name[0]

    partial = [s for s in securities if needle in str(s.getName()).casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(sorted(str(s.getName()) for s in partial))
        raise ToolError(f"{query!r} matches several securities: {names}")

    raise ToolError(
        f"no security matches {query!r}. Use list_securities to see the {len(securities)} available."
    )


def resolve_named(items: list[NamedT], query: str, kind: str) -> NamedT:
    needle = query.strip().casefold()
    exact = [i for i in items if str(i.getName()).casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [i for i in items if needle in str(i.getName()).casefold()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(str(i.getName()) for i in items))
    raise ToolError(f"no unique {kind} matches {query!r}. Available: {names}")


def _capped(rows: Sequence[object], limit: int, order: str) -> JsonObject:
    total = len(rows)
    return {
        "total_matching": total,
        "returned": min(total, limit),
        "truncated": total > limit,
        "showing": f"{order} {min(total, limit)} of {total}" if total > limit else "all matches",
    }


# --- tools -----------------------------------------------------------------------------------


def list_securities(include_retired: bool = False) -> JsonObject:
    """List the securities in the portfolio, with their latest known price.

    Args:
        include_retired: include securities marked inactive in Portfolio Performance.
    """
    handle = get_client()
    securities = [
        convert.security(s)
        for s in handle.client.getSecurities()
        if include_retired or not bool(s.isRetired())
    ]
    return convert.json_object({"count": len(securities), "securities": securities})


def list_accounts() -> JsonObject:
    """List cash accounts with their current balances."""
    handle = get_client()
    snapshot = _snapshot(handle)
    accounts = [
        {**convert.json_object(convert.account(position.getAccount())),
         "balance": convert.jsonable(convert.money(position.getFunds()))}
        for position in snapshot.getAccounts()
    ]
    return {"count": len(accounts), "accounts": accounts}


def list_portfolios() -> JsonObject:
    """List securities accounts (portfolios) with their current market value."""
    handle = get_client()
    snapshot = _snapshot(handle)
    portfolios = [
        {**convert.json_object(convert.portfolio(position.getPortfolio())),
         "value": convert.jsonable(convert.money(position.getValue()))}
        for position in snapshot.getPortfolios()
    ]
    return {"count": len(portfolios), "portfolios": portfolios}


def get_holdings(date: str | None = None) -> JsonObject:
    """Current holdings — every security position and cash account, with share of total assets.

    Args:
        date: value the portfolio on this ISO date instead of today.
    """
    handle = get_client()
    on = parse_date(date, "date")
    snapshot = _snapshot(handle, on)

    rows: list[tuple[Decimal, JsonObject]] = []
    for position in snapshot.getAssetPositions().toList():
        security_position = position.getPosition()
        held = position.getSecurity()
        value = convert.money_of(position.getValuation())
        rows.append((value.amount, {
            "name": str(position.getDescription()),
            "kind": "SECURITY" if held is not None else "ACCOUNT",
            "isin": convert.text(held.getIsin()) if held is not None else None,
            "shares": convert.jsonable(convert.shares(int(security_position.getShares()))) if security_position is not None else None,
            "value": convert.jsonable(value),
            "share_of_total": round(float(position.getShare()), 6),
        }))

    rows.sort(key=lambda row: row[0], reverse=True)
    holdings: list[JsonObject] = [row for _, row in rows]
    return {
        "date": (on or dt.date.today()).isoformat(),
        "total_assets": convert.jsonable(convert.money(snapshot.getMonetaryAssets())),
        "count": len(holdings),
        "holdings": holdings,
    }


def get_transactions(
    security: str | None = None,
    account: str | None = None,
    portfolio: str | None = None,
    type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = MAX_TRANSACTION_ROWS,
    include_notes: bool = False,
) -> JsonObject:
    """Transactions, most recent first, filtered by any combination of arguments.

    A buy or sell is one row carrying both its portfolio and its cash account (Portfolio
    Performance stores it as two linked legs). Transfers stay two rows, one per side.

    Args:
        security: UUID, ISIN, ticker or name fragment.
        account: cash account name (or fragment).
        portfolio: securities account name (or fragment).
        type: stable enum name such as BUY, SELL, DIVIDENDS, DEPOSIT, FEES, TAXES.
        start: ISO date, inclusive.
        end: ISO date, inclusive.
        limit: maximum rows to return; the response says how many matched.
        include_notes: include free-text notes stored in the file.
    """
    handle = get_client()
    wanted_security = resolve_security(handle, security) if security else None
    from_date = parse_date(start, "start")
    to_date = parse_date(end, "end")
    wanted_type = type.strip().upper() if type else None
    limit = max(1, min(int(limit), 1000))

    rows: list[convert.TransactionInfo] = []

    def collect(
        java_transaction: j.Transaction,
        account_name: str | None,
        portfolio_name: str | None,
    ) -> None:
        if wanted_security is not None and java_transaction.getSecurity() != wanted_security:
            return
        info = convert.transaction(
            java_transaction,
            account_name=account_name,
            portfolio_name=portfolio_name,
            include_notes=include_notes,
        )
        if wanted_type and info.type != wanted_type:
            return
        if from_date and info.datetime.date() < from_date:
            return
        if to_date and info.datetime.date() > to_date:
            return
        rows.append(info)

    accounts = list(handle.client.getAccounts())
    portfolios = list(handle.client.getPortfolios())
    wanted_account = resolve_named(accounts, account, "account") if account else None
    wanted_portfolio = resolve_named(portfolios, portfolio, "portfolio") if portfolio else None

    # Cash legs. A trade's cash leg is skipped here and reported with its securities leg below.
    if wanted_portfolio is None:
        for java_account in [wanted_account] if wanted_account else accounts:
            name = str(java_account.getName())
            for java_transaction in java_account.getTransactions():
                if _trade_counterpart(java_transaction) is None:
                    collect(java_transaction, name, None)

    # Securities legs, with the cash account of a trade filled in.
    for java_portfolio in [wanted_portfolio] if wanted_portfolio else portfolios:
        name = str(java_portfolio.getName())
        for java_transaction in java_portfolio.getTransactions():
            cash = _trade_counterpart(java_transaction)
            if wanted_account is not None and (
                cash is None or str(cash.getUUID()) != str(wanted_account.getUUID())
            ):
                continue
            collect(java_transaction, str(cash.getName()) if cash else None, name)

    rows.sort(key=lambda row: row.datetime, reverse=True)
    meta = _capped(rows, limit, "most recent")
    return {**meta, "transactions": convert.jsonable(rows[:limit])}


def _trade_counterpart(transaction: j.Transaction) -> j.TransactionOwner | None:
    """For one leg of a buy or sell, the owner of the other leg (portfolio or cash account)."""
    if str(transaction.getType().name()) not in TRADE_TYPES:
        return None
    cross = transaction.getCrossEntry()
    return cross.getCrossOwner(transaction) if cross is not None else None


def get_prices(
    security: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = MAX_PRICE_ROWS,
) -> JsonObject:
    """Historical prices for one security, most recent first, with a summary of the whole range.

    Args:
        security: UUID, ISIN, ticker or name fragment.
        start: ISO date, inclusive.
        end: ISO date, inclusive.
        limit: maximum rows; the summary always covers every matching price.
    """
    handle = get_client()
    java_security = resolve_security(handle, security)
    from_date = parse_date(start, "start")
    to_date = parse_date(end, "end")
    limit = max(1, min(int(limit), 2000))

    # getPrices() holds history only; the latest quote lives beside it, as in the app's chart.
    prices = [convert.price(p) for p in java_security.getPricesIncludingLatest()]
    if from_date:
        prices = [p for p in prices if p.date >= from_date]
    if to_date:
        prices = [p for p in prices if p.date <= to_date]

    summary: JsonObject = {"count": len(prices)}
    if prices:
        values = [p.value for p in prices]
        low, high = min(prices, key=lambda p: p.value), max(prices, key=lambda p: p.value)
        summary |= {
            "first": convert.jsonable(prices[0]),
            "last": convert.jsonable(prices[-1]),
            "min": convert.jsonable(low),
            "max": convert.jsonable(high),
            "mean": round(float(sum(values) / len(values)), 4),
        }

    ordered = list(reversed(prices))
    meta = _capped(ordered, limit, "most recent")
    return {
        "security": str(java_security.getName()),
        "currency": convert.text(java_security.getCurrencyCode()),
        "summary": summary,
        **meta,
        "prices": convert.jsonable(ordered[:limit]),
    }


def _dimensions(taxonomy: j.Taxonomy) -> list[str]:
    dimensions = taxonomy.getDimensions()
    return [str(d).strip() for d in dimensions] if dimensions else []


def list_taxonomies() -> JsonObject:
    """List the classification schemes (taxonomies) defined in the file."""
    handle = get_client()
    taxonomies = [
        {
            "id": str(t.getId()),
            "name": str(t.getName()),
            "dimensions": _dimensions(t),
            "classifications": t.getAllClassifications().size(),
        }
        for t in handle.client.getTaxonomies()
    ]
    return {"count": len(taxonomies), "taxonomies": taxonomies}


@dataclass
class _Node:
    """A classification with its exact value, before rounding for the response."""

    name: str
    total: Decimal
    children: list[_Node]


def get_allocation(taxonomy: str, date: str | None = None, depth: int = 2) -> JsonObject:
    """Break current assets down by one taxonomy, following PP's assignment weights.

    Args:
        taxonomy: taxonomy id or name (fragment).
        date: value the portfolio on this ISO date instead of today.
        depth: how many levels of the classification tree to return.

    `classified` and `unassigned` sum to `total_assets`. A share above 100% is possible and
    correct when unclassified cash is negative, as a margin account is.
    """
    handle = get_client()
    on = parse_date(date, "date")
    snapshot = _snapshot(handle, on)

    taxonomies = list(handle.client.getTaxonomies())
    needle = taxonomy.strip().casefold()
    matches = [t for t in taxonomies if str(t.getId()).casefold() == needle] or [
        t for t in taxonomies if needle in str(t.getName()).casefold()
    ]
    if len(matches) != 1:
        names = ", ".join(str(t.getName()) for t in taxonomies)
        raise ToolError(f"no unique taxonomy matches {taxonomy!r}. Available: {names}")
    chosen = matches[0]

    by_vehicle = snapshot.getPositionsByVehicle()
    hundred_percent = int(j.classification().ONE_HUNDRED_PERCENT)
    assigned_weight: dict[j.InvestmentVehicle, int] = {}

    def valuation_of(vehicle: j.InvestmentVehicle) -> Decimal:
        position = by_vehicle.get(vehicle)
        if position is None:
            return Decimal(0)
        return convert.money_of(position.getValuation()).amount

    def walk(classification: j.Classification) -> _Node:
        direct = Decimal(0)
        for assignment in classification.getAssignments():
            vehicle = assignment.getInvestmentVehicle()
            weight = int(assignment.getWeight())
            assigned_weight[vehicle] = assigned_weight.get(vehicle, 0) + weight
            direct += valuation_of(vehicle) * weight / hundred_percent

        children = [walk(child) for child in classification.getChildren()]
        total = direct + sum((child.total for child in children), Decimal(0))
        return _Node(name=str(classification.getName()), total=total, children=children)

    def render(node: _Node, level: int) -> JsonObject:
        out: JsonObject = {"name": node.name, "value": round(float(node.total), 2)}
        if node.children and level < depth:
            out["children"] = [render(child, level + 1) for child in node.children if child.total]
        return out

    root = walk(chosen.getRoot())
    classified = root.total

    unassigned = Decimal(0)
    for entry in by_vehicle.entrySet():
        vehicle = entry.getKey()
        remaining = hundred_percent - assigned_weight.get(vehicle, 0)
        if remaining > 0:
            unassigned += valuation_of(vehicle) * remaining / hundred_percent

    total_assets = convert.money_of(snapshot.getMonetaryAssets())
    buckets: list[JsonObject] = []
    if depth > 0:
        for child in root.children:
            if child.total:
                bucket = render(child, 1)
                bucket["share_of_total"] = round(round(float(child.total), 2) / float(total_assets.amount), 6)
                buckets.append(bucket)

    return {
        "taxonomy": str(chosen.getName()),
        "date": (on or dt.date.today()).isoformat(),
        "currency": total_assets.currency,
        "total_assets": float(total_assets.amount),
        "classified": round(float(classified), 2),
        "unassigned": round(float(unassigned), 2),
        "allocation": buckets,
    }


TOOLS: list[Callable[..., JsonObject]] = [
    list_securities,
    list_accounts,
    list_portfolios,
    get_holdings,
    get_transactions,
    get_prices,
    list_taxonomies,
    get_allocation,
]
