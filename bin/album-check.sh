#!/usr/bin/env bash
# Album-tag consistency: collapse every spelling of the same album onto ONE name.
#
# WHY: albums should be grouped, preferring the shorter name, so you are not scrolling past four
# spellings of one record to find it on a phone. Players group
# by album, so "2001" and "2001 (Explicit Version)" show up as two entries for the same record.
# FEWER ALBUM ENTRIES BEATS METADATA PURITY - group aggressively.
#
# Grouping: album names are normalised (lowercase, drop bracketed/parenthetical suffixes, drop
# edition wording like deluxe / remaster / anniversary / explicit / UK version, drop stray years).
# Everything that normalises the same is one album.
#   ⚠️ An album can BE a year ("2001"). Stripping the year emptied the key and those albums fell
#   out of the comparison entirely - "2001" vs "2001 (Explicit Version)" went unreported until
#   2026-07-28. The year is only stripped when something survives it.
#
# Canonical name: the SHORTEST variant, then edition wording removed from it, e.g.
#   "Ready to Die (The Remaster)" + "Ready to Die (The Remaster; 2015 Remaster)" -> "Ready to Die"
# "(feat. ...)" is never stripped - it is part of the title, not an edition.
#
# Report-only by default; --apply rewrites the odd ones out.
#
# Usage: album-check.sh [folder-under-~/Music] [--apply]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"

APPLY=""; SCOPE=""
for a in "$@"; do
  case "$a" in --apply) APPLY=1;; *) SCOPE="$a";; esac
done

APPLY="$APPLY" "$PY" - "$MUSIC" "$SCOPE" <<'PY'
import os, re, sys, collections, unicodedata
from mutagen.mp3 import MP3
from mutagen.id3 import TALB, Encoding

music, scope = sys.argv[1], sys.argv[2]
root = os.path.join(music, scope) if scope else music
apply_ = bool(os.environ.get("APPLY"))

EDITION_WORDS = (r"deluxe|remaster(?:ed)?|anniversary|expanded|edition|version|complete|bonus"
                 r"|reissue|explicit|clean|mono|stereo|soundtrack|remix")
EDITION = re.compile(r"\b(?:%s)\b" % EDITION_WORDS, re.I)
# a (...) or [...] group that contains edition wording - safe to drop from a display name.
EDITION_GROUP = re.compile(r"\s*[\(\[][^)\]]*(?:%s)[^)\]]*[\)\]]" % EDITION_WORDS, re.I)

def norm(s):
    base = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", base)
    t = re.sub(r"\s-\s.*$", " ", t) if EDITION.search(t) else t
    t = EDITION.sub(" ", t)
    kept = re.sub(r"[^a-z0-9]", "", t)
    dropped_year = re.sub(r"[^a-z0-9]", "", re.sub(r"\b(19|20)\d{2}\b", " ", t))
    return dropped_year or kept or re.sub(r"[^a-z0-9]", "", base)

def display(name):
    """Shortest variant with edition brackets removed. '(feat. ...)' survives untouched.

    Scans brackets with a depth counter rather than a regex: "[International Version (Explicit)]"
    nests, and a regex match stopped at the inner ')' and left a stray ']' behind.
    """
    out, i, n, removed = [], 0, len(name), False
    while i < n:
        c = name[i]
        if c in "([":
            depth, j = 1, i + 1
            while j < n and depth:
                if name[j] in "([": depth += 1
                elif name[j] in ")]": depth -= 1
                j += 1
            group = name[i:j]
            if EDITION.search(group):
                removed = True; i = j; continue     # drop the whole bracket group
            out.append(group); i = j
        else:
            out.append(c); i += 1
    s = re.sub(r"\s{2,}", " ", "".join(out)).strip()
    # Only tidy trailing punctuation when something was actually removed - otherwise a title that
    # really ends in one loses it ("My Dear Melancholy," is the album's real name).
    if removed:
        s = re.sub(r"[\s\-;,]+$", "", s)
    return s or name

