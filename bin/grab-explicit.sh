#!/usr/bin/env bash
# Fill a folder's gaps, CHOOSING THE YOUTUBE SOURCE instead of letting the
# matcher choose it.
#
# WHY: spotdl takes metadata from Spotify and audio from YouTube Music, and it
# picks whichever upload it thinks matches. That upload is frequently an
# official music video, which is cut for broadcast, so a track Spotify flags
# explicit can still play censored. Nothing in the metadata reveals it, and
# transcribing the audio only ever proves the GOOD case (a model will not
# invent profanity, but finding none proves nothing). Both approaches were
# built and both failed, which is why the source is chosen here instead.
#
# So this searches YouTube per track and scores what comes back:
#   REJECTED  official video, music video, clean, radio edit, edited,
#             censored, karaoke, instrumental, cover, reaction, live
#   PREFERRED "explicit" in the title, then "lyrics" (lyric videos are
#             usually the unedited album cut), then a "- Topic" channel,
#             which is the album audio YouTube Music generates
# and a candidate whose length is far from the Spotify track's is dropped
# outright, because that is a different recording whatever it is called.
#
# A track with no acceptable source is LEFT ALONE and reported. That is the
# whole point: a gap is better than a censored file, and the alternative is
# what put one in the library in the first place.
#
# Usage: grab-explicit.sh <name> [--dry-run]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

DRY=""
NAME=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1;;
    *) NAME="$a";;
  esac
done
[ -n "$NAME" ] || { echo "usage: grab-explicit.sh <name> [--dry-run]"; exit 1; }

pid="$(reg_get "$NAME" playlist_id)"
[ -n "$pid" ] || { echo "$NAME: no registry entry"; exit 1; }

plan="$(mktemp --suffix=.picks)"
trap 'rm -f "$plan"' EXIT

echo "### $NAME  [$(entity_of "$pid")]"
NAME="$NAME" PID="$(spotify_id "$pid")" ENTITY="$(entity_of "$pid")" \
MUSIC="$MUSIC" PLAN="$plan" CID="$SPOTIFY_ID" SECRET="$SPOTIFY_SECRET" \
"$PY" - <<'PY'
import os, re, subprocess
from spotdl.utils.spotify import SpotifyClient

name, pid = os.environ["NAME"], os.environ["PID"]
folder = os.path.join(os.environ["MUSIC"], name)
SpotifyClient.init(client_id=os.environ["CID"], client_secret=os.environ["SECRET"],
                   user_auth=False, cache_path=None, no_cache=True)
sp = SpotifyClient()

def fname(track):        # must match grab.sh's --output "{artists} - {title}"
    a = ", ".join(x["name"] for x in track["artists"])
    return f"{a} - {track['name']}".replace("/", " ") + ".mp3"

# A music video is the thing to avoid; the rest are wrong recordings that a
# title search drags in. "live" is here because an album track's live cut is a
# different performance, not a different encoding of the same one.
REJECT_TERMS = [
    r"official\s*(music\s*)?video", r"\bmusic\s*video\b", r"\bvideo\s*oficial\b",
    r"\bclean\b", r"radio\s*edit", r"\bedited\b", r"censored",
    r"karaoke", r"instrumental", r"\bcover\b", r"reaction",
    r"\blive\b", r"\bremix\b", r"sped\s*up", r"slowed",
]

def reject_for(track_title):
    """The reject list, MINUS any term the wanted track's own title contains.

    "Diamonds From Sierra Leone - Remix" IS the album track on Late
    Registration, and a blanket "remix" rule refused every source for it
    (2026-08-15, the first refusal these rules ever produced). The word only
    signals a wrong recording when the recording we want does not have it in
    its name, and the same holds for a live album or an a cappella cut. So the
    filter is built per track: a term that appears in the Spotify title stops
    being disqualifying for that track and for that track only.
    """
    live = [t for t in REJECT_TERMS if not re.search(t, track_title, re.I)]
    return re.compile("|".join(live), re.I)
EXPLICIT = re.compile(r"\bexplicit\b", re.I)
LYRICS = re.compile(r"\blyric", re.I)

have = {f for f in os.listdir(folder) if f.endswith(".mp3")} if os.path.isdir(folder) else set()

