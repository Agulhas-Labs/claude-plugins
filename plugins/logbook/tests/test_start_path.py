"""Tests for the path by which a board starts: published whole, recovered by call, never through a link.

Run: python3 -m unittest discover -s plugins/logbook/tests

The hook tests run the real `gate.sh` through the helpers in `test_hooks.py`, whose environment has
every `GIT_*` variable removed; the in-process tests call `board_hook.handle` and `board.start` with
that same environment, with `os.environ` patched to it, since `board.start` runs git. A race is arranged with a patch that runs the second start from inside the
first, at the step named in each test, never with a sleep.
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from unittest import mock

from test_board import NOW, event
from test_hooks import PLUGIN, SESSION, TRANSCRIPT, Hooks, fixture, task_create, transcript_lines

import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402  (test_hooks puts the hooks folder on the path)

BOARD_PY = os.path.join(PLUGIN, "board", "board.py")
FOUR = ((1, "alpha"), (2, "beta"), (3, "gamma"), (4, "delta"))
LINKED = "logbook: the boards folder is a link, so no board was started\n"


def update_lines(number, status, use):
    """A transcript's lines for one `TaskUpdate` call and its result, in the shape of the captured one."""
    lines = [
        {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": use, "name": "TaskUpdate", "input": {"taskId": str(number), "status": status}},
        ]}},
        {"type": "user", "isSidechain": False, "toolUseResult": {"success": True, "taskId": str(number)},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": use, "content": f"Updated task #{number} status"},
         ]}},
    ]
    return [json.dumps(line, separators=(",", ":")) for line in lines]


class StartPath(Hooks):

    def setUp(self):
        super().setUp()
        # The in-process starts run git too: they see the hooks' environment, with no `GIT_*` variable.
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def transcript_of(self, lines):
        """The captured transcript's first prompt, then exactly these lines."""
        with open(TRANSCRIPT, encoding="utf-8") as f:
            first_line = f.readline()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(first_line + "".join(line + "\n" for line in lines))

    def status(self, step_id):
        return next(s["status"] for s in self.state()["steps"] if s["id"] == step_id)

    def boards(self):
        return os.path.join(self.project, ".logbook")

    def handled(self, payload):
        payload = dict(payload, transcript_path=self.transcript)
        return board_hook.handle(payload, self.env, NOW)


class Recovery(StartPath):

    def test_a_status_the_gate_dropped_as_the_board_started_is_recovered(self):
        # One turn: `TaskCreate` #5 and `TaskUpdate 1 completed` in parallel. The update's hook ran before
        # the board existed, so the gate dropped it, and the transcript did not hold it yet at the start.
        before = transcript_lines(*FOUR) + update_lines(1, "in_progress", "toolu_update_1a")
        self.transcript_of(before)
        for number in range(1, 5):
            self.assertIsNone(self.hook(task_create(number)))
        self.hook(task_create(5, "epsilon"))
        self.assertEqual(self.status("1"), "in_progress")

        self.transcript_of(before + update_lines(1, "completed", "toolu_update_1b") + transcript_lines((5, "epsilon")))
        self.hook(task_create(6, "zeta"))

        self.assertEqual(self.status("1"), "completed")

    def test_an_older_recovered_status_does_not_undo_a_newer_one_a_hook_recorded(self):
        log = [
            event("start", 0, title="x"),
            event("step", 0, id="1", subject="alpha", use="toolu_create_1", early=True),
            event("step-status", 1, id="1", status="completed", use="toolu_update_1b"),
            event("step-status", 2, id="1", status="in_progress", use="toolu_update_1a", early=True),
        ]
        steps = board.derive(log, [], board.settings({}))["steps"]
        self.assertEqual([(s["id"], s["status"]) for s in steps], [("1", "completed")])

    def test_the_triggering_call_found_in_the_transcript_later_is_recorded_once(self):
        self.write_prompt_only()
        self.hook(task_create(5, "epsilon"))
        self.transcript_of(transcript_lines(*FOUR, (5, "epsilon")))
        self.hook(fixture("Stop"))

        log = board.events(self.folder)
        self.assertEqual([e.get("use") for e in log].count("toolu_create_5"), 1)
        self.assertEqual(sorted(e["id"] for e in log if e["kind"] == "step"), ["1", "2", "3", "4", "5"])
        self.assertTrue(all(e.get("early") is True for e in log if e["kind"] == "step" and e["id"] != "5"))

    def test_recovery_stops_after_five_reads(self):
        self.write_prompt_only()
        self.hook(task_create(5, "epsilon"))
        for _ in range(5):
            self.hook(fixture("Stop"))
        self.transcript_of(update_lines(5, "completed", "toolu_update_5"))

        self.hook(fixture("Stop"))

        with open(os.path.join(self.folder, "catch-up"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "5")
        self.assertEqual(self.status("5"), "pending")


class Publication(StartPath):

    def test_a_board_is_published_whole_with_its_early_events(self):
        early = [
            ("step", {"id": "1", "subject": "alpha", "use": "toolu_create_1", "early": True}),
            ("step-status", {"id": "1", "status": "completed", "use": "toolu_update_1", "early": True}),
        ]
        seen, real = [], board.write_state

        def looking(folder, *args):
            gate_test = subprocess.run(
                ["sh", "-c", '[ -f "$1/.logbook/$2/.logbook" ]', "sh", self.project, SESSION],
                env=self.env,
            )
            seen.append((os.path.exists(self.folder), gate_test.returncode, os.listdir(self.boards())))
            return real(folder, *args)

        with mock.patch.object(board, "write_state", side_effect=looking):
            created = board.start(self.project, SESSION, NOW, "Title", self.env, early=early)

        self.assertEqual(created, self.folder)
        exists, gate_test, names = seen[0]
        self.assertFalse(exists)
        self.assertNotEqual(gate_test, 0)
        self.assertEqual([n for n in names if n.startswith(SESSION)], [n for n in names if board.STARTING in n])
        log = board.events(self.folder)
        self.assertEqual([e["kind"] for e in log], ["start", "step", "step-status"])
        self.assertEqual([(e.get("use"), e.get("early")) for e in log[1:]],
                         [("toolu_create_1", True), ("toolu_update_1", True)])
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "board.html")))
        self.assertIsNotNone(board.read_state(self.folder))

    def test_of_two_starts_for_one_session_exactly_one_creates(self):
        second, real = [], board.write_state

        def second_meanwhile(folder, *args):
            if not second:
                second.append(None)
                second[0] = board.start(self.project, SESSION, NOW, "second", self.env)
            return real(folder, *args)

        with mock.patch.object(board, "write_state", side_effect=second_meanwhile):
            first = board.start(self.project, SESSION, NOW, "first", self.env)

        self.assertEqual((first, second[0]), (None, self.folder))
        self.assertEqual([e["kind"] for e in board.events(self.folder)], ["start"])
        self.assertEqual(board.events(self.folder)[0]["title"], "second")
        self.assertEqual([n for n in os.listdir(self.boards()) if board.STARTING in n], [])

    def test_of_two_hooks_that_start_a_board_at_once_only_one_says_so(self):
        outputs, real = [], board.branch_heads

        def other_hook_meanwhile(project):
            if not outputs:
                outputs.append(None)
                outputs[0] = self.handled(task_create(6, "zeta"))
            return real(project)

        self.write_prompt_only()
        with mock.patch.object(board, "branch_heads", side_effect=other_hook_meanwhile):
            outputs.append(self.handled(task_create(5, "epsilon")))

        self.assertEqual(sum(1 for o in outputs if o and "systemMessage" in o), 1)
        self.assertEqual(sum(1 for o in outputs if o), 1)
        self.assertEqual(sorted(s["id"] for s in self.state()["steps"]), ["5", "6"])


