#!/usr/bin/env python3
"""PreToolUse on Bash: two checks on a subagent's shell commands, each refusing a call with a tagged reason.

A speed bump before repeating a long run (any subagent). A subagent's prompt cache lasted five minutes
where this was measured, and a call that outlives it makes the next turn rewrite the whole context.
Measured on one machine over seven days: 268 cold subagent turns followed a Bash call, 193 of them a
Swift build or test, after a median wait of nine minutes in a 72k context; the 56 agents that went cold
two or more times held 54% of the avoidable cost. The PostToolUse cache-expired line arrives after the
wait, and it did not stop the repeats: the cold share of subagent spend went from 7.1% to 8.6% with it
in place. So this check acts before the next run instead.

Only a build or test has a key: a simple command that, once `cd`, `NAME=value` assignments and wrappers
(`timeout N`, `gtimeout N`, `time [-p]`, `nice [-n N]`, `env …`, `nohup`, `command`, `caffeinate …`,
`lockf [-k…] <file>`, `sift run --`, and `xcrun` before `swift`/`xcodebuild`) are stripped, is `swift
build|test`, any `xcodebuild`, `cargo|go|dotnet build|test`, `npm|pnpm|yarn|bun [run] test|build`,
`pytest` or `python -m pytest`, `make`, `gradle`, `gradlew` or `mvn` (the build/test set the agent-cost
report uses). Its key is the program with all its arguments, redirections removed; a pipe's other side is
another command. So `cd /a && time -p swift test 2>&1 | tail -5` has the key `swift test`, and `swift test
--filter A` another. Every other command has no key and is never refused. The key leaves the directory
out, so `cd /a && swift test` and `cd /b && swift test` share one: a long run in one package refuses the
first identical run in another, once.

An earlier Bash call whose result came back more than the cache lifetime after the turn that issued it
makes its keys "long"; a backgrounded call counts until the notification that it finished, since that
wakes the agent cold. A command with a long key is refused once, with the earlier wait, the context size
and the choice: re-issue the identical command to run it anyway, or commit what is verified and hand the
run back for the orchestrator to queue on a cheap runner. The identical re-issue passes, recognised by an
earlier call with the same command whose result carries this check's tag; a narrower run (other
arguments) has another key and passes without asking. The whole transcript is read, keeping only the
lines that can hold a Bash call, a tool result or a completion notification.
`DELEGATE_SUBAGENT_CACHE_SECONDS` overrides the 300-second lifetime; `DELEGATE_LONG_RUN_GUARD=0` turns the
check off.

A reviewer never moves a checkout or refs that others use (only this plugin's reviewer). Observed once: a
read-only reviewer detached HEAD in the primary checkout other agents were working from. What others use
is told apart by age: a checkout, or a repository, whose `.git` entry was created before the reviewer's
transcript began. Anything the reviewer made itself (a `git init` repository, a worktree it added,
however it spelled the path) is younger, and so is free.

- A git command that moves a checkout (checkout, switch, reset, restore, stash, commit, merge, rebase,
  cherry-pick, revert, am, pull, clean, rm, mv, add, apply, bisect, read-tree, submodule update) is
  refused when its directory lies in an older checkout: the nearest ancestor holding a `.git` entry.
  Its directory is `git -C`, else the last `cd`, `pushd` or `popd` before it in the line, else the call's
  `cwd`, with `~`, `$HOME` and variables assigned earlier in the same command expanded. A `cd`, `pushd`,
  `popd` or assignment inside a subshell `( … )` or a substitution (`$( … )`, backticks) lasts until the
  group closes, nested groups included; one in a brace group `{ …; }` persists. `pushd` and `popd` keep a
  directory stack for the line, known whole since each call starts a fresh shell, and follow bash: a
  rotation (`+N`, `-N`) moves to the entry it names, and a call bash rejects (`popd` on an empty stack, an
  index out of range) stays put. A directory that still holds an unexpanded variable, or does not exist, is
  unknown and allowed; so is one inside a repository or worktree that a `git init`, `git clone` or `git
  worktree add` earlier in the same line creates (below). A `cd`, `pushd` or `popd` into a directory
  that is not there when the hook runs may fail and leave the shell where it was. Calls joined to it by `&&` (a pipe
  within that chain included) run only if it succeeded, so they are judged from its target alone; after
  the chain breaks (`;`, a newline, `||`, a single `&`), every later call is judged from the directory
  before it as well, and refused if either refuses, until its group closes or the line ends. So
  `git worktree add <dir> <rev> && cd <dir> && git checkout -b x` is allowed and the same joined by `;`
  is not: if the add fails, the checkout runs where the line started.
- A git command that moves refs every worktree shares (branch -f/-d/-m/-c/-u, update-ref, symbolic-ref
  with two arguments, tag creating or deleting, push, stash, notes add/edit/remove/append/copy/merge/prune,
  worktree add -B, and worktree remove, move or prune of an older worktree) is refused when the
  repository (its common git directory) is older, from whatever directory it runs; unless the directory
  lies in a repository that a `git init` or `git clone` earlier in the same line creates.

What a line creates: `git init` makes a repository in a directory with no `.git` entry, empty or not;
`git clone` and `git worktree add` make one only in a directory that is missing or empty, since git
refuses any other (and a line joined by `;` goes on into the older checkout). A call is in what the line
created only if no `.git` lies between the two: a checkout already nested inside the new one is its own,
older, repository.

Creation time is `st_birthtime` where the platform keeps it. Elsewhere it is the oldest modification time
among the `.git` entry and, for a directory, its immediate entries: a worktree's `.git` file is written
once, and a repository's `.git` directory holds files git writes at init and never rewrites
(`description`, say), while the directory's own time changes with every index write. Re-issuing does not
lift a refusal. Not looked into, so allowed: a command hidden in a string (`sh -c '…'`, `eval`) or fed by
`xargs`, a bare repository, and a branch moved by committing in a younger worktree that has it checked
out. `DELEGATE_REVIEWER_GUARD=0` turns the check off.

A main-session call carries no `agent_id` and is left alone. Anything unexpected exits silently: a
guard is never worth breaking a tool call.
"""
import io
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

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


