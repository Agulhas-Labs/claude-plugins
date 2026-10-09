#!/usr/bin/env python3
"""The hooks' handler: what the host reports becomes events on the board, without spending model tokens.

`gate.sh` runs first on every hook and starts this only when the session already has a board or the
event is one that can start one. So a short session never reaches Python at all.

A board starts when the task list reaches `LOGBOOK_STEPS` items (default 5), when the first
subagent starts, on the work call that brings the session's count to `LOGBOOK_CALLS`
(default 10), or at the end of a turn (`Stop`) once the count holds a call that changed something, so
a turn that changed something never ends without a board. A work call is a main-session `Write`,
`Edit`, `MultiEdit`, `NotebookEdit` or `Bash`, from `PostToolUse` or `PostToolUseFailure`; `gate.sh`
counts them, one byte each, `c` for a file tool that succeeded or a `git commit` and `x` for any
other, and this only reads the count (`board.calls_made`, `board.calls_changed`), removing its file
once the board has started. The steps, changes and commands
made before then are read back from the transcript, the call that started the board is applied on
top of them (it may or may not be in the transcript yet, so it is matched by its tool-use id and never
recorded twice), and the path is returned as `additionalContext`, which tells the model where the board is
and how to record on it. Nothing is printed to the terminal: the mod's band above the prompt points at the
board, and its button opens the page. Only the call that published the board returns the context: when two
hooks start it at once, the other records its own events on the board and prints nothing. What
`SubagentStart` adds to the context goes to the subagent, not the main session, and neither
`PostToolUseFailure` nor `Stop` is documented to carry context at all, so a board started on any of them
gives the model its context on the next main-session event that can carry it. The `announced` file, created
exclusively, makes that happen once. Nothing is ever printed to the terminal.

A work call records a `change` (the file, and the lines added and removed when the response says)
or a `command` (its text, `pass`, `fail` or `background`, the exit code a failure names, and how long
it took). A failed file change records nothing: the file did not change. A command that runs this
plugin's own `board.py` is not recorded, so the board does not list its own recording.

The host writes the transcript late, and a call whose hook ran before the board was published never
reached this handler. So every step, change, command and agent-info event carries its call's tool-use
id (`use`), and the first `CATCH_UP_LIMIT` events handled on a board each read the transcript again and
record, marked `early`, every such call whose id the log does not hold yet. A change or command read
back that way also carries `at`, the time its result reached the transcript. A command read back that
names `git` and printed the line git prints after it makes a commit (`[main c286754] Subject`)
also records a `commit` for each such line, with the hash git printed: a commit made before the board
started is behind the heads its `start` event recorded, so git alone would never show it. The call
that starts the board records its own the same way, and no other hook's call records a `commit`. An `Agent` or `Task` call is read back only when its result carries the subagent's id (`agentId`), as a launch in the background
and a call that waited for the report both do: that id is what joins it to the subagent's row. That recovery is for
calls made before the board existed and only for those: it stops at the first call the log holds as a
hook recorded, and it never runs again once the board has been closed.

`SessionStart` prunes boards older than the retention setting, and gives a board that is still open
its context back: after a compaction or a resume the model no longer has it. `gate.sh` starts this
for `SessionStart` only in a project that has a boards folder at all.

A work call made inside a subagent (it carries `agent_id`) is recorded with the subagent's id as
`agent`, never counts towards a start, and never carries the model's context, which would reach the
subagent. Not in this version: a task-list or `Agent`/`Task` call made inside a subagent is ignored,
and the subagent is one row under Agents. `Task` is the older hosts' name for the tool
that launches a subagent, read exactly as `Agent` is.

A board closed by `SessionEnd` is reopened by the next event from its session, of any kind but
`SessionEnd`: that event proves the session is still running, as when a resumed session outlives a
late `SessionEnd` from the process that exited. A board closed by hand (the `close` command) records
nothing more unless the session is resumed. A boards folder that is a symbolic link is never used.

A hook is never in the way. Any failure at all (an unreadable payload, a missing file, a transcript
of an unexpected shape) prints nothing and exits 0.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402

DEFAULT_STEPS = 5
DEFAULT_CALLS = 10
TITLE_LIMIT = 80
UNTITLED = board.UNTITLED
MESSAGE_LIMIT = 20000
ANNOUNCED_FILE = board.ANNOUNCED_FILE
CATCH_UP_FILE = board.CATCH_UP_FILE
CATCH_UP_LIMIT = 5
TASK_TOOLS = ("TaskCreate", "TaskUpdate", "TodoWrite")
# The tools whose calls are work calls: the gate counts them, and each records a change or a command.
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
WORK_TOOLS = FILE_TOOLS + ("Bash",)
# The tools that launch a subagent: `Task` is the older hosts' name for `Agent`.
AGENT_TOOLS = ("Agent", "Task")
# Every tool whose calls are read back from the transcript when a board starts late.
READ_TOOLS = TASK_TOOLS + WORK_TOOLS + AGENT_TOOLS
TOOL_EVENTS = ("PostToolUse", "PostToolUseFailure")
# The events whose hook output reaches the main session's context.
CARRIES_CONTEXT = ("PostToolUse", "UserPromptSubmit")
# The events that mark where a turn or a board begins or ends, rather than record anything in it.
TURN_MARKERS = ("turn-start", "turn-end", "start", "reopen", "session-tokens")
# A transcript larger than this is not read for its token total: the hook must stay quick.
TOKEN_READ_LIMIT = 64 * 1024 * 1024
DIGITS = re.compile(r"[0-9]+")
TRANSCRIPT_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z")
# The exit code a failed command's error names first, as the host writes it: `Exit code 3`.
EXIT_CODE = re.compile(r"(?:Error: )?Exit code (-?[0-9]+)\b")
# The line git prints after it makes a commit, at the start of a line of the command's output:
# `[main c286754] Subject`, `[main (root-commit) c286754] Subject`, `[detached HEAD c286754] Subject`.
COMMIT_LINE = re.compile(
    r"^\[(?:detached HEAD|(?P<branch>[^\s\[\]]+))(?: \(root-commit\))? (?P<hash>[0-9a-f]{7,40})\](?:[ \r]|$)",
    re.MULTILINE,
)
# A command that runs git: `git` as the command word, where a command can begin (the start, or after
# `;`, `&`, `|`, `(` or a new line), past variable assignments, `sudo` and the like, and a path to it.
# Text that only contains the word (`cat .git/HEAD`, `git-lfs pull`) does not run git.
NAMES_GIT = re.compile(
    r"(?:^|[;&|(\n])\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|sudo|time|env|exec|nice|then|do|else|!)\s+)*"
    r"(?:[\w.~/-]*/)?git(?![\w.-])"
)

CONTEXT = (
    "A logbook is recording this task at {page}. Record what only you know, one shell call each, "
    'batched with your other calls: python3 "{script}" <command> --board "{folder}". '
    'Commands: question "TEXT" --default "D" --affects "A" --reverse "R" | stop "TEXT" | answer Q3 "TEXT" | '
    'decision "TEXT" --why "W" --reverse "R" | deliverable "LABEL" --path P | '
    'check "WHAT IT PROVED" --command "C" --result pass|fail | title "TEXT". '
    "Files changed and commands run are recorded for you: record a check only for what a command's "
    "exit status does not show. "
    "For a question whose answer changes the work, record it with your default and keep going on the "
    "default, unless it is a hard stop. Record decisions worth a look, checks and deliverables as you "
    "make them. A line from the user like Q3: … is an answer: apply it and record it."
)


# ---------------------------------------------------------------------------------------------------
# Reading payloads tolerantly


def dig(value, *keys):
    """The value at a path of keys, or None as soon as something on the way is not an object."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def positive(env, name, default):
    value = env.get(name)
    if isinstance(value, str) and DIGITS.fullmatch(value) and int(value) > 0:
        return int(value)
    return default


