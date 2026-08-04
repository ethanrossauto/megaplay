#!/usr/bin/env python3
"""Voice control for the library, triggered by a volume down-up gesture on a
bluetooth headset.

Why this trigger, when two more obvious ones exist: the play/pause double-click
is unusable, because headset firmware collapses it into a single AVRCP "next
track" before it ever reaches the PC, leaving nothing to detect. Raw key events
are out too on any Wayland session, since the compositor owns /dev/input and a
background reader sees nothing regardless of permissions. Volume down-then-up is
what survived. It reaches the PC as two BlueZ property changes and it returns to
the exact starting value, so there is no drift.

It is not, however, a shape nothing else makes: a desktop volume slider dragged
down and back up produces the same pair at either end of its ramp. What counts
as a gesture, and every measurement behind it, is defined ONCE in THE GESTURE
CLAUSES below, beside the constants it describes. Read it there and change it
there.

Flow: gesture -> pause -> listen -> transcribe -> ask Claude -> run it -> resume.

Dispatch runs through the Claude Code CLI (`claude -p`), which authenticates
with your existing subscription rather than an API key.

Run:  bin/voice.sh          (wrapper; starts the ASR server too)
Test: bin/voice.py --selftest   (verifies the gesture path without any audio)
"""
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time

from gi.repository import Gio, GLib

HOME = os.path.expanduser("~")
# Derived from this file's location, not hardcoded, so a clone works anywhere.
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUSIC = os.environ.get("MEGAPLAY_MUSIC") or os.path.join(HOME, "Music")
REGISTRY = os.path.join(PROJECT, "playlists.tsv")
MPV_SOCK = "/tmp/spotify-mpv.sock"
# Same file play.sh uses, so the two stay interchangeable.
PLAYLIST_FILE = "/tmp/spotify-mpv-playlist.txt"
ASR_SOCK = os.environ.get("VOICE_ASR_SOCK", "/tmp/spotify-voice-asr.sock")
# Sonnet by default, not Opus. Every command ships the whole track catalogue
# (~15k tokens), and Claude Code draws on the same subscription limits as your
# chats, so an Opus default would quietly spend a Pro plan's budget on picking
# songs. Sonnet is also the faster half of a ~20s round trip. Override with
# VOICE_CLAUDE_MODEL if your plan has room.
MODEL = os.environ.get("VOICE_CLAUDE_MODEL", "claude-sonnet-5")
# Thinking time is nearly free now that playback resumes BEFORE dispatch (see
# handle_gesture): the music is already running while the model works, so a
# slow answer costs a late track change rather than a silent gap. A good answer
# in 40s beats a refusal in 5.
CLAUDE_TIMEOUT = float(os.environ.get("VOICE_CLAUDE_TIMEOUT", "180"))
# Enough headroom to look something up and still map it back onto the
# catalogue. "low" was chosen when the model could only pick from a list it had
# already been handed; it can search the web now, and that needs room.
EFFORT = os.environ.get("VOICE_EFFORT", "medium")
# The only tools it gets. It may look things up - a lyric, who featured on
# what, which film a song is from - but it has no business touching this
# machine, so the built-in set is cut to two rather than trusted to stay unused.
TOOLS = os.environ.get("VOICE_TOOLS", "WebSearch,WebFetch")

# ===========================================================================
# THE GESTURE CLAUSES - every knob for the trigger, in one place.
#
# A volume down-then-up must satisfy ALL FIVE to count. Each one is here
# because something real got through without it, and each is tunable, because
# the right numbers depend on your headset and your desktop.
#
#   1. SHAPE      exactly one change down, then one change back up.
#                 Not tunable, and the most important of the five: a headset
#                 button sends ONE change per press, while a volume slider
#                 sends a run of them. Without this, dragging the desktop
#                 slider down and back up fires the trigger, because the last
#                 step of the ramp down and the first step of the ramp up are
#                 a perfect pair. (2026-08-04, at 0.386s.)
#   2. SYMMETRY   the return lands on the EXACT starting value.
#                 The original guard. A real adjustment rarely comes back to
#                 where it began; a gesture always does.
#   3. STEP       the drop equals GESTURE_STEP exactly. 0 accepts any size.
#                 A button always moves the volume by the same amount, so a
#                 different amount is something other than a button. Every
#                 accepted gesture logs its step, so read yours off the log
#                 before pinning it. This box's Q45 steps by 8 of 127.
#   4. MIN GAP    at least GESTURE_MIN between the two presses.
#                 Deliberate gestures measured 0.978s to 1.234s over six
#                 tries. A slider ramp is far faster.
#   5. MAX GAP    at most GESTURE_WINDOW between them.
#                 Beyond this they are two unrelated adjustments. Separate
#                 gestures never came closer together than 3.220s, so the
#                 default sits in the gap between the two populations.
#
# A pair that arms and then fails logs WHICH clause rejected it, so tuning
# these is a matter of reading the log rather than guessing.
# ===========================================================================
GESTURE_MIN = float(os.environ.get("VOICE_GESTURE_MIN", "0.6"))
GESTURE_WINDOW = float(os.environ.get("VOICE_GESTURE_WINDOW", "2.0"))
GESTURE_STEP = int(os.environ.get("VOICE_GESTURE_STEP", "0"))
# How often to re-read the headset's true volume while idle. The detector needs
# a previous value to see a drop, and its own event history drifts: a headset
# reports one volume when it connects and settles on another a moment later, so
# a baseline taken once at connect was wrong by the time anyone pressed
# anything, and the FIRST gesture of every session was swallowed correcting it.
GESTURE_RESYNC = float(os.environ.get("VOICE_GESTURE_RESYNC", "2.0"))

# A bluetooth headset's own microphone only exists in a CALL profile: in A2DP
# the input node is a loopback with nothing behind it. So the daemon switches
# the card into a call profile for the length of the capture and straight back
# afterwards. The music is paused throughout, which is what makes the narrowband
# hit free - nothing is playing through it while it is narrow.
BT_ON = os.environ.get("VOICE_BT", "1") not in ("0", "", "no", "off")
BT_ADDR = os.environ.get("VOICE_BT_ADDR", "")     # pin it if two are connected
BT_HFP = os.environ.get("VOICE_BT_HFP", "headset-head-unit")
BT_A2DP = os.environ.get("VOICE_BT_A2DP", "a2dp-sink")
BT_SETTLE = float(os.environ.get("VOICE_BT_SETTLE", "3.0"))
# A headset that drops out of range mid-capture comes back offering ONLY call
# profiles, and a call profile with no call in progress has no transport at all:
# no volume to read, no volume events, so the gesture becomes undetectable and
# the music plays narrowband. wpctl cannot fix that, because A2DP is not on the
# card to switch to. Reconnecting the device renegotiates it. Set 0 to be told
# about it instead of having it fixed.
BT_AUTOHEAL = os.environ.get("VOICE_BT_AUTOHEAL", "1") not in ("0", "", "no", "off")

