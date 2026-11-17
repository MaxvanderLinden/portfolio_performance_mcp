#!/usr/bin/env bash
# Copy the staged portfolio over your live Portfolio Performance file. You run this, by hand;
# no MCP tool can (SAFETY.md, layers 3-5).
#
#   PP_LIVE_FILE=/path/to/portfolio.xml ./scripts/promote-staged.sh
#
# Refuses unless all of these hold:
#   * Portfolio Performance is not running (it would overwrite the file on its next save);
#   * the live file is byte-identical to the replica the changes were staged on, so nothing
#     you did in the app since is lost;
#   * the staged file is the one pp-write produced (not edited since);
#   * you confirm at the terminal.
# The live file is backed up first, and the replica is refreshed afterwards.
set -euo pipefail

cd "$(dirname "$0")/.."

LIVE="${PP_LIVE_FILE:?set PP_LIVE_FILE to your live Portfolio Performance .xml file}"
STAGED_DIR="${PP_STAGED_DIR:-$HOME/pp-mcp/staged}"
STAGED="$STAGED_DIR/portfolio.staged.xml"
MANIFEST="$STAGED_DIR/manifest.json"
BACKUPS="$STAGED_DIR/backups"

die() { echo "promote: $*" >&2; exit 1; }

if command -v sha256sum >/dev/null 2>&1; then
  sha() { sha256sum "$1" | cut -d' ' -f1; }
else
  sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

manifest() {  # manifest <python expression over m>
  python3 -I -c 'import json, sys; m = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$MANIFEST" "$1"
}

[ -f "$LIVE" ] || die "live file not found: $LIVE"
[ -f "$STAGED" ] || die "nothing staged ($STAGED does not exist)"
[ -f "$MANIFEST" ] || die "$MANIFEST is missing; cannot tell what the staged file is based on"

if pgrep -f PortfolioPerformance >/dev/null 2>&1; then
  die "Portfolio Performance is running. Quit it first; it rewrites the whole file on save."
fi

base_sha="$(manifest 'm["base_sha256"]')"
staged_sha="$(manifest 'm["staged_sha256"]')"

[ "$(sha "$STAGED")" = "$staged_sha" ] \
  || die "the staged file changed since pp-write wrote it; not promoting an unknown file"
[ "$(sha "$LIVE")" = "$base_sha" ] \
  || die "your live file changed since the replica these changes are based on.
Promoting would lose those edits. Delete $STAGED_DIR/{portfolio.staged.xml,manifest.json},
refresh the replica, and stage the changes again."

echo "Staged changes ($(manifest 'len(m["changes"])')):"
manifest '"\n".join("  " + c["at"] + "  " + c["description"] + "  " + str(c["delta"]) for c in m["changes"])'
echo
echo "live file: $LIVE"

# Ask the terminal, not stdin: a pipe or an agent's shell cannot answer this.
if ! { exec 3</dev/tty; } 2>/dev/null; then
  die "no terminal to confirm on; run this yourself in a terminal"
fi
printf 'Write these changes to your live file? [y/N] ' >/dev/tty
read -r answer <&3 || answer=""
exec 3<&-
case "$answer" in y|Y|yes|YES) ;; *) die "cancelled; nothing changed" ;; esac

stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUPS"
backup="$BACKUPS/$(basename "$LIVE" .xml).$stamp.xml"
cp -p "$LIVE" "$backup"

# Copy next to the live file, re-check it did not change meanwhile, then rename atomically.
tmp="$(dirname "$LIVE")/.$(basename "$LIVE").promote.tmp"
cp "$STAGED" "$tmp"
chmod u+w "$tmp"
if [ "$(sha "$LIVE")" != "$base_sha" ]; then
  rm -f "$tmp"
  die "the live file changed while promoting; nothing changed (backup kept at $backup)"
fi
mv -f "$tmp" "$LIVE"
echo "promoted; previous version backed up to $backup"

archive="$BACKUPS/promoted-$stamp"
mkdir -p "$archive"
mv "$MANIFEST" "$archive/"
chmod u+w "$STAGED" && mv "$STAGED" "$archive/"

PP_LIVE_FILE="$LIVE" ./scripts/refresh-replica.sh
