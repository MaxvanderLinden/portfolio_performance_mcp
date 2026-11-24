"""Exercise pp-engine-mcp over stdio, the way an MCP client does, against your configured replica.

    ./.venv/bin/python scripts/smoke_read.py         # a representative call from each group
    ./.venv/bin/python scripts/smoke_read.py list    # just enumerate the tools

MCP clients start servers with a filtered environment, so PP_FILE and friends are passed on
explicitly (that is why this does not use `fastmcp call`).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

SERVER = Path(sys.executable).with_name("pp-engine-mcp")

CALLS: list[tuple[str, dict[str, object]]] = [
    ("list_portfolios", {}),
    ("get_holdings", {}),
    ("get_performance", {"period": "YTD"}),
    ("get_performance_snapshot", {"period": "1Y"}),
    ("get_dividends", {"by": "year"}),
]


async def run(list_only: bool) -> int:
    failures = 0
    async with Client(StdioTransport(command=str(SERVER), args=[], env=dict(os.environ))) as client:
        if list_only:
            for tool in await client.list_tools():
                print(tool.name)
            return 0
        calls = list(CALLS)
        listed = await client.call_tool("list_taxonomies", {})
        taxonomies = (listed.structured_content or {}).get("taxonomies", [])
        if taxonomies:
            calls.append(("get_allocation", {"taxonomy": taxonomies[0]["id"]}))
        for name, arguments in calls:
            result = await client.call_tool(name, arguments, raise_on_error=False)
            failures += bool(result.is_error)
            print(f"=== {'FAIL' if result.is_error else 'ok'} {name} {arguments}")
            print(result.content[0].text if result.content else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(sys.argv[1:] == ["list"])))
