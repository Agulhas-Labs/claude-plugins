#!/usr/bin/env python3
"""SessionStart: point a fresh session at the handoff the session before it left behind.

The guard's cheapest way out of a cold context is the single word `handoff`, which writes a file and
then asks for `/clear`. `/clear` starts a session that knows nothing about any of it, so this hook
tells it: a handoff for this directory, written minutes ago, is worth reading before anything else.

It tells the *user* too, and that is the point of the `systemMessage` half. Hook stdout reaches the
model's context and nothing else, so the first version of this hook fired correctly and left the
person in front of the terminal looking at an ordinary blank prompt with no way to know a handoff was
waiting or that its summary had landed. A pointer only the model can see is not an announcement.

Only a recent one: the freshness window is what keeps this from being noise, and inside that window
every fresh session in the directory is told, because every one of them needs it. An earlier version
also marked each handoff as announced once and for all, which meant a session that started and was
closed again swallowed the only announcement anyone would get. Anything unexpected prints nothing.
"""
import json
import os
import sys
from collections import namedtuple
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_guard  # noqa: E402
import handoff  # noqa: E402

DEFAULT_FRESH_MINUTES = 30
ANNOUNCING_SOURCES = ("clear", "startup")

# What the user reads in the terminal, and what the model reads in its context. They say the same
# thing to two different readers, so they are written separately rather than one quoting the other.
Announcement = namedtuple("Announcement", "spoken context headline")
NOTHING = Announcement("", "", "")


def newest_handoff(directory):
    """(path, modification time) of the newest handoff in the directory, or None when there is none."""
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".md")]
    except OSError:
        return None
    newest = None
    for name in names:
        path = os.path.join(directory, name)
        try:
            when = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or when > newest[1]:
            newest = (path, when)
    return newest


def minutes_phrase(minutes):
    count = int(minutes)
    return "less than a minute ago" if count < 1 else f"{count} minute{'' if count == 1 else 's'} ago"


def spoken_line(path, minutes, pending):
    """The line the user sees. It leads with the fact, because it competes with an empty prompt."""
    tail = (
        "its background summary is still being written, so re-read it in a moment"
        if pending
        else "it is complete, summary included"
    )
    return f"cache-guard: a handoff from {minutes_phrase(minutes)} is waiting — {tail}.\n{path}"


def context_line(path, minutes, pending):
    """The line the model sees: where the file is, and whether it is worth reading again shortly."""
    message = (
        f"A handoff from an earlier session in this directory was written {minutes_phrase(minutes)}: "
        f"{path}. Read it before starting if the user's request continues that work."
    )
    if pending:
        message += " Its summary is still being written; re-read it if the summary section is missing."
    return message


def announcement(payload, now, env):
    """What to tell the new session and its user, or two empty strings when there is nothing to say."""
    try:
        if payload.get("source") not in ANNOUNCING_SOURCES:
            return NOTHING  # a resumed session already has the conversation the handoff was written from
        directory = handoff.handoff_dir(payload, {}, env)
        newest = newest_handoff(directory)
        if newest is None:
            return NOTHING
        path, written = newest
        age_minutes = (now.timestamp() - written) / 60
        if age_minutes >= fresh_limit(env) or age_minutes < 0:
            return NOTHING
        pending = handoff.PENDING_SUMMARY in handoff.read_text(path)
        headline = (
            "handoff waiting, its summary still being written"
            if pending
            else "handoff waiting from your last session"
        )
        return Announcement(
            spoken_line(path, age_minutes, pending),
            context_line(path, age_minutes, pending),
            headline,
        )
    except Exception:
        return NOTHING


def fresh_limit(env):
    try:
        value = int(env["CACHE_GUARD_HANDOFF_FRESH_MINUTES"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_FRESH_MINUTES
    return value if value > 0 else DEFAULT_FRESH_MINUTES


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    note = announcement(payload, datetime.now(timezone.utc), os.environ)
    if not note.context:
        return
    output = {
        "systemMessage": note.spoken,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note.context,
        },
    }
    sequence = cache_guard.notification_sequence(note.headline, os.environ)
    if sequence:
        output["terminalSequence"] = sequence
    print(json.dumps(output))


if __name__ == "__main__":
    main()
