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
settle=8
workdir=$(pwd)
envs=""
prompt=""
# One input per line, sent in order after --prompt.
sends=""
newline='
'

usage() {
    cat >&2 <<'USAGE'
usage: prove-render.sh --plugin <dir> --grep <text> [--env K=V]... [--seconds N] [--settle N] [--cwd <dir>]
                       [--prompt <text>] [--send <text>]...

  --plugin   the plugin working copy to load (the directory holding .claude-plugin/plugin.json)
  --grep     a distinctive phrase from the message that should reach the terminal
  --env      an environment variable for the session; repeatable. Use this to point the plugin at a
             scratch location rather than moving --cwd, which would trigger the trust dialog.
  --seconds  how long to let the session run before quitting (default 18)
  --cwd      where to run, which must be a directory already trusted (default: the current one)
  --prompt   a message to send once the session is up, for output that only appears after a turn;
             it runs on the session's model (haiku), so keep it to one short line
  --send     a further input, typed and entered after --prompt and any earlier --send; repeatable.
             A slash command works (--send /clear), and so does a second prompt.
  --settle   seconds to wait before each input: for the session to start, before the first, and for
             the previous one to finish, before each after it (default 8)
USAGE
    exit 2
}

# A value expect cannot use as a timeout would fail inside its catch, silently, and no input is sent.
whole() {
    case $2 in ''|*[!0-9]*|0*)
        echo "prove-render.sh: $1 takes a whole number of seconds above 0, not '$2'" >&2; exit 2 ;;
    esac
}