items, offset = [], 0
if os.environ["ENTITY"] == "album":
    while True:
        page = sp.album_tracks(pid, limit=50, offset=offset)
        items += page["items"]
        if not page.get("next"): break
        offset += 50
else:
    while True:
        page = sp.playlist_items(pid, limit=100, offset=offset)
        items += [i.get("track") or {} for i in page["items"]]
        if not page.get("next"): break
        offset += 100

def search(query, want_secs, reject):
    """Ask YouTube for candidates and score them. Returns (id, title, why) or None.

    `reject` is built per track by reject_for, not global, so a track whose own
    title says remix or live is not refused for saying it.
    """
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-update", "--skip-download", "--flat-playlist",
             "--print", "%(id)s\t%(title)s\t%(channel)s\t%(duration)s",
             f"ytsearch12:{query}"],
            capture_output=True, text=True, timeout=120).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    best = None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        vid, title, channel, dur = parts[0], parts[1], parts[2], parts[3]
        try:
            secs = int(float(dur))
        except ValueError:
            continue
        # A length that far off is a different recording whatever it claims.
        if want_secs and abs(secs - want_secs) > 20:
            continue
        if reject.search(title):
            continue
        score, why = 0, []
        if EXPLICIT.search(title):
            score += 100; why.append("explicit in title")
        if LYRICS.search(title):
            score += 50; why.append("lyric video")
        if channel.strip().endswith("- Topic"):
            score += 30; why.append("Topic channel")
        gap = abs(secs - want_secs) if want_secs else 0
        if best is None or (score, -gap) > (best[0], -best[3]):
            best = (score, vid, title, gap, ", ".join(why) or "no positive signal")
    return best

picked = skipped = 0
with open(os.environ["PLAN"], "a", encoding="utf-8") as plan:
    for t in items:
        if not t.get("name"):
            continue
        f = fname(t)
        if f in have:
            continue                      # already on disk, gaps only
        artist = t["artists"][0]["name"]
        want = int((t.get("duration_ms") or 0) / 1000)
        best = None
        # Ask for the explicit cut by name first, then fall back to a plain
        # search. Two cheap queries beat one broad one: the first surfaces
        # uploads that label themselves, which is exactly what is wanted.
        reject = reject_for(t["name"])
        for query in (f'{artist} {t["name"]} explicit', f'{artist} {t["name"]}'):
            best = search(query, want, reject)
            if best and best[0] > 0:
                break
        if not best:
            skipped += 1
            print(f"    NO SOURCE: {artist} - {t['name']}", flush=True)
            continue
        score, vid, title, _gap, why = best
        picked += 1
        print(f"    [{score:>3}] {t['name'][:40]:<42} {why}", flush=True)
        plan.write(f"https://www.youtube.com/watch?v={vid}|{t['external_urls']['spotify']}\n")
print(f"  {picked} source(s) chosen, {skipped} track(s) with nothing acceptable")
PY

n=$(grep -c . "$plan" 2>/dev/null || echo 0)
if [ "$n" -eq 0 ]; then echo "Nothing to fetch."; exit 0; fi
if [ -n "$DRY" ]; then echo "(dry run) $n track(s) would be fetched from the sources above."; exit 0; fi

echo "Downloading $n track(s) from the chosen sources..."
cd "$MUSIC" || exit 1
# Batched, for the same reason grab.sh batches: a long list of individual URLs
# makes spotdl do a lookup per URL and it stalls on rate limits.
mapfile -t pairs < "$plan"
for ((i=0; i<${#pairs[@]}; i+=8)); do
  "$SPOTDL" download "${pairs[@]:i:8}" \
    --output "$NAME/{artists} - {title}.{output-ext}" 2>&1 \
    | grep --line-buffered -iE 'downloaded|error|skipping' | sed 's/^/    /'
done
"$HERE/tag.sh" "$NAME"
# Verify what just landed. Picking a good SOURCE is not the same as proving the
# AUDIO is uncensored: this script chooses an upload that says it is explicit,
# and this pass checks whether the words are actually in it.
"$HERE/verify-explicit.sh" "$NAME"
# And the LENGTH pass: a radio edit or a truncated download is shorter than the
# real recording, and neither shows up in the tags. Cheap, no decoding.
"$HERE/verify-length.sh" "$NAME"
