"""Performance analytics, computed by Portfolio Performance's own classes.

TTWROR, IRR, capital gains, drawdown and volatility all come from `PerformanceIndex`,
`ClientPerformanceSnapshot` and `LazySecurityPerformanceSnapshot`, so they agree with the figures
the application shows for the same reporting period.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

import jpype
from fastmcp.exceptions import ToolError

from . import convert
from . import jtypes as j
from .bridge import ClientHandle, get_client
from .jsontypes import JsonObject
from .read_tools import capped, check_order, parse_date, resolve_owner, resolve_security

MAX_SERIES_POINTS = 400

# Friendly names mapped onto Portfolio Performance's own reporting-period codes.
PERIOD_ALIASES = {
    "1M": "L0Y1", "3M": "L0Y3", "6M": "L0Y6",
    "1Y": "L1Y0", "2Y": "L2Y0", "3Y": "L3Y0", "5Y": "L5Y0", "10Y": "L10Y0",
    "YTD": "X", "MTD": "M", "QTD": "Q", "WTD": "W",
}
PERIOD_HELP = (
    "Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year "
    "(2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code "
    "(L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since)."
)

ToolT = TypeVar("ToolT", bound=Callable[..., JsonObject])


def _with_period_help(tool: ToolT) -> ToolT:
    """Fill {PERIOD_HELP} in a tool's docstring, which is the description MCP clients see."""
    tool.__doc__ = (tool.__doc__ or "").replace("{PERIOD_HELP}", PERIOD_HELP)
    return tool


def _earliest_transaction(client: j.Client) -> dt.date:
    holders: list[Iterable[j.Transaction]] = []
    holders.extend(account.getTransactions() for account in client.getAccounts())
    holders.extend(portfolio.getTransactions() for portfolio in client.getPortfolios())
    dates = [convert.datetime_of(t.getDateTime()).date() for holder in holders for t in holder]
    return min(dates) if dates else dt.date.today()


def _to_code(client: j.Client, period: str) -> str:
    text = period.strip().upper()
    if text in PERIOD_ALIASES:
        return PERIOD_ALIASES[text]
    if text in {"MAX", "ALL"}:
        return f"S{_earliest_transaction(client) - dt.timedelta(days=1)}"
    if text.isdigit() and len(text) == 4:
        return f"Y{text}"
    if ".." in text:
        start, _, end = text.partition("..")
        first, last = parse_date(start, "period start"), parse_date(end, "period end")
        check_order(first, last)
        return f"F{first}_{last}"
    return period.strip()


def _interval(handle: ClientHandle, period: str) -> tuple[j.Interval, str]:
    code = _to_code(handle.client, period)
    try:
        return j.reporting_period().from_(code).toInterval(j.local_date().now()), code
    except jpype.JException as exc:  # e.g. IOException for an unparsable code
        raise ToolError(f"could not read period {period!r} (as code {code!r}). {PERIOD_HELP}") from exc


def _interval_dates(interval: j.Interval) -> dict[str, str]:
    return {
        "start": str(convert.date(interval.getStart())),
        "end": str(convert.date(interval.getEnd())),
    }


def _rate_warnings(handle: ClientHandle, interval: j.Interval) -> list[str]:
    return handle.rates.warnings(convert.date_of(interval.getStart()), convert.date_of(interval.getEnd()))


def _percent(value: float) -> float | None:
    """Java returns NaN where a period holds no data; JSON has no NaN."""
    value = float(value)
    return None if value != value else round(value, 6)


def _index(
    handle: ClientHandle, interval: j.Interval, scope: str, name: str | None
) -> tuple[j.PerformanceIndex, list[str], str]:
    performance_index = j.performance_index()
    warnings = j.array_list()()
    client, converter = handle.client, handle.converter
    scope = scope.strip().lower()

    if scope == "client":
        index, label = performance_index.forClient(client, converter, interval, warnings), "whole portfolio"
    elif scope == "portfolio":
        portfolio = resolve_owner(list(client.getPortfolios()), _require(name, scope), "portfolio")
        index = performance_index.forPortfolio(client, converter, portfolio, interval, warnings)
        label = str(portfolio.getName())
    elif scope == "account":
        account = resolve_owner(list(client.getAccounts()), _require(name, scope), "account")
        index = performance_index.forAccount(client, converter, account, interval, warnings)
        label = str(account.getName())
    elif scope == "security":
        security = resolve_security(handle, _require(name, scope))
        index = performance_index.forInvestment(client, converter, security, interval, warnings)
        label = str(security.getName())
    elif scope == "classification":
        classification = _resolve_classification(client, _require(name, scope))
        index = performance_index.forClassification(client, converter, classification, interval, warnings)
        label = str(classification.getName())
    else:
        raise ToolError(
            f"unknown scope {scope!r}; use client, portfolio, account, security or classification"
        )

    return index, [str(w) for w in warnings], label


