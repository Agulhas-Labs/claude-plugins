#!/usr/bin/env python3
"""PreToolUse on Bash: two checks on a subagent's shell commands, each refusing a call with a tagged reason.

A speed bump before repeating a long run (any subagent). A subagent's prompt cache lasted five minutes
where this was measured, and a call that outlives it makes the next turn rewrite the whole context.
Measured on one machine over seven days: 268 cold subagent turns followed a Bash call, 193 of them a
Swift build or test, after a median wait of nine minutes in a 72k context; the 56 agents that went cold
two or more times held 54% of the avoidable cost. The PostToolUse cache-expired line arrives after the
wait, and it did not stop the repeats: the cold share of subagent spend went from 7.1% to 8.6% with it
in place. So this check acts before the next run instead. An earlier Bash call whose result came back
more than the cache lifetime after the turn that issued it makes its command family "long"; a
backgrounded call counts until the notification that it finished, since that wakes the agent cold. A
command of a long family is refused once, with the earlier wait, the context size, and the choice:
re-issue the identical command to run it anyway, or commit what is verified and hand the run back for
the orchestrator to queue on a cheap runner. The identical re-issue passes, recognised by an earlier call
with the same command whose result carries this check's tag. `DELEGATE_SUBAGENT_CACHE_SECONDS`
overrides the 300-second lifetime; `DELEGATE_LONG_RUN_GUARD=0` turns the check off.

A command's families are those of its simple commands, after env assignments and wrappers (`timeout N`,
`time`, `nice`, `env`, `nohup`, `caffeinate …`, `sift run --`) are stripped: the tool's name and its
first subcommand word, with a few tools normalised (`npm run test` is `npm test`, `python3 -m pytest`
is `pytest`, any xcodebuild that tests is `xcodebuild test`). Commands that only look or finish at once
(`cd`, `ls`, `cat`, `sed`, `grep`, `echo`, git's local subcommands, …) have no family, so they never
match a long run they once shared a line with.

A reviewer never moves a shared checkout (only this plugin's reviewer). Observed once: a read-only
reviewer detached HEAD in the primary checkout other agents were working from. So a git command that
moves HEAD, the index, the worktree or refs is refused anywhere in a compound command, after git's
global options, unless its directory (`-C`, else the last `cd` before it, else the call's `cwd`) is
inside a worktree the reviewer added itself with `git worktree add`. Re-issuing does not lift it.
`DELEGATE_REVIEWER_GUARD=0` turns the check off.

A main-session call carries no `agent_id` and is left alone. Anything unexpected exits silently: a
guard is never worth breaking a tool call.
"""
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

TAIL = 1024 * 1024  # the transcript's end, as the budget hook reads it
CACHE_SECONDS = 300  # a subagent's prompt cache lifetime; DELEGATE_SUBAGENT_CACHE_SECONDS overrides
LONG_TAG = "[delegate: long-run guard]"
REVIEWER_TAG = "[delegate: reviewer guard]"
NOTIFIED = re.compile(r"<tool-use-id>([^<\s]+)</tool-use-id>")


def transcript(payload):
    """The subagent's own transcript: the payload may name the parent's, so derive it from `agent_id`."""
    path, agent = payload.get("transcript_path") or "", payload.get("agent_id")
    if not agent or not path:
        return None
    if os.path.basename(path).startswith("agent-"):
        return path
    session = os.path.splitext(path)[0]
    return os.path.join(session, "subagents", f"agent-{agent}.jsonl")


def cache_seconds(env):
    """The prompt cache's lifetime: the override if it is a positive integer, else the default."""
    try:
        seconds = int(env.get("DELEGATE_SUBAGENT_CACHE_SECONDS", ""))
    except ValueError:
        return CACHE_SECONDS
    return seconds if seconds > 0 else CACHE_SECONDS


