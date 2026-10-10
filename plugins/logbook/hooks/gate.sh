#!/bin/sh
# The fast path every logbook hook runs. Starting Python costs tens of milliseconds before it
# does anything, so this reads the payload with the shell alone and starts the handler only when there
# is work for it: the session already has a board, or the event is one that can start one (a subagent
# starting, a `TaskCreate` whose id reaches the threshold, a `TodoWrite`, the work call that reaches
# `LOGBOOK_CALLS`, a `Stop` after a work call that changed something). Everything else exits
# here, silently, so a short session that changed nothing pays for this script and nothing more.
# `SessionStart` reaches the handler only in a project that has a boards folder, where it prunes old
# boards and gives this session's board back (reopening it, on a resume), or that a next-session setting
# may apply to (`next_setting`); a project that has never had either pays a few file tests at start.
#
# A task's id is its position in the list, so the `TaskCreate` that reaches its threshold says so
# itself. Work calls (a main-session `Write`, `Edit`, `MultiEdit`, `NotebookEdit` or `Bash`, whether
# it succeeded or failed) are counted here instead, and only here: one byte is appended to
# `$CLAUDE_PLUGIN_DATA/calls/<session>` per call, and the count is the file's length, read back with
# the shell's own `read`, so counting starts no external program. The byte is `c` for a call that
# changed something (a file tool that succeeded, or a command that ran `git commit`) and `x` for any other, so the
# `Stop` that ends a turn can start a board for a turn that changed something before the count reached
# its threshold. The handler reads the same file to decide and never adds to it, so the two agree.
# Nothing is written inside the project before a board starts. The handler removes the file when the
# board starts, and `SessionEnd` removes it here. A session that already has a board is not counted;
# without `CLAUDE_PLUGIN_DATA` nothing is counted and neither the threshold nor `Stop` starts a board.
#
# The payload is compact JSON on one line. A field is cut out of it with parameter expansion, after a
# `case` has checked it is there (a pattern that does not match would otherwise scan the whole payload
# once per character). That expansion's cost grows with the square of the key's offset in what it
# scans (measured: a `session_id` following a 100 KB value took 20 s under bash 3.2). So every field
# this script reads -- `session_id`, `cwd`, `hook_event_name`, `tool_name`, `agent_id`, a `Bash`
# call's `command`, and a `TaskCreate`'s task id -- is read from the payload's first 4096 bytes alone,
# taken once with the shell's own `printf`, never from the whole payload. This depends on the host
# writing those keys before any large value, which it does today. When it does not -- a key past the
# head -- a `session_id` or `hook_event_name` this script cannot read means the payload is one it
# cannot read either, and it exits silently, as it does for any payload it cannot read; a `TaskCreate`
# whose task id falls past the head, or may be cut short by it, is instead handed to the handler, which
# parses the JSON properly and decides, and of a `command` only the start is read. Inside a JSON string every quote is escaped, so `"key":"` only ever matches a key. A boards
# folder that is a symbolic link is never used. Anything this cannot read exits 0 and prints nothing:
# a hook is never in the way.
set -u

payload=$(cat) || exit 0
# The first 4096 bytes of the payload: where every field below is expected to sit. Built with the
# shell's own `printf`, so reading a large payload never itself starts an external program.
head=$(printf '%.4096s' "$payload")

case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac

# Hand the payload to the handler on its stdin, and exit 0 whatever it does.
run() {
  printf '%s\n' "$payload" | sh "$here/run-python.sh" "$here/board_hook.py" 2>/dev/null
  exit 0
}

# The string value of the first "$1":"..." in the first 4096 bytes, in $value; false when there is none.
field() {
  case $head in
    *"\"$1\":\""*) ;;
    *) return 1 ;;
  esac
  value=${head#*\"$1\":\"}
  value=${value%%\"*}
}

field session_id || exit 0
session=$value
case $session in
  '' | *[!A-Za-z0-9._-]*) exit 0 ;;
esac
case $session in
  *[!.]*) ;;
  *) exit 0 ;;
esac

event=
field hook_event_name && event=$value