def threshold(env):
    """`LOGBOOK_STEPS` when it is a positive integer, else the default. `gate.sh` reads it the same way."""
    return positive(env, "LOGBOOK_STEPS", DEFAULT_STEPS)


def calls_threshold(env):
    """`LOGBOOK_CALLS` when it is a positive integer, else the default. `gate.sh` reads it the same way."""
    return positive(env, "LOGBOOK_CALLS", DEFAULT_CALLS)


def task_events(tool, tool_input, tool_response):
    """The events one task-list call records, as (kind, fields) pairs."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "TaskCreate":
        step_id = board.text(dig(tool_response, "task", "id"))
        subject = board.text(tool_input.get("subject")) or board.text(dig(tool_response, "task", "subject"))
        return [("step", {"id": step_id, "subject": subject})] if step_id else []
    if tool == "TaskUpdate":
        step_id = board.text(tool_input.get("taskId"))
        if not step_id:
            return []
        found = []
        subject = board.text(tool_input.get("subject"))
        if subject:
            found.append(("step", {"id": step_id, "subject": subject}))
        status = board.text(tool_input.get("status"))
        if status:
            found.append(("step-status", {"id": step_id, "status": status}))
        return found
    if tool == "TodoWrite":
        items = todo_items(tool_input)
        return [("steps-replace", {"items": items})] if items is not None else []
    return []


def todo_items(tool_input):
    todos = dig(tool_input, "todos")
    if not isinstance(todos, list):
        return None
    return [
        {"content": board.text(todo.get("content")) or "", "status": board.text(todo.get("status")) or "pending"}
        for todo in todos
        if isinstance(todo, dict)
    ]


def agent_events(payload):
    """`agent-info` from an `Agent` call, with only the fields its response carries."""
    response, tool_input = payload.get("tool_response"), payload.get("tool_input")
    agent_id = board.text(dig(response, "agentId"))
    fields = {
        "model": board.text(dig(response, "resolvedModel")),
        "description": board.text(dig(tool_input, "description")) or board.text(dig(response, "description")),
    }
    fields = {key: value for key, value in fields.items() if value}
    if not agent_id or not fields:
        return []
    return [("agent-info", dict(id=agent_id, **fields))]


def lines_in(content):
    """How many lines a file's whole content holds, or None when it is not text."""
    if not isinstance(content, str):
        return None
    return content.count("\n") + (1 if content and not content.endswith("\n") else 0)


