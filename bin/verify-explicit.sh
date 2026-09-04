#!/usr/bin/env bash
# Verify that songs which SHOULD contain profanity actually do, so a censored
# file cannot sit in the library unnoticed.
#
# WHY THIS SHAPE. Censorship cannot be read off metadata: Spotify's explicit
# flag describes the LISTING while the audio comes from somewhere else, and an
# album verified 23 of 23 explicit still produced a censored track. Nor can it
# be read off the audio alone: a model will not invent profanity, so finding
# some proves a track is uncensored, but finding none proves nothing, because
# the song may simply have nothing to censor.
#
# So the two halves answer different questions and neither is sufficient:
#   STAGE 1  the LYRICS say what SHOULD be in the audio  (a plain web lookup)
#   STAGE 2  the AUDIO says what IS there                (early-exit Whisper)
#
# A song whose lyrics are clean is skipped rather than decoded, which is most
# of the saving. A song whose lyrics carry profanity is decoded only until the
# first hit, usually the first verse. What is left over, lyrics say profanity
# and the audio has none, is the actual finding.
#
# ⚠️ NO API. Deliberately no Spotify and no YouTube: both are heavily rate
# limited, and neither can answer this question anyway.
# ⚠️ Lyrics are used as data and never stored or printed. Only counts survive.
#
# Usage: verify-explicit.sh <name>|--all [--stage1-only]
#   Results append to .state/verify-results.tsv and are a CACHE: a file already
#   verified is skipped, so re-running after a grab only costs the new tracks.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

TARGET=""; STAGE1_ONLY=""; BACKFILL=""
for a in "$@"; do
  case "$a" in
    --stage1-only) STAGE1_ONLY=1;;
    --backfill) BACKFILL=1; TARGET="${TARGET:---all}";;
    *) TARGET="$a";;
  esac
done
[ -n "$TARGET" ] || { echo "usage: verify-explicit.sh <name>|--all [--stage1-only]"; exit 1; }

RESULTS="$STATE/verify-results.tsv"
WORK="$(mktemp --suffix=.verify)"
trap 'rm -f "$WORK"' EXIT
mkdir -p "$STATE"

if [ "$TARGET" = "--all" ]; then ROOT="$MUSIC"; else ROOT="$MUSIC/$TARGET"; fi
[ -d "$ROOT" ] || { echo "no such folder: $ROOT"; exit 1; }

# ------------------------------------------------------------- backfill
# An UNKNOWN already carries its audio result: it was decoded and produced no
# hit, and only the LYRICS half was missing. So a second source can re-decide it
# with no decoding at all, which is the whole reason this mode exists.
if [ -n "$BACKFILL" ]; then
  MUSIC="$MUSIC" RESULTS="$RESULTS" "$PY" - <<'PY'
import os, re, json, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from mutagen.id3 import ID3
MUSIC, RESULTS = os.environ["MUSIC"], os.environ["RESULTS"]
WORDS = {"fuck","fucking","fucked","fucker","fuckin","shit","shitty","bitch","bitches",
         "ass","asshole","nigga","niggas","niggaz","cunt","dick","pussy","motherfucker",
         "motherfuckin","goddamn","whore","slut","cock"}
rows = [l.rstrip("\n").split("\t") for l in open(RESULTS, encoding="utf-8") if l.strip()]
unknown = [r for r in rows if r and r[0] == "UNKNOWN"]
print(f"backfill: {len(unknown)} UNKNOWN row(s) to re-decide from lyrics", flush=True)

def fetch_ovh(a, t):
    u = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(a)}/{urllib.parse.quote(t)}"
    with urllib.request.urlopen(u, timeout=20) as r:
        return json.load(r).get("lyrics") or ""

def fetch_lrclib(a, t):
    base, hdr = "https://lrclib.net/api/", {"User-Agent": "megaplay-verify/1.0"}
    u = base + "get?artist_name=" + urllib.parse.quote(a) + "&track_name=" + urllib.parse.quote(t)
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=20) as r:
            return json.load(r).get("plainLyrics") or ""
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        u2 = base + "search?q=" + urllib.parse.quote(f"{a} {t}")
        with urllib.request.urlopen(urllib.request.Request(u2, headers=hdr), timeout=20) as r:
            arr = json.load(r)
        return (arr[0].get("plainLyrics") or "") if arr else ""

