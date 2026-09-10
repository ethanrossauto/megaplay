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

# The audit log sits beside this file. The directory is put on the path
# explicitly because this module is also loaded through importlib by the test
# suite, which does not add it the way running the file as a script does.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit                                                    # noqa: E402

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
# THE GESTURE CLAUSES - every knob for both triggers, in one place.
#
# TWO GESTURES, SAME FIVE CLAUSES, MIRRORED SHAPES (second one 2026-08-14):
#
#   DOWN then UP    talk to it. Pauses, listens, asks, plays.
#   UP then DOWN    something is WRONG with the song you are hearing. Flags
#                   it with your position in the track, and skips it.
#
# The second exists because censorship cannot be detected from metadata and,
# as it turns out, not from the audio either. Spotify's explicit flag
# describes the LISTING while the audio comes from a different source
# entirely, so a track flagged explicit can still play clean. Transcribing
# the audio and looking for the words fails in the other direction: a model
# will not invent profanity, so finding it proves a track is uncensored, but
# finding none proves nothing, because the model may simply have declined to
# write it down. The larger model found FEWER hits than the small one on the
# same files, which is the tell.
#
# So the only reliable detector is a person hearing it, and this gives that
# person one flick to record what they heard, at the moment they hear it.
# The flagged track is appended to the file named by FLAG_FILE below and
# skipped immediately. Nothing is deleted: a flag is a label, and what to do
# about it is a separate decision made later, with the list in hand.
#
# A gesture must satisfy ALL FIVE clauses to count. Each one is here because
# something real got through without it, and each is tunable, because the
# right numbers depend on your headset and your desktop.
#
#   1. SHAPE      exactly one change one way, then one change back.
#                 Not tunable, and the most important of the five: a headset
#                 button sends ONE change per press, while a volume slider
#                 sends a run of them. Without this, dragging the desktop
#                 slider down and back up fires the trigger, because the last
#                 step of the ramp down and the first step of the ramp up are
#                 a perfect pair. (2026-08-04, at 0.386s.)
#                 It is the clause that keeps the two gestures apart, and it
#                 does so for free: a ramp in either direction is refused
#                 before the direction is ever consulted.
#   2. SYMMETRY   the return lands on the EXACT starting value, OR the way back
#                 up is one press of a known size (clause 3), OR the headset has
#                 not been measured yet and the return leg is button-sized and
#                 misses by less than its own length.
#                 The original guard. A real adjustment rarely comes back to
#                 where it began; a gesture always does. The two escapes both
#                 exist because the starting value is not always trustworthy:
#                 see "the drifted baseline" below. A button moves the volume
#                 along the HEADSET'S own grid, so a real gesture cannot land
#                 anywhere but where it started - if the return misses, the miss
#                 is an error in the value this machine remembered rather than
#                 something the person did.
#   3. STEP       the press is one of the sizes THIS headset produces, and the
#                 daemon works those out for itself. See MEASURING THE STEP.
#                 A button moves the volume by a fixed amount, so a different
#                 amount is something other than a button - the desktop slider,
#                 another application, a drift correction.
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
#
# MEASURING THE STEP - clause 3 configures itself, per headset (2026-09-09).
#
# 🔴 THE NUMBER IN CLAUSE 3 IS A PROPERTY OF THE HEADSET, AND IT USED TO BE A
# CONSTANT FOR THE WHOLE PROCESS. So it had to be pinned by hand, to one
# headset, and plugging in a different one broke the gesture with no clue
# beyond a line in a log nobody was reading. Measured: one headset presses in
# steps of 7 or 8, another in steps of 4, and the second one silently did
# nothing for weeks while every other clause passed.
#
# 🔑 ONE PRESS IS ENOUGH, BUT ONLY TO LOCATE THE SIZE, NOT TO PIN IT. A headset
# has N steps over a range of 0 to 127, and 127 divides evenly by nothing, so
# its presses come out as two adjacent sizes - mostly one, occasionally the
# other. A measurement therefore has to accept a neighbourhood: the size seen
# and one either side. See steps_for(), which says what the first version of
# this got wrong and why a sharper answer was worse than a blunt one.
#
# WHERE THE MEASUREMENT IS TAKEN, and it is the whole safety argument: only
# from a pair that has already satisfied the OTHER FOUR clauses. A single
# volume change on its own could be anything. One that is part of a symmetric,
# correctly-timed, one-out-one-back pair is a gesture, and the only thing that
# makes that shape is a person pressing a button twice.
#
# 🔒 THE FIRST GESTURE ON A HEADSET NOBODY HAS MEASURED WORKS, AND THAT TOOK A
# SECOND FIX. Clause 3 passing while unmeasured is not enough on its own,
# because clause 2 was then the strict one and a brand new headset is exactly
# the case whose baseline is wrong: PipeWire writes an off-grid volume ON
# CONNECT. So the first press on a new headset was spent correcting the baseline
# and the second one was the one that worked - buy a headset, press the button,
# nothing happens. Clause 2 now forgives a miss while a headset is unmeasured,
# under the bounds in feed(), and the measurement is taken from the return leg
# so the drift is not written into it. Learning only ever ADDS a guard; it can
# never be what stops a gesture firing.
#
# 🔁 AND IT CORRECTS ITSELF. A measurement is the one thing here that can be
# wrong about the DEVICE rather than about the gesture - taken from the wrong
# headset it refuses every real press forever, which is the failure this whole
# section exists to end. So GESTURE_RELEARN pairs in a row that fail on the
# step size AND NOTHING ELSE are taken as proof the number is wrong, and the
# size is measured again from the pair in hand.
#
# Sizes are kept in GESTURE_STEP_FILE, by bluetooth address, so swapping
# between headsets costs nothing after the first gesture on each.
#
# THE DRIFTED BASELINE - "the first gesture never works", part 4 (2026-08-09).
# The headset's volume and the number this machine holds for it are two
# different things, and they come apart: PipeWire writes an absolute volume of
# its own on connect and on the profile round trip a command makes, and it does
# not land on the headset's own 16-step grid. Nothing is wrong at that moment,
# and nothing can see it either - re-reading BlueZ returns the same written
# number, so resync() agrees with itself and "volume baseline corrected" had
# never once appeared in the log.
#
# The next press is where it surfaces. It moves the headset by a real step, so
# the drop measured against the drifted value comes out odd (5, 6, 9, 10, 11
# were all logged), and the return lands on the headset's own value rather than
# on the one being remembered, so clause 2 fails by exactly the drift. Then the
# rejected press has just told BlueZ the truth, so the retry two seconds later
# is clean - which is what made this look like flaky hardware for three days.
#
# So the way back up is measured instead, from two numbers the headset itself
# reported. The drift is logged when it fires, because it is a real fault
# somewhere else and should not be silently absorbed here.
# ===========================================================================
GESTURE_MIN = float(os.environ.get("VOICE_GESTURE_MIN", "0.6"))
GESTURE_WINDOW = float(os.environ.get("VOICE_GESTURE_WINDOW", "2.0"))
# The AVRCP volume range every bluetooth headset is addressed through. A
# headset's own step grid is this range divided by however many steps it has,
# which is the fact the whole of clause 3 now rests on.
BT_VOLUME_MAX = 127
# How many step-size refusals in a row mean the SIZE is wrong rather than the
# gesture. Three, because a headset producing both sizes of its own grid is
# already covered by steps_for(), so three in a row is a different device or a
# bad measurement rather than the ordinary odd step.
GESTURE_RELEARN = int(os.environ.get("VOICE_GESTURE_RELEARN", "3"))
# 🔑 THE SMALLEST MOVE A HEADSET BUTTON MAKES, and the largest. One definition,
# used twice: it decides what may be measured as a step, and it decides whether
# a return leg is button-sized enough to be believed over a stale baseline.
# Below 3 a change is indistinguishable from a desktop slider notch; above 32
# there would be fewer than four steps on the whole range, which no headset has.
MIN_BUTTON_STEP, MAX_BUTTON_STEP = 3, 32


