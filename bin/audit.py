#!/usr/bin/env python3
"""The audit log for the voice daemon: what was asked, what the model decided, what ran.

    bin/audit.py                  the last 20 commands, grouped, newest first
    bin/audit.py --limit 5        fewer
    bin/audit.py --command <id>   one command and everything it caused
    bin/audit.py --track <path>   every row naming one track, deleted or not
    bin/audit.py --since <id>     rows newer than a cursor, oldest first
    bin/audit.py --json           the same rows as JSON, one object per line

THE RECORD IS THIS TABLE, NOT THE DAEMON'S TEXT LOG. The rolling log in .state is
one process's memory of what it printed, it is rotated and gitignored, and it is
prose: you cannot ask it how often the model was called, how often a decision was
refused, or what happened to a track that has since been swapped out. This is the
same events written as rows, so those are queries.

WHY EVERY ACTION IS IN HERE, AND NOT ONLY THE ONES SOMEBODY REMEMBERED TO LOG.
There is exactly one function in the daemon that turns a model decision into
playback, and it writes its own row at every one of its exits, including the ones
that refuse. There is exactly one function that handles the flag gesture, and it
does the same. So "the log is complete" is a property of where the writes sit
rather than a promise, and the way to break it is to add a second path that acts
on the player.

WRITTEN BEFORE THE EFFECT. The row recording what the model chose is inserted
before that choice reaches the player, so a crash between the decision and the
music still leaves the intent on record. The per-action rows are written after
their action, because their whole content is how it turned out.

WHAT THE COLUMNS ARE FOR

    command_id          groups every row produced by one gesture. One flick of
                        the thumb is one command and usually three rows: what was
                        heard, what was decided, what ran.
    parent_command_id   set when one command is a follow-up to another, so a
                        chain reads as one interaction instead of two unrelated
                        ones. Nothing writes it today; see the note on 'clarify'.
    source              'voice' (spoken command), 'gesture' (the flag flick),
                        'system' (the daemon itself starting).
    tier                'llm' on the rows that cost a model call, null on the
                        rows that did not. This column is why "the model is only
                        called when it earns its latency" is a query rather than
                        a claim: the capture, the flag and the startup rows are
                        all tier null, and they are most of the table.
    result              'ok', 'rejected', 'clarify', 'error'. See below.
    track_path          the track a row is about, relative to the music folder.
    detail              prose, composed at the moment of the decision. On the
                        model's own row this is its stated reasoning, which is
                        the one thing that cannot be reconstructed afterwards.

'clarify' IS ITS OWN OUTCOME AND NOTHING HERE WRITES IT, DELIBERATELY. It means
the system understood the request, declined to guess, and asked a question. This
daemon has no reply channel: one gesture, one utterance, one command, so asking
a question would waste the whole turn and it is built to pick instead. The value
is kept because that rule is then checkable rather than merely stated. If

    select count(*) from events where result = 'clarify'

is ever anything but zero, something started asking questions and the design
changed without anybody saying so.

'rejected' vs 'error' IS A REAL DISTINCTION, NOT A SEVERITY. Rejected is the
system working correctly and declining: the model named a folder that is not on
disk, or picked track numbers outside the catalogue. Error is something broken:
the model call timed out, its reply would not parse, the player is not answering.
Folding them together would make the two most useful questions about this log
unanswerable, which are how often the model asks for something impossible and how
often the machinery underneath it fails.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Runtime scratch, like every other file beside it: regenerable in the sense that
# the daemon will start a fresh one, and NOT backed up. The hand-made flag labels
# sit in the project root instead, precisely because they are not regenerable.
DB = os.environ.get("MEGAPLAY_AUDIT_DB", os.path.join(PROJECT, ".state", "audit.db"))

# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------
#
# `integer primary key autoincrement` RATHER THAN A PLAIN ROWID, AND THE KEYWORD
# IS LOAD-BEARING. Without it SQLite is free to reuse the id of a deleted row, so
# a reader holding "I have seen everything up to 412" could be handed a 400 it has
# never seen and skip it forever. AUTOINCREMENT costs one extra table and buys a
# cursor that means what it says.
#
# `events.track_path` IS DELIBERATELY NOT A FOREIGN KEY, and there is nothing to
# point it at on purpose. A track here is a file, and files move: the swapper
# replaces a bad download with a better source, a folder gets renamed for the
# card's filesystem, a playlist is deleted outright. An audit log that forgot a
# track when the file went away would lose the one question it exists to answer,
# which is what happened to the thing that is no longer here.
#
# THE MIGRATION TRAP, AND IT IS WORSE HERE THAN IN A REAL DATABASE.
# `create table if not exists` is a no-op on a table that already exists,
# INCLUDING its constraints. Editing a `check` below therefore changes what a
# fresh log gets and changes nothing at all about the one on this machine, which
# then rejects the first row using the new value against a schema file sitting
# right there appearing to permit it. Postgres can drop and re-add a constraint;
# SQLite cannot. The only route is to create a new table, copy the rows, drop the
# old one and rename, so:
#
#   ANY CHANGE TO A `check` BELOW MEANS BUMPING SCHEMA_VERSION AND WRITING THE
#   REBUILD. Adding a plain column is fine and can be done with `alter table add
#   column`; adding one with a constraint is not.
SCHEMA_VERSION = 1

SCHEMA = """
create table if not exists events (
    id                integer primary key autoincrement,
    ts                text    not null,

    command_id        text,
    parent_command_id text,

    actor             text    not null default 'operator',
    source            text    not null,
    tier              text,

    tool              text    not null,
    params            text    not null default '{}',
    result            text    not null,
    detail            text,
    latency_ms        integer,

    track_path        text,

    check (source in ('voice', 'gesture', 'system')),
    check (result in ('ok', 'rejected', 'clarify', 'error')),
    check (tier is null or tier in ('parser', 'llm'))
);

