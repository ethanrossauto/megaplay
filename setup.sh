#!/usr/bin/env bash
# One-time setup for a fresh clone.
#
#   ./setup.sh              full install, including voice control
#   ./setup.sh --no-voice   skip the Whisper venv (~430 MB) and its model download
#
# Installs nothing globally and touches nothing outside this directory and the
# spotdl venv path. Starts no downloads and plays no music: it only prepares.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WANT_VOICE=1
[ "${1:-}" = "--no-voice" ] && WANT_VOICE=0

SPOTDL_VENV="${SPOTDL_VENV:-$HOME/.local/spotdl-venv}"
ASR_VENV="$HERE/.venv"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  MISS  %s\n' "$*"; }

# --- 1. system tools ---------------------------------------------------------
# Report everything missing in one pass. Telling someone about one missing
# package, then another after they install it, is a miserable way to start.
say "Checking system tools"
missing=()
need() {
  if command -v "$1" >/dev/null 2>&1; then ok "$1"; else bad "$1 ($2)"; missing+=("$1"); fi
}
need python3 "runs everything"
need ffmpeg   "audio conversion and microphone capture"
need rsync    "phone sync"
need mpv      "playback"

# Optional: only some paths need these, so name them but do not block.
#   wpctl, pw-dump   voice control switches the headset into a call profile to
#                    reach its microphone, and reads the card back to confirm it
#   bluetoothctl     reconnects a headset that came back without its A2DP profile
#   espeak-ng        renders the spoken acknowledgement if you have no piper
for opt in socat findmnt gio wpctl pw-dump bluetoothctl espeak-ng; do
  if command -v "$opt" >/dev/null 2>&1; then ok "$opt"
  else warn "$opt missing (some features degrade)"; fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo
  echo "Install the missing tools first:"
  if   command -v apt  >/dev/null 2>&1; then echo "    sudo apt install ${missing[*]} mpv-mpris"
  elif command -v dnf  >/dev/null 2>&1; then echo "    sudo dnf install ${missing[*]} mpv-mpris"
  elif command -v pacman >/dev/null 2>&1; then echo "    sudo pacman -S ${missing[*]} mpv-mpris"
  else echo "    (use your package manager to install: ${missing[*]})"
  fi
  exit 1
fi

# mpv-mpris is what makes headset and notification-bar controls work. Not fatal.
if ls /usr/lib/mpv-mpris/mpris.so /etc/mpv/scripts/mpris.so >/dev/null 2>&1; then
  ok "mpv-mpris"
else
  warn "mpv-mpris not found: media keys and the GNOME widget will not control playback"
fi

# --- 2. spotdl venv ----------------------------------------------------------
# Its own venv because most distros now ship an externally-managed python that
# refuses a plain `pip install`.
say "Setting up spotdl"
if [ -x "$SPOTDL_VENV/bin/spotdl" ]; then
  ok "already present at $SPOTDL_VENV"
else
  echo "  creating venv at $SPOTDL_VENV"
  python3 -m venv "$SPOTDL_VENV" || { echo "  FAILED to create venv"; exit 1; }
  "$SPOTDL_VENV/bin/pip" install --quiet --upgrade pip
  echo "  installing spotdl (this pulls in yt-dlp and mutagen)"
  "$SPOTDL_VENV/bin/pip" install --quiet spotdl || { echo "  FAILED to install spotdl"; exit 1; }
  ok "installed"
fi
"$SPOTDL_VENV/bin/spotdl" --version 2>/dev/null | sed 's/^/  spotdl /'

# --- 3. whisper venv (optional) ---------------------------------------------
say "Setting up voice control"
if [ "$WANT_VOICE" -eq 0 ]; then
  warn "skipped (--no-voice). Re-run without the flag to add it later."
elif [ -n "${VOICE_ASR_PY:-}" ] && [ -x "${VOICE_ASR_PY}" ]; then
  ok "using the venv you pointed VOICE_ASR_PY at: $VOICE_ASR_PY"
elif [ -x "$ASR_VENV/bin/python" ]; then
  ok "already present at $ASR_VENV"
else
  echo "  This installs faster-whisper and its dependencies, roughly 430 MB."
  echo "  Ctrl-C now and re-run with --no-voice to skip it."
  sleep 3
  python3 -m venv "$ASR_VENV" || { echo "  FAILED to create venv"; exit 1; }
  "$ASR_VENV/bin/pip" install --quiet --upgrade pip
  "$ASR_VENV/bin/pip" install --quiet faster-whisper || { echo "  FAILED"; exit 1; }
  ok "installed"
  echo "  Note: the speech model itself downloads on first use, not now."
fi

# --- 4. registry -------------------------------------------------------------
say "Registry"
if [ -f "$HERE/playlists.tsv" ]; then
  ok "playlists.tsv already exists, left alone"
else
  cp "$HERE/playlists.tsv.example" "$HERE/playlists.tsv"
  ok "created playlists.tsv from the example"
fi

# --- 5. what to do next ------------------------------------------------------
say "Done"
cat <<'NEXT'
  Add something:     bin/add-playlist.sh "<spotify playlist or album url>"
  Under a parent:    bin/add-playlist.sh "<url>" "" "90s Rap/Best Of"
  See the library:   bin/status.sh
  Play it:           bin/play.sh

  Voice control additionally needs the Claude Code CLI and a paid Claude plan:
    bin/voice.sh install     then volume down-up on your headset to talk
    bin/voice.sh selftest    check the gesture alone, no audio needed

  Machine-specific settings go in bin/env.local.sh (gitignored):
    VOICE_ASR_PY, VOICE_CLAUDE_MODEL, MEGAPLAY_MUSIC, SPOTDL_VENV
NEXT