def redecide(row):
    rel = row[4]
    path = os.path.join(MUSIC, rel)
    stem = os.path.basename(path)[:-4]
    artists, _, title = stem.partition(" - ")
    title = title or stem
    try:
        primary = str(ID3(path).get("TPE2") or "") or artists.split(",")[0]
    except Exception:
        primary = artists.split(",")[0]
    body = ""
    for fetch in (fetch_ovh, fetch_lrclib):
        try:
            body = fetch(primary.strip(), title.strip())
            if body:
                break
        except Exception:
            continue
    if not body:
        return row, "still unknown"
    found = sorted(set(re.findall(r"[a-z']+", body.lower())) & WORDS)
    if found:
        # lyrics say profanity, the audio had none: that is the finding
        return [ "SUSPECT", str(len(found)), "0", "-", rel ], "-> SUSPECT"
    return [ "NOPROFANITY", "0", "0", "-", rel ], "-> NOPROFANITY"

changed = {}
with ThreadPoolExecutor(max_workers=8) as pool:
    for newrow, what in pool.map(redecide, unknown):
        if what != "still unknown":
            changed[newrow[4]] = newrow
            print(f"  {what:<16} {newrow[4]}", flush=True)
out = []
for r in rows:
    out.append(changed.get(r[4], r) if len(r) >= 5 else r)
tmp = RESULTS + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    for r in out:
        f.write("\t".join(r) + "\n")
os.replace(tmp, RESULTS)
print(f"backfill done: {len(changed)} of {len(unknown)} resolved, "
      f"{len(unknown)-len(changed)} still unknown", flush=True)
PY
  exit 0
fi

# ---------------------------------------------------------------- stage 1
ROOT="$ROOT" MUSIC="$MUSIC" RESULTS="$RESULTS" WORK="$WORK" "$PY" - <<'PY'
import os, re, json, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from mutagen.id3 import ID3

MUSIC, ROOT, RESULTS, WORK = (os.environ[k] for k in ("MUSIC","ROOT","RESULTS","WORK"))
WORDS = {"fuck","fucking","fucked","fucker","fuckin","shit","shitty","bitch","bitches",
         "ass","asshole","nigga","niggas","niggaz","cunt","dick","pussy","motherfucker",
         "motherfuckin","motherfucker's","goddamn","whore","slut","cock"}

done = set()
if os.path.exists(RESULTS):
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
print(f"stage 1: {len(files)} track(s) to look up ({len(done)} already verified)", flush=True)

def artist_title(path, rel):
    stem = os.path.basename(path)[:-4]
    artists, _, title = stem.partition(" - ")
    if not title:
        title = stem
    try:
        t = ID3(path)
        primary = str(t.get("TPE2") or "") or artists.split(",")[0]
    except Exception:
        primary = artists.split(",")[0]
    return primary.strip(), title.strip()

def fetch_ovh(artist, title):
    u = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"
    with urllib.request.urlopen(u, timeout=20) as r:
        return json.load(r).get("lyrics") or ""

def fetch_lrclib(artist, title):
    """Second source. Open API meant for third-party use, and it covers what the
    first one misses: every track that failed over on 2026-09-03 was found here,
    which is why there are two. Falls back to the fuzzy search endpoint on a 404,
    because an exact artist and title match is stricter than a filename can be."""
    base = "https://lrclib.net/api/"
    hdr = {"User-Agent": "megaplay-verify/1.0 (personal library check)"}
    u = base + "get?artist_name=" + urllib.parse.quote(artist) + "&track_name=" + urllib.parse.quote(title)
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=20) as r:
            return json.load(r).get("plainLyrics") or ""
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        u2 = base + "search?q=" + urllib.parse.quote(f"{artist} {title}")
        with urllib.request.urlopen(urllib.request.Request(u2, headers=hdr), timeout=20) as r:
            arr = json.load(r)
        return (arr[0].get("plainLyrics") or "") if arr else ""

def classify(body):
    """Counts only. The text is never returned, stored or printed."""
    return sorted(set(re.findall(r"[a-z']+", body.lower())) & WORDS)

def lyrics_for(artist, title):
    for fetch in (fetch_ovh, fetch_lrclib):
        try:
            body = fetch(artist, title)
            if body:
                return body
        except Exception:
            continue
    return ""