def patch_lines(patch):
    """(added, removed) from a `structuredPatch`, or (None, None) when it is not one."""
    if not isinstance(patch, list):
        return None, None
    added = removed = 0
    for hunk in patch:
        lines = dig(hunk, "lines")
        if not isinstance(lines, list):
            return None, None
        added += sum(1 for line in lines if isinstance(line, str) and line.startswith("+"))
        removed += sum(1 for line in lines if isinstance(line, str) and line.startswith("-"))
    return added, removed


def change_events(tool, tool_input, tool_response):
    """The `change` one successful file call records. A count the response does not give is left out."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = board.text(tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path"))
    path = path or board.text(dig(tool_response, "filePath"))
    if not path:
        return []
    created = tool == "Write" and dig(tool_response, "type") == "create"
    fields = {"path": path, "tool": tool, "created": created}
    if created:
        content = dig(tool_response, "content")
        added = lines_in(content if isinstance(content, str) else tool_input.get("content"))
        fields.update(added=added, removed=None if added is None else 0)
    elif tool != "NotebookEdit":
        fields["added"], fields["removed"] = patch_lines(dig(tool_response, "structuredPatch"))
    return [("change", fields)]


def own_command(command, env):
    """Whether a command runs this plugin's own `board.py`: the recording, which the board does not list."""
    plugin = env.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HERE)
    scripts = {os.path.join(os.path.abspath(p), "board", "board.py") for p in (plugin, os.path.dirname(HERE))}
    return any(script in command for script in scripts)


def command_events(event, tool_input, tool_response, error, duration, env):
    """The `command` one `Bash` call records: `fail` on a failure or an interrupt, `background` for one
    left running, else `pass`, with the exit code a failure's error names first."""
    command = board.text(dig(tool_input, "command"))
    if not command or own_command(command, env):
        return []
    if event == "PostToolUseFailure":
        result = "fail"
    elif dig(tool_input, "run_in_background") is True:
        result = "background"
    else:
        result = "fail" if dig(tool_response, "interrupted") is True else "pass"
    description = board.text(dig(tool_input, "description"))
    fields = {
        "command": command[:board.TEXT_CAP], "description": description[:board.TEXT_CAP] if description else None,
        "result": result,
    }
    found = EXIT_CODE.match(error) if isinstance(error, str) else None
    if found:
        fields["exit"] = int(found.group(1))
    if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
        fields["ms"] = duration
    return [("command", fields)]


