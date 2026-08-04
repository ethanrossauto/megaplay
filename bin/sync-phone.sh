#!/usr/bin/env bash
# Mirror the local library (~/Music) onto the phone, OVERWRITING the target folder so it
# matches the current loadout exactly.
#
# POLICY: this project is SD-CARD-ONLY. We NEVER sync to the phone's internal storage.
# TWO transports, both landing on the SD card:
#   - SD CARD IN PC READER (preferred): the phone's microSD popped into the PC's card reader
#     (or a USB reader), auto-mounted under /run/media/$USER or /media/$USER. Plain filesystem
#     copy - fast and reliable. Used whenever a removable card is present.
#   - SD CARD IN PHONE over MTP (fallback): phone plugged in over USB in "File transfer" mode;
#     we target the phone's "SD card" MTP volume (NOT "Internal shared storage"). Slower/flaky.
#     If no SD card volume is found, we ERROR OUT rather than write to internal storage.
#
# Usage: sync-phone.sh [--dry-run] [--target "<subfolder>"] [--sdcard | --mtp]
#   default target folder is "Music"; default transport is auto (SD card if found, else MTP)
set -u
source "$(dirname "$0")/env.sh"

DRY=""; TARGET="Music"; MODE="auto"; MTP_STORAGE=""; FORCE=""; CHANGED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY="-n"; shift;;
    --target)  TARGET="$2"; shift 2;;
    --sdcard)  MODE="sdcard"; shift;;
    --mtp)     MODE="mtp"; shift;;
    --mtp-storage) MTP_STORAGE="$2"; shift 2;;  # pick which MTP storage (substr match), e.g. "SD card"
    --force-all) FORCE="1"; shift;;             # re-copy every file even if size matches
    --changed)   CHANGED="1"; shift;;           # only files newer than .state/last-sync
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

# --- find an auto-mounted removable card (SD via built-in reader or USB reader) ---
find_sdcard() {
  local base d src
  for base in "/run/media/$USER" "/media/$USER"; do
    [ -d "$base" ] || continue
    for d in "$base"/*/; do
      [ -d "$d" ] || continue
      mountpoint -q "$d" 2>/dev/null || continue
      src="$(findmnt -no SOURCE --target "$d" 2>/dev/null)"
      case "$src" in
        /dev/mmcblk*|/dev/sd*) printf '%s\n' "${d%/}"; return 0;;
      esac
    done
  done
  return 1
}

do_rsync() {  # do_rsync <dest_dir>
  local dest="$1"
  # FAT/MTP don't preserve perms/timestamps. Default compares by SIZE (fast) - but that MISSES
  # tag-only edits that fit in the MP3's ID3 padding (size unchanged). --force-all re-copies
  # every file regardless of size, which is what you want after a metadata/retag pass.
  local compare="--size-only"
  [ -n "$FORCE" ] && compare="--ignore-times"
  # --delete-BEFORE, not plain --delete. rsync's default (--delete-during) can start copying new
  # files before it has freed the stale ones, and this card has no headroom for both: on
  # 2026-07-28 the library was 2.4G, the card 3.8G with 1.8G of now-obsolete flat folders on it -
  # 4.4G of demand on a 3.8G card if the deletes come late. Deleting first costs an extra scan
  # pass (slow over MTP) and buys a sync that cannot run out of space.
  # --changed: copy ONLY files newer than the last sync marker. A retag doesn't change file size,
  # so a normal sync skips it, and --force-all fixes that by re-copying the ENTIRE library - 2.6G,
  # which over MTP is a 15-minute job to push a handful of edited tags. This sends just the edits.
  # It does NOT delete, so use a full sync after adding or removing songs.
  if [ -n "$CHANGED" ]; then
    local marker="$STATE/last-sync" list
    [ -f "$marker" ] || { echo "No $marker yet - run a full sync first."; return 1; }
    list="$(mktemp)"
    ( cd "$MUSIC" && find . -name '*.mp3' -newer "$marker" -printf '%P\n' ) > "$list"
    local n; n=$(wc -l < "$list")
    if [ "$n" -eq 0 ]; then echo "Nothing changed since the last sync."; rm -f "$list"; return 0; fi
    echo "Syncing $n changed file(s) only  ->  $dest/   (no deletions)"
    if [ -n "$DRY" ]; then sed 's/^/  would copy: /' "$list"; rm -f "$list"; return 0; fi
    # Plain cp, not rsync. Over MTP the DATA is fine (~4.5 MB/s measured) - it is rsync's
    # per-file stat/enumeration through the gvfs fuse mount that crawls, and a targeted copy
    # doesn't need any of it. On the card reader this is just as fast either way.
    local i=0 rc=0
    while IFS= read -r rel; do
      mkdir -p "$dest/$(dirname "$rel")"
      cp "$MUSIC/$rel" "$dest/$rel" || rc=1
      i=$((i+1))
      [ $((i % 10)) -eq 0 ] && echo "  $i/$n"
    done < "$list"
    rm -f "$list"
    echo "  $i/$n copied."
    [ $rc -eq 0 ] && touch "$marker"
    return $rc
  fi
  echo "Syncing $MUSIC/  ->  $dest/   (mirror, --delete-before${FORCE:+, --force-all})"
  # Music-only: carry directories + .mp3 and nothing else, so stray files never reach the phone.
  rsync -rv --delete-before --modify-window=2 $compare --no-perms --no-owner --no-group --inplace \
    $DRY --include '*/' --include '*.mp3' --exclude '*' "$MUSIC"/ "$dest"/
  local rc=$?
  # Marker for --changed. Only on a real (non-dry) run.
  [ -z "$DRY" ] && [ $rc -eq 0 ] && touch "$STATE/last-sync"
  return $rc
}

