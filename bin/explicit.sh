#!/usr/bin/env bash
# Find (and optionally replace) CENSORED songs - clean/radio-edit versions sitting in the loadout.
#
# WHY: spotdl downloads whatever the Spotify playlist lists. Curated playlists often list the
# CLEAN version of a track, so the download is censored through no fault of the matcher. spotdl
# has no "prefer explicit" option (only --skip-explicit), so the fix has to happen at the source:
# find the track's EXPLICIT twin on Spotify and download that instead.
#
# Report mode (default) lists every song on disk that Spotify flags clean AND for which an
# explicit twin exists. --fix downloads those twins and deletes the clean files they replace.
# A clean-flagged track with no explicit twin is left alone (it's usually just a clean song).
#
# Usage: explicit.sh <name> | --all  [--fix]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

FIX=""; TARGET=""
for a in "$@"; do
  case "$a" in
    --fix) FIX=1;;
    *) TARGET="$a";;
  esac
done
[ -n "$TARGET" ] || { echo "usage: explicit.sh <name> | --all [--fix]"; exit 1; }

if [ "$TARGET" = "--all" ]; then names="$(reg_names)"; else names="$TARGET"; fi

plan="$(mktemp --suffix=.explicit)"   # clean_file <TAB> twin_url <TAB> expected_new_file
trap 'rm -f "$plan"' EXIT

while IFS= read -r name; do
  [ -n "$name" ] || continue
  pid="$(reg_get "$name" playlist_id)"
  [ -n "$pid" ] || { echo "($name: skipped - no registry entry)"; continue; }
  echo "### $name  [$(entity_of "$pid")]"
  NAME="$name" PID="$(spotify_id "$pid")" ENTITY="$(entity_of "$pid")" \
  MUSIC="$MUSIC" PLAN="$plan" CACHE="$STATE/explicit-cache.tsv" SKIP="$STATE/explicit-skip.tsv" \
  CID="$SPOTIFY_ID" SECRET="$SPOTIFY_SECRET" "$PY" - <<'PY'
import os, re, unicodedata
from spotdl.utils.spotify import SpotifyClient

name, pid = os.environ["NAME"], os.environ["PID"]
folder = os.path.join(os.environ["MUSIC"], name)
SpotifyClient.init(client_id=os.environ["CID"], client_secret=os.environ["SECRET"],
                   user_auth=False, cache_path=None, no_cache=True)
sp = SpotifyClient()