def work_events(event, tool, tool_input, tool_response, error=None, duration=None, env=None):
    """The events one work call records. A failed file call records nothing: the file did not change."""
    if tool == "Bash":
        return command_events(event, tool_input, tool_response, error, duration, env or {})
    if tool in FILE_TOOLS and event == "PostToolUse":
        return change_events(tool, tool_input, tool_response)
    return []


def tagged(found, **tags):
    """`found` with `tags` added to the fields of every event."""
    return [(kind, dict(fields, **tags)) for kind, fields in found]


def transcript_tokens(path):
    """The largest context any one assistant message of a transcript was sent (input, cache writes and cache
    reads), or None when the file cannot be read or holds no usage. This is the size the context budget marks
    measure. Summing messages would count the same context again on every turn."""
    try:
        if not isinstance(path, str) or os.path.getsize(path) > TOKEN_READ_LIMIT:
            return None
        peak = None
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                size = sum(
                    usage[key] for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                    if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
                )
                peak = size if peak is None else max(peak, size)
        return peak
    except OSError:
        return None


def payload_events(payload, env=None):
    """The events one hook payload records. `SessionEnd` is not here: it closes the board instead.

    A work call made inside a subagent is recorded with its `agent`; a task-list or `Agent`/`Task`
    call made there is not.
    """
    event = payload.get("hook_event_name")
    agent_id = board.text(payload.get("agent_id"))
    if event in TOOL_EVENTS:
        tool = payload.get("tool_name")
        use = board.text(payload.get("tool_use_id"))
        if tool in WORK_TOOLS:
            found = work_events(
                event, tool, payload.get("tool_input"), payload.get("tool_response"),
                payload.get("error"), payload.get("duration_ms"), env,
            )
            return tagged(found, use=use, agent=agent_id or None)
        if agent_id or event != "PostToolUse":
            return []
        if tool in TASK_TOOLS:
            return tagged(task_events(tool, payload.get("tool_input"), payload.get("tool_response")), use=use)
        return tagged(agent_events(payload), use=use) if tool in AGENT_TOOLS else []
    if event == "SubagentStart":
        return [("agent-start", {"id": agent_id, "type": board.text(payload.get("agent_type"))})] if agent_id else []
    if event == "SubagentStop":
        message = board.text(payload.get("last_assistant_message")) or ""
        tokens = transcript_tokens(payload.get("agent_transcript_path"))
        return [("agent-stop", {"id": agent_id, "message": message[:MESSAGE_LIMIT], "tokens": tokens})] if agent_id else []
    if event == "UserPromptSubmit":
        return [("turn-start", {})]
    if event == "Stop":
        tokens = transcript_tokens(payload.get("transcript_path"))
        return ([("session-tokens", {"tokens": tokens})] if tokens is not None else []) + [("turn-end", {})]
    return []


def record(folder, now, found):
    for kind, fields in found:
        board.append(folder, kind, now, **{key: value for key, value in fields.items() if value is not None})


# ---------------------------------------------------------------------------------------------------
# Starting a board


def starts(payload, env):
    """Whether this payload starts a board: the first subagent, a task list at the threshold, a
    main-session work call once the gate's count for the session has reached `LOGBOOK_CALLS`,
    or the end of a turn once the count holds a call that changed something."""
    event = payload.get("hook_event_name")
    if event == "SubagentStart":
        return True
    if event == "Stop":
        return board.calls_changed(env, payload.get("session_id"))
    tool = payload.get("tool_name")
    if event in TOOL_EVENTS and tool in WORK_TOOLS and not payload.get("agent_id"):
        return board.calls_made(env, payload.get("session_id")) >= calls_threshold(env)
    if event != "PostToolUse":
        return False
    if tool == "TaskCreate":
        step_id = board.text(dig(payload, "tool_response", "task", "id")) or ""
        return bool(DIGITS.fullmatch(step_id)) and int(step_id) >= threshold(env)
    if tool == "TodoWrite":
        items = todo_items(payload.get("tool_input"))
        return items is not None and len(items) >= threshold(env)
    return False