while [ $# -gt 0 ]; do
    case $1 in
        --plugin)  plugin=${2:?}; shift 2 ;;
        --grep)    pattern=${2:?}; shift 2 ;;
        --env)     envs="$envs ${2:?}"; shift 2 ;;
        --seconds) whole --seconds "${2:?}"; seconds=$2; shift 2 ;;
        --cwd)     workdir=${2:?}; shift 2 ;;
        --prompt)  prompt=${2:?}; shift 2 ;;
        --send)    case ${2:?} in *"$newline"*)
                       echo "prove-render.sh: --send takes one line" >&2; exit 2 ;; esac
                   sends="$sends$2$newline"; shift 2 ;;
        --settle)  whole --settle "${2:?}"; settle=$2; shift 2 ;;
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
# The probe session saves a transcript (see CLAUDE_CODE_CHILD_SESSION below) under an id of its own.
# A /clear inside it starts a second session under a new id, which nothing outside the process is
# told: the debug log does not name it, and measured on Claude Code 2.1.295 its transcript does not
# name the first id either. So the probe's settings add a SessionStart hook that appends what every
# session it starts is told (its id and transcript path) to $scratch/sessions.jsonl, and the probe's
# own project folder (the one the transcript store names after its working directory) is listed
# before the run. Afterwards each .jsonl there that is new and is the probe's (named by the probe's
# id or recorded by that hook) is removed by exact path, with the folder of the same name. Any other
# new transcript there may be a session running beside it in the same directory, so it is reported
# and left alone. The project folder goes only if the run made it and it is now empty.
# No other project folder is read, nothing is matched by pattern, and nothing older is touched. A
# file or folder that vanishes or refuses mid-way (a parallel run cleaning up its own) is reported
# and skipped, and the rest carries on.
session=$(python3 -c 'import uuid; print(uuid.uuid4())')
transcripts() {
    python3 - "$1" "$session" "$scratch/before.json" "$workdir" "$scratch/sessions.jsonl" <<'PY'
import json, os, shutil, sys
step, session, saved, cwd, started = sys.argv[1:]
config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")

def folder_name(path):
    # The transcript store's own naming, read from Claude Code 2.1.295: every UTF-16 unit that is not
    # an ASCII letter or digit becomes "-", and a name over 200 units is cut there and suffixed with
    # "-" and the path's 32-bit string hash in base 36.
    raw = path.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    name = "".join(chr(u) if u < 128 and chr(u).isalnum() else "-" for u in units)
    if len(name) <= 200:
        return name
    h = 0
    for u in units:
        h = (h * 31 + u) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = ""
    while True:
        h, r = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + digits
        if not h:
            break
    return name[:200] + "-" + digits

# The probe runs where expect's cd puts it, and Claude Code names the folder after the resolved path.
folder = os.path.join(config, "projects", folder_name(os.path.realpath(cwd)))

def listing():
    try:
        return sorted(f for f in os.listdir(folder) if f.endswith(".jsonl"))
    except (FileNotFoundError, NotADirectoryError):
        return None

def skip(what, error):
    print(f"prove-render.sh: skipped {what}: {error.strerror or error}", file=sys.stderr)

if step == "before":
    names = listing()
    with open(saved, "w", encoding="utf-8") as f:
        json.dump({"existed": names is not None, "names": names or []}, f)
    sys.exit(0)
try:
    with open(saved, encoding="utf-8") as f:
        before = json.load(f)
except (OSError, ValueError):
    sys.exit(0)  # the probe never started
try:
    names = listing() or []
except OSError as error:
    skip(f"listing {folder}", error)
    sys.exit(0)
new = [n for n in names if n not in before["names"]]
probe = {session + ".jsonl"}
try:
    with open(started, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                probe.add(f"{record.get('session_id')}.jsonl")
                probe.add(os.path.basename(str(record.get("transcript_path") or "")))
except OSError as error:
    if not isinstance(error, FileNotFoundError):
        skip(f"reading {started}", error)
ours = [n for n in new if n in probe]
if not ours:
    print(f"prove-render.sh: no transcript of the probe's found in {folder}", file=sys.stderr)
for name in ours:
    transcript = os.path.join(folder, name)
    try:
        os.remove(transcript)
        print(f"prove-render.sh: removed the probe's transcript {transcript}")
    except OSError as error:
        skip(f"removing {transcript}", error)
    alongside = transcript[: -len(".jsonl")]
    try:
        if os.path.isdir(alongside) and not os.path.islink(alongside):
            shutil.rmtree(alongside)
    except OSError as error:
        skip(f"removing {alongside}", error)
for name in new:
    if name not in ours:
        print(f"prove-render.sh: left {os.path.join(folder, name)}: new during the run, but it "
              "is not one the probe's sessions were given, so it may be another session's", file=sys.stderr)
if not before["existed"]:
    try:
        if not os.listdir(folder):
            os.rmdir(folder)
    except FileNotFoundError:
        pass
    except OSError as error:
        skip(f"removing {folder}", error)
PY
}
# Everything else this script creates lives under $scratch and goes with it, however the run ends.
# No step of the cleanup can stop the next, and the exit status stays the proof's verdict.
cleanup() {
    status=$?
    set +e
    transcripts after
    rm -rf "$scratch"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

# The copy is renamed, but an installed plugin of the original name still loads beside it and can
# draw the same output, so the proof would pass on the installed code. Switch every installed
# <name>@<marketplace> off for this session alone.
original=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["name"])' \
    "$plugin/.claude-plugin/plugin.json")
python3 - "$original" "$scratch/settings.json" "$scratch/sessions.jsonl" <<'PY'
import json, os, shlex, sys
name, out, started = sys.argv[1:]
config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
try:
    with open(os.path.join(config, "plugins", "installed_plugins.json"), encoding="utf-8") as f:
        installed = json.load(f)
    installed = installed.get("plugins", installed)
except (OSError, ValueError):
    installed = {}
off = {key: False for key in installed if key.split("@")[0] == name}
with open(out, "w", encoding="utf-8") as f:
    # Every session the probe starts (at startup, after a /clear) records its own id and transcript
    # path here, for the cleanup; the hook prints nothing, so it draws nothing.
    record = {"type": "command", "command": f"{{ cat; echo; }} >> {shlex.quote(started)}"}
    json.dump({"enabledPlugins": off, "hooks": {"SessionStart": [{"hooks": [record]}]}}, f)
for key in off:
    print(f"prove-render.sh: {key} is installed; switched off for this session")
PY

log="$scratch/session.log"
debug="$scratch/debug.log"
cat > "$scratch/drive.exp" <<'EXPECT'
set timeout [expr {$env(RUN_SECONDS) + 60}]
cd $env(RUN_CWD)
# The TUI writes cursor-moves between words, so screen patterns are unreliable: run, quit, read the
# log afterwards instead of matching on what appears.
# Wait by reading, never by sleeping: the session blocks writing to a terminal nobody reads, so
# behind a plain sleep it froze until the quit and took no input in the meantime.
proc settle {seconds} {
    set timeout $seconds
    expect eof {} timeout {}
}
set extra [lsearch -all -inline -not -exact [split [string trim $env(RUN_ENVS)]] ""]
# A session started from inside another Claude Code session inherits CLAUDE_CODE_CHILD_SESSION, which
# turns transcript saving off: anything that reads the transcript then shows nothing, and the proof
# fails for a reason that has nothing to do with the hook.
eval spawn -noecho env -u CLAUDE_CODE_CHILD_SESSION $extra claude --model haiku \
    --plugin-dir $env(RUN_PLUGIN) --settings $env(RUN_SETTINGS) --debug-file $env(RUN_DEBUG) \
    --session-id $env(RUN_SESSION)
# The prompt, then each --send, in order: wait for the session to settle, type it, press Enter.
# A session that ends early makes the next send fail; the catch moves on to the cleanup below.
set inputs [split $env(RUN_SENDS) "\n"]
if {$env(RUN_PROMPT) ne ""} { set inputs [linsert $inputs 0 $env(RUN_PROMPT)] }
catch {
    foreach input $inputs {
        if {$input eq ""} continue
        settle $env(RUN_SETTLE)
        send -- $input; settle 1; send "\r"
    }
    settle $env(RUN_SECONDS)
    send "\x03"; settle 1; send "\x03"; settle 2
    set timeout 10
    expect eof
}
# Two Ctrl-Cs do not always end the session, so close its terminal (a hangup), then wait for the
# process itself: it keeps writing its debug log after the terminal closes, and removed before it
# exits, the scratch folder is made again.
catch close
catch wait
EXPECT

transcripts before
RUN_CWD="$workdir" RUN_PLUGIN="$scratch/$name" RUN_ENVS="$envs" RUN_SECONDS="$seconds" \
    RUN_PROMPT="$prompt" RUN_SENDS="$sends" RUN_SETTLE="$settle" RUN_SESSION="$session" RUN_SETTINGS="$scratch/settings.json" RUN_DEBUG="$debug" \
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
echo "=== plugins the session loaded (debug log) ==="
grep -o 'hooks module [^ ]* loaded' "$debug" 2>/dev/null | sort -u || true
grep -i 'not loaded\|failed to load\|refused' "$debug" 2>/dev/null | head -10 || true

echo
[ "$rendered" = yes ] || { echo "prove-render.sh: NOT PROVEN" >&2; exit 1; }
echo "prove-render.sh: proven — the phrase was rendered in a real terminal"
