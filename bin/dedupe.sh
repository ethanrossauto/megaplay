#!/usr/bin/env bash
# Find (and with --apply, delete) songs that appear more than once inside a MEGAPLAYLIST -
# i.e. the same track downloaded independently into two of a parent's subfolders.
#
# A megaplaylist is several source playlists kept in subfolders of one parent (see CLAUDE.md
# "Megaplaylists"). When those sources overlap they produce real duplicate files, and nothing
# dedupes across the folders on its own.
#
# Matching is on artist+title, normalized: case, punctuation, and "(feat. ...)" / "[remaster]"
# style suffixes are ignored. It never compares across different parents, and never touches a
# non-megaplaylist folder. The copy kept is the one in the currently smallest subfolder, so
# deletions spread out instead of gutting one source.
#
# Usage: dedupe.sh [parent-folder] [--apply]      (no parent = every megaplaylist; default = report)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

APPLY=""; PARENT=""
for a in "$@"; do
  case "$a" in --apply) APPLY=1;; *) PARENT="$a";; esac
done

# Megaplaylists = the first path segment of every nested registry name.
parents="$(reg_names | awk -F/ 'NF>1{print $1}' | sort -u)"
[ -n "$PARENT" ] && parents="$PARENT"
[ -n "$parents" ] || { echo "No megaplaylists (no nested entries in the registry)."; exit 0; }

while IFS= read -r p; do
  [ -n "$p" ] || continue
  [ -d "$MUSIC/$p" ] || { echo "($p: no folder)"; continue; }
  echo "### $p"
  APPLY="$APPLY" "$PY" - "$MUSIC/$p" <<'PY'
import os, re, sys, collections, unicodedata
root = sys.argv[1]; apply_ = bool(os.environ.get("APPLY"))
subs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
files = [(s, f) for s in subs for f in sorted(os.listdir(os.path.join(root, s)))
         if f.lower().endswith(".mp3")]
def key(f):
    n = unicodedata.normalize("NFKD", os.path.splitext(f)[0]).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\([^)]*\)|\[[^]]*\]", "", n)        # (feat. ...) / [remaster]
    n = re.sub(r"\bfeat\.?\b|\bft\.?\b", "", n)
    return re.sub(r"[^a-z0-9]", "", n)
count = collections.Counter(s for s, _ in files)
groups = collections.defaultdict(list)
for s, f in files: groups[key(f)].append((s, f))
extra = 0
for k, v in sorted(groups.items()):
    if len(v) < 2: continue
    v = sorted(v)
    keep = min(v, key=lambda sf: (count[sf[0]], sf[0]))   # keep the smallest folder's copy
    for s, f in v:
        if (s, f) == keep: continue
        extra += 1
        if apply_:
            os.remove(os.path.join(root, s, f)); count[s] -= 1
            print(f"  removed {s}/{f}")
        else:
            print(f"  dup: {s}/{f}   (would keep {keep[0]}/)")
print(f"  {sum(1 for v in groups.values() if len(v)>1)} duplicated song(s), {extra} extra "
      f"{'copies removed' if apply_ else 'copies - re-run with --apply to delete them'}")
for s in subs: print(f"    {s}: {count[s]}")
PY
done <<< "$parents"
