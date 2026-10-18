#!/bin/sh
# The gate for this repository: unit tests, version agreement, manifest validation, and the privacy scan.
# A green run prints one line per stage and a failing stage prints its full output; --verbose prints
# every stage's own output instead.
# The privacy scan reads a list of terms that must never ship (names, private repositories, machine
# paths). The list lives outside the repository, since it would otherwise publish the very terms it
# guards: $PRIVATE_TERMS, or ~/.config/agulhas/private-terms.txt. One term per line, no blank lines.
set -eu
cd "$(dirname "$0")/.."

verbose=0
case "$#:${1-}" in
  0:) ;;
  1:--verbose) verbose=1 ;;
  *) echo "usage: sh scripts/check.sh [--verbose]" >&2; exit 2 ;;
esac

py=
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done
if [ -z "$py" ]; then
  echo "check.sh: no Python 3 found as python3, python or py" >&2
  exit 1
fi

# Every stage's output is captured into a scratch file and parsed the same way in both modes; --verbose
# also shows it live. A failing stage fails the run in both modes, with its exit status. ANSI colour is
# switched off for the stage and stripped again before parsing, so a coloured summary reads the same.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
# dash runs no EXIT trap when a signal kills it, so turn the signals into exits (measured: a SIGTERM
# under dash left the scratch directory behind).
trap 'exit 130' INT
trap 'exit 143' TERM
esc=$(printf '\033')
out=
run_stage() {
  { rc=0; env -u FORCE_COLOR NO_COLOR=1 "$@" 2>&1 || rc=$?; echo "$rc" >"$scratch/rc"; } | {
    if [ "$verbose" = 1 ]; then tee "$scratch/raw"; else cat >"$scratch/raw"; fi
  }
  rc=$(cat "$scratch/rc")
  out=$(sed "s/${esc}\[[0-9;]*[A-Za-z]//g" "$scratch/raw")
  if [ "$rc" -ne 0 ]; then
    if [ "$verbose" = 0 ]; then printf '%s\n' "$out"; fi
    exit "$rc"
  fi
}
fail_with_output() {
  if [ "$verbose" = 0 ]; then printf '%s\n' "$out"; fi
  echo "check.sh: $1" >&2
  exit 1
}

# A suite that ran no tests is not a pass, whatever unittest's exit status says.
unit_line=
unit_suite() {
  run_stage "$py" -m unittest discover -s "$2"
  n=$(printf '%s\n' "$out" | sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .*/\1/p' | tail -n 1)
  if [ -z "$n" ] || [ "$n" -eq 0 ]; then
    fail_with_output "the $1 suite ran no tests"
  fi
  skipped=$(printf '%s\n' "$out" | sed -n 's/^[A-Z][A-Z]* (.*skipped=\([0-9][0-9]*\)[,)].*$/\1/p' | tail -n 1)
  item="$1 $n OK"
  if [ -n "$skipped" ]; then item="$item ($skipped skipped)"; fi
  unit_line="${unit_line:+$unit_line, }$item"
}
unit_suite delegate plugins/delegate/tests
unit_suite agent-cost plugins/delegate/skills/agent-cost/tests
unit_suite cache-guard plugins/cache-guard/tests
unit_suite logbook plugins/logbook/tests
if [ "$verbose" = 0 ]; then echo "unit: $unit_line"; fi

# A plugin's version is written twice, in its manifest and in the marketplace entry; they must agree.
"$py" - "$verbose" <<'EOF'
import glob, json, sys
verbose = sys.argv[1] == "1"
with open(".claude-plugin/marketplace.json", encoding="utf-8") as f:
    listed = {entry["name"]: entry.get("version") for entry in json.load(f)["plugins"]}
ok = True
agreed = []
for manifest in sorted(glob.glob("plugins/*/.claude-plugin/plugin.json")):
    with open(manifest, encoding="utf-8") as f:
        plugin = json.load(f)
    name, version = plugin["name"], plugin.get("version")
    if version is None or version != listed.get(name):
        print(f"version mismatch for {name}: {manifest} says {version!r}, marketplace.json says {listed.get(name)!r}", file=sys.stderr)
        ok = False
    elif verbose:
        print(f"version: {name} {version}, listed the same")
    else:
        agreed.append(f"{name} {version}")
if ok and not verbose:
    print("version: " + ", ".join(agreed) + " — each listed the same")
sys.exit(0 if ok else 1)
EOF

if command -v claude >/dev/null 2>&1; then
  validated=1
  mod_line=
  run_stage claude plugin validate . --strict
  for plugin in plugins/*/; do
    run_stage claude plugin validate "${plugin%/}" --strict
    validated=$((validated + 1))
    # A mod's tests (*.test.ts) run on the engine itself; the unit-test runs above do not see them.
    if [ -n "$(find "${plugin%/}" -name '*.test.ts' -print -quit)" ]; then
      run_stage claude plugin test "${plugin%/}"
      pass=$(printf '%s\n' "$out" | sed -n 's/^ *\([0-9][0-9]*\) pass$/\1/p' | tail -n 1)
      fail=$(printf '%s\n' "$out" | sed -n 's/^ *\([0-9][0-9]*\) fail$/\1/p' | tail -n 1)
      if [ -z "$pass" ] || [ "$pass" -eq 0 ] || [ "${fail:-1}" -ne 0 ]; then
        fail_with_output "$(basename "${plugin%/}")'s mod tests: no passing count, or a failure, in the summary"
      fi
      mod_line="${mod_line:+$mod_line, }$(basename "${plugin%/}") $pass pass"
    fi
  done
  if [ "$verbose" = 0 ]; then
    echo "validate: $validated OK"
    if [ -n "$mod_line" ]; then echo "mod tests: $mod_line"; fi
  fi
else
  echo "manifest validation: claude CLI not found — skipped" >&2
fi

terms="${PRIVATE_TERMS:-$HOME/.config/agulhas/private-terms.txt}"
if [ -f "$terms" ]; then
  # What git would commit: tracked files and untracked ones it does not ignore. An ignored file (a
  # handoff, a logbook, build scratch) never ships, so it is not scanned.
  if git ls-files -z --cached --others --exclude-standard | xargs -0 grep -sniF -f "$terms" -- ; then
    echo "privacy gate: the lines above carry a private term" >&2
    exit 1
  fi
  echo "privacy gate: clean"
else
  echo "privacy gate: no terms file at $terms — skipped" >&2
fi