STATE = os.path.join(PROJECT, ".state")
# Spoken the moment your sentence is captured, before the music comes back.
# Thinking can take half a minute now, and silence for half a minute is
# indistinguishable from a daemon that has died.
ACK_ON = os.environ.get("VOICE_ACK", "1") not in ("0", "", "no", "off")
# Only ever heard if the written bank below is empty and cannot be refilled.
ACK_TEXT = os.environ.get(
    "VOICE_ACK_TEXT",
    "One moment please. Resuming the current song while I check your music.")
# Point this at your own recording to skip the bank and synthesis entirely.
ACK_WAV = os.environ.get("VOICE_ACK_WAV", "")
# A bank of pre-rendered lines, each a different wording of the same message,
# written by Claude and spoken once before being retired. Pre-rendered because
# the point of the line is to fill silence: writing and voicing one on demand
# would put three seconds of nothing exactly where the nothing already was.
ACK_BANK = os.path.join(STATE, "voice-ack")
ACK_BANK_SIZE = int(os.environ.get("VOICE_ACK_BANK", "8"))
ACK_MODEL = os.environ.get("VOICE_ACK_MODEL", "claude-opus-5")
# The character brief for those lines. Read fresh on every refill, so editing it
# changes the voice with no restart. Swap the file to swap the persona.
ACK_SEED = os.environ.get("VOICE_ACK_SEED",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "ack-seed.txt"))
# Piper is the good voice and it is machine-specific, so it lives in
# bin/env.local.sh like VOICE_ASR_PY does. Without it the chain falls back to
# espeak-ng, then pico2wave, then to saying nothing at all.
PIPER = os.environ.get("VOICE_PIPER", "")
PIPER_MODEL = os.environ.get("VOICE_PIPER_MODEL", "")

SELFTEST = "--selftest" in sys.argv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_wrapped(label, text):
    """Log a paragraph under a label, wrapped with a hanging indent.

    The model's reasoning is a few sentences, and a few sentences on one line is
    a log nobody reads. Continuation lines carry no timestamp so a decision
    stays visibly one entry.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return
    indent = " " * (len("[HH:MM:SS] ") + len(label) + 2)   # line up under the text
    lines = textwrap.wrap(body, width=76) or [body]
    log(f"{label}: {lines[0]}")
    for line in lines[1:]:
        print(f"{indent}{line}", flush=True)


def describe(cmd):
    """One readable line for a parsed command.

    Not repr(): a play_tracks reply can carry fifty numbers, and dumping the
    raw dict scrolls the reasoning that came with it off the screen.
    """
    action = cmd.get("action")
    if action == "play":
        return (f"play {cmd.get('target')!r} ({cmd.get('order', 'default')} order, "
                f"from {cmd.get('start', 'beginning')})")
    if action == "play_tracks":
        tracks = cmd.get("tracks") or []
        head = ", ".join(str(t) for t in tracks[:8])
        more = f", +{len(tracks) - 8} more" if len(tracks) > 8 else ""
        return f"play_tracks: {len(tracks)} tracks [{head}{more}]"
    return str(action)


# --------------------------------------------------------------------------
# mpv control
# --------------------------------------------------------------------------

def _command_reply(buf):
    """First COMPLETE line in buf that is a command reply, else None.

    Only whole lines are parsed. A single recv() is plenty for "success" or a
    pause flag, but it truncates a longer property mid-JSON, and a truncated
    line reads as malformed rather than as "not here yet" - the same
    can't-tell-the-difference trap the rest of this project keeps paying for.
    """
    text = buf.decode(errors="replace")
    if "\n" not in text:
        return None
    for line in text.split("\n")[:-1]:            # drop the partial tail
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if "error" in reply:              # skip async event lines
            return reply
    return None


def mpv(*command):
    """Send one IPC command to mpv. Returns the parsed reply, or None."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": list(command)}) + "\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            reply = _command_reply(buf)
            if reply is not None:
                s.close()
                return reply
        s.close()
    except (OSError, ValueError):
        return None
    return None


def mpv_ok(reply):
    """True only if mpv actually answered and accepted the command.

    mpv() returns None when the socket is dead, which is NOT the same as a
    command that ran and failed - both must be distinguishable from success.
    """
    return bool(reply) and reply.get("error") == "success"


def mpv_prop(name):
    """One mpv property, or None if mpv isn't running or doesn't know it."""
    reply = mpv("get_property", name)
    return reply.get("data") if mpv_ok(reply) else None


def mpv_paused():
    reply = mpv("get_property", "pause")
    return bool(reply and reply.get("data"))


def resume_music():
    """Unpause, and say so if mpv has gone.

    Worth checking here specifically: switching the headset's profile takes the
    sink out from under whatever is playing, and a player that did not survive
    that would otherwise look exactly like a command that decided to stay quiet.
    """
    if not mpv_ok(mpv("set_property", "pause", False)):
        log("mpv is not responding after the capture - bin/play.sh restarts it")


def mpv_set_pause(state):
    mpv("set_property", "pause", bool(state))


# --------------------------------------------------------------------------
# What Claude is allowed to know and do
# --------------------------------------------------------------------------

def library():
    """Folders actually on disk, as '<Parent>/<Sub>' paths, plus album tags.

    Read from disk rather than the registry so a hand-made folder still works,
    and so a renamed folder can't leave a stale name in the prompt.
    """
    out = []
    if not os.path.isdir(MUSIC):
        return out
    for parent in sorted(os.listdir(MUSIC)):
        ppath = os.path.join(MUSIC, parent)
        if not os.path.isdir(ppath):
            continue
        subs = sorted(d for d in os.listdir(ppath)
                      if os.path.isdir(os.path.join(ppath, d)))
        if subs:
            out.extend(f"{parent}/{s}" for s in subs)
        else:
            out.append(parent)
    return out


def entity_types():
    """name -> 'album' | 'playlist', from the registry.

    env.sh's entity_of() convention: an album stores its canonical URL, a
    playlist stores a bare id. That distinction already exists, so the daemon
    can default album playback to track order without anyone configuring it.
    """
    kinds = {}
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            next(fh, None)                              # header
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    kinds[parts[0]] = "album" if "/album/" in parts[1] else "playlist"
    except OSError:
        pass
    return kinds


def play_targets():
    """Every valid "target": the leaf folders AND the megaplaylist parents.

    A parent is a real target - tracks_in() walks the whole tree, so it already
    plays - and it is how anyone names an artist out loud. Leaving it out is
    exactly what made "play some Birdman" unanswerable on 2026-08-03: the model
    could see Birdman/Hood Rich and Birdman/Fast Money, was told never to invent
    a target, and so had nothing legal to say back.
    """
    leaves = library()
    parents = sorted({name.split("/", 1)[0] for name in leaves if "/" in name})
    return parents + leaves


