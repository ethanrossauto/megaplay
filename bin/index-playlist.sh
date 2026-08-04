#!/usr/bin/env bash
# Build/refresh the cached full track list for a playlist so batches can be sliced
# without re-querying Spotify per track. Writes index/<name>.spotdl (spotdl's own format).
# Usage: index-playlist.sh <name> <playlist_id>
set -u
source "$(dirname "$0")/env.sh"
name="$1"; pid="$2"
out="$INDEX/$name.spotdl"
mkdir -p "$(dirname "$out")"   # nested names ("<Parent>/<Sub>") need the parent dir to exist
echo "Indexing '$name' (playlist $pid) -> $out"
"$SPOTDL" save "$(spotify_url "$pid")" --save-file "$out" >/dev/null 2>&1
if [ -s "$out" ]; then
  n=$("$PY" -c "import json;print(len(json.load(open('$out'))))" 2>/dev/null)
  echo "Indexed $n tracks."
else
  echo "Index failed (Spotify rate limit?). Retry later." >&2
  exit 1
fi
