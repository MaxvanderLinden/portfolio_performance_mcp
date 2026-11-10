"""Print a summary of the replica, to be checked against the numbers the app shows.

    python -m pp_mcp
"""

from __future__ import annotations

import sys

import jpype

from .bridge import BridgeError, fingerprint, get_client
from .config import ConfigError, load_config
from .convert import account, money, portfolio, security


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        handle = get_client(config)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = handle.client
    print(f"source: {handle.path}  ({handle.size:,} bytes)")

    marks = fingerprint(handle)
    print(
        f"\n{marks.securities} securities, {marks.accounts} accounts, {marks.portfolios} portfolios, "
        f"{marks.taxonomies} taxonomies, {marks.transactions} transactions, {marks.prices:,} prices"
    )
    print(f"total assets: {marks.total_assets}")

    snapshot_class = jpype.JClass("name.abuchen.portfolio.snapshot.ClientSnapshot")
    local_date = jpype.JClass("java.time.LocalDate")
    snapshot = snapshot_class.create(client, handle.converter, local_date.now())

    print("\nportfolios")
    for position in snapshot.getPortfolios():
        info = portfolio(position.getPortfolio())
        print(f"  {info.name:<28} {money(position.getValue())!s:>18}  ({info.transaction_count} tx)")

    print("\naccounts")
    for position in snapshot.getAccounts():
        info = account(position.getAccount())
        print(f"  {info.name:<28} {money(position.getFunds())!s:>18}  ({info.transaction_count} tx)")

    print("\nsecurities")
    for java_security in client.getSecurities():
        info = security(java_security)
        latest = f"{info.latest.value:,.4f} @ {info.latest.date}" if info.latest else "no price"
        print(f"  {info.name:<40} {info.isin or '-':<14} {info.price_count:>5} prices  {latest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