create index if not exists events_command_idx on events (command_id);
create index if not exists events_track_idx   on events (track_path);
create index if not exists events_ts_idx      on events (id desc);
"""

# THOSE THREE INDEXES DO NOTHING AT THE CURRENT ROW COUNT and that is said here
# rather than glossed over. A spoken command writes three rows, so this table
# grows by tens per evening and SQLite will scan it faster than it can consult an
# index. They are declared for the SHAPE of the two filters the readers below
# offer, so those stay cheap if this ever holds a year of listening. Claiming they
# make anything faster today would be a claim that is false.

# 'parser' is permitted above and nothing writes it. There is one tier here, the
# model, because there is no local fast path that answers an utterance without it.
# The value is kept from the design this was copied from so that adding such a
# path later is a code change and not a table rebuild, which is the one migration
# this file cannot do cheaply.


def report(msg):
    """Where this module's own failures go. The daemon replaces this with its log.

    A module-level hook rather than a print, because these lines have to land in
    the same place as everything else the daemon says or nobody will see them.
    """
    print(f"[{time.strftime('%H:%M:%S')}] audit: {msg}", file=sys.stderr, flush=True)


def new_command_id():
    """One id for one interaction. Minted where the interaction starts."""
    return str(uuid.uuid4())


@contextmanager
def _connect():
    """A connection for one call, and the whole pooling question does not arise.

    THIS IS THE INVERSION OF THE DESIGN IT WAS COPIED FROM. There, a connection
    crossed a network to a database that scales to zero and cost the better part
    of a second, so it was pooled, held for a request, and validated on checkout.
    Here it is opening a file on the local disk. Per-call connections are correct,
    and they also make the threading question disappear: the flag gesture runs on
    the D-Bus thread while a spoken command is still being answered on another,
    and a shared connection would have to be locked.

    WAL mode so a reader never blocks the daemon's writer, and a busy timeout so
    two writers wait for each other instead of one raising 'database is locked'.
    """
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode=WAL")
        conn.execute("pragma busy_timeout=5000")
        yield conn
    finally:
        conn.close()


def init():
    """Create the table if it is not there. Safe to call on every write."""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(f"pragma user_version={SCHEMA_VERSION:d}")
        conn.commit()


def log_event(*, tool, source, result, command_id=None, parent_command_id=None,
              tier=None, params=None, detail=None, track_path=None,
              latency_ms=None, actor="operator"):
    """Append one row. Returns its id, or None if the write failed.

    KEYWORD-ONLY ON PURPOSE. This is called from every path that acts, and a
    positional signature whose first three arguments are all short strings is one
    transposition away from a log that is quietly wrong about what happened.

    IT NEVER RAISES, AND THAT IS A DIVERGENCE WORTH NAMING. In the design this
    came from, a failed audit write takes the request down with it, which is right
    when the caller is a web request that has not done anything yet. Here the
    caller is a daemon holding a paused player and a headset switched into its
    call profile, and a raise partway through would leave both. So a broken log
    must not break the music.

    IT FAILS LOUD RATHER THAN CLOSED, WHICH IS THE MOST THIS POSITION ALLOWS. The
    failure is reported through the daemon's own log and the return is None, so a
    log that cannot be written is visibly different from one with nothing in it.
    What it cannot do is refuse to continue.
    """
    try:
        init()
        with _connect() as conn:
            cur = conn.execute(
                """
                insert into events
                    (ts, command_id, parent_command_id, actor, source, tier,
                     tool, params, result, detail, latency_ms, track_path)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    # Local time with its offset, so a row is unambiguous about
                    # when it happened and still sorts as text.
                    time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    command_id, parent_command_id, actor, source, tier, tool,
                    # `default=str` so a value nobody anticipated is stringified
                    # rather than raising inside the logger. Losing the type of
                    # one parameter beats losing the row that explains a decision.
                    json.dumps(params or {}, default=str, sort_keys=True),
                    result, detail, latency_ms, track_path,
                ),
            )
            conn.commit()
            return cur.lastrowid
    except (sqlite3.Error, OSError, ValueError) as exc:
        report(f"COULD NOT WRITE the {tool}/{result} row: "
               f"{exc.__class__.__name__}: {exc}")
        return None


