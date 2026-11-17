#!/bin/sh
# The radio's song changes as its log has them: the time from a song's loop point ("Hit Loop") to the next song
# ("Now Playing"), which is the play after the loop plus the fade, 11 s + 7 s = 18 s. A change with a jingle or a
# spot between the songs is longer and is not counted (over 25 s).
#   song-gaps.sh last N           the last N changes in the log, printed
#   song-gaps.sh wait N "SINCE"   waits for N changes after SINCE (a journalctl time), prints each, and fails unless
#                                 every one is 18 s within a quarter second
gaps() {  # $1: journalctl's --since, or empty
  journalctl -u acnh-aircheck.service --no-pager -o short-unix ${1:+--since "$1"} 2>/dev/null | awk '
    /Hit Loop/ { t = $1; next }
    /Now Playing/ { if (t) { d = $1 - t; if (d < 25) { sub(/^.*Now Playing: /, ""); printf "%.2f s to \"%s\" at %s\n", d, $0, strftime("%T", t + d) } } t = 0 }'
}
case "$1" in
  last) gaps "" | tail -n "$2" ;;
  wait)
    n=$2; since=$3; i=0
    while [ "$(gaps "$since" | wc -l)" -lt "$n" ]; do
      i=$((i + 1)); [ "$i" -le 120 ] || { echo "fewer than $n song changes in an hour"; gaps "$since"; exit 1; }
      sleep 30
    done
    gaps "$since" | head -n "$n"
    gaps "$since" | head -n "$n" | awk '{ if ($1 < 17.75 || $1 > 18.25) bad = 1 } END { exit bad }' \
      || { echo "FAIL: a song change is not 18 s"; exit 1; }
    echo "OK: $n song changes, each 18 s from the loop point"
    ;;
  *) echo "usage: song-gaps.sh last N | wait N SINCE"; exit 2 ;;
esac
