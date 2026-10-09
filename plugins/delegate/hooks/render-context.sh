#!/bin/sh
# SessionStart: print a conventions file with its settings filled in.
#
#     render-context.sh [path to orchestrator.md]
#
# With no argument it renders ${CLAUDE_PLUGIN_ROOT}/context/orchestrator.md: the hook command names
# this script alone, as the plugin directory's validator requires.
# render-context-continued.sh runs it on context/orchestrator-continued.md, the conventions' second
# part: one hook command's stdout is capped at 10,000 characters.
#
# One setting so far. DELEGATE_MAX_CONCURRENT_AGENTS, the number of subagents the main session
# runs at a time, replaces {{MAX_AGENTS}}. It is read from the environment (set it under "env" in
# ~/.claude/settings.json, or in a repository's .claude/settings.json) and must be a positive integer;
# anything else, or nothing, means the default of 4. Plain stdout lands in the main session's context,
# so no JSON is needed. A missing file prints nothing and exits 0.
set -eu

file="${1:-${CLAUDE_PLUGIN_ROOT:-}/context/orchestrator.md}"
[ -n "$file" ] && [ -f "$file" ] && [ -r "$file" ] || exit 0

max="${DELEGATE_MAX_CONCURRENT_AGENTS:-}"
case "$max" in
  ''|*[!0-9]*|0*) max=4 ;;
esac
sed "s/{{MAX_AGENTS}}/$max/g" "$file"
