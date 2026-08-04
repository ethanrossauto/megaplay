#!/usr/bin/env bash
# "Drop the current batch and grab the next <cap> songs" from a playlist.
# Deletes the songs currently in the folder, advances the offset by cap, downloads the next batch.
# Usage: more.sh <name>
set -u
source "$(dirname "$0")/env.sh"
name="$1"
pid="$(reg_get "$name" playlist_id)"
[ -n "$pid" ] || { echo "Unknown playlist '$name'"; exit 1; }
cap="$(reg_get "$name" cap)"
off="$(reg_get "$name" offset)"

echo "Dropping current $(folder_count "$name") songs in '$name' and grabbing the next $cap..."
rm -f "$MUSIC/$name"/*.mp3 "$MUSIC/$name"/*.mp3.skip
newoff=$((off + cap))
reg_set "$name" offset "$newoff"
"$(dirname "$0")/grab.sh" "$name"