def _rows(sql, params):
    try:
        init()
        with _connect() as conn:
            return [_row(r) for r in conn.execute(sql, params).fetchall()]
    except (sqlite3.Error, OSError) as exc:
        report(f"could not read the log: {exc.__class__.__name__}: {exc}")
        return []


def _row(row):
    out = dict(row)
    try:
        out["params"] = json.loads(out.get("params") or "{}")
    except ValueError:
        # A row whose params will not parse is still a real row about a real
        # decision. Hand back the text rather than dropping the event.
        out["params"] = {"unparseable": out.get("params")}
    return out


def fetch_events(since_id=0, limit=200, track_path=None, command_id=None):
    """Rows newer than a cursor, OLDEST FIRST. For anything that polls.

    `since_id` rather than a timestamp because a reader asking "what have I not
    seen" is asking an id question, and two rows can share a second.
    """
    sql = "select * from events where id > ?"
    args = [int(since_id)]
    if track_path:
        sql += " and track_path = ?"
        args.append(track_path)
    if command_id:
        sql += " and command_id = ?"
        args.append(command_id)
    sql += " order by id asc limit ?"
    args.append(min(int(limit), 5000))
    return _rows(sql, args)


def recent_events(limit=200, track_path=None, command_id=None):
    """The NEWEST rows, returned oldest first. For a person reading the tail.

    A SECOND READER, AND IT IS NOT DUPLICATION. `fetch_events` walks forward from
    a cursor, which is what a poller wants and is exactly wrong for "show me what
    just happened": asked for 200 rows it returns the two hundred OLDEST, so it
    starts telling you about last month the moment the table outgrows the limit.
    Reading the tail is `order by id desc`, and the two orders cannot be one
    query.
    """
    sql = "select * from events where 1 = 1"
    args = []
    if track_path:
        sql += " and track_path = ?"
        args.append(track_path)
    if command_id:
        sql += " and command_id = ?"
        args.append(command_id)
    sql += " order by id desc limit ?"
    args.append(min(int(limit), 5000))
    return list(reversed(_rows(sql, args)))


