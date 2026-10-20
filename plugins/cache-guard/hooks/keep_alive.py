#!/usr/bin/env python3
"""Stop (asyncRewake): keep the prompt cache warm while background work started here is still running.

A background command, agent or monitor reports back with a notification, and that notification is a
turn: the whole context is re-sent. When the work takes longer than the cache lasts, the turn lands on
an expired cache and writes everything again, 2x the input price on the 1-hour cache and 1.25x on the
5-minute one, where a warm cache would have read it at a tenth. A 90-minute job reliably pays that.

So when a turn ends with such work still running, this hook waits in the background until just before
the cache would expire, and if the session is still idle and the work still running, it exits 2. Claude
Code then wakes the session with the one line this hook printed, the model answers in a line, and that
short turn reads the cache and buys it another lifetime. Its own Stop starts the next wait.

Each wake-up costs a cache read of the whole context, so they stop once they would have added up to one
rewrite: write price over read price, 20 on the hour and 12.5 on five minutes at list prices. That caps
the worst case, work that never reports back, at about twice what doing nothing would have cost.

It runs only where waking does something: an interactive session (`CLAUDE_CODE_ENTRYPOINT=cli`), on a
Claude Code that knows `asyncRewake` (2.1.304 or later; an older one would run this hook in the
foreground and freeze the session while it waits). Headless `claude -p` waits for a pending hook and
takes a turn every time it fires, so there it exits at once. It needs the `process-<pid>` record that
SessionStart writes, which says which session this process is running and since when work counts.

Anything unexpected exits 0 with no output, like the guard: a wake-up missed costs one cold turn, a
wake-up that should not have happened costs a turn the person did not ask for.
"""
import json
import os
import re
import sys
import time
from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_guard  # noqa: E402

KEEP_ALIVE_MARK = "cache-guard keep-alive"  # opens every wake-up, and is how earlier ones are counted
MIN_VERSION = (2, 1, 304)  # the first Claude Code measured to run an asyncRewake hook in the background
WAKE_FRACTION = 0.9  # wake at 54 minutes of the hour, 270 seconds of five minutes
POLL_SECONDS = 15  # how often the wait checks that the session is still there and still idle
# Write price over read price when the model has no list price here: 2x / 0.1x and 1.25x / 0.1x.
UNPRICED_RATIO_1H = 20.0
UNPRICED_RATIO_5M = 12.5
WAKE_EXIT = 2  # the exit status that makes Claude Code wake the session with this hook's stdout
KINDS = (("command", "commands"), ("agent", "agents"), ("monitor", "monitors"))
TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
# A monitor reports every event as a notification of its own while it keeps running; only the one
# that says how it ended carries a status, and so does every command's and agent's last one.
TASK_STATUS = re.compile(r"<status>[^<]*</status>")
NOTIFICATION = "<task-notification>"

# The turn the cache was last refreshed by, what is still running, and how many wake-ups are spent.
Plan = namedtuple("Plan", "refreshed lifetime running")


