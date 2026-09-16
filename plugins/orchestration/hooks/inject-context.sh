#!/bin/sh
# SubagentStart: put the plugin's engineering conventions into a subagent's context.
#
#     inject-context.sh <path to engineering.md>
#
# SessionStart just `cat`s the convention files: plain stdout lands in the main session's context
# there. A subagent needs the JSON `hookSpecificOutput.additionalContext` form, so this script
# JSON-escapes the file with sed, awk and tr alone. It must never depend on Python: without it a
# subagent would silently start with no conventions at all. A subagent gets the engineering
# conventions only, never the delegation rules: subagents don't delegate, and those rules would be
# re-sent on every one of their turns.
#
# A missing or empty file prints nothing and exits 0: it is never worth breaking a subagent start.
set -eu

file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] && [ -r "$file" ] || exit 0

tab="$(printf '\t')"
cr="$(printf '\r')"
# Drop the control characters JSON forbids (keeping tab, newline and carriage return) and any leading
# or trailing blank lines; escape the backslash first and then the quote, tab and carriage return; join
# the lines with a literal \n. Everything else, non-ASCII included, passes through: JSON is UTF-8.
body="$(tr -d '\000-\010\013\014\016-\037' < "$file" \
  | awk '{ line[NR] = $0 } END { first = 1; last = NR
      while (first <= last && line[first] ~ /^[[:space:]]*$/) first++
      while (last >= first && line[last] ~ /^[[:space:]]*$/) last--
      for (i = first; i <= last; i++) print line[i] }' \
  | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/$tab/\\\\t/g" -e "s/$cr/\\\\r/g" \
  | awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }')"
[ -n "$body" ] || exit 0

printf '{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":"%s"}}\n' "$body"
