#!/usr/bin/env bash
# Start (or restart) shuffled playback of the whole library, detached so it survives the session.
# Headphone/Bluetooth media buttons work via mpv-mpris. Optional arg: a single folder to play.
# Usage: play.sh [playlist-name]
set -u
source "$(dirname "$0")/env.sh"
scope="${1:-}"

pkill -x mpv 2>/dev/null; sleep 1
if [ -n "$scope" ]; then
  find "$MUSIC/$scope" -name '*.mp3' > "$MPV_PLAYLIST"
else
  find "$MUSIC" -name '*.mp3' > "$MPV_PLAYLIST"
fi
n=$(wc -l < "$MPV_PLAYLIST")
[ "$n" -gt 0 ] || { echo "No songs to play${scope:+ in '$scope'}."; exit 1; }
rm -f "$MPV_SOCK"
# NOTE: mpv-mpris auto-loads from /etc/mpv/scripts/mpris.so - do NOT also pass --script,
# or it registers on MPRIS twice and shows up as two players in GNOME.
setsid mpv --no-video --shuffle --loop-playlist=inf \
  --input-ipc-server="$MPV_SOCK" --volume=80 \
  --playlist="$MPV_PLAYLIST" >/tmp/spotify-mpv.log 2>&1 < /dev/null &
disown
sleep 2
if pgrep -x mpv >/dev/null; then
  echo "Playing $n songs${scope:+ from '$scope'} (shuffled, looping)."
else
  echo "mpv failed to start; see /tmp/spotify-mpv.log"; exit 1
fi
