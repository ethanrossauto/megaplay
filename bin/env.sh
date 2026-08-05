#!/usr/bin/env bash
# Shared environment + helpers. Source this from every other script:
#   source "$(dirname "$0")/env.sh"

# Nearly everything set here is read by the scripts that SOURCE this file, never by this file
# itself, so shellcheck sees a file full of unused variables. That is the point of the file.
# shellcheck disable=SC2034

# Project root, derived from where THIS file sits rather than hardcoded, so the
# tree works from whatever directory it was cloned into. BASH_SOURCE (not $0)
# because $0 is the calling script, and every caller lives in this same bin/.
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Machine-specific overrides live here and are gitignored, so a path that is
# true on one box never ends up in the repo. Sourced BEFORE the defaults below
# so everything it sets wins the ${VAR:-default} expansions.
# Gitignored and absent in a fresh clone, by design, so there is nothing for shellcheck to follow.
# shellcheck source=/dev/null
[ -f "$PROJECT/bin/env.local.sh" ] && . "$PROJECT/bin/env.local.sh"

MUSIC="${MEGAPLAY_MUSIC:-$HOME/Music}"     # the local library (also the phone-sync source)
INDEX="$PROJECT/index"                     # cached full track lists per playlist (.spotdl)
STATE="$PROJECT/.state"                    # runtime state: grab pidfiles for bin/watch.sh (gitignored)
REGISTRY="$PROJECT/playlists.tsv"          # name<TAB>playlist_id<TAB>cap<TAB>offset

# spotdl (https://github.com/spotDL/spotify-downloader, MIT) does the fetching,
# with yt-dlp (https://github.com/yt-dlp/yt-dlp, Unlicense) underneath it. Both
# live in their own venv because most distros ship an externally-managed python.
SPOTDL_VENV="${SPOTDL_VENV:-$HOME/.local/spotdl-venv}"
SPOTDL="$SPOTDL_VENV/bin/spotdl"
PY="$SPOTDL_VENV/bin/python"

MPV_SOCK="/tmp/spotify-mpv.sock"           # stable path so control survives sessions
MPV_PLAYLIST="/tmp/spotify-mpv-playlist.txt"

# spotdl's OWN public anonymous app credentials, shipped in its source at
# spotdl/utils/config.py. They are not yours, they are not secret, and they are
# duplicated here only so the helper scripts can call the Spotify metadata API
# the same way spotdl does. Do NOT swap in credentials from a Spotify developer
# app you registered: that would put you under the Spotify Developer Terms for
# no benefit the project needs.
SPOTIFY_ID="5f573c9620494bae87890c0f08a60293"
SPOTIFY_SECRET="212476d9b0f3472eaa762d90b19b0ba8"

mkdir -p "$INDEX" "$STATE"
[ -f "$REGISTRY" ] || printf 'name\tplaylist_id\tcap\toffset\n' > "$REGISTRY"

# Filesystem-safe slug for a playlist name (used to name its grab pidfile).
slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }

# A registry name may be NESTED: "<Parent>/<Source playlist>". That parent is a MEGAPLAYLIST - it
# keeps several Spotify playlists as separate folders (so each keeps its own id/cap/offset and
# `more.sh` still works) while presenting them as ONE playlist to players, because the ARTIST tag
# on every song underneath is the PARENT's name.
# artist_of() returns what the songs' ARTIST field should say: the first path segment.
artist_of() { printf '%s' "${1%%/*}"; }

# --- registry playlist_id may be a bare PLAYLIST id, or a full Spotify URL (playlist OR album) ---
# Albums are first-class: an album gets tagged, explicit-checked and deduped exactly like a
# playlist. These three turn whichever form the registry holds into what a caller needs.
entity_of() {  # -> album | playlist
  case "$1" in *"/album/"*) printf 'album';; *) printf 'playlist';; esac
}
spotify_url() {  # -> a full open.spotify.com URL
  case "$1" in http*) printf '%s' "$1";; *) printf 'https://open.spotify.com/playlist/%s' "$1";; esac
}
spotify_id() {   # -> the bare id, whether given an id or a URL (query string stripped)
  local v="${1##*/}"; printf '%s' "${v%%\?*}"
}

# --- registry helpers (name is the folder name, tab-safe) ---

reg_get() {  # reg_get <name> <col: playlist_id|cap|offset>
  local name="$1" col="$2"
  awk -F'\t' -v n="$name" -v c="$col" '
    NR==1{for(i=1;i<=NF;i++)h[$i]=i; next}
    $1==n{print $h[c]}' "$REGISTRY"
}

reg_set() {  # reg_set <name> <col> <value>
  local name="$1" col="$2" val="$3" tmp
  tmp="$(mktemp)"
  awk -F'\t' -v OFS='\t' -v n="$name" -v c="$col" -v v="$val" '
    NR==1{for(i=1;i<=NF;i++)h[$i]=i; print; next}
    $1==n{$h[c]=v} {print}' "$REGISTRY" > "$tmp" && mv "$tmp" "$REGISTRY"
}

reg_add() {  # reg_add <name> <playlist_id> <cap> <offset>
  local name="$1" pid="$2" cap="$3" off="$4"
  if reg_get "$name" playlist_id | grep -q .; then
    reg_set "$name" playlist_id "$pid"; reg_set "$name" cap "$cap"; reg_set "$name" offset "$off"
  else
    printf '%s\t%s\t%s\t%s\n' "$name" "$pid" "$cap" "$off" >> "$REGISTRY"
  fi
}

reg_del() {  # reg_del <name>
  local name="$1" tmp; tmp="$(mktemp)"
  awk -F'\t' -v n="$name" 'NR==1||$1!=n' "$REGISTRY" > "$tmp" && mv "$tmp" "$REGISTRY"
}

reg_names() { awk -F'\t' 'NR>1{print $1}' "$REGISTRY"; }

folder_count() { find "$MUSIC/$1" -name '*.mp3' 2>/dev/null | wc -l; }

# Kill any spotdl download / yt-dlp without ever matching this shell (match by binary + cmdline).
stop_downloads() {
  local self=$$
  for pid in $(pgrep -x python3) $(pgrep -x yt-dlp) $(pgrep -x spotdl); do
    [ "$pid" = "$self" ] && continue
    local cmd; cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    case "$cmd" in
      *mpv*|*mpris*) continue;;
      *"spotdl download"*|*yt-dlp*|*"spotdl save"*) kill -9 "$pid" 2>/dev/null;;
    esac
  done
  pkill -9 -f "capped_by_url.sh" 2>/dev/null
  pkill -9 -f "spotify/bin/grab.sh" 2>/dev/null
}
