from __future__ import annotations

import asyncio

from fastmcp import Client

from pp_engine_mcp.server import read_only


def _call(name, args=None):
    async def go():
        async with Client(read_only()) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            result = await client.call_tool(name, args or {})
            return tools, result.data

    return asyncio.run(go())


def test_tools_are_registered_and_marked_read_only(handle):
    tools, _ = _call("list_accounts")
    assert set(tools) == {
        "list_securities", "list_accounts", "list_portfolios", "get_holdings",
        "get_transactions", "get_prices", "list_taxonomies", "get_allocation",
        "get_performance", "get_performance_snapshot", "get_security_performance",
        "get_dividends", "get_performance_series",
    }
    for tool in tools.values():
        assert tool.annotations is not None, f"{tool.name} was registered with no annotations"
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_tool_call_returns_live_numbers(handle):
    _, data = _call("get_holdings")
    assert data["total_assets"]["currency"] == str(handle.client.getBaseCurrency())
    assert data["count"] > 0


WRITE_MODULES = ("pp_engine_mcp.write_tools", "pp_engine_mcp.staging", "pp_engine_mcp.write_common", "pp_engine_mcp.trade_tools",
                 "pp_engine_mcp.classification_tools")


def _modules_after(code: str) -> set[str]:
    """Run `code` in a fresh interpreter and report which write modules it loaded."""
    import json
    import subprocess
    import sys

    probe = f"""
import json, sys
{code}
print(json.dumps([m for m in {WRITE_MODULES!r} if m in sys.modules]))
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_importing_the_server_loads_no_write_code():
    """SAFETY.md layer 2: the write modules are imported inside read_write() only."""
    assert _modules_after("import pp_engine_mcp, pp_engine_mcp.server, pp_engine_mcp.__main__") == set()


def test_read_only_server_loads_no_write_code(config):
    assert _modules_after("from pp_engine_mcp.server import read_only; read_only()") == set()


def test_write_server_adds_only_annotated_write_tools(config, tmp_path, monkeypatch):
    from pp_engine_mcp.bridge import set_config_provider
    from pp_engine_mcp.server import read_write
    from pp_engine_mcp.write_tools import STAGED_READ_TOOLS, WRITE_TOOLS

    monkeypatch.setenv("PP_STAGED_DIR", str(tmp_path / "staged"))

    async def go():
        async with Client(read_write()) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    try:
        tools = asyncio.run(go())
    finally:
        set_config_provider(None)

    read, _ = _call("list_accounts")
    write_names = {tool.__name__ for tool in WRITE_TOOLS}
    staged_reads = {tool.__name__ for tool in STAGED_READ_TOOLS}
    assert write_names == {
        "add_prices", "record_cash", "discard_staged", "add_security", "record_buy", "record_sell",
        "record_dividend", "record_cash_transfer", "record_share_transfer", "record_delivery",
        "add_classification", "assign_classification",
    }
    assert set(tools) == set(read) | write_names | staged_reads
    for name, tool in tools.items():
        assert tool.annotations is not None
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.read_only_hint is (name not in write_names)
