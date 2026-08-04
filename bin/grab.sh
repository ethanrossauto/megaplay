#!/usr/bin/env bash
# Download the current batch for a playlist: <cap> songs starting at <offset>.
#
#  - offset 0 (first batch): fast path - download the playlist URL and stop once <cap> land.
#  - offset > 0 (a later batch, e.g. after "more"): slice the cached index [offset, offset+cap)
#    and download those exact tracks from embedded metadata (no per-track Spotify lookup = no hang).
#
# Usage: grab.sh <name> [--no-explicit]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"   # absolute script dir (survives the cd "$MUSIC" below)
source "$HERE/env.sh"
name="$1"; shift || true
no_explicit="${SPOTIFY_NO_EXPLICIT:-}"
for a in "$@"; do [ "$a" = "--no-explicit" ] && no_explicit=1; done
pid="$(reg_get "$name" playlist_id)"
cap="$(reg_get "$name" cap)"
off="$(reg_get "$name" offset)"
[ -n "$pid" ] || { echo "Unknown playlist '$name' (not in registry)"; exit 1; }
mkdir -p "$MUSIC/$name"

# Write a pidfile so bin/watch.sh can track progress via kill -0 (NO pgrep - pgrep -f on a
# playlist name self-matches the watcher's own command line and loops forever). Cleaned on exit.
sf="$STATE/$(slug "$name").grab"
printf 'pid=%s\ncap=%s\nname=%s\n' "$$" "$cap" "$name" > "$sf"
# Always clear the pidfile AND any skip files we seeded below - a stale .skip would silently
# block a legitimate re-download of that song later.
cleanup() { rm -f "$sf"; find "$MUSIC/$name" -maxdepth 1 -name '*.mp3.skip' -delete 2>/dev/null; }
trap cleanup EXIT

