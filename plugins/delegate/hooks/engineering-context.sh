#!/bin/sh
# SessionStart: print the engineering conventions. Plain stdout lands in the main session's context.
# Each hook command names one script by a literal ${CLAUDE_PLUGIN_ROOT} path, and the script finds its
# own files from there: the plugin directory's validator refuses a command that names two paths. A
# missing file prints nothing and exits 0.
file="${CLAUDE_PLUGIN_ROOT:-}/context/engineering.md"
[ -f "$file" ] && [ -r "$file" ] || exit 0
cat "$file"
