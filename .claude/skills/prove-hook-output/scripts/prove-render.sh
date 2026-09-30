#!/bin/sh
# Prove that a plugin's hook output reaches the terminal, by running Claude Code in a real PTY.
#
# A hook's stdout goes to the model's context; only `systemMessage` is rendered to the person and
# only `terminalSequence` is emitted to the terminal. Headless runs cannot show either, because they
# have no UI, so this drives an interactive session under `expect` and reads what the terminal got.
#
# The plugin is copied under a scratch name first: an installed plugin of the same name shadows the
# working copy silently, and the proof would then pass for the wrong reason.
set -eu

plugin=
pattern=
seconds=18
workdir=$(pwd)
envs=""

usage() {
    cat >&2 <<'USAGE'
usage: prove-render.sh --plugin <dir> --grep <text> [--env K=V]... [--seconds N] [--cwd <dir>]

  --plugin   the plugin working copy to load (the directory holding .claude-plugin/plugin.json)
  --grep     a distinctive phrase from the message that should reach the terminal
  --env      an environment variable for the session; repeatable. Use this to point the plugin at a
             scratch location rather than moving --cwd, which would trigger the trust dialog.
  --seconds  how long to let the session run before quitting (default 18)
  --cwd      where to run, which must be a directory already trusted (default: the current one)
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case $1 in
        --plugin)  plugin=${2:?}; shift 2 ;;
        --grep)    pattern=${2:?}; shift 2 ;;
        --env)     envs="$envs ${2:?}"; shift 2 ;;
        --seconds) seconds=${2:?}; shift 2 ;;
        --cwd)     workdir=${2:?}; shift 2 ;;
        -h|--help) usage ;;
        *)         echo "prove-render.sh: unknown argument $1" >&2; usage ;;
    esac
done

[ -n "$plugin" ] && [ -n "$pattern" ] || usage
[ -f "$plugin/.claude-plugin/plugin.json" ] || {
    echo "prove-render.sh: no .claude-plugin/plugin.json under $plugin" >&2; exit 2; }
command -v expect >/dev/null 2>&1 || {
    echo "prove-render.sh: expect is not installed, and there is no PTY proof without it" >&2; exit 2; }

scratch=$(mktemp -d "${TMPDIR:-/tmp}/prove-render.XXXXXX")
# Everything this script creates lives under $scratch and goes with it, however the run ends.
trap 'rm -rf "$scratch"' EXIT INT TERM

name="prove-render-$$"
cp -R "$plugin" "$scratch/$name"
python3 - "$scratch/$name/.claude-plugin/plugin.json" "$name" <<'PY'
import json, sys
path, name = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    manifest = json.load(f)
manifest["name"] = name  # the installed plugin of the original name would otherwise shadow this copy
with open(path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
PY

log="$scratch/session.log"
cat > "$scratch/drive.exp" <<'EXPECT'
set timeout [expr {$env(RUN_SECONDS) + 60}]
cd $env(RUN_CWD)
# The TUI writes cursor-moves between words, so screen patterns are unreliable: run, quit, read the
# log afterwards instead of matching on what appears.
set extra [lsearch -all -inline -not -exact [split [string trim $env(RUN_ENVS)]] ""]
eval spawn -noecho env $extra claude --model haiku --plugin-dir $env(RUN_PLUGIN)
sleep $env(RUN_SECONDS)
send "\x03"; sleep 1; send "\x03"; sleep 2
expect eof
EXPECT

RUN_CWD="$workdir" RUN_PLUGIN="$scratch/$name" RUN_ENVS="$envs" RUN_SECONDS="$seconds" \
    expect -f "$scratch/drive.exp" > "$log" 2>&1 || true

echo "=== rendered to the person (ANSI stripped) ==="
if LC_ALL=C sed -e 's/\x1b\[[0-9;?]*[a-zA-Z]//g' -e 's/\r/\n/g' "$log" | grep -F -- "$pattern"; then
    rendered=yes
else
    echo "(not found: \"$pattern\" never reached the terminal)"
    rendered=no
fi

echo
echo "=== escape sequences the terminal received ==="
python3 - "$log" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
found = re.findall(rb"\x1b\][0-9]+;[^\x07]*\x07", raw)
for one in found:
    print(one.decode("utf-8", "replace").replace("\x1b", "<ESC>").replace("\x07", "<BEL>"))
print(f"({len(found)} sequence(s))")
PY

echo
echo "=== allowlist rejections ==="
if LC_ALL=C strings "$log" | grep -i 'rejected by the allowlist'; then
    echo "(a terminalSequence was refused by the host)"
else
    echo "(none)"
fi

echo
[ "$rendered" = yes ] || { echo "prove-render.sh: NOT PROVEN" >&2; exit 1; }
echo "prove-render.sh: proven — the phrase was rendered in a real terminal"
