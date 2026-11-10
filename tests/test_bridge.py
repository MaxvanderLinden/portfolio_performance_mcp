"""Integration tests against the demo fixture. Skipped when Portfolio Performance is missing."""

from __future__ import annotations

from decimal import Decimal

from pp_mcp import convert
from pp_mcp.bridge import fingerprint, get_client


def test_loads_and_caches(config, handle):
    assert handle.client.getBaseCurrency() is not None
    assert get_client(config) is handle, "an unchanged replica must not be reloaded"


def test_fingerprint_is_populated(handle):
    marks = fingerprint(handle)
    assert marks.securities > 0
    assert marks.transactions > 0
    assert marks.total_assets.split()[0] == str(handle.client.getBaseCurrency())


def test_securities_convert(handle):
    securities = [convert.security(s) for s in handle.client.getSecurities()]
    assert securities
    for info in securities:
        assert info.name and info.uuid
        assert info.note is None, "notes must stay out of responses unless asked for"
        if info.latest is not None:
            assert isinstance(info.latest.value, Decimal)


def test_transactions_use_stable_enum_names(handle):
    known = {
        "DEPOSIT", "REMOVAL", "INTEREST", "INTEREST_CHARGE", "DIVIDENDS", "FEES", "FEES_REFUND",
        "TAXES", "TAX_REFUND", "BUY", "SELL", "TRANSFER_IN", "TRANSFER_OUT",
        "DELIVERY_INBOUND", "DELIVERY_OUTBOUND",
    }
    for java_account in handle.client.getAccounts():
        for java_transaction in java_account.getTransactions():
            info = convert.transaction(java_transaction, account_name=str(java_account.getName()))
            assert info.type in known, f"unexpected type {info.type}"
            assert info.amount is not None


def test_portfolio_transactions_convert(handle):
    seen = 0
    for java_portfolio in handle.client.getPortfolios():
        for java_transaction in java_portfolio.getTransactions():
            info = convert.transaction(java_transaction, portfolio_name=str(java_portfolio.getName()))
            assert info.security, "a portfolio transaction always has a security"
            seen += 1
    assert seen > 0


def test_responses_are_json_serialisable(handle):
    import json

    payload = [convert.security(s) for s in handle.client.getSecurities()]
    json.dumps(convert.jsonable(payload))


def test_reloads_when_the_replica_changes(config, handle):
    """Edits made in the app appear after the next refresh-replica run, without a restart."""
    import os

    stat = config.source.stat()
    os.utime(config.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    try:
        assert get_client(config) is not handle
    finally:
        os.utime(config.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
