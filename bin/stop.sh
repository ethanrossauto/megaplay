#!/usr/bin/env bash
# Stop all downloads (and optionally playback). Never touches this shell.
# Usage: stop.sh [--music]   (--music also stops mpv playback)
set -u
source "$(dirname "$0")/env.sh"
stop_downloads
echo "Downloads stopped."
if [ "${1:-}" = "--music" ]; then pkill -x mpv 2>/dev/null; echo "Playback stopped."; fi