def annotated_targets():
    """Target lines tagged with type and track count, one per line, no indent.

    Flat on purpose: the model copies a target character for character, and an
    indented line invites it to copy the leading spaces too.
    """
    kinds = entity_types()
    counts = {}
    for _number, folder, _title, _path in catalog():
        counts[folder] = counts.get(folder, 0) + 1
        if "/" in folder:
            parent = folder.split("/", 1)[0]
            counts[parent] = counts.get(parent, 0) + 1

    # Empty folders are dropped from what the model sees: an empty megaplaylist
    # awaiting its first source is a real directory and a legal target, but
    # playing it is always the wrong answer. play_targets() keeps them, so
    # validation stays pure disk truth and doesn't lean on the catalogue cache.
    leaves = [name for name in library() if counts.get(name)]
    lines, current_parent = [], None
    for leaf in leaves:                      # library() is sorted, so parents group
        if "/" in leaf:
            parent = leaf.split("/", 1)[0]
            if parent != current_parent:
                current_parent = parent
                sources = sum(1 for x in leaves if x.startswith(parent + "/"))
                lines.append(f"{parent}  [megaplaylist, {sources} sources, "
                             f"{counts.get(parent, 0)} tracks]")
        lines.append(f"{leaf}  [{kinds.get(leaf, 'playlist')}, "
                     f"{counts.get(leaf, 0)} tracks]")
    return lines


def now_playing():
    """What the player is doing right now, as prompt text.

    Without this there was no referent for "this song", "this album" or "the
    rest of it", and the model refused rather than guessed - correctly, since
    nobody had told it. Asked on 2026-08-03 to finish the current album, it
    answered "no current track is known".
    """
    path = mpv_prop("path")
    if not path:
        return "Nothing is loaded in the player. Anything you play starts fresh."

    rows = {p: (n, f, t) for n, f, t, p in catalog()}
    row = rows.get(path)
    where = f"#{row[0]}  {row[1]}  {row[2]}" if row else os.path.basename(path)
    state = "PAUSED" if mpv_prop("pause") else "PLAYING"
    lines = [f"NOW {state}: {where}"]

    pos, count = mpv_prop("playlist-pos"), mpv_prop("playlist-count")
    if isinstance(pos, int) and isinstance(count, int) and count > 0:
        lines.append(f"Queue: track {pos + 1} of {count}.")
        upcoming = []
        for step in (1, 2, 3):
            nxt = mpv_prop(f"playlist/{pos + step}/filename")
            if not nxt:
                break
            nxt_row = rows.get(nxt)
            upcoming.append(f"#{nxt_row[0]} {nxt_row[2]}" if nxt_row
                            else os.path.basename(nxt))
        if upcoming:
            lines.append("Up next: " + "; ".join(upcoming))
    return "\n".join(lines)


_catalog = {"sig": None, "rows": []}


def music_signature():
    """Cheap fingerprint of the music tree: (mp3 count, newest mtime).

    Measured: 2.4ms for 743 files, against 410ms to rebuild the catalogue. That
    ratio is why the cache is invalidated by actual change rather than a clock -
    a time-based TTL is a guess that is either stale or wasteful, and this is
    cheap enough to run on every single command.

    mtime is included on purpose: tag.sh rewrites titles in place without
    changing the file count, so a count-only check would serve stale titles
    after a retag.
    """
    count, newest = 0, 0.0
    for dirpath, _dirs, files in os.walk(MUSIC):
        for name in files:
            if name.endswith(".mp3"):
                count += 1
                try:
                    newest = max(newest, os.stat(os.path.join(dirpath, name)).st_mtime)
                except OSError:
                    continue                  # vanished mid-walk; next call sees it
    return count, newest


def catalog():
    """Every track as (index, folder, title, path), numbered and cached.

    The model is shown these numbers and answers with them. Numbers rather than
    titles because a title round-trip invites near-miss strings that then fail
    to match a real file; an integer either indexes a real track or it doesn't.

    Without this the model only ever saw 20 folder names, which is why it could
    not answer "the song where he says <lyric>" or pick tracks by mood - it had
    no idea what songs existed (2026-08-03).
    """
    sig = music_signature()
    if _catalog["rows"] and _catalog["sig"] == sig:
        return _catalog["rows"]

    from mutagen.id3 import ID3
    rows = []
    for folder in library():
        for path in tracks_in(folder):          # already in album order
            try:
                tag = ID3(path).get("TIT2")
                title = str(tag.text[0]) if tag else os.path.basename(path)[:-4]
            except Exception:
                title = os.path.basename(path)[:-4]
            # Titles carry a " - <artist>" suffix from the loadout tagging
            # scheme; keep it, it helps the model recognise features.
            rows.append((len(rows), folder, title, path))
    _catalog["rows"], _catalog["sig"] = rows, sig
    return rows


def catalog_text():
    return "\n".join(f"{i}\t{folder}\t{title}" for i, folder, title, _ in catalog())