# The file that counts this session's work calls, when the host gives the plugin a data folder.
data=${CLAUDE_PLUGIN_DATA:-}
calls=
[ -n "$data" ] && calls=$data/calls/$session
# A folder of counts that is a symbolic link is never used: nothing is counted or removed through it.
[ -n "$data" ] && [ -L "$data/calls" ] && calls=
if [ "$event" = SessionEnd ] && [ -n "$calls" ] && [ -f "$calls" ]; then
  rm -f "$calls" 2>/dev/null
fi

# The start of a `Bash` call's `command` in $value, still escaped as JSON: its first 600 bytes, or
# all of it when it is shorter; false when the head holds no command. Every quote inside a JSON
# string is escaped, so the first `"command":"` is the key, and the string ends at the first quote
# after it that an even number of backslashes precede (none, or escaped backslashes only).
#
# Only the start is read because reading costs the shell more than it looks: cutting a string at a
# quote takes time that grows with the square of its length, once per quote (measured under bash
# 3.2: a 3 kB command holding 1500 quotes took 106 ms, one holding 200 quoted strings 41 ms, against
# 50 ms for the whole hook). A commit is named where a command begins, and a long command is long
# in its message, so 600 bytes see it. A command cut there loses its last word, which may be half
# of one.
command_value() {
  # The key is looked for in the payload's first 1200 bytes only: finding it costs time that grows
  # with the square of how far in it sits (measured under dash: 50 ms at 3450 bytes). The host
  # writes it at about 300. Further in than that, the command is not read.
  near=$(printf '%.1200s' "$head")
  case $near in
    *'"command":"'*) ;;
    *) return 1 ;;
  esac
  rest=${near#*\"command\":\"}
  rest=$(printf '%.600s' "$rest")
  value=
  while :; do
    case $rest in
      *\"*) ;;
      *)
        value=$value$rest
        case $value in
          *' '*) value=${value% *} ;;
          *) value= ;;
        esac
        return 0
        ;;
    esac
    part=${rest%%\"*}
    rest=${rest#*\"}
    value=$value$part
    ends=$part
    while :; do
      case $ends in
        *'\\') ends=${ends%??} ;;
        *) break ;;
      esac
    done
    case $ends in
      *\\) value=$value\" ;;
      *) return 0 ;;
    esac
  done
}

# Whether a `Bash` call's command runs `git` with `commit` as its subcommand: `git commit …`,
# `git -C dir commit …`, `cd x && git commit …`. The command is split into words at blanks, with
# globbing off, and a word that is `git` (or ends in `/git`, or follows `;`, `&`, `|`, `(` or an
# escaped newline with no blank between) is followed by its options: `-C`, `-c`, `--git-dir`,
# `--work-tree`, `--namespace` and `--config-env` take the next word as their value, any other word
# that starts with `-` is an option on its own, and the first word after them is the subcommand
# (`commit`, or `commit` with `;`, `&`, `|` or `)` straight after it). So a quoted `"git commit"` is
# not a commit (its first word is `\"git`), nor is `git log --grep commit`: a false `c` would start a
# board for a turn that changed nothing, and a missed one only waits for the threshold. Words alone do
# not say where a command starts, so an unquoted `echo git commit` does read as a commit.
# A commit named past the command's first 600 bytes is not seen, and the call is counted as one that
# changed nothing.
commits() {
  command_value || return 1
  set -f
  after=
  for word in $value; do
    case $after in
      git)
        case $word in
          -C | -c | --git-dir | --work-tree | --namespace | --config-env) after=option ;;
          -*) ;;
          commit | commit[';&|)']*) set +f; return 0 ;;
          *) after= ;;
        esac
        continue
        ;;
      option)
        after=git
        continue
        ;;
    esac
    case $word in
      git | */git | *[';&|(']git | *\\ngit) after=git ;;
    esac
  done
  set +f
  return 1
}

