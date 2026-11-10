#!/usr/bin/env bash
# Copy the live Portfolio Performance file to a read-only replica for the MCP server.
#
#   PP_LIVE_FILE=/path/to/portfolio.xml ./scripts/refresh-replica.sh
#
# The server is never told where the live file is; this script is the only thing that touches it,
# and only to read. See SAFETY.md. Works on macOS and Linux (on Windows, use WSL or copy the file
# by hand and mark it read-only).
set -euo pipefail

LIVE="${PP_LIVE_FILE:?set PP_LIVE_FILE to your live Portfolio Performance .xml file}"
REPLICA="${PP_FILE:-$HOME/pp-mcp/replica/portfolio.xml}"

# BSD (macOS) and GNU (Linux) stat take different flags.
if [ "$(uname)" = "Darwin" ]; then
  identity() { stat -f '%m %z' "$1"; }
  blocks() { stat -f %b "$1"; }
  size() { stat -f %z "$1"; }
else
  identity() { stat -c '%Y %s' "$1"; }
  blocks() { stat -c %b "$1"; }
  size() { stat -c %s "$1"; }
fi

[ -f "$LIVE" ] || { echo "live file not found: $LIVE" >&2; exit 1; }

# iCloud may keep the file as a dataless placeholder; materialise it before copying.
if [ "$(uname)" = "Darwin" ] && [ "$(blocks "$LIVE")" -eq 0 ]; then
  echo "materialising from iCloud..."
  brctl download "$LIVE"
  for _ in $(seq 1 30); do
    [ "$(blocks "$LIVE")" -gt 0 ] && break
    sleep 1
  done
fi

mkdir -p "$(dirname "$REPLICA")"

# Portfolio Performance rewrites the whole file on save, so a copy taken mid-save can be torn.
# Copy, then confirm the source did not change while we were reading it.
for attempt in 1 2 3; do
  before="$(identity "$LIVE")"
  rm -f "$REPLICA"
  cp "$LIVE" "$REPLICA"
  after="$(identity "$LIVE")"
  if [ "$before" = "$after" ]; then
    chmod 444 "$REPLICA"
    echo "replica: $REPLICA ($(size "$REPLICA") bytes)"
    if pgrep -f PortfolioPerformance >/dev/null 2>&1; then
      echo "note: Portfolio Performance is running; unsaved changes in the app are not in this replica."
    fi
    exit 0
  fi
  echo "source changed during copy (attempt $attempt), retrying..." >&2
  sleep 2
done

echo "could not take a stable copy; is Portfolio Performance saving repeatedly?" >&2
exit 1