SYSTEM_PROMPT = """You turn a spoken music request into exactly one JSON command.

Reply with ONE line of raw JSON and nothing else. No prose, no markdown fence.

Schema - pick exactly one action. EVERY one of them also carries "why":
  {"action":"play","target":"<F>","order":"<O>","start":"<S>","why":"..."}
  {"action":"play_tracks","tracks":[<n>,...],"why":"..."}
  {"action":"next","why":"..."}            skip forward
  {"action":"prev","why":"..."}            skip back
  {"action":"pause","why":"..."}           stop playback
  {"action":"resume","why":"..."}          resume playback
  {"action":"none","why":"..."}            no music intention in the request

"why" is REQUIRED, always, on whichever action you pick. Two or three sentences
of plain English: what you took the request to mean, what you chose, and - if it
could have meant something else - what you ruled out and why you ruled it out.
Name the alternative explicitly. It is written to a log for a person to read
later, so that a choice you made on their behalf can be checked. It is never
spoken and never shown at the time, so write it for someone reading it cold.

Use "play_tracks" whenever the request is about SONGS rather than a whole
folder - a specific song, a mood, one artist scattered across compilations, a
run of tracks, "and then the rest of the album". Give the track numbers from
the catalogue, IN THE ORDER they should play. They may come from different
folders.

YOU NEVER ASK A QUESTION. Nobody can answer one. This is a single spoken turn
with no reply channel, so a question lands as silence and whatever was already
playing simply carries on. If a request could mean two or three things, PICK
one and play it. A wrong pick costs one gesture to correct; a refusal wastes
the whole turn.

"none" is close to a bug. Use it only when the request contains no music
intention at all. "Not enough information" is never a reason: the entire
library is printed below, so decide. Some worked cases, all of them things
that have actually failed here:
  "play some <artist>"        -> everything of theirs, shuffled. If they have a
                                 folder of their own, that folder with order
                                 "shuffle". If they only turn up inside
                                 compilations, gather their tracks by number
                                 with play_tracks and shuffle those.
  "play <artist>" (no album)  -> same thing. Never ask which album.
  "the rest of this album"    -> read NOW PLAYING, take the tracks after it in
                                 the same folder, in order.
  "something like this"       -> same era and feel as NOW PLAYING, from anywhere.
  "put something on"          -> pick something. Anything reasonable beats none.

TAKE THE TIME YOU NEED, AND LOOK THINGS UP. You have web search. Use it when
the request needs knowledge you do not already have: a half-remembered lyric,
"the one from that film", who featured on which track, what an artist released
last. Then map the answer back onto catalogue numbers. Searching tells you
WHICH track to pick - it never adds one. Only numbers from the catalogue below
may be played.

Examples of the shape (numbers here are illustrative):
  "that song where he says <lyric>, then the rest of the album, no shuffle"
      -> {"action":"play_tracks","tracks":[141,142,143,144],
          "why":"The lyric is from Fireman, track 5 of Tha Carter II, so I
          queued it and the four tracks that follow it in album order. Read
          'no shuffle' as keeping the running order rather than as a request
          to restart the album from track 1."}
  "something gangsta from the 90s"
      -> {"action":"play_tracks","tracks":[12,88,203],
          "why":"Nothing specific was named, so I picked three 90s west coast
          and east coast tracks from the decade compilations. Chose a short
          run over a whole folder because the request sounded like a mood
          rather than a listening session."}

Fields for "play":
  target   EXACT copy of a target entry below, without the [ ] tag.
  order    "album"    track order, start to finish
           "shuffle"  random order
           "default"  let the player choose from the entry's type - albums play
                      in order, playlists and megaplaylists shuffle. USE THIS
                      unless the request asked for an order ("shuffle it",
                      "in order").
  start    "beginning" | "middle" | "random" | a 1-based track number
           Use "beginning" unless the request said otherwise. "halfway through"
           is "middle"; "the third song" is 3; "anywhere" is "random".

Capture the WHOLE request. If it names a position or an order, put it in the
fields - do not drop it.

The input is ALWAYS someone speaking to a music player, transcribed by an
imperfect speech recogniser. It is never about anything else, however it reads.
If the words look like an unrelated topic, that is a mishearing.

Rules:
- Read the transcript PHONETICALLY against the library. It may be nonsense as
  English while clearly matching an entry by sound. "lay the card or two" is
  "play Tha Carter II". "coloring look" is "Coloring Book". Resolve these.
- "target" must match a target entry character for character. Never invent one.
- A megaplaylist is one artist or theme holding several albums or playlists.
  Target the parent for the whole artist, a child for one album.
- Track titles carry a " - <original artist>" suffix, so the catalogue is how
  you find an artist who has no folder of their own. Search it before deciding
  someone is missing.
- If the music named is genuinely nowhere in the library, play the closest
  thing you can defend and say so in "why". Only fall back to "none" if nothing
  in the library is defensible at all.

NOW PLAYING - the current state of the player, for "this song", "this album",
"the rest of it", "something like this":
{NOW}

Targets - the only valid "target" values:
{LIBRARY}

Catalogue - every track, as: number, folder, title. These numbers are the only
valid entries for "tracks". Tracks are listed in album order within a folder.
{CATALOG}
"""


def tracks_in(folder):
    """Every mp3 under a library folder, in real album order.

    Sorted by disc/track from the ID3 tags rather than filename: spotdl names
    files "<artists> - <title>.mp3", so a filename sort scrambles an album.
    Files with no track number sort last, alphabetically, instead of vanishing.
    """
    from mutagen.id3 import ID3            # imported late; only playback needs it

    root = os.path.join(MUSIC, folder)
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.lower().endswith(".mp3"):
                continue
            path = os.path.join(dirpath, name)
            disc = track = 10_000           # unknown -> sorts after everything
            try:
                tags = ID3(path)
                for frame, slot in (("TPOS", "disc"), ("TRCK", "track")):
                    value = tags.get(frame)
                    if value:
                        num = str(value.text[0]).split("/")[0].strip()
                        if num.isdigit():
                            if slot == "disc":
                                disc = int(num)
                            else:
                                track = int(num)
            except Exception:               # unreadable tags shouldn't drop a song
                pass
            found.append((disc, track, name, path))
    found.sort()
    return [f[3] for f in found]


def start_index(count, start):
    """Resolve the spoken start position to a 0-based playlist index."""
    if count <= 0:
        return 0
    if isinstance(start, int) or (isinstance(start, str) and start.isdigit()):
        return max(0, min(count - 1, int(start) - 1))     # spoken numbers are 1-based
    if start == "middle":
        return count // 2
    if start == "random":
        return random.randrange(count)
    return 0