def group_by_command(events):
    """Gather rows under the command that caused them, newest command first.

    GROUPED, BECAUSE THE GROUPING IS THE ARGUMENT. A flat list shows that things
    were logged. Rows gathered under the command that produced them show that one
    sentence was heard, turned into a decision with a stated reason, and became
    three actions, which is the claim worth being able to check.

    `parent_command_id` is what ties a chain together where there is one, so it is
    the key when present. A row with no command id at all is its own group: the
    daemon's startup row is one of those, and it is the thing that explains a gap.
    """
    groups = {}
    order = []
    for e in events:
        key = e.get("parent_command_id") or e.get("command_id") or f"solo:{e['id']}"
        g = groups.get(key)
        if g is None:
            g = {"command_id": e.get("command_id"), "ts": e["ts"], "events": []}
            groups[key] = g
            order.append(key)
        g["events"].append(e)
        # Stamped with its EARLIEST row: a command happened when it was asked, not
        # when its last effect finished writing.
        if e["ts"] < g["ts"]:
            g["ts"] = e["ts"]
    out = [groups[k] for k in order]
    out.sort(key=lambda g: (g["ts"], g["events"][0]["id"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Reading it back at a terminal
# ---------------------------------------------------------------------------

_MARK = {"ok": "ok  ", "rejected": "NO  ", "clarify": "ASK ", "error": "ERR "}


def render(groups):
    """One command per block, in the order it happened inside the block."""
    lines = []
    for g in groups:
        head = g["ts"].replace("T", " ")[:19]
        cid = (g["command_id"] or "")[:8] or "-"
        first = g["events"][0]
        lines.append(f"{head}  {first['source']:<7} {cid}")
        for e in g["events"]:
            mark = _MARK.get(e["result"], e["result"][:4].ljust(4))
            took = f"{e['latency_ms'] / 1000:.1f}s" if e.get("latency_ms") else ""
            tier = e.get("tier") or ""
            lines.append(f"    {mark} {e['tool']:<12} {tier:<4} {took:>6}  "
                         f"{_one_line(e.get('detail'))}")
            if e.get("track_path"):
                lines.append(f"{' ' * 35}{e['track_path']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _one_line(text, width=96):
    body = " ".join(str(text or "").split())
    return body if len(body) <= width else body[:width - 3] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read the voice daemon's audit log.")
    ap.add_argument("--limit", type=int, default=20,
                    help="how many commands to show (default 20)")
    ap.add_argument("--since", type=int, default=None, metavar="ID",
                    help="rows newer than this id, oldest first")
    ap.add_argument("--command", metavar="ID", help="one command id")
    ap.add_argument("--track", metavar="PATH",
                    help="every row naming this track, relative to the library")
    ap.add_argument("--json", action="store_true",
                    help="one JSON object per line instead of the grouped view")
    args = ap.parse_args(argv)

    if not os.path.exists(DB):
        # Not an error and not silence. An empty log and a missing one are
        # different facts, and the second one usually means the daemon has not
        # run since this was added.
        print(f"no audit log yet at {DB}")
        return 0

    if args.since is not None:
        # A cursor read is a poller's view, so it is not grouped and not reversed.
        events = fetch_events(since_id=args.since, limit=max(args.limit, 1) * 8,
                              track_path=args.track, command_id=args.command)
    else:
        events = recent_events(limit=max(args.limit, 1) * 8,
                               track_path=args.track, command_id=args.command)

    if args.json:
        for e in events:
            print(json.dumps(e, sort_keys=True))
        return 0

    if not events:
        print("the audit log is empty")
        return 0

    if args.since is not None:
        print(render(group_by_command(events)))
        print(f"\ncursor: {events[-1]['id']}")
        return 0

    groups = group_by_command(events)[:max(args.limit, 1)]
    print(render(groups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
