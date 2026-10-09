#!/usr/bin/env python3
"""What the band reads: this session's board folder and its state, as one line of JSON.

    mod_state.py --project DIR --session ID                       the board and its state, or {}
    mod_state.py --project DIR --session ID --start [--transcript P] start the board first, if there is none
    mod_state.py --project DIR --session ID --open                   also open the board's page in the browser

The board is found as every command finds it (`board.find_board`: the session's folder under the project or
the nearest folder above it, a linked boards folder refused), in the project the hooks would use
(`CLAUDE_PROJECT_DIR`, else `--project`, inside the home or temporary folder unless `LOGBOOK_ALLOW_ANY_PATH=1`).
The state is the one `state.js` holds (`board.read_state`). With no board, or one that cannot be used, the
line is `{}`.

`--start` starts the board the way the hooks do at their threshold, from the same functions: the title is the
first prompt in the transcript and the calls made so far are read back from it, marked `early`. It does not
write the `announced` file, so the next hook that can carry context still tells the model how to record on the
board (`board.py start` writes it, and the model would never be told). A start that is refused, or fails,
prints `{}`. This never changes what the hooks do; it only reads, or starts a board they would also have started.
Without `--transcript` (the mod is told the session's id, not its transcript's path), the transcript is the one
file named for the id under `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/`; an id that is not UUID-shaped, or
no single match, starts the board untitled.

In a project with next-session mode on, the line also carries `"briefNotUpdated": true` when the newest commit
landed after both the session's start and the brief's last write (`brief_not_updated`), board or no board; a
brief not written yet is older than any commit. The session started at the first entry in its transcript that
carries a time: the same file for the whole session, through a compaction or a resume, and a new one after a
`/clear`, which is a new session. The newest commit is read from the end of the checkout's own reflog
(`newest_commit`), so it starts no process. With the mode off, no transcript, no repository or no reflog, the key
is left out.
"""
import argparse
from datetime import datetime
import glob
import json
import os
import pathlib
import re
import stat
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))

import board  # noqa: E402
import board_hook  # noqa: E402


def board_and_state(project, session, env):
    """({"board": folder, "state": state}, or {}) for the session's board under `project`."""
    env = dict(env, **{board.SESSION_VARIABLE: session})
    project = board.contained(os.path.abspath(env.get("CLAUDE_PROJECT_DIR") or project), env)
    if project is None:
        return {}
    folder, _ = board.find_board(project, env)
    if folder is None:
        return {}
    state = board.read_state(folder)
    return {"board": folder, "state": state} if state is not None else {}


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def find_transcript(session, env):
    """The session's transcript, `<config>/projects/*/<session>.jsonl`, when exactly one file matches; else None."""
    if not UUID.fullmatch(session):
        return None
    config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    found = [path for path in glob.glob(os.path.join(glob.escape(config), "projects", "*", session + ".jsonl"))
             if os.path.isfile(path)]
    return found[0] if len(found) == 1 else None


def start_board(project, session, transcript, env):
    """Start the session's board from the transcript unless it has one. Returns whether this call started it."""
    project = board.contained(os.path.abspath(env.get("CLAUDE_PROJECT_DIR") or project), env)
    if project is None or board.find_board(project, dict(env, **{board.SESSION_VARIABLE: session}))[0] is not None:
        return False
    if transcript is None:
        transcript = find_transcript(session, env)
    prompt, earlier = board_hook.read_transcript(transcript, set(), env=env)
    return board.start(project, session, board.clock(), board_hook.title_from(prompt), env, early=earlier) is not None


# How far into a transcript to look for its first timed entry: the few untimed ones a host writes come first.
TIMED_ENTRY_LINES = 50


def session_started(transcript):
    """When the session started, in seconds since the epoch: the first transcript entry with a `timestamp`, or None."""
    if transcript is None:
        return None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(TIMED_ENTRY_LINES), f):
                try:
                    stamp = json.loads(line).get("timestamp")
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError, TypeError):
                    continue
    except OSError:
        return None
    return None


