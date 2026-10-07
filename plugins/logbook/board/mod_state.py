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
"""
import argparse
import glob
import json
import os
import pathlib
import re
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
    found = glob.glob(os.path.join(glob.escape(config), "projects", "*", session + ".jsonl"))
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
    print(json.dumps(found, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
