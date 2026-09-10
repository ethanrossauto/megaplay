#!/usr/bin/env python3
"""The audit log: the table's own rules, and that every action writes to it.

Two halves, and the second is the one worth having.

The first exercises bin/audit.py directly: that the vocabularies are enforced by
the table rather than by everybody remembering them, that a write which cannot
happen is reported instead of swallowed, that the id cursor keeps its promise
after a delete, and that rows group under the command that caused them.

The second drives voice.execute() through every exit it has, with the player and
the library stubbed out, and asserts that each one writes exactly one row with
the right outcome on it. That function's docstring claims every action is on the
record, and this is what makes the claim checkable: a branch added later that
returns without logging fails here rather than being noticed months afterwards
when somebody asks why the music changed.

No network, no player, no model, and the log is a throwaway file in a temp
directory. It never touches the real one.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="audit-test-")
os.environ["MEGAPLAY_AUDIT_DB"] = os.path.join(TMP, "audit.db")

sys.path.insert(0, os.path.join(ROOT, "bin"))
import audit                                                    # noqa: E402

FAILED = []


def check(name, got, want):
    if got == want:
        print(f"    PASS  {name}")
    else:
        FAILED.append(name)
        print(f"    FAIL  {name}\n          got  {got!r}\n          want {want!r}")


# ---------------------------------------------------------------------------
# The table's own rules
# ---------------------------------------------------------------------------

REPORTED = []
audit.report = REPORTED.append

check("a source outside the vocabulary is refused",
      audit.log_event(tool="x", source="telepathy", result="ok"), None)
check("a result outside the vocabulary is refused",
      audit.log_event(tool="x", source="voice", result="fine"), None)
check("a tier outside the vocabulary is refused",
      audit.log_event(tool="x", source="voice", result="ok", tier="vibes"), None)
check("and each refusal was reported, not swallowed", len(REPORTED), 3)
check("the report names the row it lost",
      all("COULD NOT WRITE" in r for r in REPORTED), True)
check("nothing was written", len(audit.fetch_events(0, 100)), 0)

first = audit.log_event(tool="daemon_start", source="system", result="ok",
                        detail="came up")
check("a valid row is written and returns its id", isinstance(first, int), True)

# A value nobody anticipated must not take the row down with it. The parameter
# loses its type; the decision it explains survives.
odd = audit.log_event(tool="dispatch", source="voice", tier="llm", result="ok",
                      params={"obj": object()}, detail="odd params")
check("an unserialisable parameter still writes the row", isinstance(odd, int), True)
check("and the row is readable afterwards",
      isinstance(audit.fetch_events(odd - 1, 1)[0]["params"]["obj"], str), True)

# AUTOINCREMENT, and this is the check that would catch its removal. Without the
# keyword SQLite reuses the id of a deleted row, so a reader holding a cursor
# would be handed an id it has already passed and would skip that row forever.
with sqlite3.connect(os.environ["MEGAPLAY_AUDIT_DB"]) as _c:
    _c.execute("delete from events where id = ?", (odd,))
    _c.commit()
after = audit.log_event(tool="listen", source="voice", result="ok", detail="hi")
check("an id is never reused after a delete", after > odd, True)

# ---------------------------------------------------------------------------
# Grouping, which is the argument the log makes
# ---------------------------------------------------------------------------

a, b = audit.new_command_id(), audit.new_command_id()
audit.log_event(tool="listen", source="voice", result="ok", command_id=a,
                detail="play some birdman")
audit.log_event(tool="dispatch", source="voice", tier="llm", result="ok",
                command_id=a, detail="he named an artist, so the parent it is")
audit.log_event(tool="play", source="voice", tier="llm", result="ok",
                command_id=a, detail="started Birdman")
audit.log_event(tool="flag", source="gesture", result="ok", command_id=b,
                track_path="Birdman/Fast Money/Money.mp3", detail="flagged")
# A follow-up hangs off the command it follows, so a chain reads as one story.
c = audit.new_command_id()
audit.log_event(tool="listen", source="voice", result="ok", command_id=c,
                parent_command_id=b, detail="a follow-up to the flag")

groups = audit.group_by_command(audit.recent_events(50))
by_id = {g["command_id"]: g for g in groups}
check("one gesture is one group", len(by_id[a]["events"]), 3)
check("its rows stay in the order they happened",
      [e["tool"] for e in by_id[a]["events"]], ["listen", "dispatch", "play"])
check("a follow-up joins its parent's group, not a new one",
      sorted(e["tool"] for e in by_id[b]["events"]), ["flag", "listen"])
check("a row with no command id is its own group",
      any(g["command_id"] is None for g in groups), True)
check("the newest command sorts first", groups[0]["command_id"] in (a, b, c), True)
check("the model was called on some rows and not others",
      sorted({e["tier"] or "-" for e in audit.fetch_events(0, 500)}), ["-", "llm"])
check("nothing has ever had to ask a question",
      [e for e in audit.fetch_events(0, 500) if e["result"] == "clarify"], [])

# The two readers answer different questions and the difference is the point:
# a cursor read walks forward from where you were, a tail read shows the end.
newest = max(e["id"] for e in audit.fetch_events(0, 500))
check("the tail read ends on the newest row", audit.recent_events(2)[-1]["id"], newest)
check("the cursor read starts on the oldest", audit.fetch_events(0, 2)[0]["id"], first)
check("filtering by track finds it",
      [e["tool"] for e in audit.fetch_events(
          0, 50, track_path="Birdman/Fast Money/Money.mp3")], ["flag"])

# ---------------------------------------------------------------------------
# Every exit of execute() writes a row
# ---------------------------------------------------------------------------

try:
    import gi                                                   # noqa: F401
except ImportError:
    print("    SKIP  execute() coverage (no python3-gi, which voice.py imports)")
    print(f"\n    {len(FAILED)} failed" if FAILED else
          "\n    the table enforces its own rules and rows group by command")
    sys.exit(1 if FAILED else 0)

_spec = importlib.util.spec_from_file_location(
    "voice_under_test", os.path.join(ROOT, "bin", "voice.py"))
voice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voice)          # safe: main() is behind __main__
voice.log = lambda _m: None
voice.audit = audit

OK_REPLY = {"error": "success"}
DEAD = None


def stub(*, reply=OK_REPLY, targets=("Birdman",), tracks=("a.mp3",),
         folder_ok=True, loaded=True, rows=(("x", "y", "z", "a.mp3"),)):
    """Put a whole player and library behind the function under test."""
    voice.mpv = lambda *a: reply
    voice.mpv_set_pause = lambda _s: None
    voice.play_targets = lambda: set(targets)
    voice.play_folder = lambda *a: folder_ok
    voice.tracks_in = lambda _t: list(tracks)
    voice.load_and_play = lambda *a: loaded
    voice.catalog = lambda: [(i, r[1], r[2], r[3]) for i, r in enumerate(rows)]


# Every case: what is asked, what the world looks like, and the one row it owes.
CASES = [
    ("a skip that lands",        {"action": "next"},   {}, "next", "ok"),
    ("a skip with no player",    {"action": "next"},   {"reply": DEAD}, "next", "error"),
    ("stepping back",            {"action": "prev"},   {}, "prev", "ok"),
    ("a pause",                  {"action": "pause"},  {}, "pause", "ok"),
    ("a pause with no player",   {"action": "pause"},  {"reply": DEAD}, "pause", "error"),
    ("a resume",                 {"action": "resume"}, {}, "resume", "ok"),
    ("playing a real folder",    {"action": "play", "target": "Birdman"}, {}, "play", "ok"),
    ("a folder that is not there",
     {"action": "play", "target": "Nowhere"}, {}, "play", "rejected"),
    ("a folder holding nothing",
     {"action": "play", "target": "Birdman"},
     {"folder_ok": False, "tracks": ()}, "play", "rejected"),
    ("a folder the player would not take",
     {"action": "play", "target": "Birdman"}, {"folder_ok": False}, "play", "error"),
    ("a track selection",        {"action": "play_tracks", "tracks": [0]}, {},
     "play_tracks", "ok"),
    ("track numbers out of range",
     {"action": "play_tracks", "tracks": [99]}, {}, "play_tracks", "rejected"),
    ("a selection the player would not take",
     {"action": "play_tracks", "tracks": [0]}, {"loaded": False},
     "play_tracks", "error"),
    ("the model choosing to do nothing",
     {"action": "none", "why": "there was no music in that"}, {}, "none", "rejected"),
    ("an action this daemon does not have",
     {"action": "teleport"}, {}, "unknown", "error"),
]

# Where the cases start, so the count below counts only what they wrote.
CASES_FROM = max(e["id"] for e in audit.fetch_events(0, 500))

for name, cmd, world, want_tool, want_result in CASES:
    stub(**world)
    before = audit.recent_events(1)
    cursor_id = before[-1]["id"] if before else 0
    cid = audit.new_command_id()
    voice.execute(cmd, cid)
    written = audit.fetch_events(cursor_id, 50)
    if len(written) != 1:
        check(f"{name} writes exactly one row", len(written), 1)
        continue
    row = written[0]
    check(f"{name} is logged as {want_tool}/{want_result}",
          (row["tool"], row["result"], row["command_id"], row["source"], row["tier"]),
          (want_tool, want_result, cid, "voice", "llm"))
    if not row["detail"]:
        check(f"{name} says why in words", row["detail"], "a sentence")

# The completeness property itself, stated as a count rather than trusted.
check("every action reaching execute() left a row, and only one each",
      len(audit.fetch_events(CASES_FROM, 500)), len(CASES))

# The dropped track numbers survive on the row, which is the only place a
# partially-honoured selection is visible at all.
stub()
cid = audit.new_command_id()
voice.execute({"action": "play_tracks", "tracks": [0, 99]}, cid)
row = audit.fetch_events(0, 500, command_id=cid)[0]
check("a partly honoured selection records what was dropped",
      (row["result"], row["params"]["played"], row["params"]["out_of_range"]),
      ("ok", 1, [99]))

# The model's own reason reaches the row, not just the daemon's summary of it.
cid = audit.new_command_id()
voice.execute({"action": "none", "why": "nothing in that was about music"}, cid)
check("the model's stated reason is on the row",
      "nothing in that was about music" in
      audit.fetch_events(0, 500, command_id=cid)[0]["detail"], True)

print(f"\n    {len(CASES)} exits of execute(), every one of them logged, "
      f"and the table refuses a vocabulary it does not know")
if FAILED:
    print(f"    {len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
