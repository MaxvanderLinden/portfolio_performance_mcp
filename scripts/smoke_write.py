"""Exercise pp-write over stdio, the way an MCP client does, against a throwaway demo copy.

    ./.venv/bin/python scripts/smoke_write.py

Never touches your own replica or staging folder: the demo fixture is copied to a temporary
folder, and PP_FILE / PP_STAGED_DIR are passed to the server explicitly. (MCP clients start
servers with a filtered environment, so variables exported in your shell would NOT reach it,
and the server would fall back to ~/pp-mcp. That is why this does not use `fastmcp call`.)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "demo.xml"
SERVER = Path(sys.executable).with_name("pp-write")

CALLS: list[tuple[str, dict[str, object], bool]] = [  # tool, arguments, expected to succeed
    ("get_staged_changes", {}, True),
    ("add_prices", {"security": "DWLD", "prices": [{"date": "2026-11-03", "value": 101.5}]}, True),
    ("add_prices", {"security": "DWLD", "prices": [{"date": "2026-10-30", "value": 1}]}, False),
    ("record_cash", {"account": "Savings", "type": "DEPOSIT", "amount": 100, "date": "2026-11-03"}, True),
    ("record_cash", {"account": "Savings", "type": "INTEREST", "amount": 7.5, "tax": 2.5, "date": "2026-11-03"}, True),
    ("get_prices", {"security": "DWLD", "start": "2026-11-03", "end": "2026-11-03"}, True),
    ("add_security", {"name": "Smoke Bond Fund", "currency": "EUR", "isin": "XX0000000099"}, True),
    ("record_buy", {"portfolio": "Broker", "security": "XX0000000099", "shares": 10, "date": "2026-11-03", "price": 50, "fees": 1}, True),
    ("record_buy", {"portfolio": "Broker", "security": "DUSC", "shares": 2, "date": "2026-11-03", "price": 50}, False),
    ("record_sell", {"portfolio": "Broker", "security": "DWLD", "shares": 100000, "date": "2026-11-03", "price": 1}, False),
    ("record_dividend", {"account": "Broker cash", "security": "DWLD", "amount": 8.5, "tax": 1.5, "date": "2026-11-03"}, True),
    ("record_cash_transfer", {"from_account": "Savings", "to_account": "Broker cash", "amount": 100, "date": "2026-11-03"}, True),
    ("record_share_transfer", {"security": "DWLD", "shares": 1, "from_portfolio": "Pension", "to_portfolio": "Broker", "date": "2026-10-30"}, True),
    ("record_delivery", {"portfolio": "Pension", "security": "DEMW", "shares": 1, "date": "2026-11-03", "direction": "inbound", "price": 30}, True),
    ("add_classification", {"taxonomy": "Regions", "name": "Frontier markets"}, True),
    ("assign_classification", {"taxonomy": "Regions", "classification": "Frontier markets", "security": "DUSC", "weight": 25}, True),
    ("assign_classification", {"taxonomy": "Regions", "classification": "Europe", "security": "DWLD", "weight": 5}, False),
    ("get_allocation", {"taxonomy": "Regions"}, True),
    ("get_transactions", {"start": "2026-11-03", "end": "2026-11-03"}, True),
    ("get_staged_changes", {}, True),
    ("discard_staged", {}, True),
]


def _text(result_content: object) -> str:
    return result_content.text if isinstance(result_content, TextContent) else str(result_content)


async def run(work: Path) -> int:
    staged = work / "staged"
    env = {**os.environ, "PP_FILE": str(work / "replica" / "portfolio.xml"), "PP_STAGED_DIR": str(staged)}
    failures = 0
    async with Client(StdioTransport(command=str(SERVER), args=[], env=env)) as client:
        for name, arguments, should_succeed in CALLS:
            result = await client.call_tool(name, arguments, raise_on_error=False)
            text = _text(result.content[0]) if result.content else ""
            ok = result.is_error is not should_succeed
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name:20} {'error' if result.is_error else 'success'}: {text[:110]}")
            staged_file = result.structured_content.get("staged_file") if result.structured_content else None
            if isinstance(staged_file, str) and not Path(staged_file).resolve().is_relative_to(work.resolve()):
                print(f"ABORT: the server staged to {staged_file}, outside the test folder", file=sys.stderr)
                return 2
    return 1 if failures else 0


def main() -> int:
    if not SERVER.exists():
        print(f"pp-write not found next to {sys.executable}; run pip install -e . first", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="pp-write-smoke-"))
    try:
        (work / "replica").mkdir()
        replica = work / "replica" / "portfolio.xml"
        shutil.copyfile(FIXTURE, replica)
        replica.chmod(0o444)
        code = asyncio.run(run(work))
        print(json.dumps({"result": "passed" if code == 0 else "failed"}))
        return code
    finally:
        for path in work.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