def fname(track):  # must match grab.sh's --output "{artists} - {title}.{output-ext}"
    a = ", ".join(x["name"] for x in track["artists"])
    return f"{a} - {track['name']}".replace("/", " ") + ".mp3"

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\([^)]*\)|\[[^]]*\]", "", s)          # drop (feat. ...) / [remaster]
    s = re.sub(r"\b(clean|explicit|edit|radio|album version)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)

# --- the feature list is part of the TITLE, not an edition (2026-08-03) ---
# norm() flattens "(feat. ...)" away, which is right for "[2015 Remaster]" and wrong here: two
# DIFFERENT songs on one album can differ only by their feature list. Coloring Book carries
# "Blessings (feat. Jamila Woods)" (track 5) and "Blessings (feat. Ty Dolla $ign, ...)" (track
# 14); both normalized to "blessings", track 5 was flagged clean, track 14 explicit, so track 5
# was "swapped" for a duplicate of track 14 and lost off the disk entirely.
#
# Comparing feature lists ALWAYS is too strict and was measured as such: it broke 2 of the 15
# swaps already in the cache, both where one side names its features and the other doesn't
# ("The Way You Move - Radio Edit" -> "The Way You Move (feat. Sleepy Brown)"). So compare them
# only when BOTH titles carry one - that keeps all 15 and still separates the Blessings pair.
FEATBR = re.compile(r"[\(\[]\s*(feat|ft|featuring|with)\b[^)\]]*[\)\]]", re.I)

def feats(s):
    m = FEATBR.search(s)
    if not m: return None
    t = unicodedata.normalize("NFKD", m.group(0)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)   # punctuation-blind: "Faith Evans, 112" == "Faith Evans & 112"

def same_song(a, b):
    if norm(a) != norm(b): return False   # base titles differ -> excludes "- Remix" / "- Edit"
    fa, fb = feats(a), feats(b)
    return not (fa and fb) or fa == fb

have = {f for f in os.listdir(folder) if f.endswith(".mp3")} if os.path.isdir(folder) else set()

# Albums are checked exactly like playlists - album_tracks returns simplified tracks, which still
# carry id / name / artists / explicit, everything the twin search needs.
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

# A DIFFERENT TRACK OF THIS SAME SOURCE IS NEVER A TWIN. Backstop for the norm() trap above: if a
# candidate is itself listed on this album/playlist it is either a genuinely different song (the
# Coloring Book "Blessings" case) or a version already sitting on disk, and swapping produces a
# duplicate either way. Cheap to check and it fails safe - the clean file is simply left alone.
own_ids = {i.get("id") for i in items if i.get("id")}

# Songs deliberately left alone (no usable twin, or the swap is not worth it). Without
# this the same hopeless track is re-attempted on EVERY grab - "I'll Be Missing You" has a cached
# twin that YouTube Music cannot source, so it failed on every run until it was listed here.
skip_ids = set()
skip_path = os.environ["SKIP"]
if os.path.exists(skip_path):
    for line in open(skip_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            skip_ids.add(line.split("\t")[0])

# Verdict cache: track id -> (twin url, twin filename), or "-" for "no explicit twin exists".
# A Spotify search costs ~10s, and grab.sh runs this scan on EVERY grab, so without the cache the
# same 90-odd hopeless tracks get re-searched every time. Delete .state/explicit-cache.tsv to
# force a fresh check (worth doing occasionally - a twin can appear on Spotify later).
cache_path = os.environ["CACHE"]
cache = {}
if os.path.exists(cache_path):
    for line in open(cache_path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) == 3: cache[p[0]] = (p[1], p[2])

found = swapped = cached = 0
with open(os.environ["PLAN"], "a", encoding="utf-8") as plan, \
     open(cache_path, "a", encoding="utf-8") as cf:
    for t in items:
        if not t.get("name") or t.get("explicit"): continue
        f = fname(t)
        if f not in have: continue          # not part of the current batch on disk
        found += 1
        artist = t["artists"][0]["name"]
        if t.get("id") in skip_ids:
            print(f"    left alone (on the skip list): {artist} - {t['name']}")
            continue
        if t.get("id") in cache:
            cached += 1
            url, newf = cache[t["id"]]
            if url == "-":
                print(f"    clean, no explicit version exists: {artist} - {t['name']} (cached)")
            else:
                swapped += 1
                print(f"    CENSORED -> explicit twin: {artist} - {t['name']} (cached)")
                plan.write(f"{name}\t{f}\t{url}\t{newf}\n")
            continue
        try:
            # NOTE: spotdl's client is SpotipyFree, not spotipy - search(query, type=...) takes the
            # query POSITIONALLY and ignores limit, so cap the candidate list here instead.
            res = sp.search(f'track:"{t["name"]}" artist:"{artist}"', type="track")
        except Exception as e:
            print("    (search failed:", repr(e)[:50] + ")"); continue
        def rerecord(n):   # "(Kelis' Version)", "(Taylor's Version)", "Re-Recorded" - not the original
            return bool(re.search(r"\b\w+['’]s version\b|re-?record", n, re.I))
        cands = [c for c in res["tracks"]["items"][:100]
                 if c.get("explicit")
                 and same_song(c["name"], t["name"])             # base title + feature list
                 # PRIMARY artist must match, not just appear in the credits - otherwise a cover
                 # that name-drops them wins (a "D-Money" cover of Soulja Boy did, 2026-07-26).
                 and norm(c["artists"][0]["name"]) == norm(artist)
                 and c.get("id") not in own_ids                  # see own_ids above
                 and not rerecord(c["name"])]
        # Prefer an exact title match over a normalized one, then the most popular release.
        twin = max(cands, key=lambda c: (c["name"].lower() == t["name"].lower(),
                                         c.get("popularity", 0))) if cands else None
        if twin:
            swapped += 1
            url, newf = twin["external_urls"]["spotify"], fname(twin)
            print(f"    CENSORED -> explicit twin: {artist} - {t['name']}")
            plan.write(f"{name}\t{f}\t{url}\t{newf}\n")
            cf.write(f"{t['id']}\t{url}\t{newf}\n"); cf.flush()
        else:
            print(f"    clean, no explicit version exists: {artist} - {t['name']}")
            cf.write(f"{t['id']}\t-\t-\n"); cf.flush()
print(f"  {found} clean-flagged on disk, {swapped} have an explicit twin ({cached} from cache)")
PY
done <<< "$names"

n=$(wc -l < "$plan")
echo
if [ "$n" -eq 0 ]; then echo "Nothing to swap."; exit 0; fi
if [ -z "$FIX" ]; then
  echo "$n song(s) could be swapped for the explicit version. Re-run with --fix to do it."
  exit 0
fi

echo "Downloading $n explicit version(s)..."
cd "$MUSIC" || exit 1
start_ts=$(date +%s)   # to prove same-name replacements actually happened (mtime check below)

# ⚠️ SAFETY NET - DO NOT REMOVE. `--overwrite force` makes spotdl DELETE the existing file BEFORE
# it downloads the replacement, so a yt-dlp failure destroys the song outright. That is not
# theoretical: it ate "Ashanti - Foolish" and "T.I. - Live Your Life" on 2026-07-26 and they had
# to be re-fetched by hand. Back every target up first; restore any whose swap did not land.
backup="$STATE/explicit-backup"
mkdir -p "$backup"
# The field names spell out the plan file's layout, so they all get named even where this
# loop only needs two of them.
# shellcheck disable=SC2034
while IFS=$'\t' read -r bname bclean burl bnewf; do
  [ -f "$MUSIC/$bname/$bclean" ] && cp -p "$MUSIC/$bname/$bclean" "$backup/$(slug "$bname/$bclean")"
done < "$plan"
# Batch the URLs: a long list of individual track URLs makes spotdl do a Spotify lookup each and
# it stalls on rate limits (same trap documented for batch grabs in CLAUDE.md).
awk -F'\t' '{print $1}' "$plan" | sort -u | while IFS= read -r name; do
  # pname, not n: `n` is the plan's total line count, printed in the summary at the end. The
  # pipeline above makes this a subshell so the outer n survives today, but a reader cannot
  # see that from here and a later rewrite would silently print a playlist name as the total.
  urls=()
  # shellcheck disable=SC2034
  while IFS=$'\t' read -r pname f u nf; do [ "$pname" = "$name" ] && urls+=("$u"); done < "$plan"
  echo "  $name: ${#urls[@]} track(s)"
  for ((i=0; i<${#urls[@]}; i+=10)); do
    batch=("${urls[@]:i:10}")
    for attempt in 1 2; do
      out="$(mktemp)"
      # --overwrite force is REQUIRED: an explicit twin usually has the SAME filename as the clean
      # file it replaces, and spotdl's default (skip) then refuses to write - 8 of 13 swaps
      # silently did nothing on 2026-07-26 without this.
      "$SPOTDL" download "${batch[@]}" --output "$name/{artists} - {title}.{output-ext}" \
        --overwrite force 2>&1 | tee "$out" | grep --line-buffered -iE 'downloaded|error|skipping' \
        | sed 's/^/    /'
      # YouTube Music intermittently fails a whole batch ("Could not get playlist hashes").
      # Retry once before giving up on it.
      if grep -q 'RequestError\|Could not get .* hashes' "$out" && [ "$attempt" = 1 ]; then
        echo "    (transient YouTube Music failure - retrying this batch once)"
        rm -f "$out"; continue
      fi
      rm -f "$out"; break
    done
  done
done

# Account for every planned swap. Two shapes:
#  - twin has a DIFFERENT filename: delete the clean file, but only once the replacement is on disk.
#  - twin has the SAME filename: --overwrite force rewrote it in place, so prove it by mtime
#    rather than assuming (a silent no-op here is exactly the bug that hid on 2026-07-26).
removed=0; inplace=0; failed=0; restored=0
# shellcheck disable=SC2034
while IFS=$'\t' read -r name clean url newf; do
  bak="$backup/$(slug "$name/$clean")"
  if [ "$newf" != "$clean" ]; then
    if [ -f "$MUSIC/$name/$newf" ] && [ -f "$MUSIC/$name/$clean" ]; then
      rm -f "$MUSIC/$name/$clean"; removed=$((removed+1)); echo "  removed clean: $name/$clean"
      # A RENAMED swap leaves a hole: the clean filename is gone, so the NEXT grab downloads it
      # again, every time, forever (caught live 2026-07-27 re-fetching Kelis and Diddy). Record it
      # so grab.sh can seed a skip file for it - see "the replaced-file ledger" in CLAUDE.md.
      grep -qxF "$name	$clean" "$STATE/explicit-replaced.tsv" 2>/dev/null || \
        printf '%s\t%s\n' "$name" "$clean" >> "$STATE/explicit-replaced.tsv"
    elif [ ! -f "$MUSIC/$name/$newf" ]; then
      failed=$((failed+1)); echo "  NOT SWAPPED (download failed, clean kept): $name/$clean"
    fi
  else
    mt=$(stat -c %Y "$MUSIC/$name/$newf" 2>/dev/null || echo 0)
    if [ "$mt" -ge "$start_ts" ]; then
      inplace=$((inplace+1)); echo "  replaced in place: $name/$newf"
    elif [ ! -f "$MUSIC/$name/$clean" ] && [ -f "$bak" ]; then
      # force-overwrite deleted it and the download failed - put the original back.
      cp -p "$bak" "$MUSIC/$name/$clean"
      failed=$((failed+1)); restored=$((restored+1))
      echo "  NOT SWAPPED (download failed) - RESTORED original: $name/$clean"
    else
      failed=$((failed+1)); echo "  NOT SWAPPED (file untouched): $name/$clean"
    fi
  fi
  rm -f "$bak"
done < "$plan"
rmdir "$backup" 2>/dev/null
echo "Swapped $((removed+inplace)) of $n ($removed renamed, $inplace in place, $failed failed, $restored restored)."
awk -F'\t' '{print $1}' "$plan" | sort -u | while IFS= read -r name; do "$HERE/tag.sh" "$name"; done