sd=""
if [ "$MODE" != "mtp" ]; then
  sd="$(find_sdcard || true)"
fi

if [ "$MODE" = "sdcard" ] && [ -z "$sd" ]; then
  echo "ERROR: --sdcard requested but no removable card is mounted under /run/media/$USER or /media/$USER."
  echo "Insert the microSD into the reader, wait for it to mount, then rerun."
  exit 1
fi

if [ -n "$sd" ]; then
  # ---- SD CARD path (default when a card is present) ----
  dest="$sd/$TARGET"
  echo "Transport: SD card"
  echo "Card mounted at: $sd"
  echo "Destination: $dest"
  mkdir -p "$dest"
  do_rsync "$dest"; rc=$?
else
  # ---- MTP path (phone over USB) ----
  echo "Transport: MTP (no SD card found)"
  GVFS="/run/user/$(id -u)/gvfs"
  mtp_root() { find "$GVFS" -maxdepth 1 -name 'mtp:host=*' 2>/dev/null | head -1; }
  root="$(mtp_root)"
  if [ -z "$root" ]; then
    echo "Phone not mounted yet - attempting to mount over MTP..."
    dev="$(gio mount -li 2>/dev/null | grep -oE 'mtp://[^ ]+' | head -1)"
    [ -n "$dev" ] && gio mount "$dev" 2>/dev/null
    sleep 2
    root="$(mtp_root)"
  fi
  [ -n "$root" ] || {
    echo "ERROR: no SD card and no MTP phone found."
    echo "Either insert the microSD into the reader, or plug the phone in, UNLOCK it,"
    echo "and set USB mode to 'File transfer' (MTP), then rerun."
    exit 1
  }
  echo "Phone mounted at: $root"
  # HARDCODED POLICY: always target the phone's SD CARD, never internal storage.
  # (Default MTP_STORAGE to the "SD card" volume; explicit --mtp-storage overrides, e.g.
  # --mtp-storage "Internal" if you ever truly need internal storage.)
  [ -n "$MTP_STORAGE" ] || MTP_STORAGE="SD card"
  storage="$(find "$root" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | grep -iF "$MTP_STORAGE" | head -1)"
  if [ -z "$storage" ]; then
    echo "ERROR: no MTP storage matching '$MTP_STORAGE' on the phone."
    echo "Refusing to fall back to internal storage - this project is SD-card-only."
    echo "Insert the microSD (into the phone, or the PC's card reader) and retry."
    echo "Available storages:"; find "$root" -maxdepth 1 -mindepth 1 -type d 2>/dev/null
    exit 1
  fi
  dest="$storage/$TARGET"
  echo "Destination: $dest"
  mkdir -p "$dest" 2>/dev/null || gio mkdir "$dest" 2>/dev/null || true
  do_rsync "$dest"; rc=$?
fi

if [ -n "$DRY" ]; then
  echo "(dry run - nothing changed)"
elif [ "${rc:-1}" -eq 0 ]; then
  echo "Sync complete. The phone's '$TARGET' folder now matches your current loadout."
else
  echo "rsync finished with code $rc (rerun if some files were skipped)."
fi
