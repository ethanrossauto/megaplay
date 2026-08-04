#!/usr/bin/env bash
# Watch an in-flight grab's progress - the SAFE way, with no pgrep self-match trap.
#
# WHY THIS EXISTS: the obvious `while pgrep -f "grab.sh <name>"; do ...; done` ticker is broken.
# `pgrep -f` matches on the FULL command line, and the watcher loop's own command line contains
# the literal "grab.sh <name>" - so pgrep always finds itself, the condition never goes false,
# and the ticker spins forever (one did for 1h43m on 2026-07-12). Instead, grab.sh writes a
# pidfile ($STATE/<slug>.grab) and we poll folder count while that PID is alive via `kill -0`.
# No pgrep anywhere, so there is nothing to self-match.
#
# Usage: watch.sh <name> [interval_seconds]   (default 15s). Safe to run backgrounded.
set -u
source "$(cd "$(dirname "$0")" && pwd)/env.sh"

name="${1:?usage: watch.sh <name> [interval_seconds]}"
interval="${2:-15}"
sf="$STATE/$(slug "$name").grab"

# A just-launched grab may not have written its pidfile yet - wait briefly for it.
for _ in 1 2 3 4 5; do [ -f "$sf" ] && break; sleep 1; done

cap="$(sed -n 's/^cap=//p' "$sf" 2>/dev/null)"; cap="${cap:-?}"
gpid="$(sed -n 's/^pid=//p' "$sf" 2>/dev/null)"

# Loop while the grab pidfile exists AND its process is still alive. The -f check guards against
# PID reuse: grab.sh removes the file on exit, so a recycled PID can't keep us spinning.
while [ -f "$sf" ] && [ -n "${gpid:-}" ] && kill -0 "$gpid" 2>/dev/null; do
  printf '[%s] %s / %s songs on disk\n' "$(date +%H:%M:%S)" "$(folder_count "$name")" "$cap"
  sleep "$interval"
done
printf '[%s] DONE - %s songs on disk for "%s"\n' "$(date +%H:%M:%S)" "$(folder_count "$name")" "$name"
