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
import tempfile

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

    # ⚠️ THIS IS THE EXPLICIT OFF SWITCH, NOT THE DEFAULT. VOICE_GESTURE_STEP=0 turns clause 3
    # off for good, so no measurement is ever coming and the forgiveness below would be
    # permanent rather than once per headset. Clause 2 therefore stays strict here, which is
    # what this case pins. The shipped default is "auto", covered further down.
    ("with clause 3 switched off entirely, a drifted baseline is still refused",
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

# ---------------------------------------------------------------------------
# MEASURING THE STEP - clause 3 configuring itself, per headset
#
# The clause used to be a constant somebody had to read off a log and pin by hand, so it
# described ONE headset and quietly refused every press from any other. These check the three
# things that has to be true for it to configure itself safely: that one press is enough to
# derive the whole grid, that the first gesture on an unmeasured headset still fires, and that
# a wrong measurement corrects itself instead of bricking the gesture forever.
# ---------------------------------------------------------------------------

# 127 divides evenly by nothing, so a headset's presses come out as two adjacent sizes and a
# measurement has to accept a neighbourhood. ⚠️ THE FIRST VERSION OF THIS RETURNED A GRID
# computed as round(127/press), which is a precise answer from an imprecise sample: see the
# oscillation case below, which is the real headset that disproved it within a minute.
STEP_MATH = [
    ("an 8-step press accepts 7 through 9", 8, {7, 8, 9}),
    ("a 7-step press accepts 6 through 8, so both of that headset's sizes pass", 7, {6, 7, 8}),
    ("a 4-step press accepts 3 through 5", 4, {3, 4, 5}),
    ("a 16-step press accepts 15 through 17", 16, {15, 16, 17}),
    # Nothing below 3 survives, so a measurement can never widen down onto slider territory.
    ("a 3-step press does not widen down onto a slider notch", 3, {3, 4}),
    # Refusing to learn is not a failure to learn. A slider notch would hand clause 3 a set
    # that accepts exactly what the clause exists to refuse, so it is left unmeasured instead.
    ("a 1-step change is a slider, not a button, and teaches nothing", 1, set()),
    ("nor is a 2-step change, which would accept a slider too", 2, set()),
    ("and nothing above 32, which would be fewer than four steps in the range", 40, set()),
]
for name, press, want in STEP_MATH:
    got = set(voice.steps_for(press))
    if got != want:
        failures.append(f"{name}: steps_for({press}) gave {sorted(got)}, wanted {sorted(want)}")
    else:
        print(f"PASS  {name}")

# The address is what everything is filed under, and the depth of the path is not fixed: the
# same headset reports .../dev_X/fd0 on one stack and .../dev_X/sep3/fd0 here.
# 🔒 THE ADDRESSES ARE FROM RFC 7042's DOCUMENTATION RANGE (00-00-5E-00-53-xx), NOT REAL ONES.
# This file publishes, and a bluetooth MAC is a persistent identifier for a device somebody
# wears in public. A test about PARSING a path needs a well-formed address, never a real one.
PATHS = [
    ("an address is read out of a transport path",
     "/org/bluez/hci0/dev_00_00_5E_00_53_01/sep3/fd0", "00:00:5E:00:53:01"),
    ("and out of a shallower one",
     "/org/bluez/hci0/dev_00_00_5E_00_53_02/fd0", "00:00:5E:00:53:02"),
    ("a path with no device in it is not a headset",
     "/org/bluez/hci0", None),
]
for name, path, want in PATHS:
    got = voice.device_of(path)
    if got != want:
        failures.append(f"{name}: device_of gave {got!r}, wanted {want!r}")
    else:
        print(f"PASS  {name}")


def run_learning(events, start=48, steps=frozenset(), learn=True):
    """A detector that measures its own step, the way the daemon builds one."""
    LOG.clear()
    fired = {"voice": 0, "flag": 0}

    def bump(kind):
        def fire():
            fired[kind] += 1
        return fire

    g = voice.Gesture(bump("voice"), bump("flag"), steps=steps, learn=learn)
    g.prime(start)
    for vol, at in events:
        g.feed(vol, at)
    return g, fired["voice"], list(LOG)


# 🔒 THE HEADSET NOBODY HAS MEASURED STILL WORKS. This is the property that makes learning
# safe to switch on by default: it can only ever ADD a guard, never be the reason a gesture
# fails. A 4-step press is the one that was silently dead before this existed.
g, fires, lines = run_learning([(64, 0.0), (68, 1.0)], start=68)
if fires != 1:
    failures.append(f"first gesture on an unmeasured headset: fired {fires}, wanted 1. "
                    f"log: {' | '.join(lines)}")
elif set(g.steps) != {3, 4, 5}:
    failures.append(f"after learning: steps are {sorted(g.steps)}, wanted [3, 4, 5]")
else:
    print("PASS  the first gesture on an unmeasured headset fires, and measures it")

# 🔴 THE REGRESSION THAT MATTERS, and it came off a real headset rather than out of a
# thought experiment. A WH-1000XM5 pressed 68 -> 64 and then 64 -> 59 inside one minute:
# four AND five, from the same headset, seconds apart. A measurement that pinned the grid
# from either sample excluded the other size, so clause 3 refused half the presses and the
# relearn below flipped the answer back and forth forever. Whichever size arrives first,
# both have to pass.
for seen, other in ((4, 5), (5, 4)):
    learned = voice.steps_for(seen)
    if not {seen, other} <= set(learned):
        failures.append(f"a {seen}-step press learned {sorted(learned)}, which excludes the "
                        f"same headset's {other}-step press")
        break
else:
    print("PASS  a headset that presses 4 and 5 has both accepted, whichever arrives first")

# And having measured it, the clause is live: the other headset's press is now refused, which
# is the guard that was lost while the shipped default accepted any size at all.
_g, fires, lines = run_learning([(61, 0.0), (68, 1.0)], start=68, steps=frozenset({3, 4, 5}))
if fires != 0 or "step size (want 3 or 4 or 5)" not in " | ".join(lines):
    failures.append(f"a measured headset should refuse a 7-step press: fired {fires}. "
                    f"log: {' | '.join(lines)}")
else:
    print("PASS  once measured, a press from a different headset is refused")

# 🔑 THE DEGRADATION THE OLD DEFAULT CARRIED, GONE. With no step pinned, clause 2 had to stay
# strict, so a drifted baseline was refused and the first gesture after a profile switch was
# spent correcting it. Measuring the step is what lets the rise be believed instead.
_g, fires, lines = run_learning([(60, 0.0), (68, 1.0)], start=64, steps=frozenset({7, 8}))
if fires != 1:
    failures.append(f"a drifted baseline should survive once the step is known: "
                    f"fired {fires}. log: {' | '.join(lines)}")
elif "baseline had drifted" not in " | ".join(lines):
    failures.append("the drift should be said out loud, not absorbed silently")
else:
    print("PASS  a measured step rescues a drifted baseline, which pinning by hand also did")

# 🔴 THE FIRST GESTURE ON A BRAND NEW HEADSET, WITH THE BASELINE ALREADY WRONG. This is the
# case the whole thing turns on: PipeWire writes an off-grid volume ON CONNECT, so a headset
# nobody has measured is exactly the one whose baseline is least trustworthy, and clause 2
# had nothing to lean on because clause 3 needs a measurement that needs a gesture that needs
# clause 2. Buying a headset and having the first press do nothing is the symptom.
g, fires, lines = run_learning([(59, 0.0), (63, 1.0)], start=64)
joined = " | ".join(lines)
if fires != 1:
    failures.append(f"a new headset with a drifted baseline should still fire: "
                    f"fired {fires}. log: {joined}")
# 🔑 AND IT MUST MEASURE FROM THE RETURN LEG, NOT THE DROP. The drop was measured against the
# value that is wrong (64 -> 59 reads as 5); the rise is between two numbers the headset
# itself reported (59 -> 63, the true 4). Believing the drop would write the drift into the
# measurement permanently, and every later press would be judged against it.
elif set(g.steps) != {3, 4, 5}:
    failures.append(f"measured {sorted(g.steps)} from a drifted first gesture, wanted [3, 4, 5] "
                    f"- the drop says 5 and only the return leg says the true 4")
elif "baseline had drifted" not in joined:
    failures.append(f"the drift should be said out loud, not absorbed silently. log: {joined}")
else:
    print("PASS  a new headset fires on a drifted first gesture, and measures the return leg")

# ⚠️ AND THE FORGIVENESS IS BOUNDED, or it stops being about drift. A return leg the size of a
# slider notch is not a button, and a miss bigger than the press itself is a different action
# rather than a stale baseline.
for name, events, start in (
        ("a slider notch is not forgiven, however close it lands", [(63, 0.0), (62, 1.0)], 64),
        ("nor is a miss bigger than the press itself", [(59, 0.0), (54, 1.0)], 64)):
    g, fires, lines = run_learning(events, start)
    if fires != 0:
        failures.append(f"{name}: fired {fires}, wanted 0. log: {' | '.join(lines)}")
    elif g.steps:
        failures.append(f"{name}: measured {sorted(g.steps)} off a pair it should have refused")
    else:
        print(f"PASS  {name}")

# 🔁 A MEASUREMENT TAKEN FROM THE WRONG HEADSET CORRECTS ITSELF. Without this, swapping
# headsets would replace "pinned wrong by hand" with "measured wrong automatically", which is
# the same dead gesture with a longer story behind it.
events, t = [], 0.0
for _ in range(3):                       # three 4-step gestures at a 7/8 measurement
    events += [(64, t), (68, t + 1.0)]
    t += 4.0
g, fires, lines = run_learning(events, start=68, steps=frozenset({7, 8}))
joined = " | ".join(lines)
if fires != 1:
    failures.append(f"the third refusal should relearn and fire: fired {fires}. log: {joined}")
elif set(g.steps) != {3, 4, 5}:
    failures.append(f"after relearning: steps are {sorted(g.steps)}, wanted [3, 4, 5]")
elif "measured from the wrong headset" not in joined:
    failures.append(f"relearning should say why, out loud. log: {joined}")
else:
    print("PASS  three step-size refusals in a row remeasure, rather than staying wrong")

# ⛔ AND A PIN IS NOT OVERRULED. Someone who set the value by hand has decided; correcting it
# from under them would make their setting look broken with nothing to explain it.
g, fires, _lines = run_learning(events, start=68, steps=frozenset({7, 8}), learn=False)
if fires != 0 or set(g.steps) != {7, 8}:
    failures.append(f"a pinned step must not relearn: fired {fires}, steps {sorted(g.steps)}")
else:
    print("PASS  a step pinned by hand is never remeasured")

# ---------------------------------------------------------------------------
# Two headsets at once, which is the other half of "any device"
# ---------------------------------------------------------------------------
voice.GESTURE_LEARN = True
voice.GESTURE_STEP = frozenset()
voice.GESTURE_STEP_FILE = os.path.join(
    tempfile.mkdtemp(prefix="gesture-steps-"), "gesture-steps.json")

XM, Q45 = "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA/sep3/fd0", "/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB/fd0"
LOG.clear()
fired = []
router = voice.GestureRouter(lambda a: fired.append(("voice", a)),
                             lambda a: fired.append(("flag", a)))
router.feed(XM, 68, 0.0)
router.feed(Q45, 48, 0.0)

# 🔴 THE INTERLEAVING BUG. One headset's press used to arrive as the return leg of the other's,
# because all of this state lived on a single detector for the whole daemon. Here the second
# headset changes volume in the MIDDLE of the first one's gesture, and the gesture still lands.
router.feed(XM, 64, 1.0)                 # the press down
router.feed(Q45, 40, 1.2)                # the other headset, mid-gesture
router.feed(XM, 68, 2.0)                 # the press back up, completing it
if fired != [("voice", "AA:AA:AA:AA:AA:AA")]:
    failures.append(f"two headsets at once: fired {fired}, wanted one voice trigger "
                    f"naming the headset that made it. "
                    f"log: {' | '.join(LOG)}")
else:
    print("PASS  a second headset moving mid-gesture does not eat the first one's press")

# Each keeps its own measurement, which is what makes swapping between them free.
if set(router.by_addr["AA:AA:AA:AA:AA:AA"].steps) != {3, 4, 5}:
    failures.append("the first headset should have measured a 4-step press")
elif router.by_addr["BB:BB:BB:BB:BB:BB"].steps:
    failures.append("the second headset never completed a gesture and must stay unmeasured")
elif voice.load_learned_steps().get("AA:AA:AA:AA:AA:AA") != frozenset({3, 4, 5}):
    failures.append("the measurement should have been written to disk")
else:
    print("PASS  each headset keeps its own step, saved under its own address")

# A headset that disconnects loses its baseline and keeps its measurement: the volume it comes
# back at is not the one it left at, but the hardware did not change in the drawer.
router.forget("AA:AA:AA:AA:AA:AA")
if router.detector("AA:AA:AA:AA:AA:AA").prev is not None:
    failures.append("a reconnected headset must not keep a stale baseline")
elif set(router.detector("AA:AA:AA:AA:AA:AA").steps) != {3, 4, 5}:
    failures.append("a reconnected headset should not have to be measured again")
else:
    print("PASS  disconnecting drops the baseline and keeps the measurement")

if failures:
    print()
    for f in failures:
        print(f"FAIL  {f}")
    sys.exit(1)

print(f"      {len(CASES) + len(LIST_CASES) + len(FLAG_CASES)} sequences, "
      "all five clauses covered, both gesture directions,")
print(f"      and clause 3 measuring itself across {len(STEP_MATH)} step sizes "
      "and two headsets at once")
