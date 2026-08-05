#!/usr/bin/env bash
# Show every registered playlist: songs on disk, cap, current batch offset, index size.
set -u
source "$(dirname "$0")/env.sh"
printf '%-44s %6s %5s %7s %8s\n' "PLAYLIST" "ONDISK" "CAP" "OFFSET" "INDEXED"
printf '%-44s %6s %5s %7s %8s\n' "--------" "------" "---" "------" "-------"
total=0
prev_parent=""
# The registry's four columns are all named so the layout reads off this line; playlist_id
# is not needed here.
# shellcheck disable=SC2034
while IFS=$'\t' read -r name pid cap off; do
  [ "$name" = "name" ] && continue
  [ -z "$name" ] && continue
  cnt=$(folder_count "$name")
  total=$((total+cnt))
  idxn="-"
  [ -s "$INDEX/$name.spotdl" ] && idxn=$("$PY" -c "import json;print(len(json.load(open('$INDEX/$name.spotdl'))))" 2>/dev/null)
  # Nested "<Parent>/<Sub>" entries print under their MEGAPLAYLIST header - one playlist to
  # players (shared ARTIST tag), separate folders so each source can fetch more on its own.
  disp="$name"
  case "$name" in
    */*)
      parent="${name%%/*}"
      [ "$parent" = "$prev_parent" ] || { printf '%s  (megaplaylist: ARTIST=%s)\n' "$parent" "$parent"; prev_parent="$parent"; }
      disp="  └ ${name#*/}";;
    *) prev_parent="";;
  esac
  printf '%-44s %6s %5s %7s %8s\n' "$disp" "$cnt" "$cap" "$off" "$idxn"
done < <(sort -t/ -k1,1 -s "$REGISTRY")
echo "-----"
echo "Total songs on disk: $total   Library: $MUSIC"
# Note any folders on disk that are not registered. A parent of nested entries is registered
# by proxy (its children are), so it must not be reported as stray.
for d in "$MUSIC"/*/; do
  [ -d "$d" ] || continue
  b=$(basename "$d")
  if ! awk -F'\t' -v b="$b" 'NR>1 && ($1==b || index($1, b "/")==1){found=1} END{exit !found}' "$REGISTRY"; then
    # An EMPTY unregistered folder is a megaplaylist waiting for its first source, not a stray -
    # a megaplaylist only becomes real in the registry once something is nested under it.
    if [ "$(folder_count "$b")" -eq 0 ]; then
      echo "  (empty megaplaylist: $b - no sources added yet)"
    else
      echo "  (unregistered folder: $b - $(folder_count "$b") songs)"
    fi
  fi
done
