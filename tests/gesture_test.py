#!/usr/bin/env python3
"""The gesture clauses, exercised without a headset.

bin/voice.py fires on one specific shape: volume down, then back up. Everything else on a
desktop moves the volume too, so the detector has five clauses (see THE GESTURE CLAUSES at
the top of voice.py) and each one exists because something real got through without it. The
GNOME volume slider was the expensive one: dragged down and back up it produces the same
down-then-up pair at either end of its ramp.

Ten sequences run here. Both real shapes fire; the rest stay silent and each names the clause
that rejected it, because "it did not fire" and "it fired for the wrong reason" look identical
from the couch.

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
    """Feed (volume, timestamp) pairs to a primed detector. Returns (fired, log lines)."""
    LOG.clear()
    fired = []
    g = voice.Gesture(lambda: fired.append(True))
    g.prime(start)
    for vol, at in events:
        g.feed(vol, at)
    return len(fired), list(LOG)


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
]

failures = []
for name, events, start, want_fires, want_log in CASES:
    fires, lines = run(events, start)
    joined = " | ".join(lines)
    if fires != want_fires:
        failures.append(f"{name}: fired {fires} time(s), wanted {want_fires}. log: {joined}")
    elif want_log and want_log not in joined:
        failures.append(f"{name}: log did not mention '{want_log}'. log: {joined}")
    elif not want_log and joined:
        failures.append(f"{name}: expected silence, got: {joined}")
    else:
        print(f"PASS  {name}")

if failures:
    print()
    for f in failures:
        print(f"FAIL  {f}")
    sys.exit(1)

print(f"      {len(CASES)} sequences, all five clauses covered")