files = []
for dirpath, _dirs, names in os.walk(root):
    for f in sorted(names):
        if f.lower().endswith(".mp3"):
            files.append(os.path.join(dirpath, f))

groups = collections.defaultdict(lambda: collections.defaultdict(list))
for p in files:
    try:
        tags = MP3(p).tags
        if tags is None: continue
        alb = str(tags.get("TALB") or "").strip()
    except Exception:
        continue
    if alb:
        groups[norm(alb)][alb].append(p)

issues = fixed = 0
for k, variants in sorted(groups.items()):
    shortest = sorted(variants, key=lambda a: (len(a), -len(variants[a])))[0]
    canon = display(shortest)
    odd = {a: v for a, v in variants.items() if a != canon}
    if not odd: continue          # already all one name
    issues += 1
    print(f"\n  '{canon}'  <- {len(variants)} spelling(s), {sum(len(v) for v in variants.values())} song(s)")
    for alb, paths in sorted(variants.items(), key=lambda kv: -len(kv[1])):
        mark = "KEEP" if alb == canon else "->  "
        print(f"    {mark} {len(paths):3} x  {alb!r}")
        if alb == canon: continue
        for p in paths:
            print(f"           {os.path.relpath(p, music)}")
            if apply_:
                try:
                    a = MP3(p)
                    if a.tags is None: a.add_tags()
                    a.tags.setall("TALB", [TALB(encoding=Encoding.UTF8, text=[canon])])
                    a.save(); fixed += 1
                except Exception as e:
                    print(f"           ERR {e!r}"[:90])

if issues == 0:
    print(f"  every album has a single consistent name across {len(files)} song(s).")
elif apply_:
    print(f"\n  {issues} album(s) collapsed, {fixed} file(s) retagged.")
else:
    print(f"\n  {issues} album(s) spelled more than one way across {len(files)} song(s) - "
          f"re-run with --apply to collapse them.")

# --- second pass: COVER ART consistency -------------------------------------------------------
# One album name with two different embedded images is the same bug in picture form: the phone
# can't settle on a cover. Found 2026-07-28 on "Acid Rap", where 13 tracks carried the original
# cover and the reissued "Juice" carried the 10th-anniversary one. The majority image wins; a
# song with no art at all gets the album's image too.
import hashlib
from mutagen.id3 import APIC

art_issues = art_fixed = 0
for k, variants in sorted(groups.items()):
    paths = [p for v in variants.values() for p in v]
    if len(paths) < 2: continue
    by_img, none_art = collections.defaultdict(list), []
    for p in paths:
        try:
            tags = MP3(p).tags
            pics = tags.getall("APIC") if tags else []
        except Exception:
            continue
        if not pics: none_art.append(p)
        else: by_img[hashlib.md5(pics[0].data).hexdigest()].append((p, pics[0]))
    if len(by_img) < 2 and not (none_art and by_img): continue
    art_issues += 1
    winner = max(by_img.values(), key=len)
    ref = winner[0][1]
    album_name = display(sorted(variants, key=lambda a: (len(a), -len(variants[a])))[0])
    odd = [p for grp in by_img.values() if grp is not winner for p, _ in grp] + none_art
    print(f"\n  cover art: '{album_name}' has {len(by_img)} different image(s)"
          f"{' + %d with none' % len(none_art) if none_art else ''}"
          f" - {len(winner)} song(s) share the winner")
    for p in odd:
        print(f"    ->  {os.path.relpath(p, music)}")
        if apply_:
            try:
                a = MP3(p)
                if a.tags is None: a.add_tags()
                a.tags.delall("APIC")
                a.tags.add(APIC(encoding=Encoding.UTF8, mime=ref.mime, type=3,
                                desc="Cover", data=ref.data))
                a.save(); art_fixed += 1
            except Exception as e:
                print(f"        ERR {e!r}"[:90])

if art_issues:
    print(f"\n  {art_issues} album(s) with mismatched cover art"
          + (f", {art_fixed} file(s) re-arted." if apply_ else " - re-run with --apply to unify."))
PY
