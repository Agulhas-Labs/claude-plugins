#!/usr/bin/env python3
"""Print, as one JSON object, the newest handoff of this directory and a few cheap git facts.

The resume band (`resume.tsx`) runs this when a session starts and again after a `/clear` or a resume.
It only states what it finds: the handoff's path, when the file was written, one line saying what it is
about, and the repository's branch, commits ahead of its upstream and uncommitted file count. It does
not judge whether the work is finished, and it reads nothing from the network: git runs against the
local repository with a short timeout, and any fact git cannot give is null.

Prints `{}` when the directory has no handoff. It looks where the SessionStart hook does, through the
same functions.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handoff  # noqa: E402
import session_start  # noqa: E402

GIT_TIMEOUT_SECONDS = 3
SUMMARY_CAP = 140
REQUESTS_HEADING = "## What was asked"
# "## Goal" at any heading level, with an optional number ("## 1. Goal"), or a bold label ("**Goal**", "**Goal:**").
GOAL_HEADING = re.compile(r"^(?:#{1,6}[ \t]+(?:\d+[.)][ \t]*)?Goal[ \t]*:?|\*\*Goal:?\*\*:?)[ \t]*$", re.IGNORECASE)
BULLET = re.compile(r"^\s*[-*+][ \t]+")


def clip(text):
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CAP else text[: SUMMARY_CAP - 1].rstrip() + "…"


def first_line_under(lines, heading):
    """The first non-empty line of the section `heading` opens, or None.

    `heading` is the exact heading line, or a compiled pattern a heading line matches. A section ends at the next
    heading of any level.
    """
    opens = heading.match if isinstance(heading, re.Pattern) else heading.__eq__
    for index, line in enumerate(lines):
        if opens(line):
            for following in lines[index + 1:]:
                if following.startswith("#"):
                    break
                if following.strip():
                    return following
            return None
    return None


def summary_line(document):
    """What the handoff says it is about: its finished summary, else its goal, else the first request it recorded."""
    lines = document.split("\n")
    for line in lines:
        if line.startswith(handoff.SUMMARY_LINE_PREFIX):
            text = line[len(handoff.SUMMARY_LINE_PREFIX):].strip()
            if text and line != handoff.PENDING_SUMMARY:
                return clip(text)
            break
    goal = first_line_under(lines, GOAL_HEADING)
    if goal:
        return clip(BULLET.sub("", goal))
    request = first_line_under(lines, REQUESTS_HEADING)
    if request:
        return clip(request.split(". ", 1)[1] if request[:1].isdigit() and ". " in request else request)
    return clip(lines[0].lstrip("# ")) if lines and lines[0].strip() else ""


def git_output(cwd, *args):
    """git's stdout for a read-only query in `cwd`, or None when it fails or takes too long."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_facts(cwd):
    branch = git_output(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    ahead = git_output(cwd, "rev-list", "--count", "@{upstream}..HEAD")
    status = git_output(cwd, "status", "--porcelain")
    return {
        "branch": branch.strip() or None if branch is not None else None,
        "ahead": int(ahead) if ahead is not None and ahead.strip().isdigit() else None,
        "uncommitted": len(status.splitlines()) if status is not None else None,
    }


def resume_facts(cwd, env):
    found = session_start.newest_handoff(handoff.handoff_dir({"cwd": cwd}, {}, env))
    if found is None:
        return {}
    path, written = found
    return {
        "path": path,
        "writtenAt": datetime.fromtimestamp(written, timezone.utc).isoformat(),
        "summary": summary_line(handoff.read_text(path)),
        **git_facts(cwd),
    }


def main(argv):
    cwd = argv[1] if len(argv) > 1 else os.getcwd()
    try:
        facts = resume_facts(cwd, os.environ)
    except Exception:
        facts = {}
    print(json.dumps(facts))


if __name__ == "__main__":
    main(sys.argv)