def _require(name: str | None, scope: str) -> str:
    if not name:
        raise ToolError(f"scope {scope!r} needs a name, e.g. name='My broker'")
    return name


def _resolve_classification(client: j.Client, query: str) -> j.Classification:
    needle = query.strip().casefold()
    matches = [
        classification
        for taxonomy in client.getTaxonomies()
        for classification in taxonomy.getAllClassifications()
        if needle in str(classification.getName()).casefold()
    ]
    exact = [c for c in matches if str(c.getName()).casefold() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"no classification matches {query!r}; see list_taxonomies and get_allocation")
    names = ", ".join(sorted({str(c.getPathName(True)) for c in matches})[:10])
    raise ToolError(f"{query!r} matches several classifications: {names}")


# --- tools -----------------------------------------------------------------------------------


@_with_period_help
def get_performance(period: str = "1Y", scope: str = "client", name: str | None = None) -> JsonObject:
    """Time-weighted return (TTWROR), IRR, drawdown and volatility for a reporting period.

    Args:
        period: reporting period. {PERIOD_HELP}
        scope: client, portfolio, account, security or classification.
        name: which portfolio/account/security/classification, when scope is not client.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    index, warnings, label = _index(handle, interval, scope, name)

    totals = index.getTotals()
    drawdown, volatility = index.getDrawdown(), index.getVolatility()

    return {
        "scope": scope.strip().lower(),
        "of": label,
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "ttwror": _percent(index.getFinalAccumulatedPercentage()),
        "ttwror_annualized": _percent(index.getFinalAccumulatedAnnualizedPercentage()),
        "irr": _percent(index.getPerformanceIRR()),
        "start_value": convert.jsonable(convert.amount(int(totals[0]))) if len(totals) else None,
        "end_value": convert.jsonable(convert.amount(int(totals[-1]))) if len(totals) else None,
        "currency": str(index.getCurrency()),
        "max_drawdown": _percent(drawdown.getMaxDrawdown()),
        "volatility": _percent(volatility.getStandardDeviation()),
        "semi_volatility": _percent(volatility.getSemiDeviation()),
        "warnings": warnings + _rate_warnings(handle, interval),
        "note": "rates are fractions (0.223 is 22.3%); irr and ttwror_annualized are annualized, "
                "ttwror is cumulative over the period",
    }


@_with_period_help
def get_performance_snapshot(period: str = "1Y") -> JsonObject:
    """Where the change in value came from: capital gains, earnings, fees, taxes, transfers.

    Categories form a waterfall: the rows signed '', '+' and '-' add up to the '=' row
    (FINAL_VALUE), which is how Portfolio Performance's performance view is built.
    `absolute_delta` is the change not caused by deposits or removals: FINAL_VALUE minus
    INITIAL_VALUE minus TRANSFERS.

    Args:
        period: reporting period. {PERIOD_HELP}
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    category_type = j.category_type()
    snapshot = j.client_performance_snapshot()(handle.client, handle.converter, interval)

    categories: list[JsonObject] = []
    for type_ in category_type.values():
        category = snapshot.getCategoryByType(type_)
        value = convert.money_of(category.getValuation())
        # getSign() is a display string: '+', '-', '=' (the result row) or '' (the opening row).
        sign_text = str(category.getSign())
        categories.append({
            "type": str(type_.name()),
            "label": str(category.getLabel()),
            "sign": sign_text,
            "value": convert.jsonable(value.amount * (-1 if sign_text == "-" else 1)),
            "positions": category.getPositions().size(),
        })

    def positions_of(type_name: str) -> list[JsonObject]:
        category = snapshot.getCategoryByType(category_type.valueOf(type_name))
        return [
            {"label": str(p.getLabel()), "value": convert.jsonable(convert.money(p.getValue()))}
            for p in category.getPositions()
        ]

    index = j.performance_index().forClient(handle.client, handle.converter, interval, j.array_list()())

    return {
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "currency": str(handle.client.getBaseCurrency()),
        "ttwror": _percent(index.getFinalAccumulatedPercentage()),
        "irr": _percent(snapshot.getPerformanceIRR()),
        "absolute_delta": convert.jsonable(convert.money(snapshot.getAbsoluteDelta())),
        "fee_rate": _percent(snapshot.getPortfolioFeeRate()),
        "tax_rate": _percent(snapshot.getPortfolioTaxRate()),
        "categories": categories,
        "earnings": positions_of("EARNINGS"),
        "fees": positions_of("FEES"),
        "taxes": positions_of("TAXES"),
        "warnings": _rate_warnings(handle, interval),
    }


