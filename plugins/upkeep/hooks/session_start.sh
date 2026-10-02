#!/bin/sh
# SessionStart: say once, in one line, that config upkeep is due. Silent otherwise.
#
# It reads one file, the stamp `/upkeep` writes (a single YYYY-MM-DD line), and runs one subprocess,
# `date`. No settings, no transcripts, no network. A missing, malformed or CRLF-ended stamp counts as
# "never run"; a stamp dated in the future counts as not due. UPKEEP_INTERVAL_DAYS=0 silences it.
# A subagent's payload carries agent_id: it is not announced to. A compaction does not announce again:
# the line was already given at the session's start.

dir=${CLAUDE_PLUGIN_DATA:-${HOME:-}/.claude/upkeep}

IFS= read -r payload || true
case $payload in *'"agent_id"'* | *'"compact"'*) exit 0 ;; esac

# Strip leading zeros so sh arithmetic never reads 08 or 09 as octal.
unzero() {
  v=$1
  while [ "${#v}" -gt 1 ] && [ "${v#0}" != "$v" ]; do v=${v#0}; done
  echo "$v"
}

# Days since a fixed epoch for a civil date (y m d), proleptic Gregorian.
days() {
  y=$1 m=$2 d=$3
  [ "$m" -le 2 ] && y=$((y - 1))
  era=$((y / 400))
  yoe=$((y - era * 400))
  doy=$(((153 * ((m + 9) % 12) + 2) / 5 + d - 1))
  echo $((era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy))
}

interval=14
case ${UPKEEP_INTERVAL_DAYS:-} in
  '' | *[!0-9]*) ;;
  *)
    [ "${#UPKEEP_INTERVAL_DAYS}" -le 6 ] && interval=$(unzero "$UPKEEP_INTERVAL_DAYS")
    ;;
esac
[ "$interval" -eq 0 ] && exit 0

today=$(date +%Y-%m-%d) || exit 0
case $today in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;; *) exit 0 ;; esac

last=never
IFS= read -r stamp <"$dir/last-upkeep" 2>/dev/null || true
case $stamp in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
    sy=${stamp%%-*} rest=${stamp#*-}
    sm=$(unzero "${rest%%-*}") sd=$(unzero "${rest#*-}")
    if [ "$sm" -ge 1 ] && [ "$sm" -le 12 ] && [ "$sd" -ge 1 ] && [ "$sd" -le 31 ]; then
      ty=${today%%-*} trest=${today#*-}
      tm=$(unzero "${trest%%-*}") td=$(unzero "${trest#*-}")
      age=$(($(days "$(unzero "$ty")" "$tm" "$td") - $(days "$(unzero "$sy")" "$sm" "$sd")))
      [ "$age" -lt "$interval" ] && exit 0
      last=$stamp
    fi
    ;;
esac

echo "Config upkeep is due (last run $last): run /upkeep when convenient."
