#!/bin/sh
# SessionStart: print a conventions file with its settings filled in.
#
#     render-context.sh <path to orchestrator.md>
#
# One setting so far. ORCHESTRATION_MAX_AGENTS, the number of subagents the main session runs at a
# time, replaces {{MAX_AGENTS}}. It is read from the environment (set it under "env" in
# ~/.claude/settings.json, or in a repository's .claude/settings.json) and must be a positive integer;
# anything else, or nothing, means the default of 4. Plain stdout lands in the main session's context,
# so no JSON is needed. A missing file prints nothing and exits 0.
set -eu

file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] && [ -r "$file" ] || exit 0

max="${ORCHESTRATION_MAX_AGENTS:-}"
case "$max" in
  ''|*[!0-9]*|0*) max=4 ;;
esac
sed "s/{{MAX_AGENTS}}/$max/g" "$file"
