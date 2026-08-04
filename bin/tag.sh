#!/usr/bin/env bash
# Retag a playlist folder to the loadout scheme:
#   TITLE  = "<original title> - <original artist>"
#   ARTIST = the playlist name - for a NESTED name ("<Parent>/<Sub>") it is the PARENT,
#            so several source playlists under one parent read as ONE playlist in players.
#   ALBUM  = left as-is (real album kept if present; blank stays blank)
# Also normalizes all text tags to valid UTF-8 (prevents the mpv-mpris crash-on-skip).
#
# Idempotent: stamps a hidden TXXX:LOADOUT_TAGGED marker; a song already carrying it is never
# re-titled (so re-runs never append the artist twice) - but its ARTIST is ALWAYS re-stamped,
# so moving a folder under a parent (or renaming one) is fixed by just re-running this.
#
# Usage: tag.sh <name>          # one playlist folder (nested: "Parent/Sub")
#        tag.sh --all           # every registered playlist
set -u
source "$(dirname "$0")/env.sh"

tag_folder() {
  local name="$1"
  local dir="$MUSIC/$name"
  [ -d "$dir" ] || { echo "  ($name: no folder)"; return; }
  ARTIST="$(artist_of "$name")" "$PY" - "$dir" <<'PY'
import os, sys
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TXXX, Encoding
artist_playlist = os.environ["ARTIST"]
MARK = "LOADOUT_TAGGED"
def clean(s): return s.encode("utf-8", "replace").decode("utf-8", "replace")
def first(tags, key):
    fr = tags.get(key)
    if fr is None or not getattr(fr, "text", None): return ""
    return ", ".join(clean(str(t)) for t in fr.text).strip()
def marked(tags):
    for fr in tags.getall("TXXX"):
        if getattr(fr, "desc", "") == MARK: return True
    return False

d = sys.argv[1]; done = 0; skipped = 0; moved = 0
for f in sorted(os.listdir(d)):
    if not f.lower().endswith(".mp3"): continue
    p = os.path.join(d, f)
    try:
        a = MP3(p)
        if a.tags is None: a.add_tags()
        # normalize existing text frames to clean utf-8 (crash-prevention) - always
        for fr in a.tags.values():
            if hasattr(fr, "text"):
                fr.text = [clean(str(t)) for t in fr.text]
                try: fr.encoding = Encoding.UTF8
                except Exception: pass
        if marked(a.tags):
            # already loadout-titled: never re-title, but keep ARTIST in sync with the
            # (possibly changed) playlist/parent name.
            if first(a.tags, "TPE1") != artist_playlist:
                a.tags.setall("TPE1", [TPE1(encoding=Encoding.UTF8, text=[artist_playlist])])
                a.save(); moved += 1; continue
            a.save(); skipped += 1; continue
        title = first(a.tags, "TIT2") or os.path.splitext(f)[0]
        artist = first(a.tags, "TPE1")
        new_title = f"{title} - {artist}" if artist else title
        a.tags.setall("TIT2", [TIT2(encoding=Encoding.UTF8, text=[new_title])])
        a.tags.setall("TPE1", [TPE1(encoding=Encoding.UTF8, text=[artist_playlist])])
        # ALBUM (TALB) intentionally left untouched.
        a.tags.add(TXXX(encoding=Encoding.UTF8, desc=MARK, text=["1"]))
        a.save(); done += 1
    except Exception as e:
        print("  ERR", f, repr(e)[:60])
print(f"  {os.path.basename(d)}: retagged {done}, artist-updated {moved}, already-tagged {skipped}")
PY
}

if [ "${1:-}" = "--all" ]; then
  while IFS=$'\t' read -r name _pid _cap _off; do
    [ "$name" = "name" ] || [ -z "$name" ] && continue
    tag_folder "$name"
  done < "$REGISTRY"
else
  [ -n "${1:-}" ] || { echo "usage: tag.sh <name> | --all"; exit 1; }
  tag_folder "$1"
fi