def relevant(raw):
    """Whether a transcript line can hold a Bash call, a tool result or a completion notification."""
    return b'"tool_result"' in raw or b"<task-notification>" in raw or (b'"tool_use"' in raw and b'"Bash"' in raw)


def parsed(raw):
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def history(path):
    """(Bash calls in order as dicts {id, command, issued, ended, result}, the latest context size, the
    transcript's first timestamp), from the whole transcript."""
    with open(path, "rb") as f:
        lines = f.read().splitlines()
    calls, by_id = [], {}
    for raw in lines:
        if not relevant(raw):
            continue
        entry = parsed(raw)
        if entry is None:
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        stamp = epoch(entry.get("timestamp"))
        if entry.get("type") == "assistant":
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
    return calls, context_size(lines), first_stamp(lines)


def context_size(lines):
    """The context size the latest assistant line reports, or 0."""
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        entry = parsed(raw)
        message = entry.get("message") if entry and entry.get("type") == "assistant" else None
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict) and usage:
            return sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 0


def first_stamp(lines):
    """The transcript's first timestamp in epoch seconds: when this agent started. None if there is none."""
    for raw in lines:
        entry = parsed(raw)
        stamp = epoch(entry.get("timestamp")) if entry else None
        if stamp is not None:
            return stamp
    return None


# --- splitting a command line into simple commands ---------------------------------------------------

OPEN, CLOSE = "(", ")"  # with segments(…, groups=True): where a subshell or substitution opens and closes
BREAK = ";"  # with segments(…, groups=True): where an `&&` chain ends, so what follows runs even if it failed

