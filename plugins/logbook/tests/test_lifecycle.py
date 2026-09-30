"""Tests for five review findings: who closed a board decides what reopens it, recovery is for calls made
before the board existed, a boards folder that is a link is refused everywhere, a rewritten report keeps
its commits, and a task id the gate's head cuts short is handed to the handler.

Run: python3 -m unittest discover -s plugins/logbook/tests

Reuses the helpers `test_hooks.py` and `test_board.py` already set up: `Hooks` runs the real `gate.sh`
in a temporary project with `GIT_*`, `LOGBOOK_*` and `CLAUDE_*` stripped from the environment
first, and `Workspace` makes a repository with the `GIT_*`-free `git` helper. Every program started
here gets that stripped environment.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_board import Workspace, at
from test_hooks import (
    PLUGIN, SESSION, TRANSCRIPT, Hooks, compact, fixture, task_create, transcript_lines,
)

import board  # noqa: E402  (test_hooks puts the board folder on the path)
import board_hook  # noqa: E402  (test_hooks puts the hooks folder on the path)

BOARD_SCRIPT = os.path.join(PLUGIN, "board", "board.py")
LINKED = "logbook: the boards folder is a link, so nothing was recorded\n"


def task_update(task_id, status, use):
    payload = fixture("PostToolUse-TaskUpdate")
    payload["tool_input"] = {"taskId": str(task_id), "status": status}
    payload["tool_response"] = {"success": True, "taskId": str(task_id), "updatedFields": ["status"]}
    payload["tool_use_id"] = use
    return payload


def update_lines(task_id, status, use):
    """A transcript's lines for one `TaskUpdate` call, in the shape of the captured ones."""
    lines = [
        {"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": use, "name": "TaskUpdate", "input": {"taskId": str(task_id), "status": status}},
        ]}},
        {"type": "user", "isSidechain": False, "toolUseResult": {"success": True, "taskId": str(task_id)},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": use, "content": f"Updated task #{task_id} status"},
         ]}},
    ]
    return [json.dumps(line, separators=(",", ":")) for line in lines]


