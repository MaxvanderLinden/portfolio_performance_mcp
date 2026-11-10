"""Read-only tools over the portfolio replica.

Every tool here answers from PP's own snapshot classes, so values agree with the application.
Two conventions, both about keeping responses useful to a model:

* Anything that could be large is capped and says so (`truncated`, `total_matching`), rather than
  returning four thousand price rows.
* Free text stored in the file (notes) is opt-in per call. See SAFETY.md, layer 7.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import jpype
from fastmcp.exceptions import ToolError

from . import convert
from .bridge import get_client

MAX_PRICE_ROWS = 250
MAX_TRANSACTION_ROWS = 100


# --- helpers ---------------------------------------------------------------------------------


def _java_date(value: dt.date) -> Any:
    return jpype.JClass("java.time.LocalDate").of(value.year, value.month, value.day)


def _parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{field} must be an ISO date like 2026-01-31, got {value!r}") from exc


def _snapshot(handle, on: dt.date | None = None) -> Any:
    snapshot_class = jpype.JClass("name.abuchen.portfolio.snapshot.ClientSnapshot")
    when = _java_date(on) if on else jpype.JClass("java.time.LocalDate").now()
    return snapshot_class.create(handle.client, handle.converter, when)


def _resolve_security(handle, query: str) -> Any:
    """Accept a UUID, ISIN, ticker or (part of a) name. Ambiguity is an error, not a guess."""
    needle = query.strip().casefold()
    if not needle:
        raise ToolError("security must not be empty")

    securities = list(handle.client.getSecurities())
    for attribute in ("getUUID", "getIsin", "getTickerSymbol"):
        exact = [s for s in securities if (getattr(s, attribute)() or "") and str(getattr(s, attribute)()).casefold() == needle]
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


def _resolve_named(items: list[Any], query: str, kind: str) -> Any:
    needle = query.strip().casefold()
    exact = [i for i in items if str(i.getName()).casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [i for i in items if needle in str(i.getName()).casefold()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(str(i.getName()) for i in items))
    raise ToolError(f"no unique {kind} matches {query!r}. Available: {names}")


def _capped(rows: list[Any], limit: int, order: str) -> dict[str, Any]:
    total = len(rows)
    return {
        "total_matching": total,
        "returned": min(total, limit),
        "truncated": total > limit,
        "showing": f"{order} {min(total, limit)} of {total}" if total > limit else "all matches",
    }


# --- tools -----------------------------------------------------------------------------------


def list_securities(include_retired: bool = False) -> dict[str, Any]:
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
    return convert.jsonable({"count": len(securities), "securities": securities})


def list_accounts() -> dict[str, Any]:
    """List cash accounts with their current balances."""
    handle = get_client()
    snapshot = _snapshot(handle)
    accounts = [
        {**convert.jsonable(convert.account(position.getAccount())),
         "balance": convert.jsonable(convert.money(position.getFunds()))}
        for position in snapshot.getAccounts()
    ]
    return {"count": len(accounts), "accounts": accounts}


def list_portfolios() -> dict[str, Any]:
    """List securities accounts (portfolios) with their current market value."""
    handle = get_client()
    snapshot = _snapshot(handle)
    portfolios = [
        {**convert.jsonable(convert.portfolio(position.getPortfolio())),
         "value": convert.jsonable(convert.money(position.getValue()))}
        for position in snapshot.getPortfolios()
    ]
    return {"count": len(portfolios), "portfolios": portfolios}


def get_holdings(date: str | None = None) -> dict[str, Any]:
    """Current holdings — every security position and cash account, with share of total assets.

    Args:
        date: value the portfolio on this ISO date instead of today.
    """
    handle = get_client()
    on = _parse_date(date, "date")
    snapshot = _snapshot(handle, on)

    holdings = []
    for position in snapshot.getAssetPositions().toList():
        security_position = position.getPosition()
        vehicle = position.getInvestmentVehicle()
        is_security = position.getSecurity() is not None
        holdings.append({
            "name": str(position.getDescription()),
            "kind": "SECURITY" if is_security else "ACCOUNT",
            "isin": convert.text(position.getSecurity().getIsin()) if is_security else None,
            "shares": convert.jsonable(convert.shares(int(security_position.getShares()))) if security_position is not None else None,
            "value": convert.jsonable(convert.money(position.getValuation())),
            "share_of_total": round(float(position.getShare()), 6),
        })

    holdings.sort(key=lambda row: row["value"]["amount"], reverse=True)
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
) -> dict[str, Any]:
    """Transactions, most recent first, filtered by any combination of arguments.

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
    wanted_security = _resolve_security(handle, security) if security else None
    from_date = _parse_date(start, "start")
    to_date = _parse_date(end, "end")
    wanted_type = type.strip().upper() if type else None
    limit = max(1, min(int(limit), 1000))

    rows: list[convert.TransactionInfo] = []

    def collect(container, java_transactions, **names):
        for java_transaction in java_transactions:
            if wanted_security is not None and java_transaction.getSecurity() != wanted_security:
                continue
            info = convert.transaction(java_transaction, include_notes=include_notes, **names)
            if wanted_type and info.type != wanted_type:
                continue
            if from_date and info.datetime.date() < from_date:
                continue
            if to_date and info.datetime.date() > to_date:
                continue
            rows.append(info)

    accounts = list(handle.client.getAccounts())
    portfolios = list(handle.client.getPortfolios())
    if account:
        accounts = [_resolve_named(accounts, account, "account")]
    if portfolio:
        portfolios = [_resolve_named(portfolios, portfolio, "portfolio")]

    if not portfolio:
        for java_account in accounts:
            collect(java_account, java_account.getTransactions(), account_name=str(java_account.getName()))
    if not account:
        for java_portfolio in portfolios:
            collect(java_portfolio, java_portfolio.getTransactions(), portfolio_name=str(java_portfolio.getName()))

    rows.sort(key=lambda row: row.datetime, reverse=True)
    meta = _capped(rows, limit, "most recent")
    return {**meta, "transactions": convert.jsonable(rows[:limit])}


