#!/bin/sh
# Record that an upkeep run is complete: write today's date to the stamp file the session-start hook
# reads. Takes the data directory as its one argument (the skill passes the plugin data directory);
# with none, falls back to the same place the hook does. This is the only file upkeep ever writes.
set -eu
dir=${1:-${CLAUDE_PLUGIN_DATA:-${HOME:-}/.claude/upkeep}}
mkdir -p "$dir"
date +%Y-%m-%d >"$dir/last-upkeep"
echo "upkeep: stamped $dir/last-upkeep"
