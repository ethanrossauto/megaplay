#!/usr/bin/env bash
# Add a playlist OR an album: register it, then grab the first <cap> songs.
#
# Usage: add-playlist.sh <spotify-url-or-playlist-id> [cap] [name] [--no-grab]
#   cap   default 100 for a playlist; for an ALBUM it defaults to the album's own track count
#         (a 14-track album with cap 100 just looks perpetually unfinished)
#   name  default = the real Spotify name (sanitized for a folder). Pass "<Megaplaylist>/<Name>"
#         to drop it straight into a megaplaylist - see CLAUDE.md "Megaplaylists".
#   --no-grab  register only, don't download. Use when queueing several sources for
#              bin/autopull.sh to pull one at a time - see CLAUDE.md "Autonomous pulling".
#
# ALBUMS ARE FIRST CLASS (2026-07-27). The old version stripped ".*/playlist/" off the input, which
# silently did nothing to an album URL: the whole URL became the "id" and the name lookup then
# called playlist() on it and failed. That is how "10 Day" ended up holding a full URL.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"
raw="${1:?usage: add-playlist.sh <spotify-url-or-id> [cap] [name] [--no-grab]}"
no_grab=""
args=()
for a in "$@"; do
  if [ "$a" = "--no-grab" ]; then no_grab=1; else args+=("$a"); fi
done
set -- "${args[@]}"
cap="${2:-}"; name="${3:-}"

kind="$(entity_of "$raw")"       # album | playlist
id="$(spotify_id "$raw")"        # bare id, whether given a URL or an id

info="$("$PY" - "$id" "$kind" "$SPOTIFY_ID" "$SPOTIFY_SECRET" <<'PY'
import sys, re
from spotdl.utils.spotify import SpotifyClient
pid, kind, cid, secret = sys.argv[1:5]
SpotifyClient.init(client_id=cid, client_secret=secret, user_auth=False,
                   cache_path=None, no_cache=True)
sp = SpotifyClient()
if kind == "album":
    a = sp.album(pid)
    name = a["name"]
    total = a.get("total_tracks") or len(sp.album_tracks(pid)["items"])
else:
    name = sp.playlist(pid)["name"]
    total, off = 0, 0
    while True:
        page = sp.playlist_items(pid, limit=100, offset=off)
        total += len(page["items"])
        if not page.get("next"): break
        off += 100
name = re.sub(r'[\\/]', ' ', name).strip()   # keep it filesystem-clean
print(f"{name}\t{total}")
PY
)" || info=""

resolved="${info%%$'\t'*}"
total="${info##*$'\t'}"
[ -n "$name" ] || name="$resolved"
[ -n "$name" ] || { echo "Could not resolve a name for that $kind; pass one explicitly."; exit 1; }
case "$total" in ''|*[!0-9]*) total=0;; esac

# Cap: an album defaults to its own length, a playlist to 100.
if [ -z "$cap" ]; then
  if [ "$kind" = album ] && [ "$total" -gt 0 ]; then cap="$total"; else cap=100; fi
fi
[ "$total" -gt 0 ] && [ "$cap" -gt "$total" ] && \
  echo "NOTE: cap $cap is higher than this $kind's $total tracks - it will fill to $total and stop."

# Registry stores a bare id for a playlist, the canonical URL for an album; env.sh's
# spotify_url()/entity_of() read either form, and that URL is what marks it as an album.
if [ "$kind" = album ]; then store="https://open.spotify.com/album/$id"; else store="$id"; fi

echo "Adding $kind '$name' ($total tracks, cap $cap)."
reg_add "$name" "$store" "$cap" 0
mkdir -p "$MUSIC/$name"
if [ -n "$no_grab" ]; then
  echo "Registered only (--no-grab). Queue it with: echo '$name' >> .state/queue.tsv"
else
  "$HERE/grab.sh" "$name"
fi