def get_prices(
    security: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = MAX_PRICE_ROWS,
) -> dict[str, Any]:
    """Historical prices for one security, most recent first, with a summary of the whole range.

    Args:
        security: UUID, ISIN, ticker or name fragment.
        start: ISO date, inclusive.
        end: ISO date, inclusive.
        limit: maximum rows; the summary always covers every matching price.
    """
    handle = get_client()
    java_security = _resolve_security(handle, security)
    from_date = _parse_date(start, "start")
    to_date = _parse_date(end, "end")
    limit = max(1, min(int(limit), 2000))

    # getPrices() holds history only; the latest quote lives beside it, as in the app's chart.
    prices = [convert.price(p) for p in java_security.getPricesIncludingLatest()]
    if from_date:
        prices = [p for p in prices if p.date >= from_date]
    if to_date:
        prices = [p for p in prices if p.date <= to_date]

    summary: dict[str, Any] = {"count": len(prices)}
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


def list_taxonomies() -> dict[str, Any]:
    """List the classification schemes (taxonomies) defined in the file."""
    handle = get_client()
    taxonomies = [
        {
            "id": str(t.getId()),
            "name": str(t.getName()),
            "dimensions": [str(d).strip() for d in t.getDimensions()] if t.getDimensions() else [],
            "classifications": t.getAllClassifications().size(),
        }
        for t in handle.client.getTaxonomies()
    ]
    return {"count": len(taxonomies), "taxonomies": taxonomies}


def _without_exact(node: dict[str, Any]) -> dict[str, Any]:
    node.pop("_exact", None)
    for child in node.get("children", []):
        _without_exact(child)
    return node


def get_allocation(taxonomy: str, date: str | None = None, depth: int = 2) -> dict[str, Any]:
    """Break current assets down by one taxonomy, following PP's assignment weights.

    Args:
        taxonomy: taxonomy id or name (fragment).
        date: value the portfolio on this ISO date instead of today.
        depth: how many levels of the classification tree to return.

    `classified` and `unassigned` sum to `total_assets`. A share above 100% is possible and
    correct when unclassified cash is negative, as a margin account is.
    """
    handle = get_client()
    on = _parse_date(date, "date")
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
    hundred_percent = int(jpype.JClass("name.abuchen.portfolio.model.Classification").ONE_HUNDRED_PERCENT)
    assigned_weight: dict[Any, int] = {}

    def valuation_of(vehicle: Any) -> Decimal:
        position = by_vehicle.get(vehicle)
        if position is None:
            return Decimal(0)
        return convert.money_of(position.getValuation()).amount

    def walk(classification: Any, level: int) -> dict[str, Any]:
        direct = Decimal(0)
        for assignment in classification.getAssignments():
            vehicle = assignment.getInvestmentVehicle()
            weight = int(assignment.getWeight())
            assigned_weight[vehicle] = assigned_weight.get(vehicle, 0) + weight
            direct += valuation_of(vehicle) * weight / hundred_percent

        children = [walk(child, level + 1) for child in classification.getChildren()]
        total = direct + sum(child["_exact"] for child in children)
        node: dict[str, Any] = {
            "name": str(classification.getName()),
            "value": round(float(total), 2),
            "_exact": total,
        }
        if children and level < depth:
            node["children"] = [_without_exact(child) for child in children if child["_exact"]]
        return node

    root = walk(chosen.getRoot(), 0)
    classified = root["_exact"]

    unassigned = Decimal(0)
    for entry in by_vehicle.entrySet():
        vehicle = entry.getKey()
        remaining = hundred_percent - assigned_weight.get(vehicle, 0)
        if remaining > 0:
            unassigned += valuation_of(vehicle) * remaining / hundred_percent

    total_assets = convert.money_of(snapshot.getMonetaryAssets())
    buckets = root.get("children", [])
    for bucket in buckets:
        bucket["share_of_total"] = round(bucket["value"] / float(total_assets.amount), 6)

    return {
        "taxonomy": str(chosen.getName()),
        "date": (on or dt.date.today()).isoformat(),
        "currency": total_assets.currency,
        "total_assets": float(total_assets.amount),
        "classified": round(float(classified), 2),
        "unassigned": round(float(unassigned), 2),
        "allocation": buckets,
    }


TOOLS = [
    list_securities,
    list_accounts,
    list_portfolios,
    get_holdings,
    get_transactions,
    get_prices,
    list_taxonomies,
    get_allocation,
]
