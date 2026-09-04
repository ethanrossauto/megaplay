#!/usr/bin/env python3
"""What the daemon hands to mpv is what mpv plays.

The daemon works out an order and a starting track, writes them to a file and loads that
file into the running player. Everything up to the write was covered; nothing checked what
the player then did with it, and on 2026-08-09 the answer turned out to be "reshuffles it".
play.sh starts mpv with --shuffle for whole-library playback, mpv applies that option to
every playlist loaded into the process afterwards, and so "play Hood Rich album" wrote
sixteen tracks in album order, logged "from #1", and played the ninth. The daemon was right
and was overruled by the player.

That failure is invisible from either side on its own, which is what this group is for: a
real mpv, started the way play.sh starts it, asked for a real load, and then asked what it
actually holds. It makes no sound (--ao=null) and registers no MPRIS player
(--no-config --load-scripts=no), so it cannot disturb anything already playing.

The audio is generated here and deleted with the temp directory, as T4's is: thirty seconds
of silence, long enough that nothing advances mid-assertion.
"""
import importlib.util
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="playback-test-")
SOCK = os.path.join(TMP, "mpv.sock")
LIST = os.path.join(TMP, "playlist.txt")

_spec = importlib.util.spec_from_file_location(
    "voice_under_test", os.path.join(ROOT, "bin", "voice.py"))
voice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voice)          # safe: main() is behind __main__
voice.MPV_SOCK, voice.PLAYLIST_FILE = SOCK, LIST
voice.log = lambda message: print(f"        {message}")

failures = []


def check(name, got, want):
    if got == want:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name}: got {got!r}, wanted {want!r}")
        print(f"  FAIL  {name}: got {got!r}, wanted {want!r}")


def tracks(count=6):
    """Silent mp3s with predictable names, so an order can be read at a glance."""
    made = []
    for n in range(1, count + 1):
        path = os.path.join(TMP, f"{n:02d} track.mp3")
        subprocess.run(
            ["ffmpeg", "-loglevel", "quiet", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=44100:cl=mono", "-t", "30", "-q:a", "9", path],
            stdin=subprocess.DEVNULL, check=True)
        made.append(path)
    return made


def mpv_playlist():
    """The order mpv actually holds, which is the whole question here."""
    reply = voice.mpv("get_property", "playlist")
    return [entry["filename"] for entry in reply["data"]] if voice.mpv_ok(reply) else None


def start_player():
    """mpv as play.sh starts it - the --shuffle is the point, not an accident."""
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--ao=null", "--idle=yes", "--shuffle",
         "--no-config", "--load-scripts=no", "--really-quiet",
         "--loop-playlist=inf", f"--input-ipc-server={SOCK}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        time.sleep(0.1)
        if os.path.exists(SOCK) and voice.mpv("get_property", "idle-active") is not None:
            return proc
    proc.terminate()
    print("  FAIL  mpv never opened its IPC socket")
    failures.append("mpv never came up")
    return proc


player = start_player()
try:
    if not failures:
        paths = tracks()

        # The regression. An album is asked for in album order from the first track, and the
        # player it is handed to was started with --shuffle.
        voice.load_and_play(paths, 0)
        check("the order mpv holds is the order the daemon wrote", mpv_playlist(), paths)
        check("it starts on the first track", voice.mpv_prop("playlist-pos"), 0)
        check("and it is playing", voice.mpv_paused(), False)

        # Same load a second time, from a player sitting somewhere else in the list, which is
        # what "play this album from the beginning" means after any skipping.
        voice.mpv("set_property", "playlist-pos", 4)
        time.sleep(0.3)
        voice.load_and_play(paths, 0)
        check("asking again from a player mid-list still starts at the top",
              voice.mpv_prop("playlist-pos"), 0)

        # A start point other than the first track, from a paused player, since the daemon
        # pauses for every capture and the unpause has its own race.
        voice.mpv_set_pause(True)
        time.sleep(0.2)
        voice.load_and_play(paths, 3)
        check("a start point other than the first is honoured",
              voice.mpv_prop("playlist-pos"), 3)
        check("and a paused player is playing again", voice.mpv_paused(), False)

        # The other half of turning mpv's shuffle off: shuffle must still be available. The
        # daemon shuffles the paths itself in play_folder, so the order it wrote is the order
        # it wants, random or not.
        mixed = paths[:]
        random.Random(11).shuffle(mixed)
        voice.load_and_play(mixed, 0)
        check("a list the daemon shuffled itself arrives in that order", mpv_playlist(), mixed)
finally:
    player.terminate()
    try:
        player.wait(timeout=5)
    except subprocess.TimeoutExpired:
        player.kill()
    shutil.rmtree(TMP, ignore_errors=True)

if failures:
    print()
    for f in failures:
        print(f"FAIL  {f}")
    sys.exit(1)

print("        the daemon's order and starting track survive the handover to mpv")
