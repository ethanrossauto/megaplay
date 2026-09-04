#!/usr/bin/env bash
# Replace a bad file with a better SOURCE for the same song, and keep the new
# one only if it measurably improves.
#
# WHY VERIFY BEFORE KEEPING: a swap is a download, and a download can be worse
# than what it replaces. This never trusts the swap. It backs the file up, picks
# a source the length agrees with, fetches it, then MEASURES the result and puts
# the original back if the new file is not better. A silent downgrade is the
# worst outcome: the library gets quietly worse and nothing reports it.
#
# ⚠️ --overwrite force DELETES BEFORE DOWNLOADING, which is why the backup is
# not optional: a failed fetch would otherwise destroy the song, and one has.
#
# Source choice reuses the rules from grab-explicit.sh (no music videos, no
# radio edits, prefer explicit and lyric uploads) and adds two constraints that
# only make sense for a repair:
#   - the candidate must NOT be the source this file already came from
#   - its length must match the reference length, which is the fault being fixed
#
# Usage: swap-track.sh <listfile>     one library-relative path per line
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"
LIST="${1:?usage: swap-track.sh <listfile>}"
[ -f "$LIST" ] || { echo "no such list: $LIST"; exit 1; }
BACKUP="$STATE/swap-backup"; mkdir -p "$BACKUP"

LIST="$LIST" MUSIC="$MUSIC" STATE="$STATE" BACKUP="$BACKUP" SPOTDL="$SPOTDL" \
"$PY" - <<'PY'
import os, re, json, subprocess, shutil, urllib.request, urllib.parse, urllib.error
from mutagen.mp3 import MP3
from mutagen.id3 import ID3

MUSIC, STATE, BACKUP, SPOTDL = (os.environ[k] for k in ("MUSIC","STATE","BACKUP","SPOTDL"))
UA = {"User-Agent": "megaplay-verify/1.0 (personal library check)"}
REJECT_TERMS = [r"official\s*(music\s*)?video", r"\bmusic\s*video\b", r"\bclean\b",
                r"radio\s*edit", r"\bedited\b", r"censored", r"karaoke", r"instrumental",
                r"\bcover\b", r"reaction", r"\blive\b", r"\bremix\b", r"sped\s*up", r"slowed"]
EXPLICIT = re.compile(r"\bexplicit\b", re.I)
LYRICS = re.compile(r"\blyric", re.I)

def reject_for(title):
    live = [t for t in REJECT_TERMS if not re.search(t, title, re.I)]
    return re.compile("|".join(live), re.I)

def reference(artist, title):
    base = "https://lrclib.net/api/"
    u = base + "get?artist_name=" + urllib.parse.quote(artist) + "&track_name=" + urllib.parse.quote(title)
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
            return json.load(r).get("duration")
    except urllib.error.HTTPError as e:
        if e.code != 404: return None
        try:
            u2 = base + "search?q=" + urllib.parse.quote(f"{artist} {title}")
            with urllib.request.urlopen(urllib.request.Request(u2, headers=UA), timeout=20) as r:
                arr = json.load(r)
            return arr[0].get("duration") if arr else None
        except Exception:
            return None
    except Exception:
        return None

def current_source(path):
    try:
        t = ID3(path)
        for fr in t.getall("COMM"):
            m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", str(fr))
            if m: return m.group(1)
    except Exception:
        pass
    return ""

def spotify_url(path):
    try:
        for fr in ID3(path).getall("WOAS"):
            if "open.spotify.com/track/" in fr.url: return fr.url
    except Exception:
        pass
    return ""

def pick(artist, title, want, avoid):
    reject = reject_for(title)
    best = None
    for query in (f"{artist} {title} explicit", f"{artist} {title}"):
        try:
            out = subprocess.run(["yt-dlp","--no-update","--skip-download","--flat-playlist",
                                  "--print","%(id)s\t%(title)s\t%(channel)s\t%(duration)s",
                                  f"ytsearch12:{query}"],
                                 capture_output=True, text=True, timeout=120).stdout
        except Exception:
            continue
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) < 4: continue
            vid, vtitle, channel, dur = p[0], p[1], p[2], p[3]
            if vid == avoid: continue            # the source that produced the bad file
            try: secs = int(float(dur))
            except ValueError: continue
            if want and abs(secs - want) > 10: continue   # the fault being fixed
            if reject.search(vtitle): continue
            score = 0
            if EXPLICIT.search(vtitle): score += 100
            if LYRICS.search(vtitle): score += 50
            if channel.strip().endswith("- Topic"): score += 30
            gap = abs(secs - want) if want else 0
            if best is None or (score, -gap) > (best[0], -best[3]):
                best = (score, vid, vtitle, gap, secs)
        if best and best[0] > 0:
            break
    return best

def duration(path):
    try: return MP3(path).info.length
    except Exception: return 0