class Lifecycle(Hooks):

    def session_start(self, source):
        payload = fixture("SessionStart")
        payload["source"] = source
        return self.hook(payload)

    def close_by_hand(self):
        """The model's `close` command, run as the model runs it."""
        done = subprocess.run(
            [sys.executable, BOARD_SCRIPT, "close", "--board", self.folder],
            env=self.env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    def closes(self):
        return [event.get("by") for event in board.events(self.folder) if event["kind"] == "close"]

    def write_after_prompt(self, extra):
        """The transcript's first prompt, then `extra`: none of the captured calls."""
        with open(TRANSCRIPT, encoding="utf-8") as f:
            first_line = f.readline()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(first_line + "".join(line + "\n" for line in extra))


class ClosedBy(Lifecycle):
    """Finding 1: a board its session's end closed is reopened by the next event from the session."""

    def test_a_late_session_end_after_a_resume_does_not_stop_the_recording(self):
        self.start_with_subagent()
        self.hook(fixture("SessionEnd"))
        self.session_start("resume")
        self.hook(fixture("SessionEnd"))
        self.assertEqual(self.state()["state"], "finished")
        self.assertEqual(self.closes(), ["session-end", "session-end"])

        self.hook(task_update(1, "in_progress", "toolu_after_the_late_end"))

        self.assertEqual(self.kinds()[-2:], ["reopen", "step-status"])
        self.assertEqual(self.state()["state"], "live")
        self.assertEqual(self.state()["steps"][0]["status"], "in_progress")

    def test_a_board_closed_by_hand_records_nothing_more(self):
        self.start_with_subagent()
        self.close_by_hand()
        self.assertEqual(self.closes(), ["hand"])
        logged = self.kinds()
        self.assertIsNone(self.hook(task_update(1, "in_progress", "toolu_after_the_close")))
        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        self.assertEqual(self.kinds(), logged)
        self.assertEqual(self.state()["state"], "finished")

    def test_a_board_closed_by_hand_records_again_after_a_resume(self):
        self.start_with_subagent()
        self.close_by_hand()
        self.session_start("resume")
        self.hook(task_update(1, "in_progress", "toolu_after_the_resume"))
        self.assertEqual(self.kinds()[-1], "step-status")
        self.assertEqual(self.state()["state"], "live")

    def test_a_close_without_by_counts_as_by_hand(self):
        self.start_with_subagent()
        board.append(self.folder, "close", at(1))
        self.assertEqual(board.closed_by(board.events(self.folder)), "hand")
        logged = self.kinds()
        self.assertIsNone(self.hook(task_update(1, "in_progress", "toolu_after_an_old_close")))
        self.assertEqual(self.kinds(), logged)
        self.session_start("resume")
        self.hook(task_update(1, "in_progress", "toolu_after_the_resume"))
        self.assertEqual(self.kinds()[-2:], ["reopen", "step-status"])


class RecoveryBeforeTheBoard(Lifecycle):
    """Finding 2: recovery reads back calls made before the board existed, and only those."""

    def test_a_call_dropped_while_closed_by_hand_is_not_recovered_after_a_resume(self):
        creates = transcript_lines(*[(n, f"step {n}") for n in range(1, 6)])
        self.write_after_prompt(creates[:8])
        for number in range(1, 6):
            self.hook(task_create(number))
        self.write_after_prompt(creates + update_lines(5, "in_progress", "toolu_five_started"))
        self.hook(task_update(5, "in_progress", "toolu_five_started"))
        self.close_by_hand()
        closed_at = len(board.events(self.folder))

        self.write_after_prompt(
            creates + update_lines(5, "in_progress", "toolu_five_started")
            + update_lines(5, "completed", "toolu_five_done")
        )
        self.assertIsNone(self.hook(task_update(5, "completed", "toolu_five_done")))
        self.hook(fixture("SessionEnd"))
        self.session_start("resume")
        self.hook(fixture("UserPromptSubmit"))

        step = next(s for s in self.state()["steps"] if s["id"] == "5")
        self.assertEqual(step["status"], "in_progress")
        after = board.events(self.folder)[closed_at:]
        self.assertEqual([e for e in after if e.get("early")], [])

    def test_recovery_stops_at_the_first_call_the_log_holds_as_a_hook_recorded_it(self):
        self.write_after_prompt([])
        self.hook(task_create(5, "step 5"))
        self.assertEqual([s["id"] for s in self.state()["steps"]], ["5"])

        self.write_after_prompt(
            transcript_lines(*[(n, f"step {n}") for n in range(1, 6)])
            + update_lines(5, "completed", "toolu_after_the_board")
        )
        self.hook(fixture("Stop"))

        self.assertEqual([s["id"] for s in self.state()["steps"]], ["1", "2", "3", "4", "5"])
        self.assertNotIn("toolu_after_the_board", [e.get("use") for e in board.events(self.folder)])
        self.assertEqual(self.state()["steps"][-1]["status"], "pending")


class LinkedBoards(Lifecycle):
    """Finding 3: a boards folder that is a symbolic link is used nowhere, not only at the start."""

    def setUp(self):
        super().setUp()
        self.start_with_subagent()
        boards = os.path.join(self.project, ".logbook")
        self.elsewhere = os.path.join(self.tmp, "elsewhere")
        os.rename(boards, self.elsewhere)
        os.symlink(self.elsewhere, boards)
        self.log = os.path.join(self.elsewhere, SESSION, "events.jsonl")
        self.before = self.log_bytes()

    def log_bytes(self):
        with open(self.log, "rb") as f:
            return f.read()

    def test_the_gate_prints_nothing_changes_nothing_and_never_starts_python(self):
        # The same `python3` that notes each start as in `test_hooks`.
        bin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bin_dir.cleanup)
        started = os.path.join(bin_dir.name, "python-started")
        with open(os.path.join(bin_dir.name, "python3"), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho started >> "{started}"\nexec "{sys.executable}" "$@"\n')
        os.chmod(os.path.join(bin_dir.name, "python3"), 0o755)
        env = dict(self.env, PATH=bin_dir.name + os.pathsep + self.env.get("PATH", ""))

        self.assertIsNone(self.hook(fixture("Stop"), env))
        self.assertIsNone(self.hook(task_update(1, "in_progress", "toolu_through_the_link"), env))
        resume = dict(fixture("SessionStart"), source="resume")
        self.assertIsNone(self.hook(resume, env))
        self.assertEqual(self.log_bytes(), self.before)
        self.assertFalse(os.path.exists(started), "the gate started Python for a linked boards folder")

    def test_the_handler_does_nothing_through_the_link(self):
        now = at(5)
        for payload in (fixture("Stop"), task_update(1, "completed", "toolu_x"), fixture("SessionEnd"),
                        dict(fixture("SessionStart"), source="resume")):
            payload["transcript_path"] = self.transcript
            self.assertIsNone(board_hook.handle(payload, self.env, now))
        self.assertEqual(self.log_bytes(), self.before)

    def test_the_question_command_says_so_and_records_nothing(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SESSION)
        for board_args in (["--board", os.path.join(self.project, ".logbook", SESSION)], []):
            with self.subTest(board_args=board_args):
                done = subprocess.run(
                    [sys.executable, BOARD_SCRIPT, "question", "Which format?", "--default", "CSV", *board_args],
                    cwd=self.project, env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", LINKED))
        self.assertEqual(self.log_bytes(), self.before)


def timing_out(*args, **kwargs):
    raise subprocess.TimeoutExpired("git", board.GIT_TIMEOUT_SECONDS)


class RewrittenReport(Workspace):
    """Finding 4: a report written back after it was lost keeps the commits the board had kept."""

    def test_a_kept_commit_is_in_the_rewritten_report(self):
        self.make_repository()
        self.commit("before")
        folder = self.start()
        kept = self.commit("the kept commit")
        board.render(folder, at(1), env={}, template=self.template)
        board.close(folder, at(2), env={}, template=self.template)
        report = os.path.join(folder, board.REPORT_FILE)
        self.assertIn(kept, self.read(report))

        os.remove(report)
        with mock.patch.object(board.subprocess, "run", side_effect=timing_out):
            board.close(folder, at(3), env={}, template=self.template)
        self.assertIn(kept, self.read(report))
        self.assertIn("the kept commit", self.read(report))


class CutTaskId(Hooks):
    """Finding 5: a `TaskCreate` id the gate's 4096-byte head cuts short goes to the handler."""

    def test_an_id_cut_by_the_head_starts_the_board(self):
        payload = task_create(12, "twelve")
        payload["transcript_path"] = self.transcript
        payload["tool_input"]["description"] = ""
        marker = '"task":{"id":"'
        base = compact(payload)
        payload["tool_input"]["description"] = "x" * (4095 - (base.index(marker) + len(marker)))
        text = compact(payload)
        self.assertTrue(text[:4096].endswith('"id":"1'), text[4080:4100])

        output = self.hook(text)

        self.assertIsNotNone(output, "the gate read the cut id as a smaller one")
        self.assertIn("systemMessage", output)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "board.html")))


if __name__ == "__main__":
    unittest.main()