# How much of a reflog's end is read: a few hundred entries, never the whole file.
REFLOG_TAIL_BYTES = 32 * 1024
# The reflog messages git writes when HEAD moves to a commit made or merged here: `commit: …` and `commit (amend|
# initial|merge|cherry-pick): …`, `merge <name>: …` (a fast-forward too), `cherry-pick: …`, `revert: …`, `am: …`,
# and a rebase finishing, `rebase (finish): …` (`rebase -i (finish): …` from older gits). A checkout, a reset, a
# rebase's own start and steps, and a pull are not commits landing here.
LANDED = re.compile(r"(?:commit(?: \([^)]*\))?|merge [^:]*|cherry-pick|revert|am|rebase(?: -i)? \(finish\)):")


def read_regular(path, limit):
    """The last `limit` bytes of `path` when it is a regular file, else None. Never blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        end = os.lseek(fd, 0, os.SEEK_END)
        os.lseek(fd, max(0, end - limit), os.SEEK_SET)
        chunks, wanted = [], min(end, limit)
        while wanted > 0:
            chunk = os.read(fd, wanted)
            if not chunk:
                break
            chunks.append(chunk)
            wanted -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def git_dir(project):
    """The project's own git directory, found as git finds it from inside: the nearest `.git` above, a folder, or
    a file naming one (a linked worktree's, a submodule's). None outside a repository."""
    directory = os.path.realpath(project)
    while True:
        dot = os.path.join(directory, ".git")
        if os.path.isdir(dot):
            return dot
        if os.path.isfile(dot):
            pointer = (read_regular(dot, 4096) or b"").decode("utf-8", errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return None
            return os.path.join(directory, pointer[len("gitdir:"):].strip())
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def newest_commit(project):
    """When HEAD last moved to a commit made or merged in this checkout, in seconds since the epoch, or None.

    Read from the last entries of `<git dir>/logs/HEAD` whose message `LANDED` matches; none, or no reflog, is None.
    """
    found = git_dir(project)
    tail = read_regular(os.path.join(found, "logs", "HEAD"), REFLOG_TAIL_BYTES) if found else None
    if not tail:
        return None
    lines = tail.split(b"\n")
    if len(tail) == REFLOG_TAIL_BYTES:
        lines = lines[1:]  # the first may be cut
    for line in reversed(lines):
        head, tab, message = line.partition(b"\t")
        if not tab or not LANDED.match(message.decode("utf-8", errors="replace")):
            continue
        stamp = head.rsplit(b" ", 2)  # `<old> <new> <name> <email> <seconds> <zone>`
        if len(stamp) == 3 and stamp[1].isdigit():
            return int(stamp[1])
    return None


def brief_not_updated(project, session, transcript, env):
    """Whether next-session mode is on and the newest commit is newer than both the session's start and the brief."""
    project = board.contained(os.path.abspath(env.get("CLAUDE_PROJECT_DIR") or project), env)
    brief = board.next_session(project, count_lines=False) if project is not None else None
    if brief is None:
        return False
    started = session_started(transcript or find_transcript(session, env))
    if started is None:
        return False
    committed = newest_commit(project)
    if committed is None or committed <= started:
        return False
    try:
        return committed > os.stat(brief["path"]).st_mtime
    except OSError:
        return True  # no brief yet


def open_board(folder):
    """Open the board's page in the default browser. Returns whether the browser took it."""
    page = os.path.join(folder, board.BOARD_FILE)
    return os.path.isfile(page) and webbrowser.open(pathlib.Path(page).as_uri())


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    args.add_argument("--project", required=True)
    args.add_argument("--session", required=True)
    args.add_argument("--start", action="store_true")
    args.add_argument("--transcript")
    args.add_argument("--open", action="store_true", help="open the board's page in the browser")
    args = args.parse_args(argv)
    try:
        if args.start:
            start_board(args.project, args.session, args.transcript, os.environ)
        found = board_and_state(args.project, args.session, os.environ)
        if args.open:
            found = dict(found, opened=bool(found) and open_board(found["board"]))
    except Exception:
        found = {}
    if not args.open:
        try:
            if brief_not_updated(args.project, args.session, args.transcript, os.environ):
                found = dict(found, briefNotUpdated=True)
        except Exception:
            pass
    print(json.dumps(found, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
