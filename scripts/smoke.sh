#!/usr/bin/env bash
# Exercise the MCP server the way a client does: over stdio, through the installed entry point.
#
#   ./scripts/smoke.sh            # a representative call from each group
#   ./scripts/smoke.sh list       # just enumerate the tools
set -euo pipefail

cd "$(dirname "$0")/.."
FASTMCP="./.venv/bin/fastmcp"
SERVER="$PWD/.venv/bin/pp-read"

call() {
  local tool="$1" args="${2:-}"
  [ -n "$args" ] || args='{}'
  echo "=== $tool $args"
  # Note: with --command, key=value arguments collide with the server-spec positional,
  # so arguments go in --input-json.
  "$FASTMCP" call --command "$SERVER" --target "$tool" --input-json "$args" --json
  echo
}

if [ "${1:-}" = "list" ]; then
  exec "$FASTMCP" list --command "$SERVER"
fi

call list_portfolios
call get_holdings
call get_performance '{"period":"YTD"}'
call get_performance_snapshot '{"period":"1Y"}'
call get_dividends '{"by":"year"}'
call get_allocation '{"taxonomy":"Regions"}'