# Count a work call made in the main session, and start the handler once the count reaches the
# threshold. A call made inside a subagent carries `agent_id` and is never counted. The byte appended
# says what kind of call it was: `c` for one that changed something (a file tool's `PostToolUse`, or
# a `Bash` call whose command runs `git commit`, failed or not), `x` for any other. The count is still the
# file's length; `Stop` starts a board for a file that holds a `c`.
count() {
  [ -n "$calls" ] || return 0
  case $event in
    PostToolUse | PostToolUseFailure) ;;
    *) return 0 ;;
  esac
  field tool_name || return 0
  tool=$value
  case $tool in
    Write | Edit | MultiEdit | NotebookEdit | Bash) ;;
    *) return 0 ;;
  esac
  case $head in
    *'"agent_id":"'*) return 0 ;;
  esac
  # A command that ran `git commit` may have made its commit and failed afterwards (the push that
  # follows it, refused), so it counts whether or not it failed. A file tool that failed changed nothing.
  kind=x
  case $tool in
    Bash) commits && kind=c ;;
    *) [ "$event" = PostToolUse ] && kind=c ;;
  esac
  [ -d "$data/calls" ] || mkdir -p "$data/calls" 2>/dev/null
  printf %s "$kind" 2>/dev/null >>"$calls" || return 0
  # `read` returns false at a missing newline, which this file never has, but still sets the value.
  made=
  read -r made 2>/dev/null <"$calls"
  made=${#made}
  limit=${LOGBOOK_CALLS:-}
  case $limit in
    '' | *[!0-9]*) limit=10 ;;
  esac
  [ "$limit" -gt 0 ] 2>/dev/null || limit=10
  [ "$made" -ge "$limit" ] 2>/dev/null && run
  return 0
}

project=${CLAUDE_PROJECT_DIR:-}
if [ -z "$project" ]; then
  field cwd || exit 0
  project=$value
  # A backslash is an escape this script does not decode; the handler reads the path properly and
  # decides whether the session has a board. A work call is counted before it gets there.
  case $project in
    *\\*)
      count
      run
      ;;
  esac
fi

# A boards folder that is a symbolic link is never used: nothing is recorded through it.
[ -L "$project/.logbook" ] && exit 0

# Whether a next-session setting may apply to $project, walking up the way the handler does
# (`board.next_session_walk`): a folder holding `.logbook/next-session.json` on the way up, or a `.git`
# file (a linked worktree, whose setting may be in the main checkout: the handler asks git). The walk
# stops at the first `.git` folder, a repository's top. File tests only: it starts no program.
next_setting() {
  dir=$project
  while :; do
    [ -e "$dir/.logbook/next-session.json" ] && return 0
    [ -d "$dir/.git" ] && return 1
    [ -f "$dir/.git" ] && return 0
    case $dir in
      /*/*) dir=${dir%/*} ;;
      /?*) dir=/ ;;
      *) return 1 ;;
    esac
  done
}

if [ "$event" = SessionStart ]; then
  [ -d "$project/.logbook" ] && run
  next_setting && run
  exit 0
fi

[ -f "$project/.logbook/$session/.logbook" ] && run

# The end of a turn in a session with no board: a count that holds a call that changed something
# starts one, so a turn that changed something never ends without a board. Any other `Stop` ends here.
if [ "$event" = Stop ]; then
  [ -n "$calls" ] && [ -f "$calls" ] || exit 0
  made=
  read -r made 2>/dev/null <"$calls"
  case $made in
    *c*) run ;;
  esac
  exit 0
fi

count
case $event in
  SubagentStart) run ;;
  PostToolUse) ;;
  *) exit 0 ;;
esac

field tool_name || exit 0
case $value in
  TodoWrite) run ;;
  TaskCreate) ;;
  *) exit 0 ;;
esac

# The new task's id: the first "id" in the last "task" object in the head, which is the tool's
# response. Not found there (a large "description" ahead of it pushed it past the head, say) hands the
# payload straight to the handler, rather than search the rest of it the slow way.
case $head in
  *'"task":{'*) ;;
  *) run ;;
esac
task=${head##*\"task\":\{}
case $task in
  *'"id":'*) ;;
  *) run ;;
esac
task=${task#*\"id\":}
task=${task#\"}
id=${task%%[!0-9]*}
# Digits that run to the end of the head may be an id cut short: the handler reads it whole.
[ "$id" = "$task" ] && run
[ -n "$id" ] || exit 0

steps=${LOGBOOK_STEPS:-}
case $steps in
  '' | *[!0-9]*) steps=5 ;;
esac
[ "$steps" -gt 0 ] 2>/dev/null || steps=5

[ "$id" -ge "$steps" ] 2>/dev/null && run
exit 0
