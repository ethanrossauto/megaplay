#!/usr/bin/env bash
# Delete a playlist: remove its music folder and its registry entry. Keeps the index cache
# (harmless) unless --purge is given.
# Usage: delete-playlist.sh <name> [--purge]
set -u
source "$(dirname "$0")/env.sh"
name="$1"; purge="${2:-}"
[ -d "$MUSIC/$name" ] || echo "(folder '$MUSIC/$name' not present)"
rm -rf "$MUSIC/$name"
reg_del "$name"
[ "$purge" = "--purge" ] && rm -f "$INDEX/$name.spotdl"
# Nested "<Parent>/<Sub>": drop the parent folder too once its last child is gone.
case "$name" in */*) rmdir "$MUSIC/${name%%/*}" 2>/dev/null;; esac
echo "Deleted playlist '$name'. Remaining:"
"$(dirname "$0")/status.sh"
