"""Exchange rates: Portfolio Performance's own ECB history, read from the app's cache.

The app downloads the ECB rates and keeps them in its workspace folder. Outside the app, its
provider finds no such folder and knows a single built-in rate per currency (2015-12-18 for most),
which it applies to every date, silently. So we read the cache (never write it, never download)
and add its rates to the provider's own series.

PP also converts silently when it has no rate at all (1:1), or only an old one. `Rates.warnings`
says so; every tool that converts currencies passes it on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import jpype

from . import jtypes as j

# The ECB publishes on working days; its longest usual gap, around Easter or Christmas, is 5 days.
STALE_AFTER = dt.timedelta(days=7)

ECB_SERIES = "ExchangeRateTimeSeriesImpl"
SECURITY_SERIES = "SecurityBasedExchangeRateProvider$SecurityBasedExchangeRate"
EMPTY_SERIES = "EmptyExchangeRateTimeSeries"

FileId = tuple[str, int, int]

# The provider's series belong to the JVM, not to one loaded file, so this does too.
_loaded: dict[str, FileId] = {}


def file_id(path: Path | None) -> FileId | None:
    """Identity of the cache file; changes when the app updates its rates."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


def _class_name(series: j.ExchangeRateTimeSeries) -> str:
    return str(series.getClass().getName()).rpartition(".")[2]


def _leaves(series: j.ExchangeRateTimeSeries) -> list[j.ExchangeRateTimeSeries]:
    """The original series behind an inverse or chained one."""
    parts = list(series.getComposition())
    return [leaf for part in parts for leaf in _leaves(part)] if parts else [series]


def _pair(series: j.ExchangeRateTimeSeries) -> str:
    return f"{series.getBaseCurrency()}/{series.getTermCurrency()}"


def used_currencies(client: j.Client) -> set[str]:
    """The currencies of everything that has transactions, except the base currency."""
    used: set[str] = set()
    transactions: list[Iterable[j.Transaction]] = []
    transactions.extend(a.getTransactions() for a in client.getAccounts())
    transactions.extend(p.getTransactions() for p in client.getPortfolios())
    for owner in transactions:
        for transaction in owner:
            used.add(str(transaction.getCurrencyCode()))
            security = transaction.getSecurity()
            if security is not None and security.getCurrencyCode() is not None:
                used.add(str(security.getCurrencyCode()))
    used.discard(str(client.getBaseCurrency()))
    return used


@dataclass(frozen=True)
class Rates:
    """The rate series one loaded file uses, and what is wrong with them."""

    base: str
    leaves: dict[str, list[j.ExchangeRateTimeSeries]] = field(default_factory=dict[str, list[j.ExchangeRateTimeSeries]])
    missing: frozenset[str] = frozenset()  # no series at all
    problem: str | None = None  # why the app's cache could not be used

    def warnings(self, *dates: dt.date) -> list[str]:
        """What to know about the rates behind amounts converted on (or between) these dates."""
        out: list[str] = []
        for currency in sorted(self.missing):
            out.append(
                f"No exchange rate from {currency} to {self.base}: Portfolio Performance counts them 1:1, "
                f"so converted amounts in {currency} are wrong."
            )
        if self.problem and any(_class_name(s) == ECB_SERIES for parts in self.leaves.values() for s in parts):
            out.append(
                f"{self.problem} Amounts in {', '.join(sorted(self.leaves))} are converted with Portfolio "
                "Performance's built-in fallback rates (from 2015 for most currencies), so values and "
                "returns will differ from the app."
            )
        checked: set[str] = set()
        for currency, parts in sorted(self.leaves.items()):
            for series in parts:
                name = _class_name(series)
                pair = _pair(series)
                if pair in checked or name not in {ECB_SERIES, SECURITY_SERIES}:
                    continue  # the rest are fixed by law or definition (DEM, GBX, ...)
                checked.add(pair)
                if self.problem and name == ECB_SERIES:
                    continue  # covered above
                out.extend(_date_warnings(series, pair, currency, dates))
        return out


def _date_warnings(
    series: j.ExchangeRateTimeSeries, pair: str, currency: str, dates: Iterable[dt.date]
) -> list[str]:
    out: list[str] = []
    for day in sorted({min(d, dt.date.today()) for d in dates}):
        rate = series.lookupRate(j.local_date().of(day.year, day.month, day.day)).orElse(None)
        if rate is None:
            return [f"The {pair} series has no rates: Portfolio Performance counts {currency} 1:1."]
        time = rate.getTime()
        found = dt.date(int(time.getYear()), int(time.getMonthValue()), int(time.getDayOfMonth()))
        if found > day:
            out.append(f"No {pair} rate before {found}: earlier dates (from {day}) use that rate.")
        elif day - found > STALE_AFTER:
            out.append(f"The {pair} rate used for {day} is from {found}.")
    return out


def load(client: j.Client, factory: j.ExchangeRateProviderFactory, cache: Path | None) -> Rates:
    """Give the provider the app's rates for the currencies this file uses."""
    base = str(client.getBaseCurrency())
    leaves: dict[str, list[j.ExchangeRateTimeSeries]] = {}
    missing: set[str] = set()
    for currency in used_currencies(client):
        series = factory.getTimeSeries(currency, base)
        if _class_name(series) == EMPTY_SERIES:
            missing.add(currency)
        else:
            leaves[currency] = _leaves(series)

    wanted = {
        _pair(series): series
        for parts in leaves.values()
        for series in parts
        if _class_name(series) == ECB_SERIES
    }
    problem = _fill(wanted, cache) if wanted else None
    return Rates(base=base, leaves=leaves, missing=frozenset(missing), problem=problem)


def _fill(wanted: dict[str, j.ExchangeRateTimeSeries], cache: Path | None) -> str | None:
    identity = file_id(cache)
    todo = {pair: series for pair, series in wanted.items() if pair not in _loaded or _loaded[pair] != identity}
    if not todo or (identity is None and all(pair in _loaded for pair in wanted)):
        return None  # rates loaded earlier stay usable when the file is gone; their age is reported
    if cache is None or identity is None:
        return (
            f"Portfolio Performance's exchange rates were not found at {cache}; open the app once while "
            "online, or set PP_RATES_FILE."
        )
    try:
        stream = j.file_input_stream()(str(cache))
        try:
            data = j.ecb_data().parseFrom(stream)
        finally:
            stream.close()
        decimals, days, rate_class = j.protobuf_util(), j.local_date(), j.exchange_rate()
        for cached in data.getSeriesList():
            series = todo.get(f"{cached.getBaseCurrency()}/{cached.getTermCurrency()}")
            if series is None:
                continue
            for rate in cached.getExchangeRatesList():
                series.addRate(
                    rate_class(days.ofEpochDay(rate.getDate()), decimals.fromDecimalValue(rate.getValue()))
                )
    except jpype.JException as exc:
        return f"Portfolio Performance's exchange rates at {cache} could not be read ({exc.message()})."
    # Also pairs the cache lacks: their age is reported by date, and re-reading would not help.
    _loaded.update(dict.fromkeys(todo, identity))
    return None