def title_from(prompt):
    """The first prompt on one line, cut to 80 characters at a word boundary with an ellipsis."""
    words = " ".join(prompt.split()) if isinstance(prompt, str) else ""
    if not words:
        return UNTITLED
    if len(words) <= TITLE_LIMIT:
        return words
    cut = words[: TITLE_LIMIT - 1]
    if words[TITLE_LIMIT - 1] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip() + "…"


def succeeded(result, block):
    return block.get("is_error") is not True and isinstance(result, dict) and result.get("success") is not False


def transcript_time(value):
    """A transcript line's `timestamp` (`2026-01-05T09:00:05.123Z`) in the board's format, or None."""
    found = TRANSCRIPT_TIME.fullmatch(value) if isinstance(value, str) else None
    return found.group(1) + "Z" if found else None


def transcript_events(call, entry, block, env):
    """The events one call read back from the transcript records, once its result line has arrived.

    A task-list call counts when it did not fail. An `Agent` or `Task` call is read as its hook would
    have read it, from its input and its `toolUseResult`, when it did not fail: a failed launch has no
    subagent, and its result is a string. A work call is read as its hook would have read it too: a
    result marked `is_error` is a failure, whose error is the `toolUseResult` string (or the result's
    own content), and a change or command carries `at`, the time on its result's line.
    """
    tool, tool_input = call
    result = entry.get("toolUseResult")
    if tool in TASK_TOOLS:
        return task_events(tool, tool_input, result) if succeeded(result, block) else []
    if tool in AGENT_TOOLS:
        failed = block.get("is_error") is True
        return [] if failed else agent_events({"tool_input": tool_input, "tool_response": result})
    failed = block.get("is_error") is True
    error = (result if isinstance(result, str) else block.get("content")) if failed else None
    found = work_events("PostToolUseFailure" if failed else "PostToolUse", tool, tool_input, result, error, None, env)
    at = transcript_time(entry.get("timestamp"))
    found = tagged(found, at=at) if at else found
    if any(kind == "command" and fields.get("result") in ("pass", "fail") for kind, fields in found):
        found += commit_events(dig(tool_input, "command"), error if failed else result, failed)
    return found


def commit_events(command, result, failed=False):
    """A `commit` for each line in a command's output in the form git prints after it makes a commit,
    when the command runs `git` (`NAMES_GIT`): its hash, and its branch unless the HEAD was detached.

    `result` is what the command left: the response of one that passed, whose `stdout` is read, or
    the error text of one that failed. A command can make its commit and fail afterwards (the push
    that follows it, refused), and git never learns of that. One that printed nothing (`--quiet`)
    names no commit here.

    Only a call read back from the transcript records these, and the call that starts the board. Its
    commit can be older than the heads the `start` event recorded, where `board.commits` never
    looks; a commit made once the board exists is found there, so a hook's own call has nothing to
    add.
    """
    output = result if failed else dig(result, "stdout")
    if not isinstance(output, str) or not isinstance(command, str) or not NAMES_GIT.search(command):
        return []
    return [
        ("commit", {"hash": found.group("hash"), "branch": found.group("branch")})
        for found in COMMIT_LINE.finditer(output)
    ]


def starting_commits(payload):
    """The commits made by the call that starts the board, as `commit` events.

    That call's hook runs after its command has, so the heads the `start` event records are already
    past its commit, and the transcript is read without it. It is the one hook call that records a
    `commit`.
    """
    event = payload.get("hook_event_name")
    if event not in TOOL_EVENTS or payload.get("tool_name") != "Bash" or payload.get("agent_id"):
        return []
    if dig(payload, "tool_input", "run_in_background") is True:
        return []
    failed = event == "PostToolUseFailure"
    result = payload.get("error") if failed else payload.get("tool_response")
    found = commit_events(dig(payload, "tool_input", "command"), result, failed)
    return tagged(found, use=board.text(payload.get("tool_use_id")), early=True)


