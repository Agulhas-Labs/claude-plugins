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

When the handoff it announces is one whose background summary is still running, this hook takes over
the record of it, so that this session's own guard reports the landing on its first prompt. Without
that step nothing reported it at all: the record belongs to the session that started the summary, and
that session has just cleared and will take no further prompt. `/clear` keeps the working directory,
so the session that clears is the one this hook is speaking to, and no record from another directory
is ever read: a handoff belongs to the directory it was written in, and announcing it anywhere else
would be noise in a session doing unrelated work.
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


def spoken_line(path, minutes, pending, watched):
    """The line the user sees. It leads with the fact, because it competes with an empty prompt."""
    if not pending:
        tail = "it is complete, summary included"
    elif watched:
        tail = "you will be told here when its background summary lands"
    else:
        tail = "its background summary is still being written, so re-read it in a moment"
    return f"cache-guard: a handoff from {minutes_phrase(minutes)} is waiting — {tail}.\n{path}"


def context_line(path, minutes, pending, watched):
    """The line the model sees: where the file is, and whether it is worth reading again shortly."""
    message = (
        f"A handoff from an earlier session in this directory was written {minutes_phrase(minutes)}: "
        f"{path}. Read it before starting if the user's request continues that work."
    )
    if pending and watched:
        message += " Its summary is still being written; you will be told when it lands."
    elif pending:
        message += " Its summary is still being written; re-read it if the summary section is missing."
    return message


def fresh(written, now, env):
    """True for a handoff written recently enough to be the work this session is about to continue."""
    age_minutes = (now.timestamp() - written) / 60
    return 0 <= age_minutes < fresh_limit(env)


def candidate(payload, now, env):
    """(path, modification time) of the handoff worth announcing here, or None when there is none.

    This directory's newest, and only this directory's: `/clear` keeps the working directory, so the
    session that clears is already here, and a handoff written anywhere else belongs to work this
    session is not doing.
    """
    newest = newest_handoff(handoff.handoff_dir(payload, {}, env))
    return newest if newest is not None and fresh(newest[1], now, env) else None


def announcement(payload, now, env):
    """What to tell the new session and its user, or empty strings when there is nothing to say.

    One side effect, because the answer depends on whether it worked: a handoff whose summary is still
    running is watched by this session, so its guard reports the landing on the first prompt. Only a
    record that reached the disk is promised.
    """
    try:
        if payload.get("source") not in ANNOUNCING_SOURCES:
            return NOTHING  # a resumed session already has the conversation the handoff was written from
        found = candidate(payload, now, env)
        if found is None:
            return NOTHING
        path, written = found
        age_minutes = (now.timestamp() - written) / 60
        pending = handoff.PENDING_SUMMARY in handoff.read_text(path)
        watched = pending and cache_guard.watch_pending(
            cache_guard.state_dir(env), cache_guard.session_of(payload), path, adopted=True
        )
        headline = (
            "handoff waiting, its summary still being written"
            if pending
            else "handoff waiting from your last session"
        )
        return Announcement(
            spoken_line(path, age_minutes, pending, watched),
            context_line(path, age_minutes, pending, watched),
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
