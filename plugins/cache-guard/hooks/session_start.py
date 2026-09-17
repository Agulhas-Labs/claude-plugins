#!/usr/bin/env python3
"""SessionStart: point a fresh session at the handoff the session before it left behind.

The guard's cheapest way out of a cold context is the single word `handoff`, which writes a file and
then asks for `/clear`. `/clear` starts a session that knows nothing about any of it, so this hook
tells it: a handoff for this directory, written minutes ago, is worth reading before anything else.

Only a recent one, and only once — a handoff announced at every startup for the rest of the day would
be noise, and noise in a system prompt is paid for on every turn. Anything unexpected prints nothing.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_guard  # noqa: E402
import handoff  # noqa: E402

DEFAULT_FRESH_MINUTES = 30
ANNOUNCING_SOURCES = ("clear", "startup")
ANNOUNCED_PREFIX = "announced-"


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


def announced_marker(state_dir, path):
    """One file per handoff already announced. The guard's own sweep clears these out with the rest."""
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]
    return os.path.join(state_dir, ANNOUNCED_PREFIX + digest)


def announcement(payload, now, env, state_dir):
    """What to tell the new session, or "" when there is nothing worth saying."""
    try:
        if payload.get("source") not in ANNOUNCING_SOURCES:
            return ""  # a resumed session already has the conversation the handoff was written from
        directory = handoff.handoff_dir(payload, {}, env)
        newest = newest_handoff(directory)
        if newest is None:
            return ""
        path, written = newest
        fresh_minutes = fresh_limit(env)
        age_minutes = (now.timestamp() - written) / 60
        if age_minutes >= fresh_minutes or age_minutes < 0:
            return ""
        if cache_guard.usable_state_dir(state_dir) is None:
            return ""  # a state directory that is not ours is not one to write a marker in
        marker = announced_marker(state_dir, path)
        if os.path.exists(marker):
            return ""
        with open(marker, "w", encoding="utf-8") as f:
            f.write("")
        message = (
            f"A handoff from an earlier session in this directory was written {int(age_minutes)} "
            f"minutes ago: {path}. Read it before starting if the user's request continues that work."
        )
        if handoff.PENDING_SUMMARY in read_text(path):
            message += " Its summary is still being written; re-read it if the summary section is missing."
        return message
    except Exception:
        return ""


def fresh_limit(env):
    try:
        value = int(env["CACHE_GUARD_HANDOFF_FRESH_MINUTES"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_FRESH_MINUTES
    return value if value > 0 else DEFAULT_FRESH_MINUTES


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    message = announcement(
        payload, datetime.now(timezone.utc), os.environ, cache_guard.state_dir(os.environ)
    )
    if message:
        print(message)


if __name__ == "__main__":
    main()