def ask_claude(utterance):
    """Send the transcript to Claude Code in print mode. Returns a dict or None.

    Uses `claude -p`, which authenticates with the logged-in subscription - no
    API key, no per-command billing.
    """
    prompt = (SYSTEM_PROMPT
              .replace("{NOW}", now_playing())
              .replace("{LIBRARY}", "\n".join(annotated_targets()))
              .replace("{CATALOG}", catalog_text()))
    try:
        proc = subprocess.run(
            ["claude", "-p", utterance,
             "--append-system-prompt", prompt,
             "--output-format", "json",
             "--effort", EFFORT,
             "--model", MODEL,
             # Both flags, and they do different jobs: --tools is the whole set
             # it may use, --allowedTools pre-approves them. Without the second
             # one a non-interactive run has nobody to answer the permission
             # prompt, so every search would be denied silently.
             "--tools", TOOLS,
             "--allowedTools", TOOLS],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Do NOT repr the exception: it carries the whole command, and the
        # prompt now contains the 743-track catalogue (~60KB of log per failure).
        log(f"claude timed out after {CLAUDE_TIMEOUT}s")
        return None
    except OSError as exc:
        log(f"could not run claude: {exc.__class__.__name__}: {exc}")
        return None

    if proc.returncode != 0:
        log(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return None

    # Claude Code wraps the answer in an envelope; the model's text is .result
    try:
        envelope = json.loads(proc.stdout)
        raw = envelope.get("result", "") if isinstance(envelope, dict) else ""
    except ValueError:
        raw = proc.stdout

    raw = raw.strip()
    if raw.startswith("```"):                     # tolerate a fenced reply
        raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        log(f"no JSON in reply: {raw[:200]!r}")
        return None
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        log(f"unparseable JSON: {raw[start:end + 1][:200]!r}")
        return None


def start_mpv(index):
    """Cold start on the playlist we just wrote (nothing was running)."""
    subprocess.Popen(
        ["mpv", "--no-video", "--loop-playlist=inf",
         f"--playlist-start={index}",
         f"--input-ipc-server={MPV_SOCK}", "--volume=80",
         f"--playlist={PLAYLIST_FILE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)          # survive this daemon, as play.sh does
    return True


def play_folder(target, order, start):
    """Load a library folder, in the requested order, from the requested point."""
    kind = entity_types().get(target, "playlist")
    if order not in ("album", "shuffle"):
        # "default": an album's running order is the point of it; a playlist is
        # a bag of songs. This is why "play Tha Carter II" used to come out
        # shuffled - play.sh hardcodes --shuffle for every scope.
        order = "album" if kind == "album" else "shuffle"

    paths = tracks_in(target)
    if not paths:
        log(f"nothing to play in {target!r}")
        return False
    if order == "shuffle":
        random.shuffle(paths)

    index = start_index(len(paths), start)
    log(f"{target}: {len(paths)} tracks, {order} order, from #{index + 1}")
    return load_and_play(paths, index)


def load_and_play(paths, index=0):
    """Hand a concrete list of files to mpv and start at index."""
    try:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
    except OSError as exc:
        log(f"could not write playlist: {exc!r}")
        return False

    # Swap the playlist inside the running mpv rather than restarting it: no
    # audible gap, and the MPRIS registration survives so the headset's own
    # buttons keep working. play.sh's kill-and-relaunch is for a cold start.
    if mpv("loadlist", PLAYLIST_FILE, "replace") is None:
        return start_mpv(index)
    if index:
        mpv("set_property", "playlist-pos", index)

    # loadlist replies "success" immediately but loads the file asynchronously,
    # and initialising the new file can clobber an unpause that landed mid-load.
    # Seen live on 2026-08-03: the same code left 13 tracks queued at 0:00 while
    # an identical earlier command happened to win the race. So assert, verify,
    # and re-assert - checking the state beats assuming the write stuck.
    mpv_set_pause(False)
    for _ in range(10):
        time.sleep(0.15)
        if not mpv_paused():
            return True
        mpv_set_pause(False)
    log("warning: mpv stayed paused after load")
    return True


def execute(cmd):
    """Run one parsed command. Returns True if it set playback state itself."""
    action = (cmd or {}).get("action")

    # Transport commands need a live mpv. Check the reply instead of assuming:
    # a dead socket used to return "handled" and do nothing, so "skip this"
    # with no player running reported success and silently no-opped.
    if action in ("next", "prev"):
        if not mpv_ok(mpv("playlist-next" if action == "next" else "playlist-prev")):
            log(f"{action}: mpv is not responding - is anything playing?")
            return False
        mpv_set_pause(False)
        return True
    if action in ("pause", "resume"):
        if not mpv_ok(mpv("set_property", "pause", action == "pause")):
            log(f"{action}: mpv is not responding - is anything playing?")
            return False
        return True
    if action == "play":
        target = cmd.get("target", "")
        # Re-check against disk: the model was told to copy exactly, but a
        # hallucinated path must not reach the filesystem on its word alone.
        if target not in play_targets():
            log(f"refusing unknown folder: {target!r}")
            return False
        return play_folder(target, cmd.get("order", "default"),
                           cmd.get("start", "beginning"))
    if action == "play_tracks":
        rows = catalog()
        paths, bad = [], []
        for n in cmd.get("tracks") or []:
            if isinstance(n, str) and n.strip().isdigit():
                n = int(n)
            # Validate every number against the catalogue. The model picked
            # these, so a stray index must drop the track, not the whole turn.
            if isinstance(n, int) and 0 <= n < len(rows):
                paths.append(rows[n][3])
            else:
                bad.append(n)
        if bad:
            log(f"ignoring out-of-range track numbers: {bad}")
        if not paths:
            log("selection contained no valid tracks")
            return False
        log(f"queued {len(paths)} tracks")
        return load_and_play(paths, 0)
    if action == "none":
        log("no action taken")           # the reasoning is logged by the caller
        return False

    log(f"unrecognised action: {cmd!r}")
    return False


# --------------------------------------------------------------------------
# The headset's microphone
# --------------------------------------------------------------------------

def bt_card():
    """The connected headset's audio card: id, profiles, current one, mic node.

    Everything is resolved fresh on every call. Device ids and profile indexes
    are handed out by the session manager and change across reboots, headset
    reconnects and even profile switches, so caching any of them would work
    right up until the day it silently pointed at nothing.
    """
    try:
        proc = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10)
        objects = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    card = None
    for obj in objects:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        if (props.get("media.class") != "Audio/Device"
                or props.get("device.api") != "bluez5"):
            continue
        if BT_ADDR and BT_ADDR not in (props.get("device.name") or ""):
            continue
        params = info.get("params") or {}
        card = {
            "id": obj.get("id"),
            # bluez_card.AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF, for bluetoothctl
            "address": (props.get("device.name") or "").replace(
                "bluez_card.", "").replace("_", ":"),
            "profiles": {p.get("name"): p.get("index")
                         for p in params.get("EnumProfile", []) if p.get("name")},
            "current": next((p.get("name") for p in params.get("Profile", [])), None),
            "source": None,
        }
        break
    if not card:
        return None

    # Find the mic node by the card's device id, not by rebuilding its name from
    # the MAC address: the node exists under both profiles and only one of them
    # actually carries audio, so the id is the thing that stays true.
    for obj in objects:
        props = ((obj.get("info") or {}).get("props") or {})
        if (props.get("media.class") == "Audio/Source"
                and props.get("device.id") == card["id"]):
            card["source"] = props.get("node.name")
            break
    return card


def set_profile(card, name):
    """Switch the headset card to a named profile, and confirm it took."""
    index = (card.get("profiles") or {}).get(name)
    if index is None or not shutil.which("wpctl"):
        return False
    if not _run(["wpctl", "set-profile", str(card["id"]), str(index)]):
        return False
    # Verify rather than assume. The switch is asynchronous, so a profile that
    # never took looks identical to one that did if you only read the exit code.
    deadline = time.time() + BT_SETTLE
    while time.time() < deadline:
        time.sleep(0.2)
        fresh = bt_card()
        if fresh and fresh.get("current") == name:
            return True
    return False


def take_mic():
    """Put the headset into its call profile for a capture.

    Returns (card_to_restore, source_name). A None card means there is nothing
    to put back: either no headset, or the switch failed and the capture will
    fall back to whatever mic the ASR server was configured with.
    """
    if not BT_ON:
        return None, None
    card = bt_card()
    if not card or not card.get("source"):
        return None, None
    if card.get("current") == BT_HFP:
        return None, card["source"]           # already there, leave it as found
    if not set_profile(card, BT_HFP):
        log(f"could not switch the headset to {BT_HFP}; falling back to the "
            "server's own mic")
        return None, None
    log(f"headset to {BT_HFP} for capture")
    return card, card["source"]


_healed_at = 0.0


def heal_stuck_profile():
    """Rescue a headset stranded in its call profile. Returns True if it acted.

    The symptom is having no volume baseline while a headset is connected. A
    call profile with no call in progress carries NO MediaTransport, so there is
    no volume to read and no volume events arriving - the gesture stops being
    detectable at all, and the music plays narrowband while it lasts.

    Seen 2026-08-04: the headset dropped mid-turn, so nothing switched it back,
    and when it reconnected it offered ONLY call profiles. `wpctl` could not fix
    that, because there was no A2DP entry left on the card to select. Only
    reconnecting the device renegotiated it.
    """
    global _healed_at
    card = bt_card()
    if not card or card.get("current") == BT_A2DP:
        return False                      # no headset, or nothing wrong here

    if BT_A2DP in (card.get("profiles") or {}):
        log(f"headset is stuck in {card['current']}; switching back")
        return set_profile(card, BT_A2DP)

    log(f"headset came back offering no {BT_A2DP} profile, so it has no "
        "microphone transport and no volume events - the gesture cannot fire")
    address = card.get("address") or ""
    if not BT_AUTOHEAL or not shutil.which("bluetoothctl") or ":" not in address:
        log(f"  fix it with: bluetoothctl disconnect {address or '<address>'} "
            f"&& bluetoothctl connect {address or '<address>'}")
        return False
    # Rate limited: if a reconnect does not fix it, doing it on a loop turns one
    # broken headset into a headset that disconnects every twenty seconds.
    if time.time() - _healed_at < 60:
        return False
    _healed_at = time.time()
    log("  reconnecting it to renegotiate the profiles")
    _run(["bluetoothctl", "disconnect", address])
    time.sleep(4)
    _run(["bluetoothctl", "connect", address])
    return True


def give_mic_back(card):
    """Put the headset back to A2DP. Always runs, even when the capture failed."""
    if not card:
        return
    if set_profile(card, BT_A2DP):
        log(f"headset back to {BT_A2DP}")
        time.sleep(0.3)          # let the sink settle before anything plays
    else:
        log(f"WARNING: headset did not return to {BT_A2DP} - audio may be narrowband")


# --------------------------------------------------------------------------
# The spoken acknowledgement
# --------------------------------------------------------------------------

_ack_warned = False
_refilling = threading.Lock()


def _run(cmd, **kwargs):
    """Run a helper quietly. True if it exited 0. Never raises."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30,
                              **kwargs).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def synthesize(text, out):
    """Render text to a wav with whatever TTS this box has. True on success."""
    if PIPER and PIPER_MODEL and os.path.exists(PIPER):
        env = dict(os.environ, LD_LIBRARY_PATH=os.path.dirname(PIPER))
        if (_run([PIPER, "--model", PIPER_MODEL, "--output_file", out],
                 input=text.encode(), env=env)
                and os.path.exists(out) and os.path.getsize(out) > 0):
            return True
    for engine in ("espeak-ng", "pico2wave"):
        binary = shutil.which(engine)
        if binary and _run([binary, "-w", out, text]) and os.path.exists(out):
            return True
    return False


def render(text, out):
    """Synthesize one line to `out`, with 400ms of leading silence.

    The silence is copied from the mock interview's say.sh: a bluetooth headset
    that has been quiet for a few seconds clips the first word while its link
    wakes up, and these clips play after exactly that kind of pause. Baked in
    once at render time rather than paid for on every playback.
    """
    global _ack_warned
    raw = out + ".raw"
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if not synthesize(text, raw):
            if not _ack_warned:
                _ack_warned = True
                log("no TTS available for the spoken acknowledgement "
                    "(set VOICE_PIPER/VOICE_PIPER_MODEL, or install espeak-ng)")
            return False
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg and _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", raw, "-af", "adelay=400:all=1", out]):
            os.remove(raw)
        else:
            os.replace(raw, out)
        return True
    except OSError as exc:
        log(f"could not render an acknowledgement: {exc!r}")
        return False


ACK_BRIEF = """You write the line a voice-controlled music player says out loud
the moment someone finishes speaking a request to it.

The line buys a few seconds. Behind it the player is putting the music back on
and working out what to play. The listener is wearing headphones, is across the
room, and has heard hundreds of these.

{SEED}
Write {N} of them, all different.

- One sentence, six to fourteen words. A speech synthesiser reads it aloud, so
  plain words only: no emoji, no markdown, no numerals, no brackets, no dashes.
- Every line carries the same two things: hang on a second, and the music is
  coming back while I go and look. Carry them IN CHARACTER rather than stating
  them plainly, and never in the same order twice.
- Vary the shape properly. Different openings, different rhythms, some short and
  flat, some with a bit of swing.
- Never promise a particular song, a particular artist, or how long this takes.
- Nothing so pleased with itself that it grates on the hundredth hearing. These
  are heard every single time; write for the hundredth, not the first.

Reply with a raw JSON array of strings and nothing else."""

# Used when the seed file is missing, so the voice degrades to plain rather than
# to nothing. The persona lives in the file; this is only a floor.
ACK_FALLBACK_SEED = """Voice: warm, brief and unhurried. Never apologetic,
never a catchphrase."""


def ack_seed():
    """The persona brief, read fresh so an edit takes effect on the next refill."""
    try:
        with open(ACK_SEED, encoding="utf-8") as fh:
            text = "\n".join(line for line in fh.read().splitlines()
                             if not line.startswith("#")).strip()
        if text:
            return ("Write them in this character. The character is the point,\n"
                    "so read it properly before writing anything:\n\n" + text + "\n")
    except OSError:
        pass
    return ACK_FALLBACK_SEED + "\n"


def ack_lines(count):
    """Ask for `count` fresh wordings. Returns a list, empty if anything fails.

    Opus at low effort with no tools: this is one short piece of writing, and
    the whole job is over in a few seconds. There is no --fast flag on `claude
    -p` (fast mode is a /config session toggle), so this is the closest thing.
    """
    try:
        proc = subprocess.run(
            ["claude", "-p", (ACK_BRIEF.replace("{SEED}", ack_seed())
                              .replace("{N}", str(count))),
             "--output-format", "json", "--model", ACK_MODEL,
             "--effort", "low", "--tools", ""],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        envelope = json.loads(proc.stdout)
        raw = envelope.get("result", "") if isinstance(envelope, dict) else ""
    except ValueError:
        raw = proc.stdout
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        lines = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    return [" ".join(line.split()) for line in lines
            if isinstance(line, str) and line.strip()]


def bank_clips():
    """Every rendered line currently in the bank."""
    try:
        return [os.path.join(ACK_BANK, name) for name in sorted(os.listdir(ACK_BANK))
                if name.endswith(".wav")]
    except OSError:
        return []


def refill_bank():
    """Top the bank back up. Slow - always call it off the turn's critical path."""
    if not ACK_ON or ACK_WAV or not _refilling.acquire(blocking=False):
        return
    try:
        # A changed persona retires the whole bank. Without this, editing the
        # seed only affects the NEXT line written, while the eight already
        # rendered keep playing in the old voice for eight more commands - so
        # the edit looks like it did nothing at all.
        stamp, seed = os.path.join(ACK_BANK, "seed.sha"), ack_seed()
        digest = hashlib.sha1(seed.encode()).hexdigest()
        try:
            with open(stamp, encoding="utf-8") as fh:
                changed = fh.read().strip() != digest
        except OSError:
            changed = True                    # no stamp yet, or unreadable
        if changed:
            retired = bank_clips()
            for path in retired:
                for target in (path, path[:-4] + ".txt"):
                    try:
                        os.remove(target)
                    except OSError:
                        pass
            if retired:
                log(f"acknowledgement persona changed; retired {len(retired)} line(s)")
            os.makedirs(ACK_BANK, exist_ok=True)
            try:
                with open(stamp, "w", encoding="utf-8") as fh:
                    fh.write(digest)
            except OSError:
                pass

        missing = ACK_BANK_SIZE - len(bank_clips())
        if missing <= 0:
            return
        made = 0
        for text in ack_lines(missing):
            # Named by content hash, so a wording that comes back a second time
            # costs nothing and cannot collide with a different one.
            wav = os.path.join(ACK_BANK,
                               hashlib.sha1(text.encode()).hexdigest()[:10] + ".wav")
            if os.path.exists(wav) or not render(text, wav):
                continue
            try:
                with open(wav[:-4] + ".txt", "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
            made += 1
        if made:
            log(f"acknowledgements: {made} new, bank now {len(bank_clips())}")
    finally:
        _refilling.release()


def fallback_clip():
    """The one fixed line, rendered once. Only used when the bank is empty."""
    if ACK_WAV:
        return ACK_WAV if os.path.exists(ACK_WAV) else None
    wav = os.path.join(STATE, "voice-ack-fallback.wav")
    stamp = os.path.join(STATE, "voice-ack-fallback.txt")
    try:
        with open(stamp, encoding="utf-8") as fh:
            if fh.read() == ACK_TEXT and os.path.exists(wav):
                return wav
    except OSError:
        pass
    if not render(ACK_TEXT, wav):
        return None
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(ACK_TEXT)
    except OSError:
        pass
    return wav


def play_ack():
    """Speak one line, retire it, and start the bank refilling behind it.

    Retiring is what keeps the wording moving: a line is heard once and then the
    bank refills with new ones. Keeping them would freeze the same eight
    sentences in place forever, which is the thing this replaced.
    """
    if not ACK_ON:
        return
    clips = bank_clips()
    clip = random.choice(clips) if clips else fallback_clip()
    if not clip:
        return

    for player in (["paplay", clip],
                   ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", clip],
                   ["aplay", "-q", clip]):
        if shutil.which(player[0]) and _run(player):
            break
    else:
        log("could not play the acknowledgement clip")

    if clips:                                   # a banked line, not the fallback
        for path in (clip, clip[:-4] + ".txt"):
            try:
                os.remove(path)
            except OSError:
                pass
    # Off this thread: refilling asks Claude for new wordings and renders them,
    # which takes several seconds and must not delay the music coming back.
    threading.Thread(target=refill_bank, daemon=True).start()


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------

def listen(source=None):
    """Ask the warm ASR server for one utterance. Returns text (possibly empty).

    `source` names the mic for this turn. Passing it per request is what lets
    the daemon hand over the headset's own node the moment it has switched the
    headset into a call profile, and say nothing at all when it hasn't.
    """
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(40)
        s.connect(ASR_SOCK)
        s.sendall(f"LISTEN {source}\n".encode() if source else b"LISTEN\n")
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return buf.decode(errors="replace").strip()
    except OSError as exc:
        log(f"ASR unavailable ({exc!r}) - is bin/voice-asr.py running?")
        return ""


busy = threading.Lock()


def handle_gesture():
    """Pause, listen, dispatch, restore. Runs off the D-Bus thread."""
    if not busy.acquire(blocking=False):
        log("already handling a command; ignoring trigger")
        return
    try:
        was_paused = mpv_paused()
        mpv_set_pause(True)                    # duck out of the way of the mic

        # Music off, THEN take the headset's mic, then hand it straight back.
        # In that order the narrowband call profile costs nothing: it is only
        # narrow while nothing is playing through it. give_mic_back runs from a
        # finally, because a headset left in a call profile is a headset that
        # sounds broken for the rest of the evening.
        card, source = take_mic()
        try:
            log("listening...")
            text = listen(source)
        finally:
            give_mic_back(card)

        if not text:
            log("heard nothing")
            if not was_paused:
                resume_music()
            return
        log(f"heard: {text!r}")

        # Think and talk at the same time. The dispatch call goes out FIRST, on
        # its own thread, and the spoken line plays over the top of it while the
        # music is still out of the way. The line is cover for the thinking, not
        # a preamble to it: running the two in sequence spent four seconds
        # saying "one moment" before the moment had started.
        thinking = {}
        thinker = threading.Thread(
            target=lambda: thinking.update(cmd=ask_claude(text)), daemon=True)
        thinker.start()

        play_ack()
        if not was_paused:
            resume_music()                # music back as the line finishes

        # Generous: ask_claude enforces its own timeout, so this only has to
        # outlast it rather than second-guess it.
        thinker.join(CLAUDE_TIMEOUT + 30)
        cmd = thinking.get("cmd")
        if not cmd:
            # ask_claude already logged the specific failure. Say the turn ended
            # so the transcript above isn't left looking like it was acted on.
            log("no decision - playback left as it was")
            return
        log(f"decision: {describe(cmd)}")
        # Every turn ends with the model's own account of the choice. It is told
        # to pick rather than ask when a request is ambiguous, so the reasoning
        # behind a pick made on your behalf has to be readable afterwards, not
        # inferred from what came out of the speakers.
        log_wrapped("why", cmd.get("why") or cmd.get("note") or cmd.get("reason")
                    or "(the model gave no reasoning)")
        execute(cmd)
    finally:
        busy.release()


# --------------------------------------------------------------------------
# Gesture detection
# --------------------------------------------------------------------------

def headset_volume():
    """The headset's current AVRCP volume from BlueZ, or None if none is connected.

    Read at startup to seed the baseline. Without it the FIRST gesture after a
    restart was always swallowed: feed() needs a previous value to see a drop,
    the first volume event is what supplies it, and so the opening DOWN press
    was spent establishing the baseline instead of arming the gesture. The
    following UP then found nothing armed. Every later gesture worked, which is
    what made it look intermittent rather than structural.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync("org.bluez", "/",
                              "org.freedesktop.DBus.ObjectManager",
                              "GetManagedObjects", None,
                              GLib.VariantType("(a{oa{sa{sv}}})"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        for _path, ifaces in reply.unpack()[0].items():
            props = ifaces.get("org.bluez.MediaTransport1") or {}
            if "Volume" in props:
                return int(props["Volume"])
    except (GLib.Error, ValueError, TypeError):
        return None
    return None


class Gesture:
    """Volume down, then back up. See THE GESTURE CLAUSES above for the rules."""

    def __init__(self, on_trigger):
        self.on_trigger = on_trigger
        self.prev = None          # last volume seen
        self.down_from = None     # value we dropped from, when armed
        self.down_to = None       # value we dropped to, for the step size
        self.down_at = 0.0
        self.dragging = False     # a run of changes in one direction

    def prime(self, vol):
        """Seed the baseline without counting it as a press."""
        self.prev, self.down_from, self.dragging = vol, None, False

    def armed(self):
        """Mid-gesture: a drop is waiting for its return. Do not resync now."""
        return self.down_from is not None

    def feed(self, vol, now):
        prev, self.prev = self.prev, vol
        if prev is None or vol == prev:
            return

        if vol < prev:                                   # a change DOWNWARD
            # Already armed means this is the second drop in a row, so whatever
            # is moving the volume is not a button. Clause 1.
            self.dragging = self.dragging or self.down_from is not None
            self.down_from, self.down_to, self.down_at = prev, vol, now
            return

        start, dragged = self.down_from, self.dragging
        self.down_from, self.dragging = None, False      # one shot per drop
        if start is None:
            return                                       # an UP with no DOWN

        gap, step = now - self.down_at, start - self.down_to
        clauses = (
            ("shape (one down, one up)", not dragged),
            ("symmetry (back to the start)", vol == start),
            (f"step size (want {GESTURE_STEP})", not GESTURE_STEP or step == GESTURE_STEP),
            (f"minimum gap ({GESTURE_MIN}s)", gap >= GESTURE_MIN),
            (f"maximum gap ({GESTURE_WINDOW}s)", gap <= GESTURE_WINDOW),
        )
        failed = [name for name, passed in clauses if not passed]
        if failed:
            # Logged, because a rejected pair is exactly what you need to see
            # when tuning the numbers above, and it is rare enough not to spam.
            log(f"ignored a volume change ({gap:.3f}s, step {step}): "
                f"failed {failed[0]}")
            return
        log(f"gesture detected ({gap:.3f}s, step {step})")
        self.on_trigger()


def main():
    trigger = (lambda: log("SELFTEST: gesture detected - path works")) if SELFTEST \
        else (lambda: threading.Thread(target=handle_gesture, daemon=True).start())
    gesture = Gesture(trigger)

    def on_signal(_conn, _sender, path, _iface, _sig, params, *_):
        iface, changed, _invalidated = params.unpack()
        if iface != "org.bluez.MediaTransport1" or "Volume" not in changed:
            return
        gesture.feed(int(changed["Volume"]), time.monotonic())

    def on_added(_conn, _sender, _path, _iface, _sig, params, *_):
        """A headset that just connected brings its own volume with it."""
        _obj, ifaces = params.unpack()
        props = ifaces.get("org.bluez.MediaTransport1") or {}
        if "Volume" in props:
            gesture.prime(int(props["Volume"]))
            log(f"headset connected, volume baseline {props['Volume']}")

    def on_removed(_conn, _sender, _path, _iface, _sig, params, *_):
        """A headset that left takes its baseline with it."""
        _obj, ifaces = params.unpack()
        if "org.bluez.MediaTransport1" in ifaces:
            gesture.prime(None)
            log("headset gone, baseline cleared")

    def resync():
        """Keep the baseline honest between gestures.

        The detector needs a previous value to recognise a drop, and its own
        event history drifts: a headset announces one volume on connect and
        settles on another seconds later. A baseline taken once was therefore
        stale by the time anyone pressed anything, and the first press of the
        session was spent correcting it instead of triggering - which is
        exactly what "the first gesture never works" was, twice over.

        Skipped while a drop is waiting for its return, and while a command is
        already running, so this can never rewrite state mid-gesture.
        """
        if not gesture.armed() and not busy.locked():
            current = headset_volume()
            if current is not None and current != gesture.prev:
                if gesture.prev is not None:
                    log(f"volume baseline corrected {gesture.prev} -> {current}")
                gesture.prime(current)
        return True                                   # keep the timer alive

    GLib.timeout_add_seconds(max(1, int(GESTURE_RESYNC)), resync)

    def watchdog():
        """Notice when the trigger has gone deaf, and fix what can be fixed.

        Having NO baseline is the tell, and a precise one: whenever a transport
        exists there is a volume to read, so a connected headset with no
        baseline means no transport, which means no volume events either. That
        is silent by nature - the daemon looks perfectly healthy while nothing
        can reach it - so something has to go looking.

        Cheap because it only looks when the baseline is missing, which is also
        the only time anything could be wrong.
        """
        if gesture.prev is None and not busy.locked():
            heal_stuck_profile()
        # Cheap no-op unless the bank is short or the persona file changed, so
        # an edit to the seed takes effect within one tick rather than waiting
        # for the next command to notice it.
        threading.Thread(target=refill_bank, daemon=True).start()
        return True

    GLib.timeout_add_seconds(20, watchdog)

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    # Subscribe with no object path filter: the transport path (…/fd0) is
    # recreated on every headset reconnect, so pinning it would silently stop
    # working the next time the headset drops.
    bus.signal_subscribe(
        "org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
        None, None, Gio.DBusSignalFlags.NONE, on_signal, None)
    # Re-seed on reconnect for the same reason it is seeded at startup: a fresh
    # transport arrives with a volume nobody has seen yet. The resync timer
    # above is the backstop for both, since neither signal is guaranteed to
    # carry a Volume the instant it fires.
    bus.signal_subscribe(
        "org.bluez", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
        None, None, Gio.DBusSignalFlags.NONE, on_added, None)
    bus.signal_subscribe(
        "org.bluez", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
        None, None, Gio.DBusSignalFlags.NONE, on_removed, None)

    # Fill the bank before the first gesture rather than on it.
    threading.Thread(target=refill_bank, daemon=True).start()

    # If a crash or a kill landed mid-capture, the headset is still sitting in
    # its call profile and everything sounds terrible. Put it back at startup:
    # the daemon is the only thing that moves it, so it is also the only thing
    # that can be sure it should come back.
    if BT_ON:
        heal_stuck_profile()

    baseline = headset_volume()
    if baseline is None:
        log("no headset volume yet - the baseline is set when one connects")
    else:
        gesture.prime(baseline)
        # Said out loud, because a silent success and a silent failure look the
        # same in a log, and this one decides whether the first gesture works.
        log(f"volume baseline {baseline}, first gesture is armed")

    if SELFTEST:
        log(f"SELFTEST: baseline {baseline}; listening for one volume down-up "
            "gesture (Ctrl-C to stop)")
    else:
        log(f"ready - volume down-up to talk (window {GESTURE_WINDOW}s, model {MODEL})")
        log(f"library: {len(library())} folders, {len(play_targets())} targets, "
            f"tools {TOOLS or 'none'}")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
