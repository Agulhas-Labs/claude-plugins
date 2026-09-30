#!/usr/bin/env python3
"""SubagentStop: hold a subagent's stop while a command it backgrounded is still running.

An agent that starts its test run in the background and then ends its turn has nothing to report,
and its stop wakes the main session with nothing to act on. Measured in one session of ~25
subagents: about 20 empty main-session turns in a day. Saying so in each agent's prompt did not
hold it, so the stop is blocked instead — at most once, since `stop_hook_active` lets the next
attempt through, so a command meant to outlive the agent never traps it.

Live ids come from the subagent's own transcript: every id it started in the background, less the
ones a task notification or a TaskStop call has since named. A start reads two ways depending on
who wrote the transcript, so both are read. That shape is undocumented, so a missing file, an
unreadable line or an unexpected shape exits silently — a stop is never worth a hook error.
"""
import io
import json
import os
import re
import sys

TASK_ID = re.compile(rb"<task-id>([^<>\s]{1,64})</task-id>")
# How a backgrounded Bash call reads in a transcript written by the CLI in print mode, which records
# the tool result's text but not the `toolUseResult` object an interactive session also writes.
STARTED = re.compile(r"Command running in background with ID: ([A-Za-z0-9_-]{1,64})")


def transcript(payload):
    """The subagent's own transcript. The payload names it directly; older ones only imply it."""
    agent = payload.get("agent_id")
    if not agent:
        return None  # not a subagent: the main session is never held
    direct = payload.get("agent_transcript_path")
    if direct:
        return direct
    path = payload.get("transcript_path") or ""
    if not path:
        return None
    session = os.path.splitext(path)[0]
    return os.path.join(session, "subagents", f"agent-{agent}.jsonl")


def record(raw):
    try:
        entry = json.loads(raw)
    except ValueError:
        return {}
    return entry if isinstance(entry, dict) else {}


def ended(entry):
    """Ids named by a TaskStop call: the agent chose to end that run rather than wait for it."""
    message = entry.get("message") or {}
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("name") != "TaskStop":
            continue
        arguments = block.get("input")
        for value in arguments.values() if isinstance(arguments, dict) else []:
            if isinstance(value, str):
                yield value


def announced(entry):
    """Ids a tool result of this agent's own opens with. The same sentence further into a result is
    quoted text — a transcript it read, another run's output file — and names a run it never started."""
    if entry.get("type") != "user":
        return
    content = (entry.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        if isinstance(body, list):
            body = next((part.get("text") for part in body if isinstance(part, dict)), None)
        match = STARTED.match(body) if isinstance(body, str) else None
        if match:
            yield match.group(1)


def live(path):
    """Background ids this agent started that nothing has since reported finished, in start order."""
    started, over = [], set()
    with open(path, "rb") as f:
        for raw in f:  # streamed, with a cheap pre-filter: most lines are neither
            if b'"backgroundTaskId"' in raw:
                result = record(raw).get("toolUseResult")
                task = result.get("backgroundTaskId") if isinstance(result, dict) else None
                if isinstance(task, str) and task not in started:
                    started.append(task)
            elif b"running in background with ID" in raw:
                for task in announced(record(raw)):
                    if task not in started:
                        started.append(task)
            if b"task-notification" in raw:
                # Any notification for an id ends it: completed, failed and killed all report. Matched
                # anywhere on the line, so quoted text can end an id too: that errs toward letting go.
                over.update(m.group(1).decode("utf-8", "replace") for m in TASK_ID.finditer(raw))
            if b'"TaskStop"' in raw:
                over.update(ended(record(raw)))
    return [task for task in started if task not in over]


def reason(payload):
    if payload.get("stop_hook_active"):
        return None  # blocked once already: let it go rather than trap a deliberate long runner
    path = transcript(payload)
    if not path or not os.path.isfile(path):
        return None
    running = live(path)
    if not running:
        return None
    return (
        f"Still running in the background: {', '.join(running)}. Get the verdict in this turn — "
        "TaskOutput blocking on the id, or Monitor on its output file — or TaskStop it, then report "
        "what it said. If it is meant to outlive you, say so in your report and stop again."
    )


def main():
    try:
        # Read stdin as UTF-8 explicitly: on native Windows Python, sys.stdin decodes with the
        # locale code page, which can fail json.load on a non-ASCII UTF-8 payload.
        message = reason(json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")))
    except Exception:
        return
    if message:
        json.dump({"decision": "block", "reason": message}, sys.stdout)


if __name__ == "__main__":
    main()
