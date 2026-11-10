"""JVM lifecycle and access to Portfolio Performance's own model classes.

Rather than parsing the XStream XML (1264 back-references, cross-entry pairs), we load the file
with PP's `ClientFactory` and read the resulting object graph. Everything derived from it —
values, performance, allocations — therefore matches what the application displays.
"""

from __future__ import annotations

import glob
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jpype

from .config import Config, load_config

# Fixed-point scaling used throughout the PP model; verified against Values.* at startup.
AMOUNT_FACTOR = 100
SHARE_FACTOR = 100_000_000
QUOTE_FACTOR = 100_000_000


class BridgeError(Exception):
    """Raised when the JVM or the portfolio file cannot be loaded."""


_lock = threading.RLock()
_started = False
_cache: "ClientHandle | None" = None


@dataclass(frozen=True)
class Fingerprint:
    """Cheap structural summary, used to prove a file did not change unexpectedly."""

    securities: int
    accounts: int
    portfolios: int
    taxonomies: int
    transactions: int
    prices: int
    total_assets: str


@dataclass
class ClientHandle:
    """A loaded portfolio, plus the file identity it was loaded from."""

    client: Any
    converter: Any
    path: Path
    mtime_ns: int
    size: int

    def is_stale(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return True
        return stat.st_mtime_ns != self.mtime_ns or stat.st_size != self.size


def find_libjvm(config: Config) -> Path:
    matches = glob.glob(
        str(config.plugins / "name.abuchen.zulu.jre*" / "jre" / "Contents" / "Home" / "lib" / "server" / "libjvm.dylib")
    )
    if not matches:
        raise BridgeError(
            f"No bundled JRE found under {config.plugins}. Portfolio Performance ships one; "
            "if this build does not, set JAVA_HOME to a Java 21 runtime and adapt find_libjvm()."
        )
    return Path(sorted(matches)[-1])


def find_classpath(config: Config) -> list[str]:
    jars = glob.glob(str(config.plugins / "*.jar"))
    if not any("name.abuchen.portfolio_" in jar for jar in jars):
        raise BridgeError(f"Portfolio Performance core jar not found in {config.plugins}.")
    return jars


def start_jvm(config: Config | None = None) -> None:
    """Start the embedded JVM once per process. Safe to call repeatedly."""
    global _started
    with _lock:
        if _started:
            return
        config = config or load_config()
        if not jpype.isJVMStarted():
            jpype.startJVM(
                str(find_libjvm(config)),
                classpath=find_classpath(config),
                convertStrings=False,
            )
        _verify_scaling_factors()
        _started = True


def _verify_scaling_factors() -> None:
    """Guard against a future PP release changing its fixed-point scaling under us."""
    values = jpype.JClass("name.abuchen.portfolio.money.Values")
    actual = {
        "Amount": (int(values.Amount.factor()), AMOUNT_FACTOR),
        "Share": (int(values.Share.factor()), SHARE_FACTOR),
        "Quote": (int(values.Quote.factor()), QUOTE_FACTOR),
    }
    wrong = {name: pair for name, pair in actual.items() if pair[0] != pair[1]}
    if wrong:
        raise BridgeError(
            "Portfolio Performance changed its scaling factors "
            f"(got/expected: {wrong}). Update the constants in bridge.py before trusting any number."
        )


def ensure_thread_attached() -> None:
    """FastMCP runs sync tools in worker threads; JPype needs each one attached to the JVM."""
    if not jpype.isJVMStarted():
        return
    thread = jpype.JClass("java.lang.Thread")
    if not thread.isAttached():
        thread.attach()


def _load(config: Config) -> ClientHandle:
    stat = config.source.stat()
    ClientFactory = jpype.JClass("name.abuchen.portfolio.model.ClientFactory")
    FileInputStream = jpype.JClass("java.io.FileInputStream")

    stream = FileInputStream(str(config.source))
    try:
        client = ClientFactory.load(stream)
    except jpype.JException as exc:  # pragma: no cover - depends on a broken file
        raise BridgeError(f"Could not load {config.source}: {exc.message()}") from exc
    finally:
        stream.close()

    return ClientHandle(
        client=client,
        converter=currency_converter(client),
        path=config.source,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def currency_converter(client: Any, currency: str | None = None) -> Any:
    factory = jpype.JClass("name.abuchen.portfolio.money.ExchangeRateProviderFactory")(client)
    impl = jpype.JClass("name.abuchen.portfolio.money.CurrencyConverterImpl")
    return impl(factory, currency or str(client.getBaseCurrency()))


def get_client(config: Config | None = None, force_reload: bool = False) -> ClientHandle:
    """Return the loaded portfolio, reloading only when the replica has changed on disk."""
    global _cache
    config = config or load_config()
    start_jvm(config)
    ensure_thread_attached()

    with _lock:
        if (
            force_reload
            or _cache is None
            or _cache.path != config.source
            or _cache.is_stale()
        ):
            _cache = _load(config)
        return _cache


def fingerprint(handle: ClientHandle) -> Fingerprint:
    client = handle.client
    accounts = list(client.getAccounts())
    portfolios = list(client.getPortfolios())
    securities = list(client.getSecurities())

    snapshot_class = jpype.JClass("name.abuchen.portfolio.snapshot.ClientSnapshot")
    local_date = jpype.JClass("java.time.LocalDate")
    snapshot = snapshot_class.create(client, handle.converter, local_date.now())

    return Fingerprint(
        securities=len(securities),
        accounts=len(accounts),
        portfolios=len(portfolios),
        taxonomies=client.getTaxonomies().size(),
        transactions=sum(a.getTransactions().size() for a in accounts)
        + sum(p.getTransactions().size() for p in portfolios),
        prices=sum(s.getPrices().size() for s in securities),
        total_assets=str(snapshot.getMonetaryAssets()),
    )


def reset_for_tests() -> None:
    """Drop the cached client. The JVM itself cannot be restarted within a process."""
    global _cache
    with _lock:
        _cache = None
