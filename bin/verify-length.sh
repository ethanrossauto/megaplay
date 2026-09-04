#!/usr/bin/env bash
# Check every song's LENGTH against what the recording should be.
#
# WHY: length catches what a censorship check cannot. A radio edit is usually
# shorter than the album cut, a truncated or failed download is shorter still,
# and a file that is much LONGER is generally a different recording altogether
# (an extended mix, a live take, or two tracks joined). None of that shows up in
# the tags, because the tags come from the metadata rather than from the audio.
#
# ⚠️ NO SPOTIFY, NO YOUTUBE. Both are heavily rate limited, and neither can
# answer this question anyway. Reference lengths come from lrclib, an open API.
# The local length is read from the file header by mutagen, so it costs nothing
# and needs no decoding.
#
# ⚠️ ONE SOURCE ON PURPOSE. MusicBrainz was tested as a fallback and rejected:
# it answers with the first matching RECORDING, which can be any release, and
# it returned 226s for a track whose real length is 300s. A wrong reference is
# worse than no reference, so a track with no reference is reported as such
# rather than measured against a guess.
#
# Usage: verify-length.sh <name>|--all [--recheck]
#   Results append to .state/verify-length.tsv and act as a cache, so running
#   it after a grab only costs the tracks that grab added.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

TARGET=""; RECHECK=""
for a in "$@"; do
  case "$a" in
    --recheck) RECHECK=1;;
    *) TARGET="$a";;
  esac
done
[ -n "$TARGET" ] || { echo "usage: verify-length.sh <name>|--all [--recheck]"; exit 1; }
if [ "$TARGET" = "--all" ]; then ROOT="$MUSIC"; else ROOT="$MUSIC/$TARGET"; fi
[ -d "$ROOT" ] || { echo "no such folder: $ROOT"; exit 1; }
mkdir -p "$STATE"

ROOT="$ROOT" MUSIC="$MUSIC" RESULTS="$STATE/verify-length.tsv" RECHECK="$RECHECK" "$PY" - <<'PY'
import os, json, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from mutagen.mp3 import MP3
from mutagen.id3 import ID3

MUSIC, ROOT, RESULTS = (os.environ[k] for k in ("MUSIC","ROOT","RESULTS"))
RECHECK = bool(os.environ.get("RECHECK"))
UA = {"User-Agent": "megaplay-verify/1.0 (personal library check)"}

# What counts as a mismatch. Masters legitimately differ by a few seconds, so
# the band is generous: the point is to catch an edit or a truncation, not to
# audit mastering.
OK_SECS, MINOR_SECS = 10, 30

done = set()
if os.path.exists(RESULTS) and not RECHECK:
    for line in open(RESULTS, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 5: done.add(p[4])

files = []
for root, _d, fs in os.walk(ROOT):
    for f in sorted(fs):
        if f.endswith(".mp3"):
            rel = os.path.relpath(os.path.join(root, f), MUSIC)
            if rel not in done:
                files.append((os.path.join(root, f), rel))
print(f"length check: {len(files)} track(s) ({len(done)} already checked)", flush=True)

def reference(artist, title):
    base = "https://lrclib.net/api/"
    u = base + "get?artist_name=" + urllib.parse.quote(artist) + "&track_name=" + urllib.parse.quote(title)
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
            return json.load(r).get("duration")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
        u2 = base + "search?q=" + urllib.parse.quote(f"{artist} {title}")
        try:
            with urllib.request.urlopen(urllib.request.Request(u2, headers=UA), timeout=20) as r:
                arr = json.load(r)
            return arr[0].get("duration") if arr else None
        except Exception:
            return None
    except Exception:
        return None

def check(job):
    path, rel = job
    stem = os.path.basename(path)[:-4]
    artists, _, title = stem.partition(" - ")
    title = title or stem
    try:
        primary = str(ID3(path).get("TPE2") or "") or artists.split(",")[0]
    except Exception:
        primary = artists.split(",")[0]
    try:
        local = MP3(path).info.length
    except Exception:
        return ("ERROR", 0, 0, 0, rel)
    ref = reference(primary.strip(), title.strip())
    if not ref:
        return ("NOREFERENCE", round(local), 0, 0, rel)
    d = round(local - ref)
    if abs(d) <= OK_SECS:      v = "LENGTHOK"
    elif abs(d) <= MINOR_SECS: v = "MINOR"
    elif d < 0:                v = "SHORT"
    else:                      v = "LONG"
    return (v, round(local), round(ref), d, rel)

counts = {}
with ThreadPoolExecutor(max_workers=6) as pool, open(RESULTS, "a", encoding="utf-8") as out:
    for i, (v, local, ref, d, rel) in enumerate(pool.map(check, files), 1):
        counts[v] = counts.get(v, 0) + 1
        out.write(f"{v}\t{local}\t{ref}\t{d:+d}\t{rel}\n"); out.flush()
        if v in ("SHORT", "LONG"):
            print(f"  {v}: {d:+d}s (file {local}s, reference {ref}s)  {rel}", flush=True)
        if i % 100 == 0:
            print(f"  ...{i}/{len(files)}", flush=True)
print("length check done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), flush=True)
PY