class StartingFolders(StartPath):

    def starting_folder(self, last):
        """What a start killed while building leaves: a marked folder with a log, under a building name."""
        folder = os.path.join(self.boards(), SESSION + board.STARTING + "4242-0a1b2c3d")
        os.makedirs(folder)
        with open(os.path.join(folder, ".logbook"), "w", encoding="utf-8") as f:
            f.write(SESSION)
        with open(os.path.join(folder, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": board.utc(last), "kind": "start", "title": "Cut short"}) + "\n")
        with open(os.path.join(folder, "state.js"), "w", encoding="utf-8") as f:
            f.write('window.BOARD = {"title": "Cut short"};\n')
        return folder

    def test_a_starting_folder_is_not_listed_in_the_index(self):
        board.start(self.project, SESSION, NOW, "Published", self.env)
        self.starting_folder(NOW)
        self.assertEqual([row["name"] for row in board.board_rows(self.boards())], [SESSION])

    def test_a_starting_name_is_not_a_session_id(self):
        with self.assertRaises(ValueError):
            board.board_dir(self.project, SESSION + board.STARTING + "4242-0a1b2c3d")

    def test_a_starting_folder_left_by_a_killed_start_is_pruned_when_old(self):
        folder = self.starting_folder(NOW - timedelta(days=30))
        self.assertEqual(board.prune(self.project, NOW, {}), [folder])
        self.assertFalse(os.path.exists(folder))


class LinkedBoardsFolder(StartPath):

    def cli(self, *argv):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SESSION)
        done = subprocess.run([sys.executable, BOARD_PY, *argv], cwd=self.project, env=env, capture_output=True, timeout=60)
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def test_a_linked_boards_folder_gets_nothing_from_a_start_the_hook_or_the_command(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        for name, target in (("the project itself", "."), ("a folder elsewhere", elsewhere)):
            with self.subTest(name):
                os.symlink(target, self.boards())
                try:
                    self.assertIsNone(board.start(self.project, SESSION, NOW, "Title", self.env))
                    self.assertIsNone(self.hook(fixture("SubagentStart")))
                    self.assertEqual(self.cli("start", "Title"), (1, "", LINKED))
                    board.update_index(self.folder, self.env)

                    self.assertEqual(os.listdir(self.project), [".logbook"])
                    self.assertEqual(os.listdir(elsewhere), [])
                finally:
                    os.unlink(self.boards())

    def test_a_boards_folder_that_is_a_file_is_refused(self):
        with open(self.boards(), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertIsNone(board.start(self.project, SESSION, NOW, "Title", self.env))
        self.assertIsNone(self.hook(fixture("SubagentStart")))
        self.assertEqual(os.listdir(self.project), [".logbook"])


if __name__ == "__main__":
    unittest.main()
