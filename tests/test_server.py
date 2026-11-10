from __future__ import annotations

import asyncio

from fastmcp import Client

from pp_mcp.server import read_only


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


def test_no_write_module_is_importable():
    """SAFETY.md layer 2: the read server cannot reach write code, because none exists yet."""
    import importlib

    for module in ("pp_mcp.write_tools", "pp_mcp.safety"):
        try:
            importlib.import_module(module)
        except ImportError:
            continue
        raise AssertionError(f"{module} exists; the read-only server must not be able to import it")
