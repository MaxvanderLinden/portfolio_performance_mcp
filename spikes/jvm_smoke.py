"""Spike: prove we can drive Portfolio Performance's own Java engine from Python.

Run:  PP_FILE=/path/to/portfolio.xml uv run --with jpype1 python spikes/jvm_smoke.py
Verified working 2026-09-05 on macOS x86_64, PP 0.87.0, Python 3.13, JPype 1.7.1.
"""

import glob
import os
import time

import jpype

PP_APP = os.environ.get("PP_APP_PATH", "/Applications/PortfolioPerformance.app")
PLUGINS = f"{PP_APP}/Contents/Eclipse/plugins"
FILE = os.environ["PP_FILE"]


def main() -> None:
    libjvm = glob.glob(f"{PLUGINS}/name.abuchen.zulu.jre*/jre/Contents/Home/lib/server/libjvm.dylib")[0]
    jpype.startJVM(libjvm, classpath=glob.glob(f"{PLUGINS}/*.jar"))

    ClientFactory = jpype.JClass("name.abuchen.portfolio.model.ClientFactory")
    FileInputStream = jpype.JClass("java.io.FileInputStream")
    ExchangeRateProviderFactory = jpype.JClass("name.abuchen.portfolio.money.ExchangeRateProviderFactory")
    CurrencyConverterImpl = jpype.JClass("name.abuchen.portfolio.money.CurrencyConverterImpl")
    ClientSnapshot = jpype.JClass("name.abuchen.portfolio.snapshot.ClientSnapshot")
    LocalDate = jpype.JClass("java.time.LocalDate")

    t0 = time.time()
    client = ClientFactory.load(FileInputStream(FILE))
    print(f"loaded in {time.time() - t0:.2f}s: {client.getSecurities().size()} securities, base {client.getBaseCurrency()}")

    converter = CurrencyConverterImpl(ExchangeRateProviderFactory(client), client.getBaseCurrency())
    snapshot = ClientSnapshot.create(client, converter, LocalDate.now())
    print("total assets:", snapshot.getMonetaryAssets())
    for position in snapshot.getPortfolios():
        print("  ", position.getPortfolio().getName(), position.getValue())


if __name__ == "__main__":
    main()