def read_transcript(path, skip, stop=(), env=None):
    """(first prompt or None, the task-list, work-call and agent-launch events so far) from the transcript
    at `path`.

    The transcript's shape is not documented, so every line is read tolerantly: a line that does not
    parse, or is not what it looks for, is stepped over. A call counts once its result has arrived; the
    calls whose tool-use ids are in `skip` are left out, and no call made after the first whose id is
    in `stop` is collected. Every event carries its call's id as `use`, and `early`.
    """
    prompt, calls, found, stopped = None, {}, [], False
    try:
        lines = open(path, encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return None, []
    with lines:
        for line in lines:
            if stopped and not calls:
                break
            if prompt is not None and not calls and not any(tool in line for tool in READ_TOOLS):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain") is True:
                continue
            kind, content = entry.get("type"), dig(entry, "message", "content")
            if kind == "user" and isinstance(content, str):
                if prompt is None and entry.get("isMeta") is not True and not content.lstrip().startswith("<"):
                    prompt = content if content.strip() else None
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "tool_use" and block.get("name") in READ_TOOLS:
                    use_id = block.get("id")
                    stopped = stopped or (isinstance(use_id, str) and use_id in stop)
                    if isinstance(use_id, str) and use_id not in skip and not stopped:
                        calls[use_id] = (block.get("name"), block.get("input"))
                elif kind == "user" and block.get("type") == "tool_result":
                    use_id = block.get("tool_use_id")
                    call = calls.pop(use_id, None) if isinstance(use_id, str) else None
                    if call:
                        found.extend(tagged(transcript_events(call, entry, block, env), use=use_id, early=True))
    return prompt, found


def unrecorded(log, found):
    """`found` without the changes and commands of a call `log` already holds, by its tool-use id.

    A call read back from the transcript can be in the log before its own hook runs, and a plugin
    loaded twice runs every hook twice. A change or a command recorded twice is counted twice, so
    those are recorded once; a step or an agent's details say the same thing however often.
    """
    known = {
        event.get("use") for event in log
        if event.get("kind") in board.WORK_KINDS and isinstance(event.get("use"), str)
    }
    return [
        (kind, fields) for kind, fields in found
        if kind not in board.WORK_KINDS or fields.get("use") not in known
    ]


def start(project, session, folder, payload, env, now):
    """Start the board with the steps, changes and commands read so far and this payload's events. Only
    the call that published it says so; one that lost the race records its events on the published board."""
    try:
        found = payload_events(payload, env) + starting_commits(payload)
        prompt, earlier = read_transcript(payload.get("transcript_path"), {payload.get("tool_use_id")}, env=env)
        published = board.start(project, session, now, title_from(prompt), env, early=earlier + found)
    finally:
        # Whether or not the board started: a hook running beside this one still finds the count
        # while the board is being built, and one that comes after a failed start does not.
        board.forget_calls(env, session)
        board.prune_calls(now, env)
    if published is None:
        found = unrecorded(board.events(folder), found) if board.is_board(folder) else []
        if found:
            record(folder, now, found)
            board.render(folder, now, env)
        return None
    event = payload.get("hook_event_name")
    if event in CARRIES_CONTEXT and announce(folder):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context(folder, env)}}
    return None


# ---------------------------------------------------------------------------------------------------
# Telling the model


