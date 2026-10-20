#!/usr/bin/env python3
"""Stop (asyncRewake): keep the prompt cache warm while this session's background work is still running.

A background command, agent or monitor reports back with a notification, and that notification is a
turn: the whole context is re-sent. When the work takes longer than the cache lasts, the turn lands on
an expired cache and writes everything again, 2x the input price on the 1-hour cache and 1.25x on the
5-minute one, where a warm cache would have read it at a tenth. A 90-minute job reliably pays that.

So when a turn ends with such work still running, this hook waits in the background until just before
the cache would expire, and if the session is still idle then, it exits 2. Claude Code then wakes the
session with the one line this hook printed, the model answers in a line, and that short turn reads the
cache and buys it another lifetime. Its own Stop starts the next wait.

What is running is what Claude Code itself lists in this hook's input as `background_tasks`: the items
still running or pending, apart from Claude Code's own housekeeping and any monitor the transcript shows
was started as persistent, which watches with no end. Work that finishes needs no tracking here: its
notification starts a turn, and any turn ends the wait.

The hook can start before its own turn's last message is in the transcript, so it first waits for that
message (the transcript's last assistant message, matching `last_assistant_message` from the input) and
judges nothing until it is there; if it never arrives, the hook exits quietly. The wake-up is due at nine
tenths of the cache lifetime after that message's request, and is dropped when a newer assistant
message, a prompt or a notification reaches the transcript first. A local command such as `/tasks`, or
`!` bash mode, starts no turn and changes nothing.

Each wake-up costs a cache read of the whole context, so they stop once they would have added up to one
rewrite: write price over read price, 20 on the hour and 12.5 on five minutes at list prices. That caps
the worst case, work that never reports back, at about twice what doing nothing would have cost.

It runs only where waking does something: an interactive session (`CLAUDE_CODE_ENTRYPOINT=cli`), on a
Claude Code that knows `asyncRewake` (2.1.304 or later; an older one would run this hook in the
foreground and freeze the session while it waits). Headless `claude -p` waits for a pending hook and
takes a turn every time it fires, so there it exits at once. It needs the `process-<pid>` record that
SessionStart writes, which says which session this process is running: a `/clear` in the same process
rewrites it, and that ends the wait.

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
# How long the hook waits for its own turn's last message to reach the transcript before giving up.
STALE_GRACE_SECONDS = 60
# Write price over read price when the model has no list price here: 2x / 0.1x and 1.25x / 0.1x.
UNPRICED_RATIO_1H = 20.0
UNPRICED_RATIO_5M = 12.5
WAKE_EXIT = 2  # the exit status that makes Claude Code wake the session with this hook's stdout
IN_FLIGHT = ("running", "pending")
# Claude Code's own work in the background, which reports to nobody's turn.
HOUSEKEEPING = ("dream", "auto-mode scan", "memory import")
# How each type of background task is named in the wake-up; any other type is a task.
KINDS = (("shell", "command", "commands"), ("subagent", "agent", "agents"),
         ("workflow", "workflow", "workflows"), ("monitor", "monitor", "monitors"))
OTHER_KIND = ("task", "tasks")
# What a local command (`/tasks`, `/context`, `!` bash mode) writes: a user record, but no turn.
LOCAL_COMMAND = ("<command-name>", "<local-command-", "<bash-input>", "<bash-stdout>", "<bash-stderr>")
# The main thread's request record, written when a request goes out: a busy session, and the time a
# turn's request reached the cache.
MAIN_REQUEST = "repl_main_thread"

# When the cache was last refreshed, how long it lasts, and the types of the work still running.
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


def message_id(entry):
    return (entry.get("message") or {}).get("id")


def refreshed_at(entries, last):
    """When the last turn's request reached the cache: the main thread's request record for it when
    there is one, else the earliest record of its message.

    A response is written as one record per content block, all with the same message id; the cache is
    read and written before the first of them, so the last record's time is late by the whole answer,
    and the first record itself is written once its first content block is done. The request record
    counts only between the previous assistant message and that first record.
    """
    wanted = None
    for position, entry in entries:
        if position == last.position:
            wanted = message_id(entry)
            break
    if not wanted:
        return last.time
    times = [
        cache_guard.parse_time(entry.get("timestamp"))
        for _, entry in entries
        if entry.get("type") == "assistant" and not entry.get("isSidechain") and message_id(entry) == wanted
    ]
    times = [when for when in times if when is not None]
    if not times:
        return last.time
    first = min(times)
    first_position = previous = None
    for position, entry in entries:
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        if message_id(entry) == wanted:
            first_position = position
            break
        previous = position
    if first_position is None:
        return first
    requests = [
        cache_guard.parse_time(entry.get("timestamp"))
        for position, entry in entries
        if (previous is None or position > previous) and position < first_position and is_main_request(entry)
    ]
    requests = [when for when in requests if when is not None and when <= first]
    return max(requests) if requests else first


def in_flight(payload):
    """The background tasks Claude Code lists as running or pending, less its own housekeeping.

    None when the input carries no list at all, which an older Claude Code would send.
    """
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return None
    return [
        task for task in tasks
        if isinstance(task, dict) and task.get("status") in IN_FLIGHT and task.get("type") not in HOUSEKEEPING
    ]


def is_main_request(entry):
    """True for the record written when the main thread sends a request."""
    return (entry.get("type") == "api-request" and not entry.get("isSidechain")
            and entry.get("querySource") == MAIN_REQUEST)


def persistent_monitors(path):
    """Ids of the monitors the whole transcript shows were started as persistent: they watch with no end.

    The whole file, not its tail: in a long session the start of a monitor scrolls out of the tail.
    Only lines that mention `persistent` are parsed.
    """
    found = set()
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"persistent"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    found |= persistent_in(entry)
    except OSError:
        pass
    return found


def persistent_in(entry):
    """The id of the persistent monitor this record started, as a set of zero or one."""
    result = entry.get("toolUseResult")
    if isinstance(result, dict) and result.get("taskId") and result.get("persistent") is True:
        return {str(result["taskId"])}
    return set()


def running_work(tasks):
    """The types of the tasks that are work still running."""
    return [str(task.get("type") or "") for task in tasks]


def is_local(entry, text):
    """True for a record a local command or Claude Code itself wrote, which starts no turn."""
    return isinstance(text, str) and text.lstrip().startswith(LOCAL_COMMAND)


def prompt_text(entry):
    """The text of a record that puts a prompt or a notification in front of the model, else None.

    A user message whose content is a string, a queued command attachment's prompt, or an enqueue
    that carries a string: the shapes a typed prompt, a notification and a wake-up are stored in. A
    local command's record has the same shape and is never one.
    """
    kind = entry.get("type")
    if kind == "user":
        text = (entry.get("message") or {}).get("content")
    elif kind == "attachment":
        attachment = entry.get("attachment")
        queued = isinstance(attachment, dict) and attachment.get("type") == "queued_command"
        text = attachment.get("prompt") if queued else None
    elif kind == "queue-operation" and entry.get("operation") == "enqueue":
        text = entry.get("content")
    else:
        text = None
    if not isinstance(text, str) or is_local(entry, text):
        return None
    return text


def relevant(entry):
    """True for a main-chain record that is part of a turn: an assistant message, a prompt or
    notification, or a tool result; a local command, a meta record or bookkeeping is not."""
    if entry.get("isSidechain"):
        return False
    kind = entry.get("type")
    if kind == "assistant":
        return True
    if kind == "user":
        return not is_local(entry, (entry.get("message") or {}).get("content"))
    return prompt_text(entry) is not None


def message_text(entries, wanted):
    """The text of a main-chain message, its records' text blocks joined as Claude Code joins them."""
    parts = []
    for _, entry in entries:
        if entry.get("type") != "assistant" or entry.get("isSidechain") or message_id(entry) != wanted:
            continue
        for block in (entry.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts).strip()


