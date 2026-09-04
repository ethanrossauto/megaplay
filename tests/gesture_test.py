#!/usr/bin/env python3
"""The gesture clauses, exercised without a headset.

bin/voice.py fires on one specific shape: volume down, then back up. Everything else on a
desktop moves the volume too, so the detector has five clauses (see THE GESTURE CLAUSES at
the top of voice.py) and each one exists because something real got through without it. The
GNOME volume slider was the expensive one: dragged down and back up it produces the same
down-then-up pair at either end of its ramp.

Both real shapes fire; the rest stay silent and each names the clause that rejected it, because
"it did not fire" and "it fired for the wrong reason" look identical from the couch. The count
is printed at the end rather than written here, so adding a case cannot make this line wrong.

bin/voice.sh selftest is the human version of this and cannot stand in for it: it enters a
GLib main loop and waits for a real headset, so on a build machine it would hang forever.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pin the three tunable clauses. Step 8 is a real headset's step, read off two accepted
# gestures; the shipped default is 0 (any size) because it is a per-headset number. Pinning
# them here means this test describes one headset rather than whatever the machine running it
# happens to have configured.
os.environ["VOICE_GESTURE_STEP"] = "8"
os.environ["VOICE_GESTURE_MIN"] = "0.6"
os.environ["VOICE_GESTURE_WINDOW"] = "2.0"

_spec = importlib.util.spec_from_file_location(
    "voice_under_test", os.path.join(ROOT, "bin", "voice.py"))
voice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voice)          # safe: main() is behind __main__

LOG = []
voice.log = LOG.append                   # the rejection reason is the thing under test


def run(events, start=48):
    """Feed (volume, timestamp) pairs to a primed detector.

    Returns (voice fires, flag fires, log lines). The two are counted
    SEPARATELY on purpose: the daemon now has two mirrored gestures, and a
    single counter would let a sequence pass by firing the wrong one, which is
    the exact failure worth testing for.
    """
    LOG.clear()
    fired = {"voice": 0, "flag": 0}

    def bump(kind):
        def fire():
            fired[kind] += 1
        return fire

    g = voice.Gesture(bump("voice"), bump("flag"))
    g.prime(start)
    for vol, at in events:
        g.feed(vol, at)
    return fired["voice"], fired["flag"], list(LOG)


# name, events, start volume, expected fires, expected substring in the log ("" = silent)
CASES = [
    ("a real gesture at 1.0s fires",
     [(40, 0.0), (48, 1.0)], 48, 1, "gesture detected"),

    ("a real gesture at 1.234s fires",
     [(40, 0.0), (48, 1.234)], 48, 1, "gesture detected"),

    ("clause 1: a fast slider ramp is not a button",
     [(47, 0.0), (46, 0.05), (45, 0.10), (46, 0.15), (47, 0.20), (48, 0.25)], 48, 0, "shape"),

    ("clause 1: a slow slider ramp is not a button either",
     [(47, 0.0), (46, 0.7), (45, 1.4), (46, 2.1), (47, 2.8), (48, 3.5)], 48, 0, "shape"),

    ("clause 2: a partial return is not a gesture",
     [(40, 0.0), (44, 1.0)], 48, 0, "symmetry"),

    ("clause 3: a single-step press is the wrong size",
     [(47, 0.0), (48, 0.386)], 48, 0, "step size"),

    ("clause 3: a seven-step drop is the wrong size",
     [(41, 0.0), (48, 1.0)], 48, 0, "step size"),

    ("clause 4: too fast to be two presses",
     [(40, 0.0), (48, 0.3)], 48, 0, "minimum gap"),

    ("clause 5: 3.5s apart is two unrelated presses",
     [(40, 0.0), (48, 3.5)], 48, 0, "maximum gap"),

    # Not one of the five: an UP with nothing armed has nothing to reject, so it must be
    # silent rather than logged. This is the shape the very first event of a session takes.
    ("an up with no down is ignored silently",
     [(48, 1.0)], 40, 0, ""),

    # "The first gesture never works", part 3, reported 2026-08-04 and fixed the same morning.
    # A headset settling to a lower volume after it connects is a DOWN with no matching UP. It
    # used to arm the detector forever, because only an UP cleared down_from, so the next real
    # press read as the second drop of a ramp and died on clause 1. The log said it exactly:
    # "ignored a volume change (0.745s, step 8): failed shape", then a clean gesture four
    # seconds later. An arm past the window is now discarded instead.
    ("a phantom settle long ago does not eat the next real gesture",
     [(40, 0.0), (32, 100.0), (40, 101.0)], 48, 1, "discarded a stale arm"),

    # The other half of the same fix: a FRESH arm must still block, or clause 1 is gone and the
    # GNOME volume slider fires the mic again.
    ("a drop half a second ago still counts as a ramp",
     [(40, 0.0), (32, 0.5), (40, 1.5)], 48, 0, "shape"),

    # "The first gesture never works", part 4 (2026-08-09): the drifted baseline. PipeWire had
    # written 45 while the headset sat at 48, so the press to 40 measured as a 5-step drop and
    # the return to 48 missed the remembered start by 3. Logged four times over three days as
    # an odd step plus a symmetry failure, always with a clean step 8 two seconds later - the
    # rejected press is what tells BlueZ the truth. The rise, 40 to 48, is a press either way.
    ("a press against a drifted baseline still fires",
     [(40, 0.0), (48, 1.0)], 45, 1, "gesture detected"),

    ("and it says how far the baseline had drifted",
     [(40, 0.0), (48, 1.0)], 45, 1, "baseline had drifted +3"),

    # Drift in the other direction: PipeWire wrote 51, the headset was at 48. The drop reads 11.
    ("a drift the other way fires too",
     [(40, 0.0), (48, 1.0)], 51, 1, "baseline had drifted -3"),

    # The relaxation is bounded by clause 3, and this is what keeps clause 2 meaningful: a
    # return that is NOT one press back is still refused, drift or no drift.
    ("a partial return is refused even when the start is suspect",
     [(40, 0.0), (44, 1.0)], 45, 0, "symmetry"),
]

# Clause 3 takes a LIST, and these are the sequences that say why (2026-08-06). The volume
# range is 0 to 127, which does not divide evenly by the number of steps a headset has, so most
# presses move by N and a few land on N-1. Pinned to a single 8, the gesture was dead at exactly
# one volume and worked at every other: four presses in a row logged "failed step size (want 8)"
# while shape, symmetry and both timing clauses passed. They run against the SAME parser the
# daemon uses, called through voice.parse_steps, so the test cannot drift from the real one.
LIST_CASES = [
    ("clause 3: with 7,8 pinned an eight-step drop fires",
     "7,8", [(40, 0.0), (48, 1.0)], 48, 1, "gesture detected"),

    ("clause 3: with 7,8 pinned a seven-step drop fires too",
     "7,8", [(41, 0.0), (48, 1.0)], 48, 1, "gesture detected"),

    ("clause 3: with 7,8 pinned a five-step drop is still refused",
     "7,8", [(43, 0.0), (48, 1.0)], 48, 0, "step size (want 7 or 8)"),

    ("clause 3: 0 accepts any size",
     "0", [(43, 0.0), (48, 1.0)], 48, 1, "gesture detected"),

    # The fallback has to be visible, not silent: an unreadable value accepts any size, and the
    # ready line prints "step any" so it reads as a typo rather than as a dead gesture.
    ("clause 3: an unreadable value falls back to any size",
     "eight", [(43, 0.0), (48, 1.0)], 48, 1, "gesture detected"),

    # Part 4 again, on the list form: the rise is what gets matched, so a drifted baseline is
    # survivable at either of the two sizes rather than only at the one somebody read off a log.
    ("a drifted baseline survives on the seven-step size too",
     "7,8", [(41, 0.0), (48, 1.0)], 44, 1, "gesture detected"),

    # THE DEGRADATION, stated as a test because it is a real limit and not an oversight: with no
    # step size pinned there is nothing to tell a press from a drift, so clause 2 stays strict
    # and the drifted pair is refused exactly as it was before. The shipped default is 0, so
    # this is the behaviour anyone gets until they read their own step off the log.
    ("with no step pinned a drifted baseline is still refused",
     "0", [(40, 0.0), (48, 1.0)], 45, 0, "symmetry"),
]

# THE MIRRORED GESTURE, added 2026-08-14: up then down means "this song is censored".
# It shares all five clauses with the voice trigger, so what these sequences are really
# testing is that the direction is the ONLY thing separating them, and that a slider ramp
# is still refused when it runs the other way. Censorship cannot be detected from Spotify's
# metadata (which describes the listing, not the audio) nor from transcribing the audio (a
# model finding no profanity may simply have declined to write it down), so a person hearing
# it is the detector and this is how they record it.
FLAG_CASES = [
    ("the mirrored shape flags, and does not talk",
     [(56, 0.0), (48, 1.0)], 48, "flag gesture detected"),

    ("a slider ramp UP and back is not a button either",
     [(49, 0.0), (50, 0.05), (51, 0.10), (50, 0.15), (49, 0.20), (48, 0.25)], 48, "shape"),

    ("clause 5 applies to the mirror too",
     [(56, 0.0), (48, 3.5)], 48, "maximum gap"),

    ("clause 4 applies to the mirror too",
     [(56, 0.0), (48, 0.3)], 48, "minimum gap"),

    ("a rise that does not come back is refused",
     [(56, 0.0), (52, 1.0)], 48, "symmetry"),

    # THE REAL SEQUENCE, off the headset, 2026-08-14, and the bug it caught within an hour
    # of the mirrored gesture shipping. The volume rests where the last gesture left it (40),
    # so the gesture arrives as UP(40->48) then DOWN(48->40) while an older DOWN is still
    # armed. That opening UP was being eaten as the old arm's return leg, failing the window
    # by three quarters of a minute, and discarded - so the gesture that followed could never
    # begin. The log said "48 -> 40 -> 48 failed maximum gap" over and over while BlueZ was
    # plainly reporting 40 -> 48 -> 40 nine tenths of a second apart. A failed return leg is
    # still a good FIRST press, and it now re-arms as one.
    ("a stale arm does not eat the opening press of the next gesture",
     [(40, 0.0), (48, 45.0), (40, 45.9)], 48, "flag gesture detected"),
]

failures = []


def check(name, fires, lines, want_fires, want_log):
    joined = " | ".join(lines)
    if fires != want_fires:
        failures.append(f"{name}: fired {fires} time(s), wanted {want_fires}. log: {joined}")
    elif want_log and want_log not in joined:
        failures.append(f"{name}: log did not mention '{want_log}'. log: {joined}")
    elif not want_log and joined:
        failures.append(f"{name}: expected silence, got: {joined}")
    else:
        print(f"PASS  {name}")


def check_flags(name, flags, want_flags, lines):
    """The other counter. A voice sequence must never flag, and vice versa."""
    if flags != want_flags:
        failures.append(f"{name}: flagged {flags} time(s), wanted {want_flags}. "
                        f"log: {' | '.join(lines)}")
        return False
    return True


for name, events, start, want_fires, want_log in CASES:
    fires, flags, lines = run(events, start)
    if check_flags(name, flags, 0, lines):
        check(name, fires, lines, want_fires, want_log)

for name, spec, events, start, want_fires, want_log in LIST_CASES:
    voice.GESTURE_STEP = voice.parse_steps(spec)
    voice.GESTURE_STEP_DESC = (
        " or ".join(str(s) for s in sorted(voice.GESTURE_STEP)) or "any")
    fires, flags, lines = run(events, start)
    if check_flags(name, flags, 0, lines):
        check(name, fires, lines, want_fires, want_log)

# Back to the shipped default before the mirror cases, or the last LIST_CASES spec leaks in.
voice.GESTURE_STEP = voice.parse_steps("0")
voice.GESTURE_STEP_DESC = "any"

for name, events, start, want_log in FLAG_CASES:
    want_flags = 1 if want_log == "flag gesture detected" else 0
    fires, flags, lines = run(events, start)
    joined = " | ".join(lines)
    if fires != 0:
        failures.append(f"{name}: fired the VOICE trigger {fires} time(s), wanted 0. log: {joined}")
    elif flags != want_flags:
        failures.append(f"{name}: flagged {flags} time(s), wanted {want_flags}. log: {joined}")
    elif want_log not in joined:
        failures.append(f"{name}: log did not mention '{want_log}'. log: {joined}")
    else:
        print(f"PASS  {name}")

if failures:
    print()
    for f in failures:
        print(f"FAIL  {f}")
    sys.exit(1)

print(f"      {len(CASES) + len(LIST_CASES) + len(FLAG_CASES)} sequences, "
      "all five clauses covered, both gesture directions")