def parse_steps(spec):
    """Clause 3's knob: a comma-separated list of accepted drop sizes.

    An empty result means accept any size, which is what 0 asks for and also
    what an unreadable value falls back to. The fallback is safe rather than
    silent: the effective setting is printed on the ready line every start, so
    a typo shows up as "step any" instead of as a gesture that never fires.
    """
    try:
        got = frozenset(int(s) for s in spec.split(",") if s.strip())
    except ValueError:
        return frozenset()
    return frozenset() if 0 in got else got


def step_desc(steps):
    """Clause 3's setting, in the words the log and the ready line use."""
    return " or ".join(str(s) for s in sorted(steps)) or "any"


def steps_for(press):
    """Every step size a headset that just moved by `press` can produce.

    A HEADSET DOES NOT HAVE ONE STEP SIZE. It has N volume steps spread over a
    range of 0 to 127, and 127 divides evenly by nothing, so its presses come
    out as two adjacent sizes: mostly one, occasionally the other. Pinned by
    hand to whichever size somebody happened to observe, the gesture dies at the
    volumes that sit on the other one and nowhere else, which reads as flaky
    hardware rather than as a setting. That cost days on one headset in
    2026-08, where the press is 8 almost everywhere and 7 around volume 71.

    🔴 SO THE ANSWER IS A NEIGHBOURHOOD, NOT A GRID, AND THE FIRST VERSION OF
    THIS GOT THAT WRONG. It recovered N as round(127/press) and returned that
    grid exactly, which is a precise answer computed from an imprecise
    measurement: at the small end round(127/press) swings wildly, so a 4-step
    press claims a 32-step headset and a 5-step press claims a 25-step one.
    Measured on a real WH-1000XM5 inside one minute, presses of 4 AND 5 from the
    same headset seconds apart, so it is neither - it is about 30, and a single
    sample cannot tell you that. Locking in either grid excludes the other real
    size, and then clause 3 refuses half the presses and the relearn below flips
    the measurement back and forth forever.

    ⚠️ AN EMPTY RESULT MEANS "DO NOT LEARN FROM THIS", NOT "ANY SIZE". Below 3
    the press is indistinguishable from a desktop slider notch, and learning it
    would hand clause 3 a set that accepts exactly what the clause exists to
    refuse. Above 32 there would be fewer than four steps on the whole range,
    which no headset has. A device outside that band stays unlearned and keeps
    the strict clauses, which is a working gesture with one guard fewer.
    """
    if not MIN_BUTTON_STEP <= press <= MAX_BUTTON_STEP:
        return frozenset()
    # One either side, because a single measurement is uncertain by about that
    # much: the headset's own two sizes are adjacent, and the remembered
    # baseline can sit a unit off the headset's grid (see "the drifted
    # baseline"). Both perturb one sample by one, and neither is worth trying
    # to distinguish from the other.
    return frozenset(n for n in (press - 1, press, press + 1)
                     if n >= MIN_BUTTON_STEP)


# 🔑 "auto" IS THE DEFAULT, AND IT IS WHAT MAKES THIS WORK ON A HEADSET NOBODY
# HAS MEASURED. The size is learned per device from the first gesture that
# satisfies the other four clauses, then kept. The two explicit settings remain:
# a list pins it and never learns, and 0 turns clause 3 off entirely.
_STEP_SPEC = os.environ.get("VOICE_GESTURE_STEP", "auto").strip()
GESTURE_LEARN = _STEP_SPEC.lower() in ("", "auto")
GESTURE_STEP = frozenset() if GESTURE_LEARN else parse_steps(_STEP_SPEC)
GESTURE_STEP_DESC = "learned per device" if GESTURE_LEARN else step_desc(GESTURE_STEP)
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
# Where the up-then-down gesture records what it heard. Deliberately NOT under
# .state: everything there is runtime scratch that no backup covers, and these
# are hand-made labels that cost a person listening to a song to produce. They
# sit beside the registry so they travel with the project.
FLAG_FILE = os.environ.get("VOICE_FLAG_FILE", os.path.join(PROJECT, "flagged.tsv"))
# What each headset's volume step was measured to be, by bluetooth address.
# Runtime scratch on purpose: it is a measurement of the hardware plugged into
# THIS machine, it costs one gesture to rebuild, and a headset that has never
# been seen here has nothing to restore anyway.
GESTURE_STEP_FILE = os.path.join(STATE, "gesture-steps.json")
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


# A log that cannot be written has to be visible in the place everything else
# about this daemon is visible, or the failure is indistinguishable from a quiet
# evening with nothing to record.
audit.report = lambda msg: log(f"audit: {msg}")


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