def segments(command, groups=False):
    """The simple commands of a shell command line, each a list of words with quotes removed. Splits on
    `;`, `&`, `|`, newlines, parentheses, `$(` and backticks; drops redirections and here-document bodies.
    Not a shell: a command hidden inside a quoted string (`sh -c '…'`) stays one word. With `groups`, a
    subshell or substitution (`(`, `$(`, a backtick pair) is also marked where it opens and closes, by the
    strings OPEN and CLOSE among the word lists; a `)` that closes nothing (a `case` pattern) is no mark.
    Also with `groups`, the string BREAK marks each separator after which the next command runs whether or
    not the one before succeeded: `;`, `;;`, `||`, a single `&` (not `&>`), and a newline that does not
    follow `&&` or `|`. `&&` and a pipe (`|`, `|&`) are no mark."""
    out, words, word, has = [], [], [], False
    opened = []  # the groups open at this point: "(" for `(` and `$(`, "`" for a backtick
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
            j = i - 1
            while j >= 0 and command[j] in " \t":
                j -= 1
            if groups and (j < 0 or not (command[j] == "|" or command[j - 1:j + 1] in ("&&", "|&"))):
                out.append(BREAK)  # a newline after `&&` or a pipe carries the chain on to the next line
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
            step = 2 if c == "$" or command.startswith(("&&", "||", "|&", ";;"), i) else 1
            op, i = command[i:i + step], i + step
            if not groups:
                continue
            if op in (";", ";;", "||") or (op == "&" and command[i:i + 1] != ">"):
                out.append(BREAK)
                continue
            if c in "$(" or (c == "`" and opened[-1:] != ["`"]):
                opened.append("`" if c == "`" else "(")
                out.append(OPEN)
            elif c in ")`" and opened and (opened[-1] == "`") == (c == "`"):
                opened.pop()
                out.append(CLOSE)
        else:
            word.append(c)
            has = True
            i += 1
    end_segment()
    return out


KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "fi", "done", "esac"}
HEADERS = {"for", "case", "select", "function"}  # a segment opening with one of these names no command
WRAPPERS = {  # wrapper: (options that take the next word as their value, words it takes after its options)
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "gtimeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "time": (set(), 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "env": ({"-u", "--unset", "-C", "--chdir", "-S", "-P"}, 0),
    "nohup": (set(), 0),
    "command": (set(), 0),
    "caffeinate": ({"-t", "-w"}, 0),
    "lockf": ({"-t"}, 1),
}
XCRUN_VALUE = {"-sdk", "--sdk", "-toolchain", "--toolchain"}
XCRUN_TOOLS = {"swift", "xcodebuild"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def core(words):
    """A simple command's words with env assignments, shell keywords and wrappers stripped from its front,
    or [] if it names no command (a loop header, a bare assignment)."""
    words = list(words)
    while words:
        head = words[0]
        name = os.path.basename(head)
        if head in HEADERS:
            return []
        if head in KEYWORDS or ASSIGNMENT.match(head):
            words.pop(0)
        elif name == "sift" and words[1:2] == ["run"]:
            words = words[words.index("--") + 1:] if "--" in words else _after_options(words[2:])
        elif name == "xcrun":
            rest = _after_options(words[1:], XCRUN_VALUE)
            if not rest or os.path.basename(rest[0]) not in XCRUN_TOOLS:
                break
            words = rest
        elif name in WRAPPERS:
            takes, operands = WRAPPERS[name]
            words = _after_options(words[1:], takes)
            if name == "env":
                while words and ASSIGNMENT.match(words[0]):
                    words.pop(0)
            words = words[operands:]
        else:
            break
    return words


def _after_options(words, takes=frozenset()):
    words = list(words)
    while words and words[0].startswith("-") and words[0] != "-":
        if words.pop(0) in takes and words:
            words.pop(0)
    return words


def subcommand(tool, args):
    """A tool's first word that is not an option or an option's value, and the words after it."""
    takes = VALUE_OPTIONS.get(tool, set())
    rest = list(args)
    while rest:
        word = rest.pop(0)
        if word in takes:
            if rest:
                rest.pop(0)
        elif not (word.startswith("-") or word.startswith("+")):
            return word, rest
    return None, []


# --- check 1: the long-run speed bump ---------------------------------------------------------------

VALUE_OPTIONS = {  # options whose value is the next word, so it is not taken for the subcommand
    "npm": {"--prefix", "-w", "--workspace"},
    "pnpm": {"-C", "--dir", "--filter", "-F"},
    "yarn": {"--cwd"},
    "swift": {"--package-path", "--scratch-path", "-c", "--configuration"},
    "go": {"-C"},
    "cargo": {"-C", "--manifest-path"},
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"},
}
ALWAYS_BUILDS = {"xcodebuild", "make", "gradle", "gradlew", "mvn", "pytest"}
PYTHON = re.compile(r"^python[0-9.]*$")


def builds_or_tests(words):
    """Whether a stripped simple command is a build or a test."""
    tool, args = os.path.basename(words[0]), words[1:]
    if tool in ALWAYS_BUILDS:
        return True
    if PYTHON.match(tool):
        for i, word in enumerate(args):
            if word == "-m":
                return args[i + 1:i + 2] == ["pytest"]
            if not word.startswith("-"):
                return False
        return False
    sub, rest = subcommand(tool, args)
    if tool in ("swift", "cargo", "go", "dotnet"):
        return sub in ("build", "test")
    if tool in ("npm", "pnpm", "yarn", "bun"):
        if sub in ("run", "run-script"):
            sub = next((w for w in rest if not w.startswith("-")), None)
        return bool(sub and re.match(r"(test|build)\b", sub))
    return False


def keys(command):
    """The build and test commands of a line, each normalised to its program and arguments."""
    found = set()
    for words in segments(command):
        words = core(words)
        if words and builds_or_tests(words):
            found.add(" ".join([os.path.basename(words[0])] + words[1:]))
    return found


def long_run(payload, command, calls, size, env):
    wanted = keys(command)
    if not wanted:
        return None
    limit = cache_seconds(env)
    current = payload.get("tool_use_id")
    earlier = [c for c in calls if c["id"] != current]
    if any(c["command"] == command and LONG_TAG in c["result"] for c in earlier):
        return None  # the identical re-issue after a refusal: the agent chose to run it
    for call in reversed(earlier):
        if call["issued"] is None or call["ended"] is None:
            continue
        wait = call["ended"] - call["issued"]
        if wait > limit and wanted & keys(call["command"]):
            minutes = max(1, int(wait) // 60)
            return (
                f"{LONG_TAG} An earlier run of this command came back {minutes} minute{'' if minutes == 1 else 's'} "
                f"after the turn that issued it, longer than the prompt cache lasts, so the turn after it rewrote "
                f"this agent's whole context, now {size // 1000}k tokens. A narrower run (other arguments, such "
                "as a test filter) is not refused. If this one is needed anyway, re-issue the identical command "
                "and it runs. Otherwise commit what is verified and hand back, naming this command in your "
                "report, so the orchestrator queues it on a cheap runner."
            )
    return None


# --- check 2: a reviewer never moves a checkout or refs others use ----------------------------------

GIT_GLOBAL_VALUE = VALUE_OPTIONS["git"]
MOVES_CHECKOUT = {"checkout", "switch", "reset", "restore", "commit", "merge", "rebase", "cherry-pick", "revert",
                  "am", "pull", "clean", "rm", "mv", "add", "read-tree"}
BRANCH_MOVES = {"-f", "--force", "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
                "-u", "--set-upstream-to", "--unset-upstream", "--edit-description"}
TAG_READS = {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "-v",
             "--verify"}  # list and verify modes: nothing they name is created
TAG_VALUE = {"-m", "--message", "-F", "--file", "--trailer", "--cleanup", "-u", "--local-user", "--contains",
             "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
NOTES_MOVES = {"add", "edit", "remove", "append", "copy", "merge", "prune"}
WORKTREE_ADD_VALUE = {"-b", "-B", "--reason", "--orphan"}
INIT_VALUE = {"--template", "--separate-git-dir", "-b", "--initial-branch", "--object-format", "--ref-format"}
CLONE_VALUE = {"-o", "--origin", "-b", "--branch", "-u", "--upload-pack", "--template", "-c", "--config",
               "--depth", "--shallow-since", "--shallow-exclude", "--reference", "--reference-if-able",
               "--separate-git-dir", "-j", "--jobs", "--filter", "--bundle-uri", "--server-option", "--ref-format"}
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def created(path):
    """When the file or directory at `path` was created, in epoch seconds (raises OSError). The birth time
    where the platform keeps one; elsewhere the oldest modification time among the entry and, for a
    directory, its immediate entries, which stays put for a `.git` directory nobody re-initialised."""
    info = os.lstat(path)
    birth = getattr(info, "st_birthtime", None)
    if birth:
        return birth
    times = [info.st_mtime]
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    times.append(entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    return min(times)


def older(path, start):
    """Whether `path` was created before the reviewer started; False if that cannot be told."""
    try:
        return created(path) < start
    except OSError:
        return False


def expand(path, names, env):
    """`path` with `~`, `$HOME` and the line's own assignments expanded; None if a variable is unknown."""
    home = env.get("HOME") or os.path.expanduser("~")
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]

    def value(match):
        name = match.group(1) or match.group(2)
        known = dict(names, HOME=home) if "HOME" not in names else names
        return known.get(name) if known.get(name) else match.group(0)

    path = VARIABLE.sub(value, path)
    return None if "$" in path or "`" in path else path


def resolve(base, path, names, env):
    """The directory the shell would reach from `base` by `path`, or None if it cannot be told."""
    path = expand(path, names, env)
    if path is None or (base is None and not os.path.isabs(path)):
        return None
    return os.path.realpath(os.path.join(base or "", path))


def inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def dot_git(directory):
    """The `.git` entry (file or directory) of the nearest ancestor of `directory` that has one, or None."""
    here = directory
    while True:
        candidate = os.path.join(here, ".git")
        if os.path.lexists(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def gitdir_of(entry):
    """The git directory a `.git` entry stands for: itself, or the `gitdir:` a worktree's `.git` file names."""
    if os.path.isdir(entry):
        return entry
    with open(entry, encoding="utf-8") as f:
        line = f.readline().strip()
    if not line.startswith("gitdir:"):
        return None
    return os.path.realpath(os.path.join(os.path.dirname(entry), line[len("gitdir:"):].strip()))


def common_dir(entry):
    """The repository's common git directory, shared by all its worktrees, or None."""
    try:
        git_dir = gitdir_of(entry)
        if git_dir is None:
            return None
        common = os.path.join(git_dir, "commondir")
        if os.path.isfile(common):
            with open(common, encoding="utf-8") as f:
                return os.path.realpath(os.path.join(git_dir, f.read().strip()))
        return git_dir
    except (OSError, UnicodeDecodeError):
        return None


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


def stash_moves(args):
    """Whether a `git stash` moves anything: every form but `list` and `show`, read past a leading `-q`. Any
    other leading option (`-m list`, `-u`) makes it a push."""
    first = next((a for a in args if a not in ("-q", "--quiet")), None)
    return first not in ("list", "show")


def tag_moves(args):
    """Whether a `git tag` creates or deletes a tag: it does with `-d`, or when it names a tag outside list
    mode (`-l`, `-n`, a filter such as `--contains`) and verify mode (`-v`). An option's value, a separate
    word or the rest of a short cluster (`-amx`), is not a name."""
    seen, names, rest = set(), [], list(args)
    while rest:
        word = rest.pop(0)
        if word == "--":
            names += rest
            break
        if word.startswith("--"):
            seen.add(word.split("=", 1)[0])
            if word in TAG_VALUE and rest:
                rest.pop(0)
        elif word.startswith("-") and word != "-":
            for at, letter in enumerate(word[1:], 2):
                seen.add("-" + letter)
                if letter in "mFun":  # the rest of the cluster is its value; for -m, -F and -u, else the next word
                    if letter != "n" and at == len(word) and rest:
                        rest.pop(0)
                    break
        else:
            names.append(word)
    if seen & {"-d", "--delete"}:
        return True
    return bool(names) and not seen & TAG_READS


def moves_checkout(sub, args):
    """Whether a git subcommand moves HEAD, the index or the files of the checkout it runs in."""
    if sub == "checkout" and all(a in ("-q", "--quiet", "--progress", "--no-progress") for a in args):
        return False  # a bare checkout only reports where HEAD is
    if sub in MOVES_CHECKOUT:
        return True
    if sub == "stash":
        return stash_moves(args)
    if sub == "apply":
        return not any(a in ("--check", "--stat", "--numstat", "--summary") for a in args)
    if sub == "bisect":
        return bool(args) and args[0] not in ("log", "view", "visualize", "help")
    if sub == "submodule":
        return positionals(args)[:1] == ["update"]
    return False


def moves_refs(sub, args):
    """Whether a git subcommand moves refs that every worktree of the repository shares."""
    if sub in ("update-ref", "push"):
        return True
    if sub == "stash":
        return stash_moves(args)
    if sub == "branch":
        return any(a.split("=", 1)[0] in BRANCH_MOVES for a in args)
    if sub == "tag":
        return tag_moves(args)
    if sub == "symbolic-ref":
        return any(a in ("-d", "--delete") for a in args) or len(positionals(args, {"-m"})) >= 2
    if sub == "notes":
        return (positionals(args, {"--ref"}) or [None])[0] in NOTES_MOVES
    if sub == "worktree":
        return args[:1] == ["add"] and "-B" in args
    return False


def git_call(words, cwd, names, env):
    """(effective directory or None, subcommand, its arguments) of a git simple command, or None."""
    words = core(words)
    if not words or os.path.basename(words[0]) != "git":
        return None
    rest, here, tree, store = words[1:], cwd, None, None
    while rest and rest[0].startswith("-"):
        option, value = rest.pop(0), None
        if "=" in option and option.startswith("--"):
            option, value = option.split("=", 1)
        elif option in GIT_GLOBAL_VALUE and rest:
            value = rest.pop(0)
        if value is None:
            continue
        if option == "-C":
            here = resolve(here, value, names, env)
        elif option == "--work-tree":
            tree = resolve(here, value, names, env)
        elif option == "--git-dir":
            store = resolve(here, value, names, env)
    if not rest:
        return None
    return tree or store or here, rest[0], rest[1:]


def stacked(plain, cwd, stack, names, env):
    """The directory after a `pushd` or `popd` (`plain`, its words), updating `stack` (the top last) in place,
    as bash does. In bash's `dirs` order, entry 0 the current directory then the stack from its top, `pushd +N`
    rotates entry N to the front and changes to it (with `-n`, stays and drops it), `popd +N` removes entry N
    and changes directory only for 0 (with `-n`, removes entry 1 instead and stays), and `-N` counts from the
    right. A call bash rejects (an index out of range, `popd` with no stack, a bad option) stays put."""
    keep, index, target, words = False, None, None, plain[1:]
    while words:
        word = words.pop(0)
        if word == "-n":
            keep = True  # change the stack only, not the directory
        elif word == "--":
            target = words[0] if words and plain[0] == "pushd" else None  # popd ignores what follows
            break
        elif re.fullmatch(r"[+-]\d+", word):
            index = int(word[1:]) if word[0] == "+" else len(stack) - int(word[1:])  # entry `index` of `dirs`
            if plain[0] == "pushd" and not 0 <= index <= len(stack):
                return cwd  # pushd checks each index as it reads it
        elif word == "-" and plain[0] == "pushd":
            break  # kept as a bare pushd
        elif word.startswith(("-", "+")):
            return cwd
        else:
            target = word
            break
    if plain[0] == "popd":
        if target is not None or not stack or not 0 <= (index or 0) <= len(stack):
            return cwd  # popd takes no directory, and fails on an empty stack or an index out of range
        if index:
            del stack[len(stack) - index]
            return cwd
        top = stack.pop()
        return cwd if keep else top
    if index is not None:
        entries = [cwd, *reversed(stack)]
        entries = entries[index:] + entries[:index]
        stack[:] = reversed(entries[1:])
        return cwd if keep else entries[0]
    if target is None:  # a bare pushd swaps the directory with the top of the stack
        if not stack:
            return cwd
        top, stack[-1] = stack[-1], cwd
        return top
    there = resolve(cwd, target, names, env)
    if keep:
        stack.append(there)
        return cwd
    stack.append(cwd)
    return there


MAX_STATES = 64  # where a line may be: beyond this many, the latest candidates are dropped


def walk(command, cwd, env):
    """Each git call in a command line, in order, as (directory, subcommand, arguments, checkouts the line
    has created by then, repositories the line has created by then, the line's variables by then),
    following `cd` and assignments. A call that may run in more than one directory comes once for each.

    Where the line may be is a list of states, each a directory and its `pushd` stack. A `cd`, `pushd` or
    `popd` to a directory that is not there may fail, leaving the shell where it was; calls joined to it by
    `&&` run only if it succeeded, so the state it left behind becomes a candidate again only at the next
    BREAK. The list is capped at MAX_STATES."""
    found, names, new_checkouts, new_repos = [], {}, set(), set()
    states = [(cwd, ())]  # where the line may be now: (directory, `pushd` stack, top last)
    fallback = []  # where it is instead if a move since the last BREAK failed
    entered = []  # what each open subshell or substitution left behind

    def fresh(path, sub):
        """Whether `sub` makes a new repository or worktree at `path`: git init does in any directory with no
        `.git`; git clone and git worktree add refuse a directory that is there and not empty."""
        if path is None or os.path.lexists(os.path.join(path, ".git")):
            return False
        if sub == "init" or not os.path.lexists(path):
            return True
        try:
            return os.path.isdir(path) and not os.listdir(path)
        except OSError:
            return False

    def note(here, sub, args):
        """Record what a git call at `here` creates."""
        if here is not None and sub in ("init", "clone"):
            spots = positionals(args, INIT_VALUE if sub == "init" else CLONE_VALUE)
            if sub == "init":
                target = resolve(here, spots[0], names, env) if spots else here
            elif len(spots) >= 2:
                target = resolve(here, spots[1], names, env)
            elif spots:
                target = resolve(here, re.sub(r"\.git$", "", spots[0].rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]), names, env)
            else:
                target = None
            if fresh(target, sub):
                new_checkouts.add(target)
                new_repos.add(target)
        elif here is not None and sub == "worktree" and args[:1] == ["add"]:
            target = next(iter(positionals(args[1:], WORKTREE_ADD_VALUE)), None)
            target = resolve(here, target, names, env) if target else None
            if fresh(target, sub):
                new_checkouts.add(target)

    for words in segments(command, groups=True):
        if words == OPEN:  # a group inherits where the line may be; a move failing inside it ends the group
            entered.append((states, fallback, dict(names)))
            fallback = []
            continue
        if words == CLOSE:
            if entered:
                states, fallback, names = entered.pop()
            continue
        if words == BREAK:
            states, fallback = list(dict.fromkeys(states + fallback))[:MAX_STATES], []
            continue
        plain = core(words)
        if not plain or plain[0] in ("export", "local", "declare", "typeset", "readonly"):
            for word in (words if not plain else plain[1:]):
                if ASSIGNMENT.match(word):
                    name, value = word.split("=", 1)
                    names[name] = expand(value, names, env) or ""  # "" also stands for `$(…)`: unknown
            continue
        if plain[0] in ("cd", "pushd", "popd"):
            moved = []
            for cwd, stack in states:
                if plain[0] == "cd":
                    target = next((w for w in plain[1:] if not w.startswith("-") or w == "-"), "~")
                    there, after = (None if target == "-" else resolve(cwd, target, names, env)), stack
                else:
                    after = list(stack)
                    there = stacked(plain, cwd, after, names, env)
                moved.append((there, tuple(after)))
                if there is not None and not os.path.isdir(there):
                    fallback.append((cwd, stack))  # it may fail, and the shell stays here
            states = list(dict.fromkeys(moved))
            continue
        calls = [call for call in (git_call(words, cwd, names, env) for cwd, _ in states) if call]
        for call in calls:
            note(*call)
        for here, sub, args in calls:
            found.append((here, sub, args, set(new_checkouts), set(new_repos), dict(names)))
    return found


def stale_worktrees(common):
    """The admin directories `git worktree prune` would delete: worktrees whose directory is gone."""
    stale, admin = [], os.path.join(common, "worktrees")
    try:
        names = os.listdir(admin)
    except OSError:
        return stale
    for name in names:
        try:
            with open(os.path.join(admin, name, "gitdir"), encoding="utf-8") as f:
                if not os.path.exists(f.read().strip()):
                    stale.append(os.path.join(admin, name))
        except (OSError, UnicodeDecodeError):
            continue
    return stale


def covers(root, here, entry):
    """Whether what the line created at `root` holds a call in `here`, whose nearest `.git` is `entry`:
    `here` is inside it, and that `.git` does not belong to a checkout nested strictly inside it."""
    owner = os.path.dirname(entry)
    return inside(here, root) and (owner == root or not inside(owner, root))


def refusal(here, sub, args, new_checkouts, new_repos, start, names, env):
    """Why this git call must not run, or None."""
    if here is None or not os.path.isdir(here):
        return None  # unknown, or not there: allowed
    entry = dot_git(here)
    if entry is None:
        return None
    if sub == "worktree" and args[:1] in (["remove"], ["move"]):
        target = next(iter(positionals(args[1:], {"--reason"})), None)
        target = resolve(here, target, names, env) if target else None
        if target and older(os.path.join(target, ".git"), start):
            return "worktree"
        return None
    if sub == "worktree" and args[:1] == ["prune"]:
        common = common_dir(entry)
        if common and any(older(admin, start) for admin in stale_worktrees(common)):
            return "worktree"
        return None
    if moves_refs(sub, args) and not any(covers(r, here, entry) for r in new_repos):
        common = common_dir(entry)
        if common and older(common, start):
            return "refs"
    if moves_checkout(sub, args) and not any(covers(c, here, entry) for c in new_checkouts) and older(entry, start):
        return "checkout"
    return None


ADVICE = (
    "To build or test a revision, add a throwaway worktree in your scratch directory and join the add and "
    "the cd with `&&` (`git worktree add <scratch>/<name> <rev> --detach && cd <scratch>/<name> && …`), "
    "work there, and remove it before reporting: after a `;`, a cd into a directory that is not there yet "
    "is judged from the directory before it too. "
    "To see whether branches overlap, `git merge-tree --write-tree <a> <b>` touches no checkout."
)
WHAT = {
    "checkout": "changes a checkout that existed before this reviewer started, and a reviewer never moves a "
                "checkout others use.",
    "refs": "changes refs (branches, tags, the stash, the remote) that every checkout of a repository that "
            "existed before this reviewer started shares, and a reviewer never moves those.",
    "worktree": "removes or moves a worktree that existed before this reviewer started, and a reviewer never "
                "touches a checkout others use.",
}


def reviewer(payload, command, start, env):
    if start is None:
        return None
    cwd = payload.get("cwd")
    cwd = os.path.realpath(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
    for here, sub, args, new_checkouts, new_repos, names in walk(command, cwd, env):
        why = refusal(here, sub, args, new_checkouts, new_repos, start, names, env)
        if why:
            return f"{REVIEWER_TAG} `git {sub}` {WHAT[why]} {ADVICE}"
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
    calls, size, start = history(path) if path and os.path.isfile(path) else ([], 0, None)
    root = env.get("CLAUDE_PLUGIN_ROOT")
    if env.get("DELEGATE_REVIEWER_GUARD", "").strip() != "0" and root \
            and payload.get("agent_type") == f"{own_name(root)}:reviewer":
        reason = reviewer(payload, command, start, env)
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