# --- pre-download dedupe (MEGAPLAYLISTS only) ---------------------------------------------
# If this playlist is a subfolder of a megaplaylist, its siblings may already hold some of the
# tracks we are about to fetch. Seed a "<song>.mp3.skip" file for each one and pass
# --respect-skip-file, so spotdl never downloads them in the first place. Verified 2026-07-26:
# spotdl logs "Skipping <song> (skip file found)" and writes nothing.
# Exact-filename match only, which is how ~99% of the overlap presents (86 of 87 real duplicates);
# the dedupe.sh report after the grab is the net for the near-miss names.
case "$name" in
  */*)
    parent="${name%%/*}"; seeded=0
    while IFS= read -r f; do
      [ -e "$MUSIC/$name/$f" ] && continue        # we already have it here; spotdl skips it anyway
      : > "$MUSIC/$name/$f.skip" && seeded=$((seeded+1))
    done < <(find "$MUSIC/$parent" -mindepth 2 -name '*.mp3' -not -path "$MUSIC/$name/*" \
             -printf '%f\n' | sort -u)
    [ "$seeded" -gt 0 ] && \
      echo "Pre-dedupe: $seeded song(s) already held by sibling folders of '$parent' will be skipped."
    ;;
esac

# --- the replaced-file ledger (EVERY playlist, not just megaplaylists) -------------------------
# When the explicit swap RENAMES a file ("Milkshake" -> "MILKSHAKE (Kelis' Version)"), the clean
# filename vanishes from disk - so the next grab cheerfully downloads the clean version again, and
# the one after that, forever. Seeding a skip file for each replaced name stops the loop.
replaced=0
if [ -s "$STATE/explicit-replaced.tsv" ]; then
  while IFS=$'\t' read -r rname rclean; do
    [ "$rname" = "$name" ] || continue
    [ -e "$MUSIC/$name/$rclean" ] && continue     # it's back on disk; leave it to explicit.sh
    : > "$MUSIC/$name/$rclean.skip" && replaced=$((replaced+1))
  done < "$STATE/explicit-replaced.tsv"
  [ "$replaced" -gt 0 ] && \
    echo "Replaced-file ledger: $replaced clean version(s) already swapped for explicit will be skipped."
fi

cd "$MUSIC" || exit 1

# Watch a running spotdl. Its ONLY job is stopping at <cap>.
#
# NO STALL TIMER BY DEFAULT (replaced an earlier hardcoded 5-min rule).
# WHY IT WENT: a fixed timer can't tell "this source is dry" from "we're still walking past songs
# we already have". Skipping runs at only ~4.5 songs/min (measured), so 5 minutes clears just ~23
# songs - every re-grab of a folder holding more than that stalled before reaching a single gap,
# three times in a row on 2026-07-28. The judgement now lives with Claude, who reviews the
# 5-minute status update and decides to continue, stop, or back off. See CLAUDE.md
# "Autonomous pulling". STALL_SECS is still honoured if explicitly set - opt-in, never a default.
watch_download() {
  local dlpid="$1" enforce="$2" last now c
  last="$(folder_count "$name")"
  local since; since="$(date +%s)"
  while kill -0 "$dlpid" 2>/dev/null; do
    c="$(folder_count "$name")"
    if [ "$enforce" = 1 ] && [ "$c" -ge "$cap" ]; then
      pkill -9 -P "$dlpid" 2>/dev/null; kill -9 "$dlpid" 2>/dev/null; return 0
    fi
    if [ -n "${STALL_SECS:-}" ]; then          # opt-in only
      if [ "$c" -ne "$last" ]; then last="$c"; since="$(date +%s)"; fi
      now="$(date +%s)"
      if [ $(( now - since )) -ge "$STALL_SECS" ]; then
        echo "STALLED: no new song in $(( STALL_SECS / 60 )) min (STALL_SECS was set) - stopping '$name' at $c songs."
        pkill -9 -P "$dlpid" 2>/dev/null; kill -9 "$dlpid" 2>/dev/null; return 1
      fi
    fi
    sleep 4
  done
  return 0
}

if [ "${off:-0}" -eq 0 ]; then
  # ---- fast path: URL download, auto-stop at cap ----
  echo "Grabbing '$name': first $cap songs (URL mode, $(entity_of "$pid"))."
  # spotify_url() handles both forms the registry can hold: a bare playlist id, or a full URL for
  # an ALBUM (e.g. "10 Day"). Building "…/playlist/$pid" blindly produced a mangled URL for albums.
  "$SPOTDL" download "$(spotify_url "$pid")" \
    --output "$name/{artists} - {title}.{output-ext}" --respect-skip-file &
  dlpid=$!
  watch_download "$dlpid" 1
  wait "$dlpid" 2>/dev/null
else
  # ---- slice path: needs the cached index ----
  idx="$INDEX/$name.spotdl"
  [ -s "$idx" ] || "$HERE/index-playlist.sh" "$name" "$pid" || exit 1
  slice="$(mktemp --suffix=.spotdl)"
  total="$("$PY" - "$idx" "$slice" "$off" "$cap" <<'PY'
import json,sys
idx,slc,off,cap=sys.argv[1],sys.argv[2],int(sys.argv[3]),int(sys.argv[4])
data=json.load(open(idx))
part=data[off:off+cap]
json.dump(part,open(slc,'w'))
print(len(part))
PY
)"
  if [ "${total:-0}" -eq 0 ]; then
    echo "No more songs in '$name' past offset $off (playlist exhausted)."; rm -f "$slice"; exit 0
  fi
  echo "Grabbing '$name': songs $((off+1))-$((off+total)) from index (slice mode)."
  "$SPOTDL" download "$slice" --output "$name/{artists} - {title}.{output-ext}" --respect-skip-file &
  dlpid=$!
  watch_download "$dlpid" 0     # no cap check - the slice is already exactly <cap> tracks
  wait "$dlpid" 2>/dev/null
  rm -f "$slice"
fi
# A folder can legitimately end BELOW its cap: the source playlist has fewer tracks than the cap,
# a megaplaylist's siblings already held some of them (pre-dedupe skips those), or YouTube
# refused some downloads. Say so, so a short folder doesn't read as a stalled or failed grab.
have=$(folder_count "$name")
if [ "$have" -lt "$cap" ]; then
  echo "NOTE: '$name' ended at $have of cap $cap - source exhausted, siblings already held some," \
       "or some downloads failed. Re-run grab.sh later to fill gaps (existing songs are skipped)."
fi

# Default: retag to loadout scheme - TITLE="title - artist", ARTIST=playlist, ALBUM kept as-is
# (and clean UTF-8). Idempotent via a TXXX marker.
"$HERE/tag.sh" "$name"

# Default: swap any censored track for its explicit twin.
# This runs AFTER the download, not before, on purpose: the offset-0 fast path hands the playlist
# URL straight to spotdl and has no per-track list to pre-screen - building one would need a slow
# `spotdl save` on every first grab, the rate-limit trap the fast path exists to dodge. Cost of
# doing it here is re-fetching the few tracks that get replaced. Skip with --no-explicit or
# SPOTIFY_NO_EXPLICIT=1 (it adds a Spotify search per clean-flagged track, so it is not fast).
if [ -z "$no_explicit" ]; then
  "$HERE/explicit.sh" "$name" --fix
fi

# A grab into a MEGAPLAYLIST can duplicate songs held by a sibling subfolder - report them
# (never auto-delete inside a grab; run `dedupe.sh <parent> --apply` to actually remove them).
case "$name" in */*) "$HERE/dedupe.sh" "${name%%/*}";; esac

# Album-tag consistency, APPLIED not just reported: a reissue, deluxe or soundtrack cut arrives
# tagged differently and splits the album into two entries on the phone. Fewer album entries
# beats preserving that distinction, so collapse them every time.
"$HERE/album-check.sh" "${name%%/*}" --apply

echo "DONE: '$name' now has $(folder_count "$name") songs."