def epoch(stamp):
    """An ISO transcript timestamp as epoch seconds, or None."""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def text_of(content):
    """A tool_result's text: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def history(path):
    """(Bash calls in order as dicts {id, command, issued, ended, result}, the latest context size)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL))
        lines = f.read().splitlines()
    calls, by_id, size = [], {}, 0
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may be cut mid-record
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        stamp = epoch(entry.get("timestamp"))
        if entry.get("type") == "assistant":
            usage = message.get("usage") or {}
            if usage:
                size = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = (block.get("input") or {}).get("command")
                    if isinstance(command, str) and block.get("id") not in by_id:
                        call = {"id": block.get("id"), "command": command, "issued": stamp, "ended": None, "result": ""}
                        by_id[call["id"]] = call
                        calls.append(call)
        elif entry.get("type") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                    call = by_id[block["tool_use_id"]]
                    call["result"] = text_of(block.get("content"))
                    call["ended"] = max(filter(None, (call["ended"], stamp)), default=None)
        if b"<task-notification>" in raw:
            # A backgrounded call returns at once; the harness wakes the agent when it finishes.
            for found in NOTIFIED.findall(raw.decode("utf-8", "replace")):
                if found in by_id:
                    call = by_id[found]
                    attached = epoch((entry.get("attachment") or {}).get("timestamp")) if isinstance(entry.get("attachment"), dict) else None
                    call["ended"] = max(filter(None, (call["ended"], stamp or attached)), default=None)
    return calls, size


# --- splitting a command line into simple commands ---------------------------------------------------

def segments(command):
    """The simple commands of a shell command line, each a list of words with quotes removed. Splits on
    `;`, `&`, `|`, newlines, parentheses, `$(` and backticks; drops redirections and here-document bodies.
    Not a shell: a command hidden inside a quoted string (`sh -c '…'`) stays one word."""
    out, words, word, has = [], [], [], False
    skip, pending = False, []  # skip: the next word is a redirection target; pending: here-doc delimiters
    i, n = 0, len(command)

    def end_word():
        nonlocal word, has, skip
        if has:
            if skip:
                skip = False
            else:
                words.append("".join(word))
        word, has = [], False

    def end_segment():
        nonlocal words, skip
        end_word()
        if words:
            out.append(words)
        words, skip = [], False

    while i < n:
        c = command[i]
        if c == "\\":
            if i + 1 < n and command[i + 1] != "\n":
                word.append(command[i + 1])
                has = True
            i += 2
        elif c in "'\"":
            j = i + 1
            while j < n and command[j] != c:
                if c == '"' and command[j] == "\\" and j + 1 < n:
                    j += 1
                word.append(command[j])
                j += 1
            has, i = True, j + 1
        elif c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_segment()
            i += 1
            for delimiter in pending:  # skip each here-document body, up to its delimiter line
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if line.strip() == delimiter:
                        break
            pending = []
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            end_word()
            i += 2
            if i < n and command[i] == "-":
                i += 1
            while i < n and command[i] in " \t":
                i += 1
            j = i
            while j < n and command[j] not in " \t\n;&|<>()":
                j += 1
            pending.append(re.sub(r"[\"'\\]", "", command[i:j]))
            i = j
        elif c in "<>":
            if has and all(ch.isdigit() for ch in word):
                word, has = [], False  # a file descriptor number, as in 2>&1
            else:
                end_word()
            while i < n and command[i] in "<>":
                i += 1
            if i < n and command[i] == "&" and i + 1 < n and (command[i + 1].isdigit() or command[i + 1] == "-"):
                i += 2  # a descriptor copy, as in >&2: no target word follows
            else:
                skip = True
        elif command.startswith("$(", i) or c in ";&|()`":
            end_segment()
            i += 2 if c == "$" else 1
        else:
            word.append(c)
            has = True
            i += 1
    end_segment()
    return out


KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time", "fi", "done", "esac"}
HEADERS = {"for", "case", "select", "function"}  # a segment opening with one of these names no command
WRAPPER_VALUE_OPTIONS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S"},
    "caffeinate": {"-t", "-w"},
    "time": set(),
    "nohup": set(),
}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def core(words):
    """A simple command's words with env assignments, shell keywords and wrappers stripped from its front,
    or [] if it names no command (a loop header, a bare assignment)."""
    words = list(words)
    while words:
        head = words[0]
        if head in HEADERS:
            return []
        if head in KEYWORDS or ASSIGNMENT.match(head):
            words.pop(0)
        elif head == "sift" and words[1:2] == ["run"]:
            words = words[words.index("--") + 1:] if "--" in words else _after_options(words[2:])
        elif head in WRAPPER_VALUE_OPTIONS:
            takes = WRAPPER_VALUE_OPTIONS[head]
            words.pop(0)
            while words and words[0].startswith("-") and words[0] != "-":
                option = words.pop(0)
                if option in takes and words:
                    words.pop(0)
            if head == "timeout" and words:
                words.pop(0)  # the duration
            if head == "env":
                while words and ASSIGNMENT.match(words[0]):
                    words.pop(0)
        else:
            break
    return words


def _after_options(words):
    words = list(words)
    while words and words[0].startswith("-"):
        words.pop(0)
    return words


# --- command families -------------------------------------------------------------------------------

QUICK = {
    "cd", "pushd", "popd", "ls", "cat", "head", "tail", "sed", "awk", "grep", "egrep", "fgrep", "rg", "wc",
    "sort", "uniq", "cut", "tr", "echo", "printf", "pwd", "true", "false", "test", "[", "[[", "export",
    "set", "unset", "source", ".", "mkdir", "touch", "rm", "cp", "mv", "ln", "which", "type", "jq", "sleep",
    "date", "basename", "dirname", "realpath", "readlink", "tee", "stat", "file", "chmod", "wait", "kill",
    "ps", "nl", "less", "more", "column", "xcbeautify", "xcpretty", "exit", "return", "local", "read",
}
QUICK_GIT = {
    "status", "log", "diff", "show", "rev-parse", "branch", "merge-base", "ls-files", "add", "commit",
    "checkout", "switch", "stash", "restore", "reset", "tag", "config", "remote", "worktree", "blame",
    "grep", "cat-file", "rev-list", "describe", "symbolic-ref", "update-ref", "merge-tree", "cherry",
    "shortlog", "reflog", "mv", "rm", "apply", "ls-tree", "for-each-ref", "show-ref", "name-rev", "var",
}
INTERPRETERS = re.compile(r"^(sh|bash|zsh|dash|node|ruby|perl|python[0-9.]*)$")
SUBCOMMAND = re.compile(r"^[A-Za-z][\w:.-]*$")
VALUE_OPTIONS = {  # options whose value is the next word, so it is not taken for the subcommand
    "make": {"-C", "-f", "-I", "--directory", "--file"},
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace"},
    "npm": {"--prefix", "-w", "--workspace"},
    "pnpm": {"-C", "--dir", "--filter", "-F"},
    "yarn": {"--cwd"},
    "swift": {"--package-path", "--scratch-path", "-c", "--configuration"},
    "go": {"-C"},
}
XCODE_ACTIONS = {"build", "build-for-testing", "analyze", "archive", "test", "test-without-building",
                 "installsrc", "install", "clean", "docbuild"}


