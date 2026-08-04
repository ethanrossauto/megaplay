#!/usr/bin/env python3
"""Voice control for the library, triggered by a volume down-up gesture on a
bluetooth headset.

Why this trigger, when two more obvious ones exist: the play/pause double-click
is unusable, because headset firmware collapses it into a single AVRCP "next
track" before it ever reaches the PC, leaving nothing to detect. Raw key events
are out too on any Wayland session, since the compositor owns /dev/input and a
background reader sees nothing regardless of permissions. Volume down-then-up is
what survived. It reaches the PC as two BlueZ property changes, it returns to
the exact starting value so there is no drift, and nobody makes that gesture by
accident, because real volume adjustment is repeated presses in ONE direction.

Measured over six gestures: down->up took 0.978-1.234s, while the gap between
separate gestures never fell below 3.220s. GESTURE_WINDOW sits in that gap.
Known false positive: a multi-step adjustment that ends with one step back up
looks identical. Not yet fixed.

Flow: gesture -> pause -> listen -> transcribe -> ask Claude -> run it -> resume.

Dispatch runs through the Claude Code CLI (`claude -p`), which authenticates
with your existing subscription rather than an API key.

Run:  bin/voice.sh          (wrapper; starts the ASR server too)
Test: bin/voice.py --selftest   (verifies the gesture path without any audio)
"""
import json
import os
import socket
import subprocess
import sys
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
# A dispatch decision should be quick. If it blows this, something is
# wrong with the request, not with how hard the model is trying.
CLAUDE_TIMEOUT = float(os.environ.get("VOICE_CLAUDE_TIMEOUT", "60"))
# Picking a track from a list is not hard reasoning. The CLI default is
# tuned for coding work and spent 90s+ on a lyric lookup before this.
EFFORT = os.environ.get("VOICE_EFFORT", "low")

# Two presses further apart than this are two separate adjustments, not a gesture.
GESTURE_WINDOW = float(os.environ.get("VOICE_GESTURE_WINDOW", "2.0"))

SELFTEST = "--selftest" in sys.argv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# mpv control
# --------------------------------------------------------------------------

