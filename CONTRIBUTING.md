# Contributing

Bug reports, fixes and improvements are welcome. A few things are settled in advance so nobody
spends an evening on a pull request that was never going to be merged.

## Not accepted, ever

**DRM circumvention.** megaplay does not decrypt protected streams and will not start.
Everything here works either on files already on your disk or through spotdl, which reads
Spotify *metadata* and gets audio from YouTube Music. That boundary is deliberate and it is not
up for discussion.

**Bundled audio.** No MP3s, no test fixtures with real music in them, not even a short clip.
The `.gitignore` and a `pre-push` hook both block audio extensions. If your patch needs a test
file, generate silence with ffmpeg at test time.

**A registered Spotify developer app.** spotdl ships its own public anonymous credentials and
that is what this uses. Wiring in credentials from an app you registered would put the project
under the Spotify Developer Terms for no benefit, so patches doing that will be declined.

**Framing changes that market this as a way to get music for free.** It is a library manager.
That is not marketing spin, it is what most of the code does: tagging, dedupe, album repair,
phone sync, playback and voice control all operate on whatever MP3s are on disk and never
invoke spotdl at all.

## Welcome

- Bug fixes, especially anything portability related. Development happens on one Ubuntu box and
  it shows.
- Support for other players, other phones, other transports.
- A local speech-to-text or local-model path for voice control, so it works without a Claude
  subscription.
- Better matching in the explicit-twin logic, which has been wrong in interesting ways before.
- Documentation, including telling me where the README assumes knowledge it should not.

## House rules for patches

- **Run `bash tests/run.sh` before opening a pull request.** It needs no network and takes about
  a minute. CI runs the same script, so a green run locally is a green run there, and a red one
  tells you what to fix without waiting on a runner.
- Shell is bash with `set -u`. Keep it POSIX-ish where it is free to do so.
- **Never monitor a background job with `pgrep -f <script name>`.** It matches the monitor's own
  command line, so the loop never sees the job end and spins forever. This has bitten this
  project twice, once for 1h43m. Use a PID and `kill -0`.
- **A check that cannot tell "nothing wrong" from "I could not look" is not a check.** Report
  what was actually covered, not just a verdict, and fail closed.
- Comments should explain why, especially where the obvious approach was tried and failed. A
  good chunk of this codebase is a record of things that did not work, and that is the useful
  part.
