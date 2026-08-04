#!/usr/bin/env bash
# Work through a queue of sources one at a time. Designed to be KILLED AND RE-RUN freely - that is
# how the back-off works (see CLAUDE.md "Autonomous pulling"): when Claude judges the connection is
# rate-limited it kills this runner, waits a couple of hours, and starts it again. Re-running
# RESUMES; it never repeats finished work.
#
#   Queue:  .state/queue.tsv       one registry name per line ("#" comments and blanks ignored)
#   Done:   .state/queue-done.tsv  appended as each source completes
#   Log:    whatever the caller redirects to (use .state/autopull.log)
#
# A source interrupted mid-grab is NOT marked done, so the next run picks it up again - and picks
# up cheaply, because everything already on disk is skipped.
#
# Usage: autopull.sh            # process the queue
#        autopull.sh --reset    # forget the done-markers and start the queue over
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

Q="$STATE/queue.tsv"
D="$STATE/queue-done.tsv"

if [ "${1:-}" = "--reset" ]; then rm -f "$D"; echo "Queue progress reset."; fi
[ -s "$Q" ] || { echo "Queue is empty ($Q) - nothing to pull."; exit 0; }
touch "$D"

pulled_total=0
while IFS= read -r name || [ -n "$name" ]; do
  case "$name" in ''|'#'*) continue;; esac
  if grep -qxF "$name" "$D" 2>/dev/null; then
    echo "ALREADY DONE, skipping: $name"
    continue
  fi
  before="$(folder_count "$name")"
  echo "===== PULL START: $name (have $before) ($(date '+%F %H:%M')) ====="
  "$HERE/grab.sh" "$name"
  after="$(folder_count "$name")"
  pulled_total=$(( pulled_total + after - before ))
  echo "===== PULL END: $name  $before -> $after (+$(( after - before ))) ($(date '+%F %H:%M')) ====="
  printf '%s\n' "$name" >> "$D"
done < "$Q"

echo "===== QUEUE COMPLETE - $pulled_total song(s) added this run ($(date '+%F %H:%M')) ====="
"$HERE/status.sh"
