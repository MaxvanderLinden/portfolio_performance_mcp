"""Generate tests/fixtures/demo.xml: a small, entirely fictional portfolio for the test suite.

    ./.venv/bin/python tests/fixtures/make_demo.py

The file is built with Portfolio Performance's own model classes and saved by its own
`ClientFactory`, so it is a valid PP file that the app can also open. Everything in it is made
up: securities, ISINs, prices and amounts. Output is deterministic apart from PP's generated UUIDs.

What the tests rely on:
  * three EUR securities with daily prices 2023-01-02 .. 2026-10-30 plus a latest quote,
    two of them with "World" in their name (the ambiguous-match test);
  * one security bought and fully sold (closed position), one retired security with no trades;
  * monthly buys with fees, quarterly dividends with withholding tax, deposits and interest;
  * two cash accounts, two portfolios, and a "Regions" taxonomy with partial assignments.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import sys
from pathlib import Path

import jpype

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pp_mcp.bridge import AMOUNT_FACTOR, QUOTE_FACTOR, SHARE_FACTOR, find_classpath, find_libjvm  # noqa: E402
from pp_mcp.config import DEFAULT_APP, Config  # noqa: E402

OUT = Path(__file__).with_name("demo.xml")
START = dt.date(2023, 1, 2)
END = dt.date(2026, 10, 30)
LATEST = dt.date(2026, 11, 2)
CURRENCY = "EUR"


def main() -> None:
    app = Path(os.environ.get("PP_APP_PATH", str(DEFAULT_APP)))
    config = Config(source=OUT, app_path=app)
    jpype.startJVM(str(find_libjvm(config)), classpath=find_classpath(config), convertStrings=False)

    m = "name.abuchen.portfolio.model."
    Client = jpype.JClass(m + "Client")
    Security = jpype.JClass(m + "Security")
    SecurityPrice = jpype.JClass(m + "SecurityPrice")
    LatestSecurityPrice = jpype.JClass(m + "LatestSecurityPrice")
    Account = jpype.JClass(m + "Account")
    Portfolio = jpype.JClass(m + "Portfolio")
    BuySellEntry = jpype.JClass(m + "BuySellEntry")
    PortfolioTransaction = jpype.JClass(m + "PortfolioTransaction")
    AccountTransaction = jpype.JClass(m + "AccountTransaction")
    Unit = jpype.JClass(m + "Transaction$Unit")
    Taxonomy = jpype.JClass(m + "Taxonomy")
    Classification = jpype.JClass(m + "Classification")
    Assignment = jpype.JClass(m + "Classification$Assignment")
    ClientFactory = jpype.JClass(m + "ClientFactory")
    Money = jpype.JClass("name.abuchen.portfolio.money.Money")
    LocalDate = jpype.JClass("java.time.LocalDate")
    LocalDateTime = jpype.JClass("java.time.LocalDateTime")
    File = jpype.JClass("java.io.File")

    def jdate(d: dt.date):
        return LocalDate.of(d.year, d.month, d.day)

    def jdatetime(d: dt.date):
        return LocalDateTime.of(d.year, d.month, d.day, 0, 0)

    def cents(value: float) -> int:
        return round(value * AMOUNT_FACTOR)

    def money(value: float):
        return Money.of(CURRENCY, cents(value))

    client = Client()
    client.setBaseCurrency(CURRENCY)
    rng = random.Random(20230102)

    # --- securities and prices -------------------------------------------------------------
    closes: dict[str, dict[dt.date, float]] = {}

    def make_security(name, isin, ticker, start_price, drift, vol, retired=False):
        security = Security(name, CURRENCY)
        security.setIsin(isin)
        security.setTickerSymbol(ticker)
        security.setRetired(retired)
        price, day, series = start_price, START, {}
        while day <= END:
            if day.weekday() < 5:
                price *= 1 + drift + rng.gauss(0, vol)
                series[day] = round(price, 4)
                security.addPrice(SecurityPrice(jdate(day), round(series[day] * QUOTE_FACTOR)))
            day += dt.timedelta(days=1)
        latest = round(price * 1.003, 4)
        security.setLatest(LatestSecurityPrice(jdate(LATEST), round(latest * QUOTE_FACTOR)))
        series[LATEST] = latest
        closes[isin] = series
        client.addSecurity(security)
        return security

    world = make_security("Demo Global World ETF", "XX0000000001", "DWLD", 80.0, 0.0004, 0.009)
    emerging = make_security("Demo Emerging World ETF", "XX0000000002", "DEMW", 30.0, 0.0002, 0.012)
    corp = make_security("Example Corp", "XX0000000003", "EXMP", 120.0, 0.0003, 0.015)
    make_security("Retired Demo Fund", "XX0000000004", "RTRD", 10.0, 0.0, 0.005, retired=True)

    def close_on(security, day: dt.date) -> float:
        series = closes[str(security.getIsin())]
        while day not in series:
            day -= dt.timedelta(days=1)
        return series[day]

    # --- accounts and portfolios -----------------------------------------------------------
    cash = Account("Broker cash")
    cash.setCurrencyCode(CURRENCY)
    savings = Account("Savings")
    savings.setCurrencyCode(CURRENCY)
    pension_cash = Account("Pension cash")
    pension_cash.setCurrencyCode(CURRENCY)
    for account in (cash, savings, pension_cash):
        client.addAccount(account)

    broker = Portfolio("Broker")
    broker.setReferenceAccount(cash)
    pension = Portfolio("Pension")
    pension.setReferenceAccount(pension_cash)
    client.addPortfolio(broker)
    client.addPortfolio(pension)

    def account_tx(account, kind, day, amount, security=None, shares=0.0, tax=0.0):
        tx = AccountTransaction()
        tx.setType(getattr(AccountTransaction.Type, kind))
        tx.setDateTime(jdatetime(day))
        tx.setCurrencyCode(CURRENCY)
        tx.setAmount(cents(amount))
        if security is not None:
            tx.setSecurity(security)
            tx.setShares(round(shares * SHARE_FACTOR))
        if tax:
            tx.addUnit(Unit(Unit.Type.TAX, money(tax)))
        account.addTransaction(tx)

    def trade(portfolio, account, kind, day, security, shares, fee):
        gross = round(shares * close_on(security, day), 2)
        total = gross + fee if kind == "BUY" else gross - fee
        entry = BuySellEntry(portfolio, account)
        entry.setType(getattr(PortfolioTransaction.Type, kind))
        entry.setDate(jdatetime(day))
        entry.setSecurity(security)
        entry.setShares(round(shares * SHARE_FACTOR))
        entry.setCurrencyCode(CURRENCY)
        entry.setAmount(cents(total))
        entry.getPortfolioTransaction().addUnit(Unit(Unit.Type.FEE, money(fee)))
        entry.insert()
        return total

    def month_starts():
        day = START
        while day <= END:
            yield day
            day = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

    def first_weekday(day: dt.date) -> dt.date:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        return day

    # --- broker: monthly deposits and buys -------------------------------------------------
    account_tx(cash, "DEPOSIT", START, 5000.0)
    for month in month_starts():
        day = first_weekday(month.replace(day=3))
        if day > END:
            break
        account_tx(cash, "DEPOSIT", day, 600.0)
        trade(broker, cash, "BUY", day, world, 5, fee=1.0)
        if month.month in (1, 4, 7, 10):
            trade(broker, cash, "BUY", day, emerging, 6, fee=1.0)

    # Example Corp: bought, paid dividends, sold completely -> a closed position.
    trade(broker, cash, "BUY", first_weekday(dt.date(2023, 2, 15)), corp, 20, fee=2.0)
    for quarter in (dt.date(2023, 6, 15), dt.date(2023, 9, 15), dt.date(2023, 12, 15),
                    dt.date(2024, 3, 15), dt.date(2024, 6, 14), dt.date(2024, 9, 16)):
        gross = 20 * 0.85
        account_tx(cash, "DIVIDENDS", quarter, gross * 0.85, corp, 20, tax=gross * 0.15)
    trade(broker, cash, "SELL", first_weekday(dt.date(2025, 3, 10)), corp, 20, fee=2.0)

    # World ETF is distributing: quarterly dividends on the shares held at the time.
    held = 0
    for month in month_starts():
        held += 5
        if month.month in (3, 6, 9, 12):
            day = first_weekday(month.replace(day=20))
            if day <= END:
                gross = round(held * 0.32, 2)
                account_tx(cash, "DIVIDENDS", day, round(gross * 0.85, 2), world, held, tax=round(gross * 0.15, 2))

    account_tx(cash, "FEES", dt.date(2024, 12, 31), 25.0)

    # --- savings: deposits and interest ----------------------------------------------------
    account_tx(savings, "DEPOSIT", dt.date(2023, 1, 10), 10000.0)
    for year in (2023, 2024, 2025):
        account_tx(savings, "INTEREST", dt.date(year, 12, 31), 10000.0 * 0.02, tax=10000.0 * 0.02 * 0.3)
    account_tx(savings, "REMOVAL", dt.date(2025, 6, 2), 2500.0)

    # --- pension: one lump sum -------------------------------------------------------------
    account_tx(pension_cash, "DEPOSIT", dt.date(2024, 1, 15), 8000.0)
    trade(pension, pension_cash, "BUY", dt.date(2024, 1, 15), world, 60, fee=5.0)
    trade(pension, pension_cash, "BUY", dt.date(2024, 1, 15), emerging, 40, fee=5.0)

    # --- taxonomy --------------------------------------------------------------------------
    regions = Taxonomy("Regions")
    root = Classification(regions.getId(), "Regions")
    regions.setRootNode(root)
    hundred = int(Classification.ONE_HUNDRED_PERCENT)

    def child(parent, cid, name, color):
        node = Classification(parent, cid, name, color)
        parent.addChild(node)
        return node

    developed = child(root, "developed", "Developed markets", "#4e79a7")
    europe = child(developed, "europe", "Europe", "#76b7b2")
    north_america = child(developed, "north-america", "North America", "#59a14f")
    emerging_markets = child(root, "emerging", "Emerging markets", "#f28e2b")

    north_america.addAssignment(Assignment(world, hundred * 70 // 100))
    europe.addAssignment(Assignment(world, hundred * 20 // 100))
    developed.addAssignment(Assignment(world, hundred * 10 // 100))
    emerging_markets.addAssignment(Assignment(emerging, hundred))
    north_america.addAssignment(Assignment(corp, hundred))
    # cash and the retired fund stay unassigned on purpose
    client.addTaxonomy(regions)

    ClientFactory.save(client, File(str(OUT)))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
