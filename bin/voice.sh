#!/usr/bin/env bash
# Voice control for the loadout.
#   voice.sh install     write + enable the systemd user units (run once)
#   voice.sh start|stop|restart|status
#   voice.sh logs        follow both logs
#   voice.sh selftest    gesture path only - no audio, no ASR server
#   voice.sh uninstall   disable and remove the units
#
# Runs under systemd --user so it comes up at login and restarts on crash.
# Everything goes through systemctl deliberately: an earlier version launched
# processes directly AND had a pid file, and the two disagreed, leaving an
# orphaned ASR server holding a Whisper model in memory.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

# Python that has faster-whisper installed. Resolution order, first hit wins:
#   1. $VOICE_ASR_PY          - set it in bin/env.local.sh to reuse a venv you
#                               already have; avoids a second multi-GB install.
#   2. <project>/.venv        - what setup.sh builds for a fresh clone.
# No third fallback on purpose: the system python almost never has
# faster-whisper, and a daemon that starts without a mic server detects
# gestures, pauses the music, and fails every command.
if [ -n "${VOICE_ASR_PY:-}" ]; then
  ASR_PY="$VOICE_ASR_PY"
else
  ASR_PY="$PROJECT/.venv/bin/python"
fi
UNIT_DIR="$HOME/.config/systemd/user"
ASR_UNIT="spotify-voice-asr.service"
GES_UNIT="spotify-voice.service"
ASR_LOG="$STATE/voice-asr.log"
DAEMON_LOG="$STATE/voice.log"

# env.local.sh is sourced by env.sh, but systemd launches these units directly
# and inherits nothing from any shell. So bake whichever knobs are set at
# install time into the unit files, otherwise a setting in env.local.sh is
# silently ignored by the daemon and only appears to work when you run the
# scripts by hand. Re-run `voice.sh install` after changing one.
unit_env() {
  local v
  for v in MEGAPLAY_MUSIC VOICE_ASR_SOCK VOICE_SOURCE VOICE_MODEL \
           VOICE_CLAUDE_MODEL VOICE_CLAUDE_TIMEOUT VOICE_EFFORT VOICE_TOOLS \
           VOICE_GESTURE_WINDOW VOICE_ACK VOICE_ACK_TEXT VOICE_ACK_WAV \
           VOICE_ACK_BANK VOICE_ACK_MODEL VOICE_ACK_SEED VOICE_PIPER VOICE_PIPER_MODEL \
           VOICE_BT VOICE_BT_ADDR VOICE_BT_HFP VOICE_BT_A2DP VOICE_BT_SETTLE \
           VOICE_GESTURE_MIN VOICE_GESTURE_STEP VOICE_GESTURE_RESYNC \
           VOICE_BT_AUTOHEAL; do
    [ -n "${!v:-}" ] && printf 'Environment=%s=%s\n' "$v" "${!v}"
  done
  return 0
}

write_units() {
  mkdir -p "$UNIT_DIR" "$STATE"

  # Logs are appended to the same files the manual version used, rather than
  # left in the journal only, so existing debugging habits keep working.
  cat > "$UNIT_DIR/$ASR_UNIT" <<EOF
[Unit]
Description=Spotify loadout voice control - Whisper ASR server
After=pipewire.service wireplumber.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$ASR_PY $HERE/voice-asr.py /tmp/spotify-voice-asr.sock
$(unit_env)
Restart=on-failure
RestartSec=5
StandardOutput=append:$ASR_LOG
StandardError=append:$ASR_LOG

[Install]
WantedBy=default.target
EOF

  # Requires= (not just After=) so the daemon never runs without a mic server:
  # it would detect gestures, pause the music, and fail every command.
  cat > "$UNIT_DIR/$GES_UNIT" <<EOF
[Unit]
Description=Spotify loadout voice control - gesture daemon
After=$ASR_UNIT
Requires=$ASR_UNIT
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $HERE/voice.py
$(unit_env)
Restart=on-failure
RestartSec=5
StandardOutput=append:$DAEMON_LOG
StandardError=append:$DAEMON_LOG

[Install]
WantedBy=default.target
EOF
  echo "wrote $UNIT_DIR/$ASR_UNIT"
  echo "wrote $UNIT_DIR/$GES_UNIT"
}

stop_strays() {
  # Anything left over from a manual start would fight systemd for the ASR
  # socket, so clear it before handing control over.
  #
  # 🚫 The pattern is ANCHORED to an actual python interpreter on purpose.
  # `pgrep -f` matches the whole command line, so the obvious pattern
  # 'bin/voice\.py' also matches any shell whose command text merely MENTIONS
  # the file - including the one running this script. That is exactly the
  # self-match trap CLAUDE.md documents for grab.sh, and it killed the
  # installing shell on 2026-08-03 before this was anchored.
  local self=$$ n=0
  for p in $(pgrep -f '^[^ ]*python[0-9.]*[[:space:]]+[^[:space:]]*/voice(-asr)?\.py' 2>/dev/null); do
    [ "$p" = "$self" ] || [ "$p" = "$PPID" ] && continue
    kill "$p" 2>/dev/null && n=$((n + 1))
  done
  rm -f "$STATE/voice.pid" "$STATE/voice-asr.pid"
  [ "$n" -gt 0 ] && echo "stopped $n stray process(es) from a manual start"
  return 0
}

case "${1:-status}" in
  install)
    if [ ! -x "$ASR_PY" ]; then
      echo "ERROR: no python with faster-whisper at $ASR_PY"
      echo
      echo "Either run ./setup.sh to build one, or point VOICE_ASR_PY at a venv"
      echo "that already has it, in bin/env.local.sh:"
      echo "    VOICE_ASR_PY=/path/to/venv/bin/python"
      exit 1
    fi
    write_units
    stop_strays
    systemctl --user daemon-reload
    systemctl --user enable --now "$GES_UNIT"
    sleep 2
    systemctl --user --no-pager --lines=0 status "$ASR_UNIT" "$GES_UNIT" | grep -E 'Loaded|Active'
    echo
    echo "Enabled at login. Volume down-up on the headset to talk."
    ;;
  start)   systemctl --user start "$GES_UNIT" && echo "started" ;;
  stop)    systemctl --user stop "$GES_UNIT" "$ASR_UNIT" && echo "stopped" ;;
  restart) systemctl --user restart "$ASR_UNIT" "$GES_UNIT" && echo "restarted" ;;
  status)
    systemctl --user --no-pager --lines=0 status "$ASR_UNIT" "$GES_UNIT" 2>/dev/null \
      | grep -E '●|Loaded|Active' || echo "units not installed - run: bin/voice.sh install"
    ;;
  logs)    tail -n 20 -F "$DAEMON_LOG" "$ASR_LOG" ;;
  selftest)
    echo "Do one volume down-up on the headset. Ctrl-C to stop."
    exec python3 "$HERE/voice.py" --selftest
    ;;
  uninstall)
    systemctl --user disable --now "$GES_UNIT" "$ASR_UNIT" 2>/dev/null
    rm -f "$UNIT_DIR/$GES_UNIT" "$UNIT_DIR/$ASR_UNIT"
    systemctl --user daemon-reload
    echo "removed"
    ;;
  *)
    echo "usage: voice.sh [install|start|stop|restart|status|logs|selftest|uninstall]"
    exit 1
    ;;
esac
