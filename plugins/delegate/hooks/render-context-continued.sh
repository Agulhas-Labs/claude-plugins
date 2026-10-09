#!/bin/sh
# SessionStart: print the second part of the orchestrator conventions, context/orchestrator-continued.md,
# with its settings filled in by render-context.sh.
#
# One hook command's stdout is capped at 10,000 characters, so the orchestrator conventions are split
# at a section boundary into two files, each printed by its own command: render-context.sh prints
# orchestrator.md, and this script prints the rest. A hook command names one script and no arguments,
# so this script names the file. A missing file prints nothing and exits 0.
set -eu

root="${CLAUDE_PLUGIN_ROOT:-}"
[ -n "$root" ] && [ -f "$root/hooks/render-context.sh" ] || exit 0
exec sh "$root/hooks/render-context.sh" "$root/context/orchestrator-continued.md"
