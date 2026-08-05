#!/usr/bin/env bash
# Delete a playlist: remove its music folder and its registry entry. Keeps the index cache
# (harmless) unless --purge is given.
# Usage: delete-playlist.sh <name> [--purge]
set -u
source "$(dirname "$0")/env.sh"
name="${1-}"; purge="${2:-}"

# The name is validated BEFORE anything is removed, because the `rm -rf` below is built from
# it. `set -u` catches a MISSING argument but not an EMPTY one, so `delete-playlist.sh ""`
# expanded to `rm -rf "$MUSIC/"` and took the entire library with it. A ".." component walks
# out of $MUSIC altogether. Neither is a playlist name, so neither gets as far as the rm.
case "$name" in
  "")                     echo "usage: delete-playlist.sh <name> [--purge]" >&2; exit 2;;
  /*|.|..)                echo "refusing '$name': not a playlist name" >&2; exit 2;;
  ../*|*/../*|*/..)       echo "refusing '$name': path traversal" >&2; exit 2;;
esac

[ -d "$MUSIC/$name" ] || echo "(folder '$MUSIC/$name' not present)"
rm -rf "$MUSIC/${name:?}"   # :? is the second layer, in the rm itself, where it cannot be skipped
reg_del "$name"
[ "$purge" = "--purge" ] && rm -f "$INDEX/$name.spotdl"
# Nested "<Parent>/<Sub>": drop the parent folder too once its last child is gone.
case "$name" in */*) rmdir "$MUSIC/${name%%/*}" 2>/dev/null;; esac
echo "Deleted playlist '$name'. Remaining:"
"$(dirname "$0")/status.sh"