rows = [l.strip() for l in open(os.environ["LIST"], encoding="utf-8") if l.strip()]
print(f"swapping {len(rows)} track(s)\n", flush=True)
kept = restored = nosource = 0
for rel in rows:
    path = os.path.join(MUSIC, rel)
    name = os.path.basename(path)
    if not os.path.exists(path):
        print(f"  MISSING  {rel}"); continue
    stem = name[:-4]
    artists, _, title = stem.partition(" - ")
    title = title or stem
    try: primary = str(ID3(path).get("TPE2") or "") or artists.split(",")[0]
    except Exception: primary = artists.split(",")[0]
    want = reference(primary.strip(), title.strip())
    before = duration(path)
    avoid = current_source(path)
    spot = spotify_url(path)
    cand = pick(primary.strip(), title.strip(), want, avoid)
    if not cand:
        nosource += 1
        print(f"  NO SOURCE  (ref {want}s, had {before:.0f}s)  {name[:52]}", flush=True)
        continue
    score, vid, vtitle, _gap, secs = cand
    bak = os.path.join(BACKUP, rel.replace("/", "___"))
    shutil.copy2(path, bak)
    folder = os.path.dirname(rel)
    # SNAPSHOT THE FOLDER. spotdl names its output from the NEW source's
    # metadata, so a replacement whose artist or title string differs by a word
    # lands as a SECOND file instead of overwriting this one. The check below
    # then measures the untouched original, reverts, and leaves an orphan
    # duplicate behind. That happened twice on 2026-09-04 and nothing else in
    # this project would have caught it: dedupe only compares exact filenames
    # inside one megaplaylist.
    fdir = os.path.dirname(path)
    before_files = set(os.listdir(fdir)) if os.path.isdir(fdir) else set()
    yt = f"https://www.youtube.com/watch?v={vid}"
    try:
        if spot:
            # The literal filename, not a template: this is a REPAIR of one
            # specific file, so the output name is not spotdl's to choose.
            stem_out = name[:-4].replace("{", "").replace("}", "")
            subprocess.run([SPOTDL, "download", f"{yt}|{spot}",
                            "--output", f"{folder}/{stem_out}.{{output-ext}}",
                            "--overwrite", "force"],
                           cwd=MUSIC, capture_output=True, text=True, timeout=420)
        else:
            # no Spotify id embedded (a failed download leaves one like this), so
            # fetch straight from the source and let tag.sh do the metadata
            subprocess.run(["yt-dlp", "--no-update", "-x", "--audio-format", "mp3",
                            "--embed-metadata", "-o", path, yt],
                           capture_output=True, text=True, timeout=420)
    except Exception as e:
        print(f"  FETCH FAILED {type(e).__name__}  {name[:48]}", flush=True)
    # Anything that appeared in this folder during THIS attempt and is not the
    # file being repaired is an artifact of the attempt. Only files this run
    # created are removed; nothing pre-existing is touched.
    stray = []
    if os.path.isdir(fdir):
        for f in set(os.listdir(fdir)) - before_files:
            if f != name and f.endswith(".mp3"):
                try:
                    os.remove(os.path.join(fdir, f)); stray.append(f)
                except OSError:
                    pass
    if stray:
        print(f"  cleaned {len(stray)} stray file(s) spotdl named differently: "
              f"{stray[0][:48]}", flush=True)
    after = duration(path) if os.path.exists(path) else 0
    # No reference means the length gate cannot judge the replacement, so the
    # only case where a blind swap is defensible is a file that is already
    # broken. Otherwise leave a working file alone.
    if not want:
        good = after > 0 and before == 0
    else:
        good = after > 0 and abs(after - want) <= 15
    if good and (before == 0 or not want or abs(after - want) < abs(before - want)):
        kept += 1
        print(f"  SWAPPED  {before:.0f}s -> {after:.0f}s (ref {want}s) [{score}]  {name[:46]}", flush=True)
        os.remove(bak)
        # The verification passes cache by path, and this path now holds DIFFERENT
        # audio, so the cached verdict describes a file that no longer exists.
        # Drop those rows and both passes will re-check it on their next run.
        for cache in ("verify-results.tsv", "verify-length.tsv"):
            cpath = os.path.join(STATE, cache)
            if not os.path.exists(cpath):
                continue
            keep = [l for l in open(cpath, encoding="utf-8")
                    if l.rstrip("\n").split("\t")[-1] != rel]
            with open(cpath, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
    else:
        shutil.copy2(bak, path); os.remove(bak); restored += 1
        print(f"  REVERTED (new {after:.0f}s no better than {before:.0f}s, ref {want}s)  {name[:40]}", flush=True)
print(f"\n  {kept} swapped, {restored} reverted, {nosource} with no acceptable source", flush=True)
PY