def settled(entries, last_message):
    """The id of this Stop's own last message once it is in the transcript, else None.

    It is there when the last record that is part of a turn is an assistant message, and, when the
    input carried the message's text, that message's text is the same. Both are needed: a turn's
    prompt or notification is written before its answer, so the turn before can never pass for it,
    even when the two answers read the same.
    """
    last = None
    for _, entry in entries:
        if relevant(entry):
            last = entry
    if last is None or last.get("type") != "assistant" or not message_id(last):
        return None
    if last_message and message_text(entries, message_id(last)) != last_message:
        return None
    return message_id(last)


def latest_message(entries):
    """(id, position of its last record) of the transcript's last main-chain assistant message."""
    for position, entry in reversed(entries):
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            return message_id(entry), position
    return None, -1


def pings_since_prompt(lines, entries):
    """How many wake-ups this hook has made since the last prompt or notification that was not one.

    A wake-up is stored as several records (an enqueue, a dequeue or remove, then a user message or a
    queued command attachment) with no assistant record between them, so what is counted is the
    stretches between assistant records that hold at least one marked record. The mark is matched
    anywhere in a record. A typed prompt or a notification without the mark resets the count: the
    session is in use again. A local command does not.
    """
    mark = KEEP_ALIVE_MARK.encode()
    count = 0
    marked_stretch = False
    by_position = dict(entries)
    for position, raw in enumerate(lines):
        entry = by_position.get(position)
        if entry is None or entry.get("isSidechain"):
            continue
        if entry.get("type") == "assistant":
            if marked_stretch:
                count += 1  # the model's own text never is a wake-up, even when it quotes one
            marked_stretch = False
            continue
        if mark in raw:
            marked_stretch = True
        elif prompt_text(entry) is not None:
            count = 0
            marked_stretch = False
    return count + (1 if marked_stretch else 0)


def busy_again(entries, after):
    """True when the person or a notification has put something in front of the model since `after`."""
    for position, entry in entries:
        if position <= after or entry.get("isSidechain"):
            continue
        if is_main_request(entry):
            return True  # a request has gone out: only a supplement, not every request has a record
        text = prompt_text(entry)
        if text is not None and KEEP_ALIVE_MARK not in text:
            return True
    return False


