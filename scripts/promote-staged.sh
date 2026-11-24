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
#   * the staged file is the one pp-write produced (not edited since), and still the one you
#     reviewed when you confirm;
#   * you confirm at the terminal.
# The live file is backed up first, and the replica is refreshed afterwards.
set -euo pipefail

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"

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

manifest() { python3 -I "$SCRIPTS/staged_manifest.py" "$@"; }

[ -f "$LIVE" ] || die "live file not found: $LIVE"
[ -f "$STAGED" ] || die "nothing staged ($STAGED does not exist)"
[ -f "$MANIFEST" ] || die "$MANIFEST is missing; cannot tell what the staged file is based on"

# Replace the file a symlink points at, not the symlink.
if [ -L "$LIVE" ]; then
  LIVE="$(python3 -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$LIVE")"
fi

# Is the app running? Fail closed: if we cannot tell, do not promote.
CANNOT_TELL="cannot check whether Portfolio Performance is running"
refuse_if_running() {
  local checked="" status tasks tasklist="" switch=""
  if command -v pgrep >/dev/null 2>&1; then
    status=0
    pgrep -f PortfolioPerformance >/dev/null 2>&1 || status=$?
    case "$status" in
      0) die "Portfolio Performance is running. Quit it first; it rewrites the whole file on save." ;;
      1) checked=1 ;;  # ran, and found nothing
      *) die "$CANNOT_TELL (pgrep failed with status $status)" ;;
    esac
  fi
  if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
    # pgrep inside WSL does not see Windows processes, so it settles nothing.
    checked=""
    tasklist="tasklist.exe" switch="/FI"
  elif [ -z "$checked" ]; then
    tasklist="tasklist" switch="//FI"  # Git Bash rewrites a single slash into a path
  fi
  if [ -z "$checked" ]; then
    command -v "$tasklist" >/dev/null 2>&1 || die "$CANNOT_TELL (no usable pgrep or tasklist)"
    tasks="$("$tasklist" "$switch" "IMAGENAME eq PortfolioPerformance.exe" 2>/dev/null)" \
      || die "$CANNOT_TELL ($tasklist failed)"
    case "$tasks" in
      *PortfolioPerformance*) die "Portfolio Performance is running. Quit it first; it rewrites the whole file on save." ;;
    esac
  fi
}
refuse_if_running

manifest_sha="$(sha "$MANIFEST")"
base_sha="$(manifest field "$MANIFEST" base_sha256)"
staged_sha="$(manifest field "$MANIFEST" staged_sha256)"
replica="$(manifest field "$MANIFEST" replica)"

[ "$(sha "$STAGED")" = "$staged_sha" ] \
  || die "the staged file changed since pp-write wrote it; not promoting an unknown file"
[ "$(sha "$LIVE")" = "$base_sha" ] \
  || die "your live file changed since the replica these changes are based on.
Promoting would lose those edits. Set the staged changes aside (the discard_staged tool, or move
portfolio.staged.xml and manifest.json out of $STAGED_DIR), refresh the replica, and stage the
changes again."

manifest show "$MANIFEST"
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

# From here on, keep pp-write out. The lock belongs to the open file, so it outlives the python
# that takes it and ends when we close descriptor 9. Windows Python has no fcntl; there the
# checksums below are the only guard.
exec 9>>"$STAGED_DIR/.lock"
locked=0
python3 -I -c '
import sys
try:
    import fcntl
except ImportError:
    sys.exit(0)
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
' || locked=$?
[ "$locked" -eq 0 ] || die "pp-write is staging a change right now; nothing changed, try again"

# Build the new file next to the live one. Starting from a copy of the live file keeps its
# permissions; then check that everything is still what was reviewed, and rename atomically.
tmp="$(dirname "$LIVE")/.$(basename "$LIVE").promote.tmp"
trap 'rm -f "$tmp"' EXIT
rm -f "$tmp"
cp -p "$LIVE" "$tmp"
chmod u+w "$tmp"
cat "$STAGED" >"$tmp"

stamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUPS"
backup="$BACKUPS/$(basename "$LIVE" .xml).$stamp.xml"
cp -p "$LIVE" "$backup"

refuse_if_running
[ "$(sha "$MANIFEST")" = "$manifest_sha" ] && [ "$(sha "$tmp")" = "$staged_sha" ] \
  || die "the staged changes changed while you were reviewing them; nothing changed"
[ "$(sha "$backup")" = "$base_sha" ] && [ "$(sha "$LIVE")" = "$base_sha" ] \
  || die "the live file changed while promoting; nothing changed (backup kept at $backup)"
mv -f "$tmp" "$LIVE"
echo "promoted; previous version backed up to $backup"

archive="$BACKUPS/promoted-$stamp"
mkdir -p "$archive"
mv "$MANIFEST" "$archive/"
chmod u+w "$STAGED" && mv "$STAGED" "$archive/"
exec 9>&-

# Refresh the replica these changes were staged on.
PP_LIVE_FILE="$LIVE" PP_FILE="$replica" bash "$SCRIPTS/refresh-replica.sh"