def family(words):
    """A simple command's family, or None for one that only looks or finishes at once."""
    words = core(words)
    if not words:
        return None
    tool = os.path.basename(words[0])
    if tool in QUICK:
        return None
    args = words[1:]
    if tool == "xcodebuild":
        actions = [w for w in args if w in XCODE_ACTIONS]
        if any("test" in a for a in actions):
            return "xcodebuild test"
        if actions:
            return "xcodebuild " + " ".join(sorted(set(actions)))
        quick = [w for w in args if w in ("-list", "-version", "-showsdks", "-showBuildSettings", "-showdestinations")]
        return "xcodebuild " + quick[0] if quick else "xcodebuild build"
    if tool in ("py.test", "pytest"):
        return "pytest"
    if INTERPRETERS.match(tool):
        if tool.startswith("python") and "-m" in args and args.index("-m") + 1 < len(args):
            module = args[args.index("-m") + 1]
            return "pytest" if module == "pytest" else f"python -m {module}"
        script = next((w for w in args if not w.startswith("-")), None)
        return f"{tool} {os.path.basename(script)}" if script else tool
    sub = None
    takes = VALUE_OPTIONS.get(tool, set())
    rest = list(args)
    while rest:
        word = rest.pop(0)
        if word in takes:
            if rest:
                rest.pop(0)
        elif word.startswith("-") or word.startswith("+"):
            continue
        else:
            sub = word if SUBCOMMAND.match(word) else None
            break
    if tool in ("npm", "pnpm", "yarn", "bun") and sub in ("run", "run-script"):
        sub = next((w for w in rest if not w.startswith("-")), None)
    if tool == "git" and sub in QUICK_GIT:
        return None
    return f"{tool} {sub}" if sub else tool


def families(command):
    return {f for f in map(family, segments(command)) if f}


# --- check 1: the long-run speed bump ---------------------------------------------------------------

def long_run(payload, command, calls, size, env):
    limit = cache_seconds(env)
    current = payload.get("tool_use_id")
    earlier = [c for c in calls if c["id"] != current]
    if any(c["command"] == command and LONG_TAG in c["result"] for c in earlier):
        return None  # the identical re-issue after a refusal: the agent chose to run it
    wanted = families(command)
    if not wanted:
        return None
    for call in reversed(earlier):
        if call["issued"] is None or call["ended"] is None:
            continue
        wait = call["ended"] - call["issued"]
        if wait > limit and wanted & families(call["command"]):
            minutes = max(1, int(wait) // 60)
            return (
                f"{LONG_TAG} An earlier run of this kind came back {minutes} minute{'' if minutes == 1 else 's'} "
                f"after the turn that issued it, longer than the prompt cache lasts, so the turn after it rewrote "
                f"this agent's whole context, now {size // 1000}k tokens. If this run is narrower and should "
                "finish within the cache lifetime, or is needed anyway, re-issue the identical command and it "
                "runs. Otherwise commit what is verified and hand back, naming this command in your report, so "
                "the orchestrator queues it on a cheap runner."
            )
    return None


# --- check 2: a reviewer never moves a shared checkout ----------------------------------------------

GIT_GLOBAL_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
MOVES = {"checkout", "switch", "reset", "restore", "commit", "merge", "rebase", "cherry-pick", "revert",
         "am", "pull", "clean", "rm", "mv", "add", "update-ref", "push"}
BRANCH_MOVES = {"-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
                "-u", "--set-upstream-to", "--unset-upstream", "--edit-description"}
BRANCH_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                "--format", "--color", "--abbrev"}
TAG_LISTS = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "-n", "--sort",
             "--format", "--column", "-v", "--verify"}
WORKTREE_ADD_VALUE = {"-b", "-B", "--reason", "--orphan"}


def resolve(base, path):
    """`path` against `base`, as a directory the shell would reach; a `$VAR` path is kept as written."""
    path = os.path.expanduser(path)
    if path.startswith("$"):
        return path
    return os.path.realpath(os.path.join(base, path))


def inside(path, roots):
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def git_call(words, cwd):
    """(effective directory, subcommand, its arguments) of a git simple command, or None."""
    words = core(words)
    if not words or os.path.basename(words[0]) != "git":
        return None
    rest, here = words[1:], cwd
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)  # a `--git-dir=…` form carries its value in the same word
        if option in GIT_GLOBAL_VALUE and rest:
            value = rest.pop(0)
            if option == "-C":
                here = resolve(here, value)
    if not rest:
        return None
    return here, rest[0], rest[1:]


def positionals(args, takes=frozenset()):
    found, rest = [], list(args)
    while rest:
        word = rest.pop(0)
        if word == "--":
            found.extend(rest)
            break
        if word in takes:
            if rest:
                rest.pop(0)
        elif not word.startswith("-"):
            found.append(word)
    return found


