#!/usr/bin/env python3
"""How long a hook takes, measured through the real `gate.sh`: a script, not a unit test.

Run: python3 plugins/logbook/tests/latency.py

The targets are 50 ms for a hook when no board is active and 200 ms for one that renders. They depend
on the machine, so this prints numbers for a person to read and never fails a gate. Each case runs the
gate 20 times on a compact fixture payload, in a temporary project that is removed afterwards, with
every `GIT_*` variable removed from the environment (the board reads git when it renders) and the
plugin's data folder, where the gate counts work calls, inside the same temporary directory. The
work-call cases with no board run with `LOGBOOK_CALLS` far above their runs, so every run is
counted and none starts a board; the script prints afterwards that none did. The `Stop` case whose
count holds a change starts a board on every run, so its project and count are reset before each
one, outside the time taken, and the script prints how many runs started one.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
GATE = os.path.join(PLUGIN, "hooks", "gate.sh")
PAYLOADS = os.path.join(HERE, "fixtures", "payloads")
sys.path.insert(0, os.path.join(PLUGIN, "board"))

import board  # noqa: E402

RUNS = 20
BOARD_EVENTS = 30
# Far above the work calls the no-board cases make, so none of them reaches the handler.
NEVER = str(100 * RUNS)


def payload(name, **changes):
    with open(os.path.join(PAYLOADS, name + ".json"), encoding="utf-8") as f:
        loaded = json.load(f)
    loaded.update(changes)
    return json.dumps(loaded, separators=(",", ":")).encode("utf-8")


def timed(data, env, before=None):
    """(median, worst) in milliseconds of RUNS runs of the gate on one payload; `before`, when given,
    runs ahead of each run, outside the time taken."""
    took = []
    for _ in range(RUNS):
        if before:
            before()
        began = time.perf_counter()
        subprocess.run(["sh", GATE], input=data, env=env, capture_output=True, check=True)
        took.append((time.perf_counter() - began) * 1000)
    return statistics.median(took), max(took)


def board_of(project, session, count):
    """A board whose log holds `count` events: the start and a step for each of the rest."""
    now = datetime.now(timezone.utc) - timedelta(minutes=count)
    folder = board.start(project, session, now, "A task with many steps")
    for n in range(1, count):
        board.append(folder, "step", now + timedelta(minutes=n), id=str(n), subject=f"step {n}")
    return folder


def count_one(path):
    """A counter file as the gate leaves it after one work call."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def count_of(path, text):
    """A counter file holding `text`, one byte a work call, as the gate writes it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    # `board_of` calls `board.py` in this process, which reads git through this process's own
    # environment; a git hook running this script exports `GIT_DIR` and `GIT_INDEX_FILE`, so every
    # `GIT_*` variable is dropped here too, not only from the environment handed to the gate below.
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        del os.environ[key]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "LOGBOOK_", "CLAUDE_"))}
    with tempfile.TemporaryDirectory() as tmp:
        quiet, busy = os.path.join(tmp, "quiet"), os.path.join(tmp, "busy")
        os.mkdir(quiet)
        os.mkdir(busy)
        data_dir = os.path.join(tmp, "data")
        session = "11111111-2222-4333-8444-555555555555"
        # `SessionEnd` removes its session's counter, so that case is another session's, recreated each run.
        ending = "99999999-8888-4777-8666-555555555555"
        counter, ending_counter = (os.path.join(data_dir, "calls", s) for s in (session, ending))
        folder = board_of(busy, session, BOARD_EVENTS)
        # The `Stop` cases are a third session's, in projects of their own: one whose count holds no
        # change, and one whose count holds a change, where each run starts a board and so the board
        # and the count are put back before it, outside the time taken.
        turn = "77777777-6666-4555-8444-333333333333"
        turn_counter = os.path.join(data_dir, "calls", turn)
        unchanged, changed = os.path.join(tmp, "unchanged"), os.path.join(tmp, "changed")
        os.mkdir(unchanged)
        os.mkdir(changed)
        starts = []

        def fresh_start():
            boards = os.path.join(changed, ".logbook")
            if os.path.exists(boards):
                starts.append(os.path.isdir(os.path.join(boards, turn)))
                shutil.rmtree(boards)
            count_of(turn_counter, "xxcx")

        # A command that costs the shell the most to read: the gate reads only its start.
        far = {"LOGBOOK_CALLS": NEVER}
        quoted = payload("PostToolUse-Bash", tool_input={
            "command": 'python3 -c "' + 'print(\\"a\\");' * 200 + '"', "description": "Print it",
        })

        cases = (
            ("SessionStart, no board folder", payload("SessionStart"), quiet, 50, {}, None),
            ("Stop, no board", payload("Stop"), quiet, 50, {}, None),
            ("TaskCreate id 1, no board", payload("PostToolUse-TaskCreate"), quiet, 50, {}, None),
            ("Bash under the threshold, no board", payload("PostToolUse-Bash"), quiet, 50,
             {"LOGBOOK_CALLS": NEVER}, None),
            ("Edit under the threshold, no board", payload("PostToolUse-Edit"), quiet, 50,
             {"LOGBOOK_CALLS": NEVER}, None),
            ("SessionEnd, no board, counter present", payload("SessionEnd", session_id=ending), quiet, 50,
             {}, lambda: count_one(ending_counter)),
            ("Bash, 200 quoted strings, no board", quoted, quiet, 50, far, None),
            ("Stop, no board, count without a change", payload("Stop", session_id=turn), unchanged, 50, {},
             lambda: count_of(turn_counter, "xxx")),
            ("Stop, no board, count with a change", payload("Stop", session_id=turn), changed, 200, {}, fresh_start),
            (f"TaskUpdate, board of {BOARD_EVENTS} events", payload("PostToolUse-TaskUpdate"), busy, 200, {}, None),
            (f"Bash, board of {BOARD_EVENTS} events", payload("PostToolUse-Bash"), busy, 200, {}, None),
        )
        print(f"{'case':<38} {'median':>8} {'worst':>8} {'target':>8}")
        for name, data, project, target, settings, before in cases:
            case_env = dict(env, CLAUDE_PROJECT_DIR=project, CLAUDE_PLUGIN_ROOT=PLUGIN, CLAUDE_PLUGIN_DATA=data_dir)
            median, worst = timed(data, dict(case_env, **settings), before)
            print(f"{name:<38} {median:>6.1f}ms {worst:>6.1f}ms {target:>6}ms")
        # Each rendering run records one event, so the board grows by one a run: proof the case recorded.
        print(f"events on the board afterwards: {len(board.events(folder))}")
        # Every no-board work call was counted and none started a board: proof those cases stayed under.
        started = os.path.exists(os.path.join(quiet, ".logbook"))
        print(f"no-board project has a .logbook afterwards: {'yes' if started else 'no'}")
        length = os.path.getsize(counter) if os.path.exists(counter) else 0
        print(f"work calls counted in it: {length} (threshold {NEVER})")
        print(f"SessionEnd removed its counter: {'no' if os.path.exists(ending_counter) else 'yes'}")
        # The last run of the `Stop` case with a change is checked here, the others by `fresh_start`.
        fresh_start()
        print(f"Stop with a change started a board: {sum(starts)} of {RUNS} runs")
        print(f"Stop without a change left a .logbook: {'yes' if os.path.exists(os.path.join(unchanged, '.logbook')) else 'no'}")


if __name__ == "__main__":
    main()