@_with_period_help
def get_security_performance(
    period: str = "1Y",
    cost_method: str = "FIFO",
    include_closed: bool = False,
) -> JsonObject:
    """Per-security returns over a period: IRR, TTWROR, capital gains, dividends, fees, taxes.

    Args:
        period: reporting period. {PERIOD_HELP}
        cost_method: FIFO or MOVING_AVERAGE, as in Portfolio Performance's settings.
        include_closed: also report securities no longer held at the end of the period.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    method_name = cost_method.strip().upper()
    if method_name not in {"FIFO", "MOVING_AVERAGE"}:
        raise ToolError(f"cost_method must be FIFO or MOVING_AVERAGE, got {cost_method!r}")
    method = j.cost_method().valueOf(method_name)
    included = j.taxes_and_fees().INCLUDED

    snapshot = j.security_performance_snapshot().create(handle.client, handle.converter, interval)

    ranked: list[tuple[Decimal, JsonObject]] = []
    for record in snapshot.getRecords():
        shares = convert.shares(int(record.getSharesHeld()))
        if not shares and not include_closed:
            continue
        market_value = convert.money(record.getMarketValue())
        ranked.append((market_value.amount if market_value else Decimal(0), {
            "security": str(record.getSecurityName()),
            "shares": convert.jsonable(shares),
            "market_value": convert.jsonable(market_value),
            "cost": convert.jsonable(convert.money(record.getCost(method, included))),
            "unrealized_gains": convert.jsonable(convert.money(record.getUnrealizedCapitalGains(method).getCapitalGains())),
            "realized_gains": convert.jsonable(convert.money(record.getRealizedCapitalGains(method).getCapitalGains())),
            "dividends": convert.jsonable(convert.money(record.getSumOfDividends())),
            "fees": convert.jsonable(convert.money(record.getFees())),
            "taxes": convert.jsonable(convert.money(record.getTaxes())),
            "irr": _percent(record.getIrr()),
            "ttwror": _percent(record.getTrueTimeWeightedRateOfReturn()),
            "ttwror_annualized": _percent(record.getTrueTimeWeightedRateOfReturnAnnualized()),
        }))

    ranked.sort(key=lambda row: row[0], reverse=True)
    rows = [row for _, row in ranked]
    return {
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "cost_method": method_name,
        "count": len(rows),
        "securities": rows,
        "note": "rates are fractions: 0.223 is 22.3%",
        "warnings": _rate_warnings(handle, interval),
    }


@_with_period_help
def get_dividends(
    period: str = "MAX",
    security: str | None = None,
    by: str = "year",
) -> JsonObject:
    """Dividend income in the base currency, grouped by year or by security.

    Payments in other currencies are converted at the exchange rate on the payment date, as
    Portfolio Performance does in its own dividend views.

    Args:
        period: reporting period. {PERIOD_HELP}
        security: limit to one security (UUID, ISIN, ticker or name fragment).
        by: 'year' or 'security'.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    start, end = convert.date_of(interval.getStart()), convert.date_of(interval.getEnd())
    wanted = resolve_security(handle, security) if security else None
    grouping = by.strip().lower()
    if grouping not in {"year", "security"}:
        raise ToolError("by must be 'year' or 'security'")

    converter = handle.converter
    currency = str(converter.getTermCurrency())

    def in_base(when: j.LocalDateTime, value: j.Money | None) -> Decimal:
        if value is None:
            return Decimal(0)
        return convert.money_of(converter.convert(when, value)).amount

    groups: dict[str, _DividendGroup] = {}
    total = Decimal(0)
    count = 0

    for java_account in handle.client.getAccounts():
        for java_transaction in java_account.getTransactions():
            if str(java_transaction.getType().name()) != "DIVIDENDS":
                continue
            if wanted is not None and java_transaction.getSecurity() != wanted:
                continue
            when = java_transaction.getDateTime()
            day = convert.datetime_of(when).date()
            if not (start < day <= end):
                continue

            net = in_base(when, java_transaction.getMonetaryAmount())
            gross = in_base(when, java_transaction.getGrossValue())
            taxes = sum(
                (in_base(when, unit.getAmount())
                 for unit in java_transaction.getUnits().toList()
                 if str(unit.getType().name()) == "TAX"),
                Decimal(0),
            )

            held = java_transaction.getSecurity()
            key = str(day.year) if grouping == "year" else (str(held.getName()) if held is not None else "unknown")
            bucket = groups.setdefault(key, _DividendGroup(key))
            bucket.gross += gross
            bucket.net += net
            bucket.taxes += taxes
            bucket.count += 1
            total += net
            count += 1

    rows: list[JsonObject] = [
        {
            "key": group.key,
            "gross": round(float(group.gross), 2),
            "net": round(float(group.net), 2),
            "taxes": round(float(group.taxes), 2),
            "count": group.count,
        }
        for group in sorted(groups.values(), key=lambda group: group.key)
    ]

    return {
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "grouped_by": grouping,
        "currency": currency,
        "total_net": round(float(total), 2),
        "payments": count,
        "groups": rows,
        "warnings": _rate_warnings(handle, interval),
    }