def mpv(*command):
    """Send one IPC command to mpv. Returns the parsed reply, or None."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(MPV_SOCK)
        s.sendall((json.dumps({"command": list(command)}) + "\n").encode())
        data = s.recv(8192).decode(errors="replace")
        s.close()
        for line in data.splitlines():
            reply = json.loads(line)
            if "error" in reply:          # skip async event lines
                return reply
    except (OSError, ValueError):
        return None
    return None


def mpv_ok(reply):
    """True only if mpv actually answered and accepted the command.

    mpv() returns None when the socket is dead, which is NOT the same as a
    command that ran and failed - both must be distinguishable from success.
    """
    return bool(reply) and reply.get("error") == "success"


def mpv_paused():
    reply = mpv("get_property", "pause")
    return bool(reply and reply.get("data"))


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


def annotated_library():
    """Library lines tagged with their type, so the model can pick an order."""
    kinds = entity_types()
    return [f"{name}  [{kinds.get(name, 'playlist')}]" for name in library()]


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

Schema - pick exactly one action:
  {"action":"play","target":"<F>","order":"<O>","start":"<S>"}
  {"action":"play_tracks","tracks":[<n>,...],"note":"<what you picked>"}
  {"action":"next"}                        skip forward
  {"action":"prev"}                        skip back
  {"action":"pause"}                       stop playback
  {"action":"resume"}                      resume playback
  {"action":"none","reason":"<why>"}       request is unclear or not about music

Use "play_tracks" whenever the request is about SONGS rather than a whole
folder - a specific song, a mood, a run of tracks, "and then the rest of the
album". Give the track numbers from the catalogue, IN THE ORDER they should
play. They may come from different folders. Put what you chose and why in
"note" - it is logged, not spoken, so be brief and concrete.

Examples of the shape (numbers here are illustrative):
  "that song where he says <lyric>, then the rest of the album, no shuffle"
      -> {"action":"play_tracks","tracks":[141,142,143,144],"note":"Fireman then rest of Tha Carter II"}
  "something gangsta from the 90s"
      -> {"action":"play_tracks","tracks":[12,88,203],"note":"90s gangsta rap picks"}

If a lyric or description doesn't clearly match anything you can see, say so
with "none" rather than guessing - a wrong song is worse than no song.

Fields for "play":
  target   EXACT copy of a library entry below, without the [type] tag.
  order    "album"    track order, start to finish
           "shuffle"  random order
           "default"  let the player choose from the entry's type - albums play
                      in order, playlists shuffle. USE THIS unless the request
                      actually asked for an order ("shuffle it", "in order").
  start    "beginning" | "middle" | "random" | a 1-based track number
           Use "beginning" unless the request said otherwise. "halfway through"
           is "middle"; "the third song" is 3; "anywhere" is "random".

Capture the WHOLE request. If it names a position or an order, put it in the
fields - do not drop it.

The input is ALWAYS someone speaking to a music player, transcribed by an
imperfect speech recogniser. It is never a request about anything else, however
it reads. If the words look like an unrelated topic, that is a mishearing.

Rules:
- Read the transcript PHONETICALLY against the library. It may be nonsense as
  English while clearly matching an entry by sound. "lay the card or two" is
  "play Tha Carter II". "coloring look" is "Coloring Book". Resolve these.
- "target" must match a library entry character for character. Never invent one.
- If, after allowing for mishearings, the request names music that is genuinely
  not in the library, use "none" and say so. Do not substitute something similar
  just because it is the same genre.
- If the request is ambiguous between two entries, pick the closer match.

Folders - the only valid "target" values:
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
        import random
        return random.randrange(count)
    return 0


def ask_claude(utterance):
    """Send the transcript to Claude Code in print mode. Returns a dict or None.

    Uses `claude -p`, which authenticates with the logged-in subscription - no
    API key, no per-command billing.
    """
    prompt = (SYSTEM_PROMPT
              .replace("{LIBRARY}", "\n".join(annotated_library()))
              .replace("{CATALOG}", catalog_text()))
    try:
        proc = subprocess.run(
            ["claude", "-p", utterance,
             "--append-system-prompt", prompt,
             "--output-format", "json",
             "--effort", EFFORT,
             "--model", MODEL],
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
        import random
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
        if target not in library():
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
        log(f"{len(paths)} tracks - {cmd.get('note', '(no note)')}")
        return load_and_play(paths, 0)
    if action == "none":
        log(f"no action: {cmd.get('reason', '(no reason given)')}")
        return False

    log(f"unrecognised action: {cmd!r}")
    return False


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------

def listen():
    """Ask the warm ASR server for one utterance. Returns text (possibly empty)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(40)
        s.connect(ASR_SOCK)
        s.sendall(b"LISTEN\n")
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
        log("listening...")

        text = listen()
        if not text:
            log("heard nothing")
            if not was_paused:
                mpv_set_pause(False)
            return
        log(f"heard: {text!r}")

        cmd = ask_claude(text)
        log(f"command: {cmd!r}")
        handled = execute(cmd)

        # Only restore if the command didn't decide playback state for us.
        if not handled and not was_paused:
            mpv_set_pause(False)
    finally:
        busy.release()


# --------------------------------------------------------------------------
# Gesture detection
# --------------------------------------------------------------------------

class Gesture:
    """Detect DOWN-then-UP-back-to-start within GESTURE_WINDOW."""

    def __init__(self, on_trigger):
        self.on_trigger = on_trigger
        self.prev = None          # last volume seen
        self.down_from = None     # value we dropped from
        self.down_at = 0.0

    def feed(self, vol, now):
        prev, self.prev = self.prev, vol
        if prev is None:
            return

        if vol < prev:                                   # a DOWN press
            self.down_from, self.down_at = prev, now
            return

        if vol > prev and self.down_from is not None:
            gap = now - self.down_at
            # Require the return to land back where it started: a genuine
            # gesture is symmetric, while a real adjustment usually isn't.
            if gap <= GESTURE_WINDOW and vol == self.down_from:
                self.down_from = None
                log(f"gesture detected ({gap:.3f}s)")
                self.on_trigger()
                return
            self.down_from = None


def main():
    trigger = (lambda: log("SELFTEST: gesture detected - path works")) if SELFTEST \
        else (lambda: threading.Thread(target=handle_gesture, daemon=True).start())
    gesture = Gesture(trigger)

    def on_signal(_conn, _sender, path, _iface, _sig, params, *_):
        iface, changed, _invalidated = params.unpack()
        if iface != "org.bluez.MediaTransport1" or "Volume" not in changed:
            return
        gesture.feed(int(changed["Volume"]), time.monotonic())

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    # Subscribe with no object path filter: the transport path (…/fd0) is
    # recreated on every headset reconnect, so pinning it would silently stop
    # working the next time the headset drops.
    bus.signal_subscribe(
        "org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged",
        None, None, Gio.DBusSignalFlags.NONE, on_signal, None)

    if SELFTEST:
        log("SELFTEST: listening for one volume down-up gesture (Ctrl-C to stop)")
    else:
        log(f"ready - volume down-up to talk (window {GESTURE_WINDOW}s, model {MODEL})")
        log(f"library: {len(library())} folders")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