def lookup(job):
    path, rel = job
    artist, title = artist_title(path, rel)
    body = lyrics_for(artist, title)
    if not body:
        return rel, path, "NOLYRICS", []
    found = classify(body)
    return rel, path, ("HASPROFANITY" if found else "CLEANLYRICS"), found

n_prof = n_clean = n_none = 0
with ThreadPoolExecutor(max_workers=8) as pool, open(WORK, "w", encoding="utf-8") as w:
    for i, (rel, path, status, found) in enumerate(pool.map(lookup, files), 1):
        if status == "HASPROFANITY": n_prof += 1
        elif status == "CLEANLYRICS": n_clean += 1
        else: n_none += 1
        w.write(f"{status}\t{','.join(found)}\t{path}\t{rel}\n")
        if i % 50 == 0:
            print(f"  ...{i}/{len(files)} looked up", flush=True)
print(f"stage 1 done: {n_prof} with profanity in lyrics, {n_clean} clean lyrics, "
      f"{n_none} no lyrics found", flush=True)
PY

if [ -n "$STAGE1_ONLY" ]; then echo "(stage 1 only)"; exit 0; fi

# ---------------------------------------------------------------- stage 2
ASR="${VOICE_ASR_PY:-}"
if [ -z "$ASR" ] || [ ! -x "$ASR" ]; then
  echo "SKIPPED stage 2: no Whisper interpreter configured (set VOICE_ASR_PY in env.local.sh)."
  echo "  Stage 1 results are complete; nothing was verified against the audio."
  exit 0
fi
WORK="$WORK" RESULTS="$RESULTS" "$ASR" - <<'PY'
import os, re, time
from faster_whisper import WhisperModel

WORK, RESULTS = os.environ["WORK"], os.environ["RESULTS"]
WORDS = {"fuck","fucking","fucked","fucker","fuckin","shit","bitch","bitches","ass",
         "asshole","nigga","niggas","niggaz","cunt","dick","pussy","motherfucker",
         "motherfuckin","goddamn"}
MIN_SECONDS = 90          # below this a track cannot produce evidence either way

rows = [l.rstrip("\n").split("\t") for l in open(WORK, encoding="utf-8") if l.strip()]
todo = [r for r in rows if r[0] in ("HASPROFANITY", "NOLYRICS")]
skip = [r for r in rows if r[0] == "CLEANLYRICS"]
print(f"stage 2: {len(todo)} to decode, {len(skip)} skipped (lyrics carry nothing to censor)",
      flush=True)

out = open(RESULTS, "a", encoding="utf-8")
for status, terms, path, rel in skip:
    out.write(f"NOPROFANITY\t0\t0\t-\t{rel}\n")
out.flush()

model = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=6)
t_start = time.time()
counts = {}
for i, (status, terms, path, rel) in enumerate(todo, 1):
    want = set(terms.split(",")) & WORDS if terms else WORDS
    try:
        segs, info = model.transcribe(path, beam_size=1, vad_filter=False)
        if info.duration < MIN_SECONDS:
            verdict, hit_at, hits = "TOOSHORT", "-", 0
        else:
            hit_at, hits = None, 0
            for s in segs:
                if any(w in want for w in re.findall(r"[a-z']+", s.text.lower())):
                    hit_at, hits = f"{s.end:.0f}", 1
                    break                      # early exit: stop decoding
            if hits:
                verdict = "VERIFIED"
            else:
                verdict = "SUSPECT" if status == "HASPROFANITY" else "UNKNOWN"
                hit_at = "-"
    except Exception as e:
        verdict, hit_at, hits = "ERROR:" + type(e).__name__, "-", 0
    counts[verdict] = counts.get(verdict, 0) + 1
    out.write(f"{verdict}\t{len(want)}\t{hits}\t{hit_at}\t{rel}\n"); out.flush()
    if verdict in ("SUSPECT", "UNKNOWN"):
        print(f"  {verdict}: {rel}", flush=True)
    if i % 25 == 0:
        rate = (time.time() - t_start) / i
        print(f"  ...{i}/{len(todo)} decoded, {rate:.1f}s each, "
              f"{(len(todo)-i)*rate/60:.0f} min left", flush=True)
out.close()
print("stage 2 done: " + ", ".join(f"{v} {c}" for v, c in sorted(counts.items())), flush=True)
PY
echo "VERIFY COMPLETE"