def moves(sub, args):
    """Whether a git subcommand moves HEAD, the index, the worktree or refs."""
    if sub in MOVES:
        return True
    if sub == "stash":
        return not args or args[0] not in ("list", "show")
    if sub == "apply":
        return not any(a in ("--check", "--stat", "--numstat", "--summary") for a in args)
    if sub == "branch":
        if any(a.split("=", 1)[0] in BRANCH_MOVES for a in args):
            return True
        if any(a in ("-l", "--list", "--show-current") for a in args):
            return False
        return bool(positionals(args, BRANCH_VALUE))
    if sub == "tag":
        return bool(args) and not any(a.split("=", 1)[0] in TAG_LISTS for a in args)
    if sub == "symbolic-ref":
        return any(a in ("-d", "--delete") for a in args) or len(positionals(args, {"-m"})) >= 2
    if sub == "worktree":
        return bool(args) and args[0] in ("remove", "move", "prune")
    return False


def walk(command, cwd, added):
    """Each git call in a command line as (directory, subcommand, arguments), in order, following `cd`.
    A `git worktree add` in the line joins `added` as it is met."""
    found = []
    for words in segments(command):
        plain = core(words)
        if plain[:1] in (["cd"], ["pushd"]):
            target = next((w for w in plain[1:] if not w.startswith("-")), None)
            if target:
                cwd = resolve(cwd, target)
            continue
        call = git_call(words, cwd)
        if not call:
            continue
        here, sub, args = call
        if sub == "worktree" and args[:1] == ["add"]:
            path = next(iter(positionals(args[1:], WORKTREE_ADD_VALUE)), None)
            if path:
                added.add(resolve(here, path))
        found.append(call)
    return found


def reviewer(payload, command, calls):
    cwd = payload.get("cwd") or os.getcwd()
    added = set()
    current = payload.get("tool_use_id")
    for call in calls:
        if call["id"] != current:
            walk(call["command"], cwd, added)
    for here, sub, args in walk(command, cwd, added):
        if sub == "worktree" and args[:1] in (["remove"], ["move"]):
            target = next(iter(positionals(args[1:])), None)
            if target and inside(resolve(here, target), added):
                continue  # removing or moving a worktree it added itself
        if moves(sub, args) and not inside(here, added):
            return (
                f"{REVIEWER_TAG} `git {sub}` changes a checkout, and a reviewer never moves a checkout others "
                "use. To build or test a revision, add a throwaway worktree in your scratch directory "
                "(`git worktree add <scratch>/<name> <rev> --detach`), work there, and remove it before "
                "reporting. To see whether branches overlap, `git merge-tree --write-tree <a> <b>` touches "
                "no checkout."
            )
    return None


# --- the hook -------------------------------------------------------------------------------------

def own_name(root):
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        return json.load(f)["name"]


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decision(payload, env):
    """The hook's output for this call, or None to leave the call alone."""
    if not payload.get("agent_id") or payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    path = transcript(payload)
    calls, size = history(path) if path and os.path.isfile(path) else ([], 0)
    root = env.get("CLAUDE_PLUGIN_ROOT")
    if env.get("DELEGATE_REVIEWER_GUARD", "").strip() != "0" and root \
            and payload.get("agent_type") == f"{own_name(root)}:reviewer":
        reason = reviewer(payload, command, calls)
        if reason:
            return deny(reason)
    if env.get("DELEGATE_LONG_RUN_GUARD", "").strip() != "0":
        reason = long_run(payload, command, calls, size, env)
        if reason:
            return deny(reason)
    return None


def main():
    try:
        # Read stdin as UTF-8 explicitly: on native Windows Python, sys.stdin decodes with the
        # locale code page, which can fail json.load on a non-ASCII UTF-8 payload.
        out = decision(json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")), os.environ)
    except Exception:
        return
    if out:
        json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
