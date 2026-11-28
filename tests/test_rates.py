"""Exchange rates read from the app's cache. Needs Portfolio Performance.

Each case runs in its own interpreter: the provider's series live as long as the JVM, so rates
loaded for one test would still be there in the next. The cache files are built with Portfolio
Performance's own classes and hold made-up rates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "demo.xml"

PRELUDE = """
import datetime as dt, os, sys, time
from pathlib import Path
import jpype
from pp_engine_mcp import analytics_tools as at, bridge, jtypes as j, read_tools as rt, rates
from pp_engine_mcp.config import load_config

CACHE = Path(os.environ["CACHE"])
TODAY = dt.date.today()

def write_cache(**series):
    proto = "name.abuchen.portfolio.model.proto.v1."
    data = jpype.JClass(proto + "PECBData").newBuilder()
    data.setLastModified(1)
    to_decimal = jpype.JClass("name.abuchen.portfolio.util.ProtobufUtil").asDecimalValue
    for term, values in series.items():
        s = jpype.JClass(proto + "PExchangeRateTimeSeries").newBuilder().setBaseCurrency("EUR").setTermCurrency(term)
        for day, value in values.items():
            epoch = (dt.date.fromisoformat(str(day)) - dt.date(1970, 1, 1)).days
            rate = jpype.JClass(proto + "PExchangeRate").newBuilder().setDate(epoch)
            s.addExchangeRates(rate.setValue(to_decimal(j.big_decimal()(value))))
        data.addSeries(s)
    out = jpype.JClass("java.io.FileOutputStream")(str(CACHE))
    data.build().writeTo(out)
    out.close()

def usd(handle, day):
    d = dt.date.fromisoformat(str(day))
    when = j.local_date_time().of(d.year, d.month, d.day, 0, 0)
    return int(handle.converter.convert(when, j.money().of("USD", 100_000_00)).getAmount())

def start():
    config = load_config()
    bridge.start_jvm(config)
    return config

def fresh():
    return {"2024-02-01": "2", "2024-06-03": "1.25", str(TODAY): "1.6"}
"""


@pytest.fixture
def run(config, tmp_path):
    replica = tmp_path / "portfolio.xml"
    shutil.copyfile(FIXTURE, replica)
    replica.chmod(0o444)
    cache = tmp_path / "rates.pb"
    cache.touch()

    def go(code: str) -> None:
        env = {**os.environ, "PP_FILE": str(replica), "PP_RATES_FILE": str(cache), "CACHE": str(cache)}
        result = subprocess.run(
            [sys.executable, "-c", PRELUDE + code], capture_output=True, text=True, env=env, timeout=180
        )
        assert result.returncode == 0, result.stderr

    yield go
    replica.chmod(0o644)


def test_each_date_uses_its_own_rate(run):
    run("""
start(); write_cache(USD=fresh())
h = bridge.get_client()
assert usd(h, "2024-02-01") == 50_000_00
assert usd(h, "2024-05-31") == 50_000_00, "between two rates, the earlier one"
assert usd(h, "2024-06-03") == 80_000_00
assert usd(h, TODAY) == 62_500_00
assert h.rates.warnings(dt.date(2024, 2, 2), TODAY) == []
assert at.get_performance("2025")["warnings"] != [], "2024-06 is too old for 2025"
assert not any("USD" in w for w in rt.get_holdings()["warnings"])
""")


def test_every_converting_tool_reports_old_rates(run):
    run("""
start(); write_cache(USD={"2024-02-01": "2"})
for result in (rt.list_accounts(), rt.list_portfolios(), rt.get_holdings(), rt.get_allocation("Regions"),
               at.get_performance("1Y"), at.get_performance_snapshot("1Y"), at.get_security_performance("1Y"),
               at.get_dividends("1Y"), at.get_performance_series("1Y")):
    assert any(f"EUR/USD rate used for {TODAY} is from 2024-02-01" in w for w in result["warnings"]), result["warnings"]
""")


def test_dates_before_the_first_rate_are_reported(run):
    run("""
start(); write_cache(USD=fresh())
h = bridge.get_client()
# Portfolio Performance's built-in rate of 2015-12-18 is the first one.
assert "before 2015-12-18" in h.rates.warnings(dt.date(2010, 1, 1))[0]
""")


def test_the_app_updating_its_rates_is_picked_up(run):
    run("""
start(); write_cache(USD={str(TODAY): "2"})
h = bridge.get_client()
assert usd(h, TODAY) == 50_000_00
assert bridge.get_client() is h
write_cache(USD={str(TODAY): "1.25", "2024-01-01": "3"})
h2 = bridge.get_client()
assert h2 is not h and usd(h2, TODAY) == 80_000_00
""")


def test_a_broken_cache_is_a_warning_and_recovers(run):
    run("""
start(); CACHE.write_bytes(b"not a protobuf file")
warnings = rt.get_holdings()["warnings"]
assert any("could not be read" in w and "USD" in w for w in warnings), warnings
write_cache(USD=fresh())
assert not any("USD" in w for w in rt.get_holdings()["warnings"])
""")


def test_no_cache_is_a_warning(run):
    run("""
import dataclasses
config = dataclasses.replace(start(), rates_file=CACHE.with_name("none.pb"))
h = bridge.get_client(config)
found = h.rates.warnings(TODAY)
assert len(found) == 1 and "not found" in found[0] and "PP_RATES_FILE" in found[0], found
""")


def test_a_currency_without_any_rate_is_a_warning(run):
    run("""
start()
client = bridge.get_client().client
factory = j.exchange_rate_provider_factory()(client)
rates.used_currencies = lambda c: {"VND", "GBX"}
got = rates.load(client, factory, CACHE)
assert got.missing == {"VND"}
warnings = got.warnings(TODAY)
assert any("VND" in w and "1:1" in w for w in warnings), warnings
assert any("EUR/GBP" in w for w in warnings), "GBX goes through GBP, whose rate is old"
""")


def test_the_cache_is_never_written(run):
    run("""
start(); write_cache(USD=fresh())
before = CACHE.read_bytes(), CACHE.stat().st_mtime_ns
at.get_performance("1Y")
assert (CACHE.read_bytes(), CACHE.stat().st_mtime_ns) == before
assert [p.name for p in CACHE.parent.iterdir() if p.name.startswith("rates")] == ["rates.pb"]
""")
