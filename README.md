# megaplay

A local music library manager for Linux, with voice control.

It keeps a curated set of MP3s on disk, tags them so a phone groups them the way you actually
want, mirrors them to an SD card, and lets you talk to the whole thing:

> *"play me something gangsta from the 90s, but skip anything mainstream"*
>
> *"put on The Weeknd, nothing off Trilogy"*
>
> *"shuffle the whole library but no Lil Wayne"*

That works because the model can see every track you own, not just folder names. More on why
that matters below.

**megaplay hosts and distributes no audio.** Importing from a Spotify playlist is one input
path, handled by [spotdl](https://github.com/spotDL/spotify-downloader), which reads playlist
*metadata* from Spotify's API and fetches audio from YouTube Music. No Spotify DRM is involved
at any point. Everything past the import step (tagging, dedupe, album repair, phone sync,
playback, voice control) works on whatever MP3s are already on your disk, whatever their
origin.

---

## Voice control

Two systemd user services: a warm Whisper server holding the model resident, and a gesture
daemon. Flow is **gesture, pause, listen, transcribe, ask Claude, play, resume.**

### The trigger is volume down then up, within 2 seconds

Not a hotkey. You are wearing headphones and your laptop is across the room, which is the whole
point. Two more obvious triggers were tried first and are dead ends, so save yourself the
afternoon:

- **Play/pause double-click is impossible.** Headset firmware collapses it into a single AVRCP
  "next track" before it ever leaves the earcup. The PC receives one `Next` event and cannot
  know you pressed twice.
- **Raw key events are impossible on Wayland.** The compositor owns `/dev/input`, so a
  background reader gets nothing no matter what permissions it has. `sudo` does not help.

Volume down-up survives because it arrives as two BlueZ property changes, it returns to the
exact starting value so there is no drift, and nobody makes that gesture by accident. Real
volume adjustment is repeated presses in one direction.

Measured over six gestures: down-then-up took 0.978s to 1.234s, while the gap between two
separate gestures never fell below 3.220s. The 2 second window sits in that gap.

Known false positive: a multi-step volume adjustment that happens to end with one step back up
looks identical. Not yet fixed.

### Why it can answer a taste question

The daemon numbers **every track in the library** into the prompt, roughly 15k tokens for a
750 song collection, and the model replies with those numbers. Integers rather than titles
because an integer either indexes a real file or is rejected outright, whereas a near-miss
title fails silently three steps later.

This is the part that makes the feature work at all. With only folder names visible it could
not answer "something gangsta from the 90s", and it once wrongly reported having no Eminem
while four Eminem tracks sat inside the decade compilations the whole time.

New music is picked up immediately with no restart. The catalogue is cached but invalidated by
a change signature (file count plus newest mtime), which costs 2.4ms to check against 410ms to
rebuild. There is deliberately no TTL: a time-based cache here would be simultaneously stale
and wasteful.

### It runs on your Claude subscription, not the API

Dispatch shells out to `claude -p`, the Claude Code CLI, which authenticates the same way an
interactive session does. There is no API key to configure and nothing charged per command.

Claude Code is included in every paid Claude plan, Pro included, and terminal usage draws from
the same limit pool as your chats. Which models your account can reach depends on your plan and
changes over time, so run `/model` in Claude Code to see what you have.

**The default here is Sonnet.** Every command ships that whole catalogue, so an Opus default
would quietly spend a Pro plan's budget picking songs. Override with `VOICE_CLAUDE_MODEL` if
your plan has room.

Latency is roughly 20 seconds end to end, about half transcription and half dispatch. That is
the honest number. `--effort low` is doing real work there: the CLI default is tuned for coding
and spent 90 seconds on a single lyric lookup before it was added.

### Accuracy is a vocabulary problem, not a model size problem

`medium.en` still heard "Tha Carter II" as "the card or two". What fixed it was seeding
Whisper's `initial_prompt` with the actual folder and album names from your library, which
makes them candidate transcriptions instead of unexpected noise. Bumping the model size does
not fix proper nouns.

```
bin/voice.sh install     # write and enable both services (once)
bin/voice.sh status      # are they up
bin/voice.sh logs        # follow both logs
bin/voice.sh selftest    # exercise the gesture path alone, no audio, no ASR
```

---

## Megaplaylists

**A megaplaylist is several source playlists or albums kept as separate folders on disk, that
present to any player as one single playlist.**

The trick is nesting plus a shared ARTIST tag. On disk you get
`~/Music/<Parent>/<Source>/*.mp3`, and every song underneath carries the parent's name as its
ARTIST. Players and phones group by artist, so you see one playlist. Meanwhile the registry
keeps one row per source, so each source keeps its own ID, batch size and offset, and you can
still refill them independently.

You get the listening experience of one big playlist without giving up per-source fetching.

Overlapping sources would normally mean duplicates, since each folder is an independent
download. Two layers handle it, both automatic:

1. **Before downloading**, a skip file is seeded for every track a sibling folder already
   holds, so those are never fetched at all.
2. **After downloading**, a dedupe pass reports anything that got through under a near-miss
   filename. It reports rather than deletes; run it with `--apply` to actually remove, and it
   keeps the copy in the smallest folder so deletions spread out.

---

## Tagging: one scheme, so the phone sorts the way you want

Every downloaded song is retagged:

| Field | Value |
|---|---|
| TITLE | `<original title> - <original artist>` |
| ARTIST | the playlist name (the parent, for a megaplaylist) |
| ALBUM | left alone, real album preserved |

Putting the playlist name in ARTIST is the whole point. Phone music apps group by artist, so
this is what makes a megaplaylist look like one playlist on the device, and it is visible in
GNOME's media widget and any file manager too.

It is idempotent. Each song gets a hidden `LOADOUT_TAGGED` marker, and a marked song is only
UTF-8 normalised on re-runs, never re-titled, so the artist never gets appended twice.

**One album, one name, one cover.** A separate pass collapses every spelling of an album onto
the shortest variant with edition brackets stripped, and unifies cover art within it, because
scrolling past four spellings of the same album on a phone is miserable. Two traps already paid
for and guarded against: an album can *be* a year (stripping years emptied the key for Dr.
Dre's `2001`), and brackets nest (`[International Version (Explicit)]` broke a naive regex).

---

## Explicit versions

Curated playlists list clean versions surprisingly often, and there is no spotdl flag for this.
Only `--skip-explicit`, which is the opposite of what you want.

The fix runs automatically after every download: find the clean track's explicit twin on
Spotify and swap it in. Matching requires the same normalised title and the same **primary**
artist, and rejects `'s Version` re-recordings, because a "D-Money" cover of Soulja Boy once
won on a credits-only match and a re-recorded MILKSHAKE once replaced the real Kelis track.
Verdicts are cached, so repeat runs are near instant. Only about one in six clean-flagged
tracks has a twin at all; the rest simply have nothing to censor.

🔥 **The dangerous part, documented so nobody removes it.** The twin usually has the same
filename, so spotdl's default skip behaviour silently refuses to write, and 8 of 13 swaps
no-opped before `--overwrite force` was added. But force makes spotdl **delete the file before
downloading**, so a yt-dlp failure destroys the song. That happened, and two tracks had to be
re-fetched by hand. Every target is now copied to a backup directory first and restored if the
swap does not land. Do not remove that step.

---

## Phone sync

**SD card only, by design.** Syncing to internal storage creates a second library that then
drifts, so the script refuses rather than falls back.

Two transports, both landing on the card:

- **Card in a PC reader (fast).** Auto-mounted under `/run/media/$USER` or `/media/$USER`,
  detected as a removable device. Plain filesystem copy.
- **Card in the phone over MTP (slow).** Plug in, unlock, set USB to File transfer. The script
  targets the phone's "SD card" volume specifically. If it cannot find one it errors out; it
  will not quietly use internal storage, which sorts first alphabetically and used to get
  picked by accident.

`rsync --delete` compares by **size**, since FAT and MTP do not keep reliable timestamps.

⚠️ **A tag-only edit does not change file size**, so after any retag or re-art pass you need
`--force-all` or the phone keeps the old tags. This is the single easiest thing to get wrong.

⚠️ **FAT32 forbids `? * : < > | " \ /` in filenames.** An album whose real title contains one
downloads fine on ext4 and then cannot be copied across. Rename the folder and drop the
character; the ALBUM tag inside each file keeps the real spelling, so the phone still displays
it correctly. Find offenders with:

```sh
find ~/Music -regex '.*[?*:<>|"\\].*'
```

---

## Install

Needs Linux with systemd, Python 3, ffmpeg, mpv, and rsync.

```sh
git clone https://github.com/ethanrossauto/megaplay
cd megaplay
./setup.sh
```

`setup.sh` checks for the system tools and names the exact package line if any are missing,
creates a venv for spotdl, and creates a second venv for faster-whisper. **The Whisper venv is
roughly 430 MB**, so if you only want the library manager:

```sh
./setup.sh --no-voice
```

If you already have a venv with faster-whisper, point at it instead of building another. Put
machine-specific settings in `bin/env.local.sh`, which is gitignored and sourced automatically:

```sh
VOICE_ASR_PY="$HOME/some/existing/venv/bin/python"
VOICE_CLAUDE_MODEL="claude-opus-5"
MEGAPLAY_MUSIC="/mnt/media/music"
```

Voice control additionally needs the Claude Code CLI, a paid Claude plan, a bluetooth headset
with volume buttons, and `mpv-mpris` for media key support.

⚠️ Re-run `bin/voice.sh install` after changing anything in `env.local.sh`. systemd launches
the services directly and inherits nothing from your shell, so the values are baked into the
unit files at install time.

---

## Commands

```
bin/status.sh                          list playlists: songs on disk, cap, offset, index
bin/add-playlist.sh <url> [cap] [name] add a playlist or album (albums default to their track count)
bin/grab.sh <name>                     (re)download the current batch, then tag, explicit, dedupe
bin/more.sh <name>                     drop the current songs and fetch the NEXT batch
bin/delete-playlist.sh <name>          remove the folder and the registry row
bin/autopull.sh                        work through a queue of sources one at a time
bin/watch.sh <name>                    live progress of an in-flight download
bin/stop.sh                            stop downloads
bin/tag.sh <name>|--all                retag to the scheme above
bin/dedupe.sh [parent] [--apply]       find and remove cross-folder duplicates
bin/album-check.sh [scope] [--apply]   collapse album spellings, unify cover art
bin/explicit.sh <name>|--all [--fix]   find censored tracks, swap in explicit twins
bin/sync-phone.sh [--dry-run] [--force-all]   mirror the library to the SD card
bin/play.sh [name]                     shuffle-play, detached, media keys work
bin/voice.sh install|start|stop|status|logs|selftest
```

Nest a source under a megaplaylist by passing the name with a parent prefix:

```sh
bin/add-playlist.sh "<spotify url>" "" "90s Rap/Best Of"
```

### The registry

`playlists.tsv` is tab separated, one row per source, with a header row. `add-playlist.sh`
maintains it, so you rarely edit it by hand.

```
name              playlist_id                             cap   offset
90s Rap/Best Of   37i9dQZF1DX...                          130   0
Lil Wayne/Tha Carter II   https://open.spotify.com/album/...   22    0
```

`cap` is the batch size and `offset` is how far into the source the current batch starts, which
is what lets `more.sh` swap in the next batch. A **playlist stores a bare ID and an album stores
its full URL**: that difference is how the scripts tell them apart, so do not tidy an album down
to an ID or every lookup will hit the wrong endpoint.

---

## Honest limitations

- **Linux and systemd only.** The voice services are systemd user units and the gesture
  listener talks to BlueZ over D-Bus. None of that is portable to macOS or Windows as written.
- **Voice control needs a paid Claude plan.** There is no free tier path and no local-model
  fallback yet.
- **~20 seconds per voice command.** Fine for "put something on", too slow to feel like a
  remote control.
- **The gesture has a known false positive** (see above).
- **YouTube throttles long download runs.** yt-dlp starts failing across unrelated tracks after
  a while. It is the IP being throttled, not the songs, and the same tracks succeed later.
  Stopping and resuming is safe: nothing partial is left behind and a re-run fills only gaps.
- **Some tracks are simply unavailable.** A `LookupError: No results found` means YouTube Music
  does not carry it, and re-running will never fix that. A hand-picked URL is the only route.
- **spotdl uses shared anonymous Spotify credentials.** If those are ever revoked upstream,
  imports break here too until spotdl ships new ones.

---

## Credits

megaplay is a thin orchestration layer. Nearly all the hard work belongs to other people:

| Project | Licence | What it does here |
|---|---|---|
| [spotdl](https://github.com/spotDL/spotify-downloader) | MIT | Reads Spotify playlist and album metadata, drives the downloads, embeds tags and cover art |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | Fetches the actual audio, underneath spotdl |
| [FFmpeg](https://ffmpeg.org/) | LGPL / GPL | Audio conversion, and microphone capture for voice commands |
| [mpv](https://mpv.io/) | GPL-2.0+ / LGPL | Playback |
| [mpv-mpris](https://github.com/hoyon/mpv-mpris) | MIT | Puts mpv on MPRIS, which is why headset and notification-bar controls work |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | Speech recognition |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT | Inference engine underneath faster-whisper |
| [OpenAI Whisper](https://github.com/openai/whisper) | MIT | The speech recognition models themselves |
| [mutagen](https://github.com/quodlibet/mutagen) | **GPL-2.0-or-later** | All ID3 tag reading and writing |
| [Claude Code](https://claude.com/product/claude-code) | proprietary | Turns a spoken sentence into a track selection |
| rsync, BlueZ, PyGObject, NumPy | various | Phone sync, the gesture signal, D-Bus, audio maths |

**None of these are bundled or redistributed here.** `setup.sh` installs them from PyPI and
your distro's package manager, onto your machine. That matters for more than tidiness: megaplay
ships no third-party code, so no third-party licence terms attach to it, which is why an MIT
licence on this repo is straightforward even though it calls GPL software like mutagen and mpv.
Calling a program, or importing a library you installed yourself, is not distributing it.

If you use this, go star spotdl and yt-dlp. They are the projects doing the work, and both have
taken real legal pressure for existing.

---

## Scope

megaplay is a library manager. It is not a Spotify client, it does not circumvent DRM, it does
not host or distribute audio, and it will not be extended to do any of those things. See
[CONTRIBUTING.md](CONTRIBUTING.md).

What you do with the tool is your business and your jurisdiction's. Downloading music you have
not paid for may well be against the terms of service of the platform it came from, and may be
unlawful where you live. That is worth knowing before you use it.

## Licence

MIT. See [LICENSE](LICENSE).
