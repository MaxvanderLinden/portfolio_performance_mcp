"""Performance analytics, computed by Portfolio Performance's own classes.

TTWROR, IRR, capital gains, drawdown and volatility all come from `PerformanceIndex`,
`ClientPerformanceSnapshot` and `LazySecurityPerformanceSnapshot`, so they agree with the figures
the application shows for the same reporting period.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import jpype
from fastmcp.exceptions import ToolError

from . import convert
from .bridge import get_client
from .read_tools import _parse_date, _resolve_named, _resolve_security

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


def _earliest_transaction(client: Any) -> dt.date:
    dates = [
        convert.datetime_of(t.getDateTime()).date()
        for holder in list(client.getAccounts()) + list(client.getPortfolios())
        for t in holder.getTransactions()
    ]
    return min(dates) if dates else dt.date.today()


def _to_code(client: Any, period: str) -> str:
    text = period.strip().upper()
    if text in PERIOD_ALIASES:
        return PERIOD_ALIASES[text]
    if text in {"MAX", "ALL"}:
        return f"S{_earliest_transaction(client) - dt.timedelta(days=1)}"
    if text.isdigit() and len(text) == 4:
        return f"Y{text}"
    if ".." in text:
        start, _, end = text.partition("..")
        _parse_date(start, "period start")
        _parse_date(end, "period end")
        return f"F{start}_{end}"
    return period.strip()


def _interval(handle, period: str) -> tuple[Any, str]:
    code = _to_code(handle.client, period)
    reporting_period = jpype.JClass("name.abuchen.portfolio.snapshot.ReportingPeriod")
    local_date = jpype.JClass("java.time.LocalDate")
    try:
        return reporting_period.from_(code).toInterval(local_date.now()), code
    except Exception as exc:  # noqa: BLE001 - Java throws IOException for an unparsable code
        raise ToolError(f"could not read period {period!r} (as code {code!r}). {PERIOD_HELP}") from exc


def _interval_dates(interval: Any) -> dict[str, str]:
    return {
        "start": str(convert.date(interval.getStart())),
        "end": str(convert.date(interval.getEnd())),
    }


def _percent(value: float) -> float | None:
    """Java returns NaN where a period holds no data; JSON has no NaN."""
    value = float(value)
    return None if value != value else round(value, 6)


def _index(handle, interval: Any, scope: str, name: str | None) -> tuple[Any, list[str], str]:
    performance_index = jpype.JClass("name.abuchen.portfolio.snapshot.PerformanceIndex")
    warnings = jpype.JClass("java.util.ArrayList")()
    client, converter = handle.client, handle.converter
    scope = scope.strip().lower()

    if scope == "client":
        index, label = performance_index.forClient(client, converter, interval, warnings), "whole portfolio"
    elif scope == "portfolio":
        target = _resolve_named(list(client.getPortfolios()), _require(name, scope), "portfolio")
        index = performance_index.forPortfolio(client, converter, target, interval, warnings)
        label = str(target.getName())
    elif scope == "account":
        target = _resolve_named(list(client.getAccounts()), _require(name, scope), "account")
        index = performance_index.forAccount(client, converter, target, interval, warnings)
        label = str(target.getName())
    elif scope == "security":
        target = _resolve_security(handle, _require(name, scope))
        index = performance_index.forInvestment(client, converter, target, interval, warnings)
        label = str(target.getName())
    elif scope == "classification":
        target = _resolve_classification(client, _require(name, scope))
        index = performance_index.forClassification(client, converter, target, interval, warnings)
        label = str(target.getName())
    else:
        raise ToolError(
            f"unknown scope {scope!r}; use client, portfolio, account, security or classification"
        )

    return index, [str(w) for w in warnings], label


def _require(name: str | None, scope: str) -> str:
    if not name:
        raise ToolError(f"scope {scope!r} needs a name, e.g. name='My broker'")
    return name


def _resolve_classification(client: Any, query: str) -> Any:
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


def get_performance(period: str = "1Y", scope: str = "client", name: str | None = None) -> dict[str, Any]:
    """Time-weighted return (TTWROR), IRR, drawdown and volatility for a reporting period.

    Args:
        period: reporting period. Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year (2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code (L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since).
        scope: client, portfolio, account, security or classification.
        name: which portfolio/account/security/classification, when scope is not client.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    index, warnings, label = _index(handle, interval, scope, name)

    totals = index.getTotals()
    drawdown, volatility = index.getDrawdown(), index.getVolatility()

    return {
        "scope": scope.lower(),
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
        "warnings": warnings,
        "note": "rates are fractions (0.223 is 22.3%); irr and ttwror_annualized are annualized, "
                "ttwror is cumulative over the period",
    }


def get_performance_snapshot(period: str = "1Y") -> dict[str, Any]:
    """Where the change in value came from: capital gains, earnings, fees, taxes, transfers.

    Categories form a waterfall: the rows signed '', '+' and '-' add up to the '=' row
    (FINAL_VALUE), which is how Portfolio Performance's performance view is built.
    `absolute_delta` is the change not caused by deposits or removals: FINAL_VALUE minus
    INITIAL_VALUE minus TRANSFERS.

    Args:
        period: reporting period. Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year (2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code (L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since).
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    snapshot_class = jpype.JClass("name.abuchen.portfolio.snapshot.ClientPerformanceSnapshot")
    category_type = jpype.JClass("name.abuchen.portfolio.snapshot.ClientPerformanceSnapshot$CategoryType")
    snapshot = snapshot_class(handle.client, handle.converter, interval)

    categories = []
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

    def positions_of(type_name: str) -> list[dict[str, Any]]:
        category = snapshot.getCategoryByType(getattr(category_type, type_name))
        return [
            {"label": str(p.getLabel()), "value": convert.jsonable(convert.money(p.getValue()))}
            for p in category.getPositions()
        ]

    index_class = jpype.JClass("name.abuchen.portfolio.snapshot.PerformanceIndex")
    index = index_class.forClient(handle.client, handle.converter, interval, jpype.JClass("java.util.ArrayList")())

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
    }


def get_security_performance(
    period: str = "1Y",
    cost_method: str = "FIFO",
    include_closed: bool = False,
) -> dict[str, Any]:
    """Per-security returns over a period: IRR, TTWROR, capital gains, dividends, fees, taxes.

    Args:
        period: reporting period. Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year (2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code (L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since).
        cost_method: FIFO or MOVING_AVERAGE, as in Portfolio Performance's settings.
        include_closed: also report securities no longer held at the end of the period.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    method_class = jpype.JClass("name.abuchen.portfolio.model.CostMethod")
    taxes_and_fees = jpype.JClass("name.abuchen.portfolio.model.TaxesAndFees")
    try:
        method = getattr(method_class, cost_method.strip().upper())
    except AttributeError as exc:
        raise ToolError(f"cost_method must be FIFO or MOVING_AVERAGE, got {cost_method!r}") from exc

    snapshot_class = jpype.JClass("name.abuchen.portfolio.snapshot.security.LazySecurityPerformanceSnapshot")
    snapshot = snapshot_class.create(handle.client, handle.converter, interval)

    rows = []
    for record in snapshot.getRecords():
        shares = convert.shares(int(record.getSharesHeld()))
        if not shares and not include_closed:
            continue
        rows.append({
            "security": str(record.getSecurityName()),
            "shares": convert.jsonable(shares),
            "market_value": convert.jsonable(convert.money(record.getMarketValue())),
            "cost": convert.jsonable(convert.money(record.getCost(method, taxes_and_fees.INCLUDED))),
            "unrealized_gains": convert.jsonable(convert.money(record.getUnrealizedCapitalGains(method).getCapitalGains())),
            "realized_gains": convert.jsonable(convert.money(record.getRealizedCapitalGains(method).getCapitalGains())),
            "dividends": convert.jsonable(convert.money(record.getSumOfDividends())),
            "fees": convert.jsonable(convert.money(record.getFees())),
            "taxes": convert.jsonable(convert.money(record.getTaxes())),
            "irr": _percent(record.getIrr()),
            "ttwror": _percent(record.getTrueTimeWeightedRateOfReturn()),
            "ttwror_annualized": _percent(record.getTrueTimeWeightedRateOfReturnAnnualized()),
        })

    rows.sort(key=lambda row: row["market_value"]["amount"], reverse=True)
    return {
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "cost_method": cost_method.strip().upper(),
        "count": len(rows),
        "securities": rows,
        "note": "rates are fractions: 0.223 is 22.3%",
    }


def get_dividends(
    period: str = "MAX",
    security: str | None = None,
    by: str = "year",
) -> dict[str, Any]:
    """Dividend income in the base currency, grouped by year or by security.

    Payments in other currencies are converted at the exchange rate on the payment date, as
    Portfolio Performance does in its own dividend views.

    Args:
        period: reporting period. Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year (2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code (L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since).
        security: limit to one security (UUID, ISIN, ticker or name fragment).
        by: 'year' or 'security'.
    """
    handle = get_client()
    interval, code = _interval(handle, period)
    start, end = convert.date_of(interval.getStart()), convert.date_of(interval.getEnd())
    wanted = _resolve_security(handle, security) if security else None
    grouping = by.strip().lower()
    if grouping not in {"year", "security"}:
        raise ToolError("by must be 'year' or 'security'")

    converter = handle.converter
    currency = str(converter.getTermCurrency())

    def in_base(when: Any, value: Any) -> Decimal:
        return convert.money_of(converter.convert(when, value)).amount

    groups: dict[str, dict[str, Any]] = {}
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
            bucket = groups.setdefault(
                key, {"key": key, "gross": Decimal(0), "net": Decimal(0), "taxes": Decimal(0), "count": 0}
            )
            bucket["gross"] += gross
            bucket["net"] += net
            bucket["taxes"] += taxes
            bucket["count"] += 1
            total += net
            count += 1

    rows = sorted(groups.values(), key=lambda row: row["key"])
    for row in rows:
        for field in ("gross", "net", "taxes"):
            row[field] = round(float(row[field]), 2)

    return {
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "grouped_by": grouping,
        "currency": currency,
        "total_net": round(float(total), 2),
        "payments": count,
        "groups": rows,
    }


def get_performance_series(
    period: str = "1Y",
    scope: str = "client",
    name: str | None = None,
    sampling: str = "monthly",
) -> dict[str, Any]:
    """The TTWROR curve and portfolio value over time, sampled to keep responses small.

    Args:
        period: reporting period. Use an alias (1M, 3M, 6M, 1Y, 2Y, 3Y, 5Y, 10Y, YTD, MTD, QTD, WTD, MAX), a calendar year (2025), an explicit range (2024-01-01..2024-12-31), or a Portfolio Performance code (L1Y0 = last year, D30 = last 30 days, T30 = last 30 trading days, S2020-01-01 = since).
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

    truncated = len(keep) > MAX_SERIES_POINTS
    if truncated:
        keep = keep[-MAX_SERIES_POINTS:]

    return {
        "scope": scope.lower(),
        "of": label,
        "period": {"requested": period, "code": code, **_interval_dates(interval)},
        "sampling": sampling,
        "points": len(keep),
        "available_points": len(dates),
        "truncated": truncated,
        "warnings": warnings,
        "series": [
            {
                "date": dates[position].isoformat(),
                "ttwror": _percent(percentages[position]),
                "value": convert.jsonable(convert.amount(int(totals[position]))),
            }
            for position in keep
        ],
    }


ANALYTICS_TOOLS = [
    get_performance,
    get_performance_snapshot,
    get_security_performance,
    get_dividends,
    get_performance_series,
]
