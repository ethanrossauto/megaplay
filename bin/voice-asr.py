#!/usr/bin/env python3
"""Warm ASR server for short voice commands.

Holds the Whisper model resident so a command costs one transcription rather
than a model load. Tuned for commands, not dictation: the tail-silence numbers
below are far tighter than a transcription tool would use, because you know
what you are going to say before you press the button, and a 5s pause after
"skip this" feels broken.

Speech recognition is faster-whisper (https://github.com/SYSTRAN/faster-whisper,
MIT), a CTranslate2 reimplementation of OpenAI's Whisper. Audio capture is
ffmpeg. Neither is vendored here; setup.sh installs faster-whisper into a venv.

Run it under a python that has faster-whisper and numpy:
    /path/to/venv/bin/python bin/voice-asr.py /tmp/spotify-voice-asr.sock

Protocol: client sends "LISTEN\\n", server records one utterance and replies with
the transcript plus a newline. One turn at a time, which is all a person can do.
"""
import os
import socket
import subprocess
import sys
import threading
import time

import numpy as np
from faster_whisper import WhisperModel

SOCK = sys.argv[1] if len(sys.argv) > 1 else "/tmp/spotify-voice-asr.sock"
# ffmpeg capture source. "default" follows the system default, which is usually
# the headset you are already wearing. Override with VOICE_SOURCE to pin a
# specific device (a laptop's built-in mic, say).
SOURCE = os.environ.get("VOICE_SOURCE", "default")
# medium.en over small.en: on a bluetooth headset mic, small.en misheard "play
# me" as "please film". medium.en is a bigger first-run download and a slower
# transcription; drop back with VOICE_MODEL=small.en if your mic is cleaner or
# your machine is slower. Model size does not fix proper nouns, though: see
# vocabulary() below for the thing that actually does.
MODEL = os.environ.get("VOICE_MODEL", "medium.en")

SR = 16000
FRAME = 480                 # 30 ms at 16 kHz
FBYTES = FRAME * 2          # s16le mono
FDUR = FRAME / SR

# --- the numbers that make this a *command* recogniser -----------------------
# A command is short and you know what you're going to say before you press the
# button, so the tail silence can be far tighter than an interview answer's.
SILENCE_END = 1.2           # quiet this long after speech = you're done
PRE_ONSET_WAIT = 6.0        # give up if you never start talking
MAX_AFTER_ONSET = 12.0      # hard stop; no music command is longer than this
ONSET_FRAMES = 4            # consecutive loud frames before we call it speech
PREROLL = 8                 # frames kept from before onset, so we don't clip "play"


MUSIC = os.environ.get("MEGAPLAY_MUSIC") or os.path.join(os.path.expanduser("~"), "Music")


def rms(buf):
    a = np.frombuffer(buf, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def vocabulary():
    """Bias Whisper toward the names that actually exist in the library.

    Model size does NOT fix this: medium.en still produced "the card or two"
    for "Tha Carter II" (2026-08-03), because no general model emits a stylised
    proper noun it has no reason to expect. initial_prompt is decoding context -
    seeing these names makes them candidate transcriptions.
    """
    names = {"play", "skip", "pause", "next", "previous", "album", "playlist"}
    if os.path.isdir(MUSIC):
        for parent in os.listdir(MUSIC):
            ppath = os.path.join(MUSIC, parent)
            if not os.path.isdir(ppath):
                continue
            names.add(parent)
            for sub in os.listdir(ppath):
                if os.path.isdir(os.path.join(ppath, sub)):
                    names.add(sub)
    # Plain comma-separated prose: initial_prompt is treated as preceding
    # transcript text, so it should read like something a person would say.
    return "Music library: " + ", ".join(sorted(names)) + "."


def record_one():
    """Capture a single utterance.

    Returns (pcm_bytes, reason). reason is "ok", "no_speech" (you never started
    talking), or "stream_died" (ffmpeg hit EOF early - see the caller's retry).
    """
    # stderr is NOT discarded: it inherits the server's, so ffmpeg's own error
    # text lands in voice-asr.log. Swallowing it once already cost a debugging
    # round on 2026-08-03 - a capture that fails silently is indistinguishable
    # from a mic that heard nothing.
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "pulse", "-i", SOURCE, "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, bufsize=FBYTES)
    try:
        # Calibrate against this moment's noise floor rather than a fixed
        # threshold - a Bluetooth mic and a laptop mic sit at very different levels.
        noise = []
        for _ in range(10):
            b = ff.stdout.read(FBYTES)
            if len(b) < FBYTES:
                return b"", "stream_died"
            noise.append(rms(b))
        floor = float(np.median(noise)) if noise else 30.0
        onset_th = max(floor * 3.5, 180.0)
        sil_th = onset_th * 0.7

        ring, kept = [], []
        onset = False
        loud_run = 0
        quiet_for = 0.0
        elapsed = 0.0

        while True:
            b = ff.stdout.read(FBYTES)
            if len(b) < FBYTES:
                # EOF. If we already had speech, keep what we got; if not, the
                # capture died under us and the turn is worth retrying.
                return (b"".join(kept), "ok") if onset else (b"", "stream_died")
            elapsed += FDUR
            level = rms(b)

            if not onset:
                ring.append(b)
                if len(ring) > PREROLL:
                    ring.pop(0)
                loud_run = loud_run + 1 if level > onset_th else 0
                if loud_run >= ONSET_FRAMES:
                    onset = True
                    kept = list(ring)          # include the pre-roll
                elif elapsed > PRE_ONSET_WAIT:
                    return b"", "no_speech"    # you never started talking
                continue

            kept.append(b)
            quiet_for = quiet_for + FDUR if level < sil_th else 0.0
            if quiet_for >= SILENCE_END or elapsed > MAX_AFTER_ONSET + PRE_ONSET_WAIT:
                break

        return b"".join(kept), "ok"
    finally:
        ff.kill()
        ff.wait(timeout=2)


def main():
    print(f"loading {MODEL}...", flush=True)
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    # One warm pass so the first real command doesn't pay lazy-init cost.
    model.transcribe(np.zeros(SR, dtype=np.float32), beam_size=1)
    print("ready", flush=True)

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(4)

    lock = threading.Lock()     # one mic, one turn at a time
    while True:
        conn, _ = srv.accept()
        try:
            req = conn.recv(64).decode(errors="replace").strip()
            if not req.startswith("LISTEN"):
                conn.sendall(b"\n")
                continue
            with lock:
                pcm, reason = record_one()
                # A Bluetooth source can be torn down and recreated the first
                # time something opens it, which kills the in-flight capture.
                # One retry costs under a second and turns a dead turn into a
                # working one; without it the first command after idle fails.
                if reason == "stream_died":
                    print("capture died early - retrying once", flush=True)
                    time.sleep(0.6)
                    pcm, reason = record_one()
            if not pcm:
                print(f"no audio ({reason})", flush=True)
                conn.sendall(b"\n")
                continue
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            # Recomputed per turn so a newly added album is recognisable
            # immediately, without restarting the server.
            segments, _ = model.transcribe(
                audio, beam_size=1, language="en", initial_prompt=vocabulary())
            text = " ".join(s.text.strip() for s in segments).strip()
            conn.sendall((text + "\n").encode())
        except Exception as exc:                      # never let one turn kill the server
            print(f"turn failed: {exc!r}", flush=True)
            try:
                conn.sendall(b"\n")
            except OSError:
                pass
        finally:
            conn.close()


if __name__ == "__main__":
    main()