def announce(folder):
    """True exactly once per board: the call that creates the `announced` file delivers the context."""
    try:
        fd = os.open(os.path.join(folder, ANNOUNCED_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def recorded_this_turn(log):
    """Whether the log holds an event of any kind but a marker after the last marker: something was
    recorded since the turn, or the board, began."""
    for event in reversed(log):
        kind = event.get("kind") if isinstance(event, dict) else None
        if kind in TURN_MARKERS:
            return False
        if kind is not None:
            return True
    return False


def context(folder, env):
    plugin = env.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HERE)
    return CONTEXT.format(
        page=os.path.join(folder, board.BOARD_FILE),
        script=os.path.join(os.path.abspath(plugin), "board", "board.py"),
        folder=folder,
    )


# ---------------------------------------------------------------------------------------------------
# The hook


def catch_up_reads(folder):
    try:
        return int(board.read_text(os.path.join(folder, CATCH_UP_FILE)).strip())
    except (OSError, ValueError):
        return 0


def catch_up(folder, payload, now, log, env):
    """Read the transcript once more for calls the log does not hold, bounded to `CATCH_UP_LIMIT` reads.

    A call whose hook ran before the board was published never reached this handler, and the host
    writes the transcript late, so the start may not have read it either. Every task-list or `Agent`
    call with a successful result, and every work call with a result, whose tool-use id is not in the
    log is recorded, in transcript order, marked
    `early`, up to the first call the log holds as a hook recorded it (not `early`): a call after
    that one was made while the board existed, and `derive` would apply it too soon. A board that has
    been closed at all is past its start, and is never caught up again. Returns whether it recorded
    anything.
    """
    if any(event.get("kind") == "close" for event in log):
        return False
    reads = catch_up_reads(folder)
    if reads >= CATCH_UP_LIMIT:
        return False
    board.write_atomic(os.path.join(folder, CATCH_UP_FILE), str(reads + 1))
    known = {event.get("use") for event in log if isinstance(event.get("use"), str)}
    hooked = {event["use"] for event in log if isinstance(event.get("use"), str) and not board.is_early(event)}
    _, found = read_transcript(payload.get("transcript_path"), known, hooked, env)
    record(folder, now, found)
    return bool(found)


def carry_on(folder, payload, env, now):
    """Record one payload on a board that exists, and deliver the model's context if it is still owed.

    A work call made inside a subagent is only recorded: what it returns would reach the subagent.
    On `Stop`, what `catch_up` recovers is appended before the turn's `turn-end`.
    """
    log = board.events(folder)
    if board.is_closed(log):
        return None
    event = payload.get("hook_event_name")
    if event == "SessionEnd":
        board.close(folder, now, env, by="session-end")
        return None
    found = unrecorded(log, payload_events(payload, env))
    caught_up = False
    if event == "Stop":
        caught_up = catch_up(folder, payload, now, log, env)
        if caught_up:
            log = board.events(folder)
    if found:
        record(folder, now, found)
        log = board.events(folder)
    if event in TOOL_EVENTS and payload.get("agent_id"):
        if found:
            board.render(folder, now, env)
        return None
    if event != "Stop":
        caught_up = catch_up(folder, payload, now, log, env)
    if found or caught_up:
        board.render(folder, now, env)
    if event in CARRIES_CONTEXT and announce(folder):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context(folder, env)}}
    return None


def session_start(project, folder, payload, env, now):
    """Prune old boards, then give an open board's context back, whether or not it was announced before.

    A resumed session (`source` is `resume`) reopens its own board if closing it had left it final:
    the events lost while it was closed stay lost, but the board records again from here. Any other
    source (`compact`, `startup`, `clear`) leaves a closed board closed.
    """
    board.prune(project, now, env, keep=folder)
    if not board.is_board(folder):
        return None
    if board.is_closed(board.events(folder)):
        if payload.get("source") != "resume":
            return None
        board.reopen(folder, now, env)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context(folder, env)}}


def revive(folder, payload, env, now):
    """Reopen a board the session's end closed, on any later event from the session but `SessionEnd`."""
    if payload.get("hook_event_name") == "SessionEnd" or not board.is_board(folder):
        return
    if board.closed_by(board.events(folder)) == "session-end":
        board.reopen(folder, now, env)


def handle(payload, env, now):
    """The hook's JSON output for one payload, or None. Raises on anything it cannot use."""
    if not isinstance(payload, dict):
        return None
    session, project = payload.get("session_id"), env.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    if not isinstance(session, str) or not isinstance(project, str) or not project:
        return None
    inside = payload.get("hook_event_name") in TOOL_EVENTS and payload.get("agent_id")
    if inside and payload.get("tool_name") not in WORK_TOOLS:
        return None
    project = board.contained(os.path.abspath(project), env)
    if project is None:
        board.forget_calls(env, session)
        return None
    folder = board.board_dir(project, session)
    if board.refused_boards(os.path.dirname(folder)):
        board.forget_calls(env, session)
        return None
    revive(folder, payload, env, now)
    if payload.get("hook_event_name") == "SessionStart":
        return session_start(project, folder, payload, env, now)
    if board.is_board(folder):
        # A count file can outlive the board's start (`gate.sh` cannot decode a project path with a
        # backslash, so it counts before handing the payload over): once the board exists, nothing
        # should still be counting towards starting it.
        board.forget_calls(env, session)
        return carry_on(folder, payload, env, now)
    if starts(payload, env):
        return start(project, session, folder, payload, env, now)
    return None


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        output = handle(payload, os.environ, datetime.now(timezone.utc))
        if output:
            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