def reasoning(cmd):
    """The model's own account of the choice, whichever field it used.

    One reading of it rather than the same fallback chain written twice: it is
    printed for a person and stored as the audit row's detail, and those two must
    not drift into disagreeing about what the model said.
    """
    c = cmd or {}
    return (c.get("why") or c.get("note") or c.get("reason")
            or "(the model gave no reasoning)")


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

    ⚠️ VERIFIED AND RETRIED, NOT FIRED ONCE. The profile round trip destroys and
    rebuilds the sink, which corks whatever is playing - and the switch BACK is a
    SECOND destroy, so an unpause can be undone a moment after it lands and the
    music simply stays off with nothing reporting a failure. Checking beats
    guessing how long the rebuild takes, which is a number nobody here has
    measured. The dictation daemon sharing this headset learned the same thing
    independently.
    """
    for attempt in range(3):
        if not mpv_ok(mpv("set_property", "pause", False)):
            log("mpv is not responding after the capture - bin/play.sh restarts it")
            return
        time.sleep(0.4)
        if mpv_prop("pause") is not True:
            return
        log(f"the sink rebuild corked the music again; unpausing "
            f"(attempt {attempt + 2})")


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


def ask_claude(utterance, command_id=None):
    """Send the transcript to Claude Code in print mode. Returns a dict or None.

    Uses `claude -p`, which authenticates with the logged-in subscription - no
    API key, no per-command billing.

    THE DECISION ROW IS WRITTEN HERE, which is the one place that knows all of
    it: the model, the sentence, how long it took, and either the command it
    produced with the reasoning behind it or the specific way it failed. It lands
    BEFORE the caller executes anything, so a crash between deciding and playing
    still leaves the intent on record.
    """
    started = time.monotonic()

    def failed(detail):
        """Record a turn the model did not complete, and return no command."""
        audit.log_event(
            tool="dispatch", source="voice", tier="llm", result="error",
            command_id=command_id, detail=detail,
            params={"model": MODEL, "effort": EFFORT, "utterance": utterance},
            latency_ms=int((time.monotonic() - started) * 1000))
        return None

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
        return failed(f"the model did not answer within {CLAUDE_TIMEOUT}s")
    except OSError as exc:
        log(f"could not run claude: {exc.__class__.__name__}: {exc}")
        return failed(f"could not run claude: {exc.__class__.__name__}: {exc}")

    if proc.returncode != 0:
        log(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return failed(f"claude exited {proc.returncode}: "
                      f"{proc.stderr.strip()[:200]}")

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
        return failed(f"the reply carried no command: {raw[:200]!r}")
    try:
        cmd = json.loads(raw[start:end + 1])
    except ValueError:
        log(f"unparseable JSON: {raw[start:end + 1][:200]!r}")
        return failed(f"the reply would not parse: {raw[start:end + 1][:200]!r}")

    # The reasoning is the part of a turn that cannot be reconstructed later. The
    # command can be inferred from what the music did; why this one was chosen
    # over the others exists only here.
    audit.log_event(
        tool="dispatch", source="voice", tier="llm", result="ok",
        command_id=command_id, detail=reasoning(cmd),
        params={"model": MODEL, "effort": EFFORT, "utterance": utterance,
                "command": cmd},
        latency_ms=int((time.monotonic() - started) * 1000))
    return cmd


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
    # TURN MPV'S OWN SHUFFLE OFF FIRST. play.sh starts the player with
    # --shuffle, which is right for its own job, and mpv keeps applying that
    # option to EVERY playlist loaded into that process afterwards - including
    # this one. So the ordering worked out here was being thrown away by the
    # player it was handed to: on 2026-08-09 "play Hood Rich album" wrote all
    # sixteen tracks in album order, logged "from #1", and played Greg Street
    # Countdown, because entry 1 of mpv's reshuffled copy WAS Greg Street
    # Countdown. Nothing is lost by switching it off, because play_folder
    # shuffles the paths itself when shuffle is what was asked for; mpv's copy
    # of the feature was never wanted on this path.
    mpv("set_property", "options/shuffle", False)

    if mpv("loadlist", PLAYLIST_FILE, "replace") is None:
        return start_mpv(index)

    # Set the position every time, index 0 included. "replace" does land on the
    # first entry today, tested against a scratch player, but that is an
    # assumption about mpv rather than something this code asks for, and the
    # difference is one IPC call.
    mpv("set_property", "playlist-pos", index)

    # loadlist replies "success" immediately but loads the file asynchronously,
    # and initialising the new file can clobber a write that landed mid-load.
    # Seen live on 2026-08-03: the same code left 13 tracks queued at 0:00 while
    # an identical earlier command happened to win the race. So assert, verify,
    # and re-assert - checking the state beats assuming the write stuck. Both
    # writes are in one loop because both lose the same race.
    mpv_set_pause(False)
    for _ in range(10):
        time.sleep(0.15)
        at, paused = mpv_prop("playlist-pos"), mpv_paused()
        if at == index and not paused:
            return True
        if at != index:
            mpv("set_property", "playlist-pos", index)
        if paused:
            mpv_set_pause(False)

    # Two warnings, not one: "it stayed paused" and "it started on the wrong
    # song" are different faults with different causes, and the second one has
    # been silent until now. The property name is the one to query by hand.
    at = mpv_prop("playlist-pos")
    if at != index:
        log(f"warning: mpv is on playlist-pos {at} after asking for {index}")
    if mpv_paused():
        log("warning: mpv stayed paused after load")
    return True


def execute(cmd, command_id=None):
    """Run one parsed command. Returns True if it set playback state itself.

    THIS IS THE ONLY PLACE A MODEL DECISION BECOMES PLAYBACK, and it writes an
    audit row at every one of its exits, the refusals included. That is what
    makes "every action is on the record" a property of where the code sits
    rather than a promise, and the way to break it is to add a second route from
    a decision to the player.

    REFUSED AND BROKEN ARE LOGGED AS DIFFERENT THINGS. A folder that is not on
    disk or a track number outside the catalogue is the system working correctly
    and declining, so it is `rejected`. A player that will not answer is
    `error`. Collapsing them would make it impossible to ask afterwards how often
    the model asks for something impossible, which is the question that says
    whether the prompt is working.
    """
    action = (cmd or {}).get("action")

    def emit(tool, result, detail, params=None):
        audit.log_event(tool=tool, source="voice", tier="llm", result=result,
                        command_id=command_id, params=params, detail=detail)

    # Transport commands need a live mpv. Check the reply instead of assuming:
    # a dead socket used to return "handled" and do nothing, so "skip this"
    # with no player running reported success and silently no-opped.
    if action in ("next", "prev"):
        if not mpv_ok(mpv("playlist-next" if action == "next" else "playlist-prev")):
            log(f"{action}: mpv is not responding - is anything playing?")
            emit(action, "error", "the player did not answer, so nothing moved")
            return False
        mpv_set_pause(False)
        emit(action, "ok", f"stepped to the {action} track")
        return True
    if action in ("pause", "resume"):
        if not mpv_ok(mpv("set_property", "pause", action == "pause")):
            log(f"{action}: mpv is not responding - is anything playing?")
            emit(action, "error", "the player did not answer, so nothing changed")
            return False
        emit(action, "ok", f"playback {action}d")
        return True
    if action == "play":
        target = cmd.get("target", "")
        order, start = cmd.get("order", "default"), cmd.get("start", "beginning")
        params = {"target": target, "order": order, "start": start}
        # Re-check against disk: the model was told to copy exactly, but a
        # hallucinated path must not reach the filesystem on its word alone.
        if target not in play_targets():
            log(f"refusing unknown folder: {target!r}")
            emit("play", "rejected",
                 f"there is no folder called {target!r} in the library", params)
            return False
        if play_folder(target, order, start):
            emit("play", "ok", f"started {target}", params)
            return True
        # play_folder returns False for two reasons and they are different facts
        # about what went wrong. Asking the folder settles it, and this only runs
        # on a path that has already failed.
        empty = not tracks_in(target)
        emit("play", "rejected" if empty else "error",
             f"{target} holds no playable files" if empty
             else "the playlist could not be written, so nothing reached the player",
             params)
        return False
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
        # The dropped numbers travel on the row whether or not the turn survived
        # them: a command that played 53 of the 55 tracks it asked for looks like
        # a success everywhere except here.
        params = {"asked": len(cmd.get("tracks") or []), "played": len(paths),
                  "out_of_range": bad}
        if not paths:
            log("selection contained no valid tracks")
            emit("play_tracks", "rejected",
                 "none of the track numbers were in the catalogue", params)
            return False
        log(f"queued {len(paths)} tracks")
        if load_and_play(paths, 0):
            emit("play_tracks", "ok", f"queued {len(paths)} tracks", params)
            return True
        emit("play_tracks", "error",
             "the playlist could not be written, so nothing reached the player",
             params)
        return False
    if action == "none":
        log("no action taken")           # the reasoning is logged by the caller
        # Logged as a refusal because nothing happened, and carrying the model's
        # own reason: this daemon is built to pick rather than decline, so a turn
        # that ends in nothing is close to a bug and the row is where that shows.
        emit("none", "rejected", f"the model took no action: {reasoning(cmd)}")
        return False

    log(f"unrecognised action: {cmd!r}")
    emit("unknown", "error", f"the model answered with an action this daemon "
                             f"does not have: {cmd!r}"[:400])
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
    # ⛔ NOT OURS TO HEAL WHEN IT IS NOT OURS TO SWITCH. With VOICE_BT off the
    # session manager owns the profile and moves it to a call profile on purpose
    # whenever any capture opens the default source - this daemon's own, and the
    # dictation daemon sharing the same headset. Forcing it back on a timer
    # would cut somebody off mid-sentence, and the twenty-second watchdog is
    # exactly the wrong thing to arm against a profile somebody else is using.
    if not BT_ON:
        return False
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


def handle_gesture(addr=None):
    """Pause, listen, dispatch, restore. Runs off the D-Bus thread.

    `addr` is the headset that gestured, so the volume it was at can be put back
    afterwards and the detector told the daemon was the one that moved it.
    """
    if not busy.acquire(blocking=False):
        log("already handling a command; ignoring trigger")
        return
    try:
        # One id for one flick of the thumb. Everything this turn produces hangs
        # off it, so the log reads as one interaction rather than three rows that
        # happen to be next to each other.
        command_id = audit.new_command_id()
        was_paused = mpv_paused()
        mpv_set_pause(True)                    # duck out of the way of the mic

        # Music off, THEN take the headset's mic, then hand it straight back.
        # In that order the narrowband call profile costs nothing: it is only
        # narrow while nothing is playing through it. give_mic_back runs from a
        # finally, because a headset left in a call profile is a headset that
        # sounds broken for the rest of the evening.
        started = time.monotonic()
        # Read BEFORE anything touches the profile: this is the number the
        # listener chose, and the whole point is to hand it back.
        found = headsets().get(addr) if addr else None
        was_at = found[0] if found else None
        card, source = take_mic()
        try:
            log("listening...")
            text = listen(source)
        finally:
            give_mic_back(card)
        heard_ms = int((time.monotonic() - started) * 1000)
        # ⚠️ ON ITS OWN THREAD, because it has to wait for the transport to come
        # back and the rest of this turn must not. The music resumes a few lines
        # below and the model is already thinking; blocking here would put a
        # couple of seconds of silence exactly where the silence used to be.
        if was_at is not None:
            threading.Thread(target=restore_volume, args=(addr, was_at),
                             daemon=True).start()

        if not text:
            log("heard nothing")
            # A turn that ended at the microphone is still a turn. Without this
            # row a gesture that caught nothing is invisible, and "the gesture
            # did not fire" and "it fired and heard silence" are the two things
            # anybody debugging this needs to tell apart.
            audit.log_event(
                tool="listen", source="voice", result="rejected",
                command_id=command_id, latency_ms=heard_ms,
                detail="the capture produced no words, so the turn ended here")
            if not was_paused:
                resume_music()
            return
        log(f"heard: {text!r}")
        # tier is null: the capture is a local model on this machine and costs no
        # dispatch. That is what makes the tier column answer how often the
        # expensive half actually ran.
        audit.log_event(tool="listen", source="voice", result="ok",
                        command_id=command_id, latency_ms=heard_ms, detail=text)

        # Think and talk at the same time. The dispatch call goes out FIRST, on
        # its own thread, and the spoken line plays over the top of it while the
        # music is still out of the way. The line is cover for the thinking, not
        # a preamble to it: running the two in sequence spent four seconds
        # saying "one moment" before the moment had started.
        thinking = {}
        thinker = threading.Thread(
            target=lambda: thinking.update(cmd=ask_claude(text, command_id)),
            daemon=True)
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
        # inferred from what came out of the speakers. ask_claude has already put
        # this same sentence on the record; this is the copy you hear about now.
        log_wrapped("why", reasoning(cmd))
        execute(cmd, command_id)
    finally:
        busy.release()


# --------------------------------------------------------------------------
# Gesture detection
# --------------------------------------------------------------------------

# The live router, so a turn can tell the detector when the DAEMON moved the
# volume rather than a person. Set once, by main().
ROUTER = None


def device_of(path):
    """The bluetooth address out of a BlueZ object path, or None.

    A transport path is `/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/sep3/fd0`. The
    `sep` and `fd` parts are torn down and rebuilt on every reconnect, and the
    `dev` part is the headset itself, which is why the address is what state
    gets filed under and why the subscription cannot just pin a path.

    ⚠️ SCANNED FOR RATHER THAN INDEXED. The depth is not fixed: the same headset
    reports `.../dev_X/fd0` on one stack and `.../dev_X/sep3/fd0` on this one,
    so counting slashes works until it silently does not.
    """
    for part in str(path).split("/"):
        if part.startswith("dev_"):
            return part[4:].replace("_", ":")
    return None


def headsets():
    """Every connected transport as `{address: (volume, name)}`.

    Read at startup to seed the baselines. Without it the FIRST gesture after a
    restart was always swallowed: feed() needs a previous value to see a drop,
    the first volume event is what supplies it, and so the opening DOWN press
    was spent establishing the baseline instead of arming the gesture. The
    following UP then found nothing armed. Every later gesture worked, which is
    what made it look intermittent rather than structural.

    🔑 A DICT RATHER THAN ONE NUMBER. This used to return the volume of
    whichever transport BlueZ happened to list first, which is fine with one
    headset connected and wrong with two: the number could belong to the one
    sitting on the desk rather than the one being worn, and there was no way to
    tell from the answer. The name rides along because it is what the log calls
    the device, and an address is not something anybody recognises.
    """
    volumes, names = {}, {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync("org.bluez", "/",
                              "org.freedesktop.DBus.ObjectManager",
                              "GetManagedObjects", None,
                              GLib.VariantType("(a{oa{sa{sv}}})"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        for path, ifaces in reply.unpack()[0].items():
            addr = device_of(path)
            if addr is None:
                continue
            props = ifaces.get("org.bluez.MediaTransport1") or {}
            if "Volume" in props:
                volumes[addr] = int(props["Volume"])
            alias = (ifaces.get("org.bluez.Device1") or {}).get("Alias")
            if alias:
                names[addr] = str(alias)
    except (GLib.Error, ValueError, TypeError):
        return {}
    return {a: (v, names.get(a, a)) for a, v in volumes.items()}


# How long to let the rebuilt transport settle before believing what it reports.
# Picked, not measured, and checked rather than trusted: restore_volume() reads
# again after writing, so a wrong guess costs another pass instead of a wrong
# volume.
VOLUME_SETTLE = float(os.environ.get("VOICE_VOLUME_SETTLE", "0.8"))


def set_headset_volume(addr, vol):
    """Write one headset's AVRCP volume. True if BlueZ took it."""
    want = max(0, min(BT_VOLUME_MAX, int(vol)))
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = bus.call_sync("org.bluez", "/",
                              "org.freedesktop.DBus.ObjectManager",
                              "GetManagedObjects", None,
                              GLib.VariantType("(a{oa{sa{sv}}})"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        for path, ifaces in reply.unpack()[0].items():
            if (device_of(path) != addr
                    or "org.bluez.MediaTransport1" not in ifaces):
                continue
            bus.call_sync(
                "org.bluez", path, "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", ("org.bluez.MediaTransport1", "Volume",
                                       GLib.Variant("q", want))),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            return True
    except (GLib.Error, ValueError, TypeError) as exc:
        log(f"could not set the headset volume: {exc}")
    return False


def restore_volume(addr, want):
    """Put the volume back where it was before a capture. Runs off the turn.

    🔴 A VOICE COMMAND MUST NOT CHANGE HOW LOUD THE MUSIC IS. Capturing takes
    the headset through a profile round trip, and the volume that comes back is
    not always the one that went in: measured here on 2026-09-09, one headset
    returns exactly ONE STEP LOW every time, so a run of commands walks the
    music quieter with nothing on screen to explain it. Three commands, three
    drops: 64 to 59, 42 to 38, 38 to 34.

    🔑 IT DOES NOT CARE WHO SWITCHED THE PROFILE, and that is deliberate. The
    daemon used to do it itself; WirePlumber does it now, when a capture opens
    the default source. Written against the OUTCOME rather than the mechanism,
    this keeps working either way, and it is a no-op on a headset that already
    comes back where it started.

    ⚠️ CHECKED, NOT TIMED. The value can be written again while the sink
    finishes rebuilding, so this reads back after writing rather than trusting
    one guess at how long that takes.
    """
    for _ in range(3):
        time.sleep(VOLUME_SETTLE)
        found = headsets().get(addr)
        if found is None:
            continue                       # mid-switch: no transport to read
        current = found[0]
        if current == want:
            break
        if not set_headset_volume(addr, want):
            break
        log(f"volume put back {current} -> {want} after the capture")

    # 🔑 AND THE DETECTOR IS TOLD. Everything above was the DAEMON moving the
    # volume, not a person pressing a button. Left in the detector's history the
    # round trip arms a phantom press, and the next real gesture is spent
    # discarding it - which is exactly the "discarded a stale arm" line that
    # appeared after every command.
    found = headsets().get(addr)
    if ROUTER is not None and found is not None:
        ROUTER.detector(addr).prime(found[0])


def load_learned_steps():
    """What each headset's step size was last measured to be. Never raises.

    A missing or unreadable file reads as "nothing learned yet", which costs one
    gesture per device to rebuild and is the correct answer either way: a grid
    that cannot be trusted must not be used, because a wrong one refuses every
    real press.
    """
    try:
        with open(GESTURE_STEP_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    out = {}
    for addr, sizes in saved.items():
        # Shape-checked rather than trusted. This file is rewritten by a daemon
        # that can be killed mid-write, and a half-parsed grid is worse than
        # none: it would silently reject every press from that headset.
        if isinstance(sizes, list) and sizes and all(
                isinstance(n, int) and 0 < n <= BT_VOLUME_MAX for n in sizes):
            out[str(addr)] = frozenset(sizes)
    return out


def save_learned_steps(known):
    """Write the measured grids back, atomically, and never take the daemon down.

    Replaced rather than written in place: this is read at startup, and a
    truncated file there would cost every headset its measurement at once.
    """
    try:
        os.makedirs(STATE, exist_ok=True)
        tmp = f"{GESTURE_STEP_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({a: sorted(v) for a, v in known.items()},
                      fh, indent=1, sort_keys=True)
        os.replace(tmp, GESTURE_STEP_FILE)
    except OSError as exc:
        # Loud, not fatal. The gesture still works this session; it just has to
        # measure again next time, and silence here would make that look like
        # the learning never happened.
        log(f"could not save the learned step sizes: {exc}")


def handle_flag(addr=None):
    """Record that the playing track has a PROBLEM, then skip it.

    Takes `addr` for symmetry with handle_gesture and uses it for nothing: this
    path opens no microphone, so there is no profile round trip and no volume to
    put back.

    Runs off the D-Bus thread. Deliberately does NOT delete, and deliberately
    does not ask. Deleting on a gesture would make a slip of the thumb destroy
    a file, and the whole point of the flick is that it costs nothing to make
    at the moment of hearing. What the list is worth is decided later.

    ⚠️ ANY problem, not only censorship (2026-09-05). The faults that actually
    turn up are a mixed bag and a listener cannot be asked to classify them
    mid-song: a censored cut, a track that stops dead partway through and jumps
    to the next one, a pitch-shifted upload that sounds like chipmunks, a live
    take where the album version was wanted. One gesture, one bit of
    information: SOMETHING IS WRONG WITH THIS ONE.

    🔑 THE POSITION IS RECORDED, and it is what makes the bit useful. A flag at
    1:45 of a file whose own header says 1:47 is a track that stopped early; a
    flag ten seconds in is a wrong version noticed immediately. The two need
    completely different repairs, and the timestamp separates them without
    asking the listener anything.

    Nothing here needs the mic, the model or the network, so it does not take
    the busy lock: flagging a song while a spoken command is still being
    answered is legitimate, and the two touch nothing in common.
    """
    command_id = audit.new_command_id()
    path = mpv_prop("path")
    if not path:
        log("flag: nothing is playing, so there is nothing to flag")
        audit.log_event(tool="flag", source="gesture", result="rejected",
                        command_id=command_id,
                        detail="the gesture fired with nothing playing")
        return
    rel = os.path.relpath(path, MUSIC) if path.startswith(MUSIC) else path
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    # Where in the track, and how long the file claims to be. Both are read
    # from the player rather than the file, so a missing value is written as
    # "?" instead of a zero that would read as "flagged at the very start".
    pos = mpv_prop("time-pos")
    dur = mpv_prop("duration")
    pos_s = f"{pos:.0f}" if isinstance(pos, (int, float)) else "?"
    dur_s = f"{dur:.0f}" if isinstance(dur, (int, float)) else "?"
    marks = {"position_s": pos_s, "duration_s": dur_s}
    try:
        # Append, never rewrite. The file is a log of what a person heard, and
        # a crash mid-write should cost the current line rather than the lot.
        with open(FLAG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{pos_s}\t{dur_s}\t{rel}\n")
    except OSError as exc:
        # Say so and still skip. A flag that cannot be written is a real
        # failure, but leaving a bad song playing on top of it helps nobody,
        # and silence here would read as success.
        log(f"flag: COULD NOT WRITE {FLAG_FILE}: {exc}")
        audit.log_event(tool="flag", source="gesture", result="error",
                        command_id=command_id, track_path=rel, params=marks,
                        detail=f"the label could not be written: {exc}")
    else:
        log(f"flagged as faulty at {pos_s}s of {dur_s}s: {rel}")
        # tier is null and the source is the gesture, not the voice: this whole
        # path costs no model call, which is why it can be made at the moment of
        # hearing without waiting for anything.
        audit.log_event(tool="flag", source="gesture", result="ok",
                        command_id=command_id, track_path=rel, params=marks,
                        detail=f"flagged as faulty at {pos_s}s of {dur_s}s")

    # The skip is a second effect on playback and gets its own row, because the
    # question this log has to answer about any given moment is why the music did
    # what it did, and "a flag was written" does not explain a track change.
    skipped = mpv_ok(mpv("playlist-next"))
    audit.log_event(tool="next", source="gesture",
                    result="ok" if skipped else "error",
                    command_id=command_id, track_path=rel,
                    detail="skipped the flagged track" if skipped else
                           "the player did not answer, so the flagged track may "
                           "still be playing")


class Gesture:
    """One change out, one change back. See THE GESTURE CLAUSES above.

    Direction decides which handler fires: down-then-up talks, up-then-down
    flags the current track as faulty. Every clause is shared, so the two
    shapes cannot be told apart by anything except the order of the presses,
    and a slider ramp is refused in both directions by clause 1.
    """

    def __init__(self, on_trigger, on_flag=None, steps=None, learn=None,
                 on_learn=None, label=""):
        self.on_trigger = on_trigger          # down then up
        self.on_flag = on_flag                # up then down
        self.prev = None          # last volume seen
        self.arm_from = None      # value the first press moved FROM, when armed
        self.arm_to = None        # value it moved TO, for the step size
        self.arm_at = 0.0
        self.arm_dir = 0          # -1 armed by a press down, +1 by a press up
        self.dragging = False     # a run of changes in one direction
        # 🔑 CLAUSE 3 LIVES ON THE DETECTOR, NOT ON THE PROCESS, because it
        # describes the HEADSET and two of them can be connected at once. As a
        # module-wide constant it was a number that had to be true of every
        # device anybody owned, which is why it had to be pinned by hand and why
        # pinning it for one headset broke the other.
        self.steps = GESTURE_STEP if steps is None else frozenset(steps)
        self.learn = GESTURE_LEARN if learn is None else learn
        self.on_learn = on_learn  # told when the measurement changes
        self.label = label        # which headset, for the log
        self.blind = 0            # step-size refusals in a row

    def say(self, msg):
        """Log, naming the headset when there is more than one that could talk."""
        log(f"{self.label}: {msg}" if self.label else msg)

    def adopt(self, press):
        """Take a press size as this headset's grid, and remember it.

        Refuses sizes no button produces rather than believing them: a grid
        learned from a slider notch would accept exactly what clause 3 exists to
        refuse, and one learned from a drift correction would refuse every real
        press. Staying unlearned costs one guard and keeps the gesture working,
        which is the better of the two failures.
        """
        learned = steps_for(press)
        if not learned:
            self.say(f"a {press}-step change is outside the range a headset "
                     "button uses, so the step size stays unmeasured")
            self.blind = 0
            return False
        was, self.steps, self.blind = self.steps, learned, 0
        if learned != was:
            self.say(f"measured the volume step from a {press}-step press: "
                     f"accepting {step_desc(learned)}")
            if self.on_learn:
                self.on_learn(learned)
        return True

    def prime(self, vol):
        """Seed the baseline without counting it as a press."""
        self.prev, self.arm_from, self.dragging = vol, None, False

    def armed(self, now=None):
        """Mid-gesture: a drop is waiting for its return. Do not resync now.

        An arm OLDER than the window is not mid-gesture, it is debris, and
        saying otherwise is what made "the first gesture never works" survive
        two fixes. A drop with no matching up - a headset settling to a lower
        volume after it connects, a single press nobody completed - used to arm
        the detector forever, because only an UP cleared it. That stuck arm then
        did two things: it marked the next real press as a drag, so the gesture
        failed clause 1, and it switched off resync() permanently, since resync
        skips while armed. The one mechanism built to repair a stale baseline
        was disabled by exactly the state it existed to repair.
        """
        if self.arm_from is None:
            return False
        return (time.monotonic() if now is None else now) - self.arm_at <= GESTURE_WINDOW

    def feed(self, vol, now):
        prev, self.prev = self.prev, vol
        if prev is None or vol == prev:
            return
        direction = -1 if vol < prev else 1

        # An arm past the window can never become a gesture - clause 5 would
        # reject it - so it is not a drag partner either. Discard it instead
        # of letting it call this press part of a ramp. resync() heals this
        # too, but only every couple of seconds, and a press can land inside
        # that gap.
        # ONLY when this press continues in the same direction. A press the
        # OTHER way is the return leg, however late it is, and it has to reach
        # the clauses so the log can say "failed maximum gap" rather than
        # "discarded a stale arm". Both refuse the gesture; only one of them
        # tells you which clause did it, and that is the whole point of the
        # rejection log.
        if (self.arm_from is not None and direction == self.arm_dir
                and now - self.arm_at > GESTURE_WINDOW):
            self.say(f"discarded a stale arm ({now - self.arm_at:.1f}s old, "
                     "no matching volume change back)")
            self.arm_from, self.dragging = None, False

        if self.arm_from is None or direction == self.arm_dir:
            # Nothing armed yet, so this press arms. Or: the same direction
            # twice, which means a run of changes one way, so whatever is
            # moving the volume is not a button and the pair this eventually
            # forms will fail clause 1.
            self.dragging = self.dragging or self.arm_from is not None
            self.arm_from, self.arm_to = prev, vol
            self.arm_at, self.arm_dir = now, direction
            return

        start, dragged, out = self.arm_from, self.dragging, self.arm_dir
        self.arm_from, self.dragging = None, False       # one shot per arm

        gap = now - self.arm_at
        # TWO measurements of the same press, and they disagree when the
        # baseline has drifted. The DROP is measured against a remembered value
        # that something else may have written; the RISE is measured between two
        # numbers the headset itself reported, seconds apart. When the step size
        # is known, the rise is therefore the one to believe. See "the drifted
        # baseline" in THE GESTURE CLAUSES above.
        first, back = abs(self.arm_to - start), abs(vol - self.arm_to)
        press = bool(self.steps) and back in self.steps

        # 🔴 THE CHICKEN AND EGG THIS CLOSES. Clause 2 forgives a drifted
        # baseline only when clause 3 can recognise a press, clause 3 can only
        # do that once the headset has been measured, and the measurement only
        # ever comes from a gesture that got past clause 2. A headset nobody has
        # measured is exactly the one whose baseline is most likely to be wrong,
        # because PipeWire writes an off-grid volume ON CONNECT - so the first
        # gesture on a brand new headset was the one case that could not work,
        # and it took a throwaway press to correct the baseline before the real
        # one landed.
        #
        # 🔑 WHY IT IS SAFE TO FORGIVE. A headset button moves the volume down
        # and back up along the headset's OWN grid, so a real gesture cannot
        # land anywhere except where it started. If the return misses, the miss
        # is by definition an error in the value THIS MACHINE remembered, never
        # something the person did. All that is left to establish is whether the
        # two events were button presses at all, and the return leg answers
        # that: it is button-sized, and it is smaller than nothing a slider
        # produces.
        #
        # ⚠️ BOUNDED THREE WAYS so it cannot become a general relaxation. It
        # applies only while the headset is unmeasured, so at most one gesture
        # per device ever; only when the daemon is going to measure, so an
        # explicit VOICE_GESTURE_STEP=0 does not get it forever; and only when
        # the miss is SMALLER than the press, because a drift is a fraction of a
        # step and anything larger is a different action.
        settling = (self.learn and not self.steps
                    and back >= MIN_BUTTON_STEP
                    and abs(vol - start) < back)

        # ⚠️ AND THE RETURN LEG IS WHAT GETS MEASURED. The drop was measured
        # against the value that is wrong; the rise is measured between two
        # numbers the headset itself reported, seconds apart. Believing the drop
        # here would write the drift into the measurement permanently.
        step = back if (press or settling) else first
        step_name = f"step size (want {step_desc(self.steps)})"
        clauses = (
            ("shape (one out, one back)", not dragged),
            ("symmetry (back to the start)", vol == start or press or settling),
            (step_name, not self.steps or step in self.steps),
            (f"minimum gap ({GESTURE_MIN}s)", gap >= GESTURE_MIN),
            (f"maximum gap ({GESTURE_WINDOW}s)", gap <= GESTURE_WINDOW),
        )
        # The absolute volumes, because a step size on its own cannot show a
        # drifted baseline: every swallowed first press read as an odd step and
        # the log gave nobody a way to see why. "61 -> 56 -> 64" says it at a
        # glance, and the arithmetic is then somebody's to check.
        seen = f"{start} -> {self.arm_to} -> {vol}"
        failed = [name for name, passed in clauses if not passed]

        # 🔴 THE STEP SIZE IS THE ONLY CLAUSE THAT CAN BE WRONG ABOUT THE HEADSET
        # RATHER THAN ABOUT THE GESTURE, so it is the only one evidence is
        # allowed to overrule. The other four describe the shape of something a
        # person did; this one repeats a measurement, and a measurement taken
        # from a different headset refuses every real press until somebody
        # notices and edits a file. Three refusals that fail on NOTHING else
        # means the number is wrong rather than the presses, which is exactly
        # the state a swapped headset lands in.
        if failed == [step_name] and self.learn:
            self.blind += 1
            if self.blind >= GESTURE_RELEARN and steps_for(step):
                self.say(f"the step size has been wrong {self.blind} times "
                         f"running (wanted {step_desc(self.steps)}, saw {step}), "
                         "so it was measured from the wrong headset")
                self.adopt(step)
                failed = []

        if failed:
            # Logged, because a rejected pair is exactly what you need to see
            # when tuning the numbers above, and it is rare enough not to spam.
            self.say(f"ignored a volume change ({gap:.3f}s, step {step}, {seen}): "
                     f"failed {failed[0]}")
            # AND THEN IT ARMS AGAIN, with this very press as the outbound leg.
            # A press that fails as somebody's return leg is still a perfectly
            # good FIRST press, and dropping it is what broke the mirrored
            # gesture within an hour of shipping it (2026-08-14).
            #
            # The volume rests wherever the last gesture left it, so a headset
            # sitting at 40 sends UP(40->48) then DOWN(48->40). That opening UP
            # arrives while an older DOWN is still armed, gets eaten as its
            # return leg, fails the window by minutes, and is discarded - so
            # the gesture that follows can never begin. Every attempt logged
            # "48 -> 40 -> 48 failed maximum gap" while the headset was plainly
            # reporting 40 -> 48 -> 40 nine tenths of a second apart.
            #
            # This was invisible in the down-up-only design, where discarding a
            # failed UP cost nothing because only a DOWN could arm.
            self.arm_from, self.arm_to = prev, vol
            self.arm_at, self.arm_dir = now, direction
            self.dragging = False
            return

        # 🔑 THE MEASUREMENT IS TAKEN HERE, from a pair that has just satisfied
        # every other clause, and nowhere else. That is the only moment this
        # daemon can be certain a BUTTON is what moved the volume: a single
        # change on its own could be the desktop slider, a drift correction, or
        # another application. Clause 2 was strict to reach this line, so the
        # drop and the rise agree and there is no question which to believe.
        if self.learn and not self.steps:
            self.adopt(step)
        self.blind = 0

        if vol != start:
            # Fired on the rise despite not landing on the remembered value.
            # Said out loud rather than absorbed silently: the drift is a real
            # fault somewhere else, and a growing one should be visible here
            # before it starts costing gestures again.
            self.say(f"baseline had drifted {vol - start:+d} (held {start}, "
                     f"headset says {vol})")
        if out < 0:
            self.say(f"gesture detected ({gap:.3f}s, step {step}, {seen})")
            self.on_trigger()
            return
        # The mirrored shape. Named differently in the log on purpose: the two
        # gestures do very different things, and a log that calls both of them
        # the same thing cannot answer "why did it skip that song".
        self.say(f"flag gesture detected ({gap:.3f}s, step {step}, {seen})")
        if self.on_flag is None:
            self.say("flag gesture has no handler wired; ignoring")
            return
        self.on_flag()


class GestureRouter:
    """One detector per headset, chosen by the address in the transport path.

    🔑 EVERY FIELD IN A Gesture IS ABOUT ONE HEADSET - the last volume seen, the
    press waiting for its return, the measured step - and there used to be a
    single one of them for the whole daemon. With two headsets connected their
    volume events interleaved into that one state machine: a press on the one
    being worn arrived as the return leg of something the other had done, so
    neither could complete a gesture and the log blamed clause 1. Filing the
    state under the address it arrived from is what makes "any device" cover any
    NUMBER of them as well as any model.

    The MEASUREMENT outlives the state on purpose. A headset that disconnects
    loses its baseline, because the volume it comes back at is not the one it
    left at; it does not lose its step size, because that is a property of the
    hardware and does not change while it is in the drawer.
    """

    def __init__(self, on_trigger, on_flag):
        self.on_trigger, self.on_flag = on_trigger, on_flag
        self.known = load_learned_steps()
        self.names = {}
        self.by_addr = {}

    def detector(self, addr):
        """The detector for one headset, created the first time it is seen."""
        found = self.by_addr.get(addr)
        if found is not None:
            return found
        # ⚠️ A PIN BEATS A MEASUREMENT. Anyone who set VOICE_GESTURE_STEP by hand
        # has overruled this, and quietly loading a stored grid over the top
        # would make their setting look broken in a way nothing would explain.
        steps = self.known.get(addr) if GESTURE_LEARN else GESTURE_STEP
        # ⚠️ THE ADDRESS IS CLOSED OVER RATHER THAN PASSED THROUGH Gesture. The
        # detector's job is to recognise a shape, not to know which headset it
        # is on, so its callbacks stay zero-argument and the router is what
        # remembers who fired.
        found = Gesture(lambda a=addr: self.on_trigger(a),
                        lambda a=addr: self.on_flag(a), steps=steps,
                        on_learn=lambda sizes, a=addr: self.remember(a, sizes),
                        label=self.names.get(addr, addr))
        self.by_addr[addr] = found
        found.say("volume step " + (
            f"{step_desc(steps)}, " + ("measured on an earlier run"
                                       if GESTURE_LEARN else "pinned by hand")
            if steps else "not measured yet; the first gesture will set it"))
        return found

    def remember(self, addr, steps):
        self.known[addr] = steps
        save_learned_steps(self.known)

    def rename(self, addr, name):
        """Use the headset's own name in the log once BlueZ has supplied it.

        An address is not something anybody recognises, and this daemon now has
        to say WHICH headset a line is about.
        """
        if name and self.names.get(addr) != name:
            self.names[addr] = name
            if addr in self.by_addr:
                self.by_addr[addr].label = name

    def feed(self, path, vol, now):
        # A path with no device in it is not a headset, so there is nothing to
        # file this under and nothing that could have gestured.
        addr = device_of(path)
        if addr is not None:
            self.detector(addr).feed(vol, now)

    def forget(self, addr):
        self.by_addr.pop(addr, None)

    def deaf(self):
        """True when nothing connected has a baseline. See watchdog()."""
        return not any(g.prev is not None for g in self.by_addr.values())


def main():
    global ROUTER
    trigger = (lambda _a: log("SELFTEST: gesture detected - path works")) if SELFTEST \
        else (lambda a: threading.Thread(target=handle_gesture, args=(a,),
                                         daemon=True).start())
    flag = (lambda _a: log("SELFTEST: flag gesture detected - path works")) if SELFTEST \
        else (lambda a: threading.Thread(target=handle_flag, args=(a,),
                                         daemon=True).start())
    router = ROUTER = GestureRouter(trigger, flag)

    def on_signal(_conn, _sender, path, _iface, _sig, params, *_):
        iface, changed, _invalidated = params.unpack()
        if iface != "org.bluez.MediaTransport1" or "Volume" not in changed:
            return
        router.feed(path, int(changed["Volume"]), time.monotonic())

    def on_added(_conn, _sender, _path, _iface, _sig, params, *_):
        """A headset that just connected brings its own volume with it."""
        obj, ifaces = params.unpack()
        addr = device_of(obj)
        if addr is None:
            return
        router.rename(addr, (ifaces.get("org.bluez.Device1") or {}).get("Alias"))
        props = ifaces.get("org.bluez.MediaTransport1") or {}
        if "Volume" in props:
            detector = router.detector(addr)
            detector.prime(int(props["Volume"]))
            detector.say(f"connected, volume baseline {props['Volume']}")

    def on_removed(_conn, _sender, _path, _iface, _sig, params, *_):
        """A headset that left takes its baseline with it, but not its step."""
        obj, ifaces = params.unpack()
        addr = device_of(obj)
        if addr is None or "org.bluez.MediaTransport1" not in ifaces:
            return
        if addr in router.by_addr:
            router.by_addr[addr].say("gone, baseline cleared")
        router.forget(addr)

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
        if busy.locked():
            return True
        for addr, (current, name) in headsets().items():
            router.rename(addr, name)
            detector = router.detector(addr)
            # Skipped per headset rather than for the daemon as a whole: one
            # headset mid-gesture is no reason to let another one's baseline rot.
            if detector.armed() or current == detector.prev:
                continue
            if detector.prev is not None:
                detector.say(f"volume baseline corrected {detector.prev} -> {current}")
            detector.prime(current)
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
        if router.deaf() and not busy.locked():
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

    found = headsets()
    if not found:
        log("no headset volume yet - the baseline is set when one connects")
    for addr, (vol, name) in found.items():
        router.rename(addr, name)
        router.detector(addr).prime(vol)
        # Said out loud, because a silent success and a silent failure look the
        # same in a log, and this one decides whether the first gesture works.
        log(f"volume baseline {vol} for {name}, first gesture is armed")
    baseline = {name: vol for (vol, name) in found.values()}

    if SELFTEST:
        log(f"SELFTEST: baseline {baseline}; listening for one volume down-up "
            "gesture (Ctrl-C to stop)")
    else:
        log(f"ready - volume down-up to talk, up-down to flag a faulty track "
            f"(window {GESTURE_WINDOW}s, step {GESTURE_STEP_DESC}, model {MODEL})")
        log(f"flags go to {FLAG_FILE}")
        folders, targets = len(library()), len(play_targets())
        log(f"library: {folders} folders, {targets} targets, "
            f"tools {TOOLS or 'none'}")
        # A row with no command id of its own, and it is the one that explains a
        # gap: an audit log with nothing in it for two days means either a quiet
        # two days or a daemon that was not running, and only this tells them
        # apart. Deliberately not written under --selftest, which handles no real
        # commands and would otherwise put rows in the record for a dry run.
        audit.log_event(
            tool="daemon_start", source="system", result="ok",
            params={"model": MODEL, "effort": EFFORT, "tools": TOOLS,
                    "folders": folders, "targets": targets,
                    "volume_baseline": baseline},
            detail="the daemon came up and armed the gesture")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