def version_of(text):
    """`2.1.304` as (2, 1, 304), or None for anything that is not a dotted version."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def records(lines):
    """Every record in the tail that parses, as (position, record)."""
    found = []
    for position, raw in enumerate(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may still be cut mid-record
        if isinstance(entry, dict):
            found.append((position, entry))
    return found


def transcript_version(entries):
    """The Claude Code version on the newest record that carries one, or None."""
    for _, entry in reversed(entries):
        if "version" in entry:
            return version_of(entry.get("version"))
    return None


def refreshed_at(entries, last):
    """When the last turn's request reached the cache: the earliest record of its message.

    A response is written as one record per content block, all with the same message id; the cache is
    read and written before the first of them, so the last record's time is late by the whole answer.
    """
    message_id = None
    for position, entry in entries:
        if position == last.position:
            message_id = (entry.get("message") or {}).get("id")
            break
    if not message_id:
        return last.time
    times = [
        cache_guard.parse_time(entry.get("timestamp"))
        for _, entry in entries
        if entry.get("type") == "assistant"
        and not entry.get("isSidechain")
        and (entry.get("message") or {}).get("id") == message_id
    ]
    times = [when for when in times if when is not None]
    return min(times) if times else last.time


def notification_text(entry):
    """The text of a task notification this record delivers, or "" when it delivers none.

    The same notification can arrive as a queue operation, as a queued command attachment, and as a
    user message with the notification as its whole content; any of them means the same thing.
    """
    if entry.get("type") == "queue-operation":
        text = entry.get("content")
    elif entry.get("type") == "attachment":
        attachment = entry.get("attachment") or {}
        text = attachment.get("prompt") if attachment.get("type") == "queued_command" else None
    elif entry.get("type") == "user":
        text = (entry.get("message") or {}).get("content")
    else:
        text = None
    return text if isinstance(text, str) and NOTIFICATION in text else ""


def started_task(result):
    """(task id, kind) for a tool result that started background work, or None."""
    if not isinstance(result, dict):
        return None
    if result.get("backgroundTaskId"):
        return str(result["backgroundTaskId"]), "command"  # also a command its timeout moved there
    if result.get("isAsync") is True and result.get("agentId"):
        return str(result["agentId"]), "agent"
    if result.get("taskId") and "timeoutMs" in result and result.get("persistent") is not True:
        return str(result["taskId"]), "monitor"  # a persistent one watches with no end in sight
    return None


def running_work(entries, since):
    """{task id: kind} for the background work started at or after `since` that has not finished.

    A task's latest event decides: a start or a restart leaves it running, a notification with a
    status or a stop ends it. Work started before `since` belonged to an earlier process, which took it
    down when it ended, and no notification for it will ever come.
    """
    state = {}  # task id: kind while running, None once finished
    agents = set()  # every agent id seen, the ones a SendMessage can set running again
    for _, entry in entries:
        if entry.get("isSidechain"):
            continue  # a subagent's own background work reports to the subagent, not here
        when = cache_guard.parse_time(entry.get("timestamp"))
        current = when is not None and when >= since
        text = notification_text(entry)
        if text and TASK_STATUS.search(text):
            for task in TASK_ID.findall(text):
                state[task] = None
        result = entry.get("toolUseResult")
        if isinstance(result, dict):
            if result.get("agentId"):
                agents.add(str(result["agentId"]))
            started = started_task(result)
            if started and current:
                state[started[0]] = started[1]
            if result.get("task_id") and str(result.get("message") or "").startswith("Successfully stopped"):
                state[str(result["task_id"])] = None  # a stopped task sends no notification
        if entry.get("type") == "assistant" and current:
            for block in (entry.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "SendMessage":
                    continue
                target = str((block.get("input") or {}).get("to") or "")
                if target in agents:
                    state[target] = "agent"  # it runs again until its next notification
    return {task: kind for task, kind in state.items() if kind}


def pings_since_prompt(lines, entries):
    """How many wake-ups this hook has made since the last prompt or notification that was not one.

    The mark is matched anywhere in a record, because how Claude Code stores the wake-up it delivers
    is not pinned down. A typed prompt or a notification resets the count: the session is in use again.
    """
    mark = KEEP_ALIVE_MARK.encode()
    count = 0
    by_position = dict(entries)
    for position, raw in enumerate(lines):
        entry = by_position.get(position)
        if entry is None or entry.get("isSidechain"):
            continue
        marked = mark in raw
        if entry.get("type") == "assistant":
            continue  # the model's own text never is a wake-up, even when it quotes one
        if marked:
            count += 1
            continue
        content = (entry.get("message") or {}).get("content") if entry.get("type") == "user" else None
        attachment = entry.get("attachment") if entry.get("type") == "attachment" else None
        queued = isinstance(attachment, dict) and attachment.get("type") == "queued_command"
        if isinstance(content, str) or queued:
            count = 0
    return count


def wake_ratio(model, lifetime, env):
    """How many wake-ups add up to one rewrite: cache-write price over cache-read price."""
    write, read = cache_guard.prices(model, lifetime, env)
    if write and read:
        return write / read
    return UNPRICED_RATIO_1H if lifetime >= cache_guard.HOUR else UNPRICED_RATIO_5M


def evaluate(path, since, env):
    """The Plan for a wake-up, or None when there should be none.

    None when there is no turn to keep warm, the context was compacted after it, it is too small to be
    worth a turn, nothing started here is still running, or the wake-ups so far add up to a rewrite.
    """
    lines = cache_guard.read_tail(path)
    entries = records(lines)
    version = transcript_version(entries)
    if version is None or version < MIN_VERSION:
        return None
    found = cache_guard.turns(lines)
    if not found:
        return None
    last = found[-1]
    if cache_guard.last_compaction(lines) > last.position:
        return None  # the context the turn measured is gone, and so is its cache
    if last.size < cache_guard.positive_int(env, "CACHE_GUARD_MIN_TOKENS", cache_guard.DEFAULT_MIN_TOKENS):
        return None
    lifetime = cache_guard.positive_int(env, "CACHE_GUARD_LIFETIME_SECONDS", 0) or cache_guard.lifetime_of(found)
    running = running_work(entries, since)
    if not running:
        return None
    if pings_since_prompt(lines, entries) + 1 >= wake_ratio(last.model, lifetime, env):
        return None
    return Plan(refreshed_at(entries, last), lifetime, running)


def describe(running):
    """`1 command, 2 agents`: what is still running, by kind."""
    counts = Counter(running.values())
    parts = []
    for kind, plural in KINDS:
        if counts[kind]:
            parts.append(f"{counts[kind]} {kind if counts[kind] == 1 else plural}")
    return ", ".join(parts)


def wake_message(running):
    return (
        f"{KEEP_ALIVE_MARK}: background work is still running ({describe(running)}). This turn only "
        "keeps the prompt cache warm so the result does not land on an expired cache: reply in one "
        "short line, make no tool calls, and end your turn."
    )


def process_alive(pid):
    """False only when the process is certainly gone.

    Signal 0 checks without signalling, on POSIX. On Windows `os.kill` with any other signal
    terminates the process, which here would be Claude Code itself, so there it is never called.
    """
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists and belongs to someone else, which is not this hook's to judge
    return True


def file_stamp(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def owns_process(record_path, session):
    record = cache_guard.read_process_record(record_path)
    return bool(record) and record.get("session_id") == session


def setup(payload, env):
    """(pid, transcript, process record, since) when this hook may wait at all, else None."""
    if str(env.get("CACHE_GUARD_DISABLE") or "").strip() == "1":
        return None
    if str(env.get("CACHE_GUARD_KEEP_ALIVE") or "").strip() == "0":
        return None
    if env.get("CLAUDE_CODE_ENTRYPOINT") != "cli":
        return None  # headless waits for this hook and takes a turn each time it fires
    pid = cache_guard.claude_pid(env)
    if pid is None:
        return None
    path = cache_guard.transcript_of(payload, env)
    if not path or not os.path.isfile(path):
        return None
    directory = cache_guard.usable_state_dir(cache_guard.state_dir(env))
    if directory is None:
        return None
    record_path = cache_guard.process_record(directory, pid)
    record = cache_guard.read_process_record(record_path)
    session = payload.get("session_id")
    if not record or not session or record.get("session_id") != session:
        return None
    since = cache_guard.parse_time(record.get("since"))
    if since is None:
        return None
    os.utime(record_path, None)  # a live session's record must not age out of the sweep
    return pid, path, record_path, since


def run(payload, env, clock=None, sleep=time.sleep, alive=process_alive):
    """The wake-up line once it is due, or None when this hook should exit quietly.

    `clock`, `sleep` and `alive` are the hook's only contact with time and other processes, and the
    tests replace all three.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        found = setup(payload, env)
        if found is None:
            return None
        pid, path, record_path, since = found
        session = payload.get("session_id")
        started = clock()
        plan = evaluate(path, since, env)
        if plan is None:
            return None
        stamp = file_stamp(path)
        while True:
            wake_at = plan.refreshed + timedelta(seconds=WAKE_FRACTION * plan.lifetime)
            remaining = (wake_at - clock()).total_seconds()
            if remaining <= 0:
                break
            sleep(min(POLL_SECONDS, remaining))
            if not alive(pid):
                return None
            if not owns_process(record_path, session):
                return None  # the process is running another session now, or is gone
            current = file_stamp(path)
            if current != stamp:
                stamp = current
                newer = evaluate(path, since, env)
                if newer is None:
                    return None
                if newer.refreshed != plan.refreshed and newer.refreshed > started:
                    return None  # another turn ran after this hook began, and its own Stop owns the wait
                plan = newer  # the same turn, or this hook's own turn written to disk late
        final = evaluate(path, since, env)
        if final is None or (final.refreshed != plan.refreshed and final.refreshed > started):
            return None
        return wake_message(final.running)
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    message = run(payload, os.environ)
    if not message:
        return 0
    print(message)
    sys.stdout.flush()
    return WAKE_EXIT


if __name__ == "__main__":
    sys.exit(main())