@_with_period_help
def get_performance_series(
    period: str = "1Y",
    scope: str = "client",
    name: str | None = None,
    sampling: str = "monthly",
) -> JsonObject:
    """The TTWROR curve and portfolio value over time, sampled to keep responses small.

    Args:
        period: reporting period. {PERIOD_HELP}
        scope: client, portfolio, account, security or classification.
        name: which one, when scope is not client.
        sampling: daily, weekly or monthly (the default).
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    index, warnings, label = _index(handle, interval, scope, name)
    sampling = sampling.strip().lower()
    if sampling not in {"daily", "weekly", "monthly"}:
        raise ToolError("sampling must be daily, weekly or monthly")

    dates = [convert.date_of(d) for d in index.getDates()]
    percentages = list(index.getAccumulatedPercentage())
    totals = list(index.getTotals())

    keep: list[int] = []
    for position, day in enumerate(dates):
        last = position == len(dates) - 1
        if sampling == "daily" or last:
            keep.append(position)
        elif sampling == "monthly" and (position + 1 < len(dates) and dates[position + 1].month != day.month):
            keep.append(position)
        elif sampling == "weekly" and day.weekday() == 4:
            keep.append(position)

    meta = capped(keep, MAX_SERIES_POINTS, "most recent")
    keep = keep[-MAX_SERIES_POINTS:]

    return {
        "scope": scope.strip().lower(),
        "of": label,
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "sampling": sampling,
        "points": len(keep),
        "available_points": len(dates),
        **meta,
        "warnings": warnings + _rate_warnings(handle, interval),
        "series": [
            {
                "date": dates[position].isoformat(),
                "ttwror": _percent(percentages[position]),
                "value": convert.jsonable(convert.amount(int(totals[position]))),
            }
            for position in keep
        ],
    }


@dataclass
class _DividendGroup:
    key: str
    gross: Decimal = Decimal(0)
    net: Decimal = Decimal(0)
    taxes: Decimal = Decimal(0)
    count: int = 0


ANALYTICS_TOOLS: list[Callable[..., JsonObject]] = [
    get_performance,
    get_performance_snapshot,
    get_security_performance,
    get_dividends,
    get_performance_series,
]