def wake_ratio(model, lifetime, env):
    """How many wake-ups add up to one rewrite: cache-write price over cache-read price, never more
    than the unpriced ratio, so no model gets more than 19 wake-ups on the hour or 12 on five minutes."""
    ceiling = UNPRICED_RATIO_1H if lifetime >= cache_guard.HOUR else UNPRICED_RATIO_5M
    write, read = cache_guard.prices(model, lifetime, env)
    if write and read:
        return min(write / read, ceiling)
    return ceiling


def evaluate(path, env, settled_id, tasks):
    """The Plan for waking after the settled message `settled_id`, or None when there should be none.

    None when a newer assistant message has been written since, the context was compacted after it, a
    prompt or notification has arrived since, it is too small to be worth a turn, none of `tasks` is
    work still running, or the wake-ups so far add up to a rewrite.
    """
    lines = cache_guard.read_tail(path)
    entries = records(lines)
    version = transcript_version(entries)
    if version is None or version < MIN_VERSION:
        return None
    latest, position = latest_message(entries)
    if latest != settled_id:
        return None  # another turn has answered since, and its own Stop owns the wait
    found = cache_guard.turns(lines)
    if not found:
        return None
    last = found[-1]
    if cache_guard.last_compaction(lines) > position:
        return None  # the context the turn measured is gone, and so is its cache
    if busy_again(entries, position):
        return None  # a prompt or notification is already on its way to a turn of its own
    if last.size < cache_guard.positive_int(env, "CACHE_GUARD_MIN_TOKENS", cache_guard.DEFAULT_MIN_TOKENS):
        return None
    lifetime = cache_guard.positive_int(env, "CACHE_GUARD_LIFETIME_SECONDS", 0) or cache_guard.lifetime_of(found)
    running = running_work(tasks)
    if not running:
        return None
    if pings_since_prompt(lines, entries) + 1 >= wake_ratio(last.model, lifetime, env):
        return None
    return Plan(refreshed_at(entries, last), lifetime, running)


def describe(running):
    """`1 command, 2 agents`: what is still running, by type."""
    named = {kind: (one, many) for kind, one, many in KINDS}
    counts = Counter(kind if kind in named else None for kind in running)
    parts = []
    for kind, one, many in KINDS + ((None,) + OTHER_KIND,):
        if counts[kind]:
            parts.append(f"{counts[kind]} {one if counts[kind] == 1 else many}")
    return ", ".join(parts)


def wake_message(running):
    return (
        f"{KEEP_ALIVE_MARK}: background work is still running ({describe(running)}). This turn only "
        "keeps the prompt cache warm so the result does not land on an expired cache: reply in one "
        "short line, make no tool calls, and end your turn. If you are in the middle of other work, ignore "
        "this and carry on."
    )


process_alive = cache_guard.process_alive  # one copy, in cache_guard, for the sweep to share


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
    """(pid, transcript, process record) when this hook may wait at all, else None."""
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
    os.utime(record_path, None)  # a live session's record must not age out of the sweep
    return pid, path, record_path


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
        pid, path, record_path = found
        tasks = in_flight(payload)
        if not tasks:
            return None
        persistent = persistent_monitors(path)  # once per run, not per poll
        tasks = [task for task in tasks if str(task.get("id")) not in persistent]
        if not tasks:
            return None
        session = payload.get("session_id")
        last_message = payload.get("last_assistant_message")
        last_message = last_message.strip() if isinstance(last_message, str) else ""

        def still_there(seconds):
            sleep(seconds)
            return alive(pid) and owns_process(record_path, session)  # else gone, or running another session

        # Settle: wait for this Stop's own last message to be on disk before judging anything.
        give_up = clock() + timedelta(seconds=STALE_GRACE_SECONDS)
        while True:
            stamp = file_stamp(path)
            message = settled(records(cache_guard.read_tail(path)), last_message)
            if message:
                break
            remaining = (give_up - clock()).total_seconds()
            if remaining <= 0 or not still_there(min(POLL_SECONDS, remaining)):
                return None

        plan = evaluate(path, env, message, tasks)
        if plan is None:
            return None
        wake_at = plan.refreshed + timedelta(seconds=WAKE_FRACTION * plan.lifetime)
        if clock() >= plan.refreshed + timedelta(seconds=plan.lifetime):
            return None  # the cache has expired already: a wake-up would write it, not read it
        while True:
            remaining = (wake_at - clock()).total_seconds()
            if remaining <= 0:
                break  # also at once when the message took longer to write, while the cache is alive
            if not still_there(min(POLL_SECONDS, remaining)):
                return None
            current = file_stamp(path)
            if current != stamp:
                stamp = current
                if evaluate(path, env, message, tasks) is None:
                    return None
        if not alive(pid) or not owns_process(record_path, session):
            return None
        final = evaluate(path, env, message, tasks)
        return wake_message(final.running) if final else None
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
