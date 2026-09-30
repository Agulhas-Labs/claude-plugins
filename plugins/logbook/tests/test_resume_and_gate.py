"""Tests for three review findings: a resumed session can reopen its closed board, the gate's 4096-byte
head keeps a large payload cheap, and older hosts' `Task` tool is read exactly as `Agent`.

Run: python3 -m unittest discover -s plugins/logbook/tests

Reuses the helpers `test_hooks.py` and `test_board.py` already set up: `Hooks` runs the real `gate.sh`
in a temporary project with `GIT_*`, `LOGBOOK_*` and `CLAUDE_*` stripped from the environment
first, and `fixture`/`task_create`/`compact` build payloads from the captured ones.
"""
import json
import os
import subprocess
import time
import unittest

from test_hooks import GATE, Hooks, SESSION, compact, fixture, task_create

import board  # noqa: E402  (test_board puts the board folder on the path)


class Reopening(Hooks):
    """Finding 1: `SessionEnd` closes a board, and a resumed session can reopen it."""

    def session_start(self, source, session=SESSION, env=None):
        payload = fixture("SessionStart")
        payload.update(source=source, session_id=session)
        return self.hook(payload, env)

    def close_the_board(self):
        self.start_with_subagent()
        self.hook(fixture("SessionEnd"))
        self.assertTrue(any(e["kind"] == "close" for e in board.events(self.folder)))

    def test_a_resume_reopens_a_closed_board_and_recording_works_again(self):
        self.close_the_board()
        output = self.session_start("resume")
        self.assertEqual(output["systemMessage"], "Logbook: " + os.path.join(self.folder, "board.html"))
        self.assertIn("hookSpecificOutput", output)
        self.assertEqual(self.state()["state"], "live")

        update = fixture("PostToolUse-TaskUpdate")
        update["tool_input"] = {"taskId": "1", "status": "in_progress"}
        self.hook(update)
        self.assertEqual(self.state()["state"], "live")
        self.assertEqual(self.kinds()[-1], "step-status")

    def test_compact_or_startup_leaves_a_board_closed_by_hand_closed(self):
        self.start_with_subagent()
        board.close(self.folder, board.clock(), self.env)
        before = self.kinds()
        self.assertIsNone(self.session_start("compact"))
        self.assertIsNone(self.session_start("startup"))
        self.assertEqual(self.kinds(), before)
        self.assertEqual(self.state()["state"], "finished")

    def test_close_reopen_close_gives_finished_with_the_step_in_between_in_the_report(self):
        self.close_the_board()
        self.session_start("resume")
        self.hook(task_create(7, "after the reopen"))
        self.hook(fixture("SessionEnd"))

        self.assertEqual(self.state()["state"], "finished")
        kinds = self.kinds()
        self.assertEqual(kinds.count("close"), 2)
        self.assertEqual(kinds.count("reopen"), 1)
        with open(os.path.join(self.folder, "report.html"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("after the reopen", report)

    def test_the_index_links_board_html_while_reopened(self):
        self.close_the_board()
        self.session_start("resume")
        index = os.path.join(self.project, ".logbook", "index.html")
        with open(index, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"{SESSION}/board.html", text)
        self.assertNotIn(f"{SESSION}/report.html", text)


class GateHead(Hooks):
    """Finding 2: the gate reads only the payload's first 4096 bytes, so a large value elsewhere stays cheap."""

    def timed(self, data):
        began = time.perf_counter()
        done = subprocess.run(["sh", GATE], input=data, env=self.env, capture_output=True, timeout=10)
        took = time.perf_counter() - began
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout, took

    def test_a_task_id_past_the_head_is_handed_to_the_handler_which_decides(self):
        payload = task_create(5, "epsilon")
        payload["tool_input"]["description"] = "x" * 200_000
        out, took = self.timed(compact(payload).encode("utf-8"))
        self.assertLess(took, 2.0)
        output = json.loads(out.decode("utf-8"))
        self.assertIn("systemMessage", output)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "board.html")))

    def test_a_session_id_past_the_head_exits_silently_and_fast(self):
        payload = {"noise": "x" * 200_000, **fixture("PostToolUse-TaskCreate")}
        out, took = self.timed(compact(payload).encode("utf-8"))
        self.assertLess(took, 2.0)
        self.assertEqual(out, b"")
        self.assertEqual(self.created_anything(), [])


class OlderHostTaskTool(Hooks):
    """Finding 3: an older host names the subagent-launching tool `Task`, not `Agent`."""

    def task_payload(self):
        """Written from documentation, not captured: `Task` was not observed on the version the other
        payloads came from. Its shape otherwise follows the captured `Agent` call."""
        payload = fixture("PostToolUse-Agent")
        payload["tool_name"] = "Task"
        payload["tool_input"] = {"description": "Simple reply task", "subagent_type": "general-purpose"}
        payload["tool_response"] = {"agentId": "ac91f2c94fb8193d6", "resolvedModel": "claude-haiku-4-5-20251001"}
        return payload

    def test_a_task_call_records_agent_info_like_an_agent_call(self):
        self.start_with_subagent()
        self.hook(self.task_payload())
        agent = self.state()["agents"][0]
        self.assertEqual(
            (agent["id"], agent["model"], agent["description"]),
            ("ac91f2c94fb8193d6", "claude-haiku-4-5-20251001", "Simple reply task"),
        )

    def test_a_task_call_from_inside_a_subagent_is_ignored(self):
        self.start_with_subagent()
        before = self.kinds()
        self.hook(dict(self.task_payload(), agent_id="ac91f2c94fb8193d6"))
        self.assertEqual(self.kinds(), before)

    def test_a_task_call_with_none_of_those_fields_records_nothing_tolerantly(self):
        self.start_with_subagent()
        before = self.kinds()
        payload = self.task_payload()
        payload["tool_response"] = {"status": "completed"}
        payload["tool_input"] = {}
        self.hook(payload)
        self.assertEqual(self.kinds(), before)


if __name__ == "__main__":
    unittest.main()
