"""Tests for subagent-background-runs.py. Run: python3 -m unittest discover -s plugins/orchestration/tests"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "subagent-background-runs.py")
spec = importlib.util.spec_from_file_location("background_runs", HOOK)
runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runs)


TEXT = (
    "Command running in background with ID: {task}. Output is being written to: "
    "/tmp/tasks/{task}.output. If it exits while you are still working you will be notified"
)


def started(task, tool_id="toolu_1"):
    """The user record an interactive session writes for a backgrounded Bash call."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "…"}]},
        "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": task},
    }


def started_in_print_mode(task, tool_id="toolu_1"):
    """The same call recorded by the CLI in print mode: the result's text, and no toolUseResult."""
    content = [{"type": "tool_result", "tool_use_id": tool_id, "content": TEXT.format(task=task)}]
    return {"type": "user", "message": {"role": "user", "content": content}}


def notified(task, status="completed"):
    """The attachment record a finished background command leaves behind."""
    prompt = (
        f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
        f"<output-file>/tmp/tasks/{task}.output</output-file>\n<status>{status}</status>\n"
        f'<summary>Background command "swift test" {status}</summary>\n</task-notification>'
    )
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}}


def task_stopped(task):
    return {
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "tool_use", "id": "toolu_2", "name": "TaskStop", "input": {"task_id": task}}]},
    }


class BackgroundRunTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="background-runs-test-")
        self.addCleanup(shutil.rmtree, self.root)
        self.parent = os.path.join(self.root, "session.jsonl")
        self.agent_file = os.path.join(self.root, "session", "subagents", "agent-a1.jsonl")
        os.makedirs(os.path.dirname(self.agent_file))

    def write(self, *entries):
        with open(self.agent_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def ask(self, **overrides):
        payload = {"hook_event_name": "SubagentStop", "transcript_path": self.parent, "agent_id": "a1", "stop_hook_active": False}
        payload.update(overrides)
        return runs.reason(payload)

    def test_a_run_still_going_blocks_the_stop_and_names_it(self):
        self.write(started("bd32o4p1o"))
        self.assertIn("bd32o4p1o", self.ask())

    def test_a_run_recorded_in_print_mode_blocks_the_stop_too(self):
        # A transcript written by the CLI in print mode carries the result's text and no
        # toolUseResult object, so reading only that object would let every such stop through.
        self.write(started_in_print_mode("bd32o4p1o"))
        self.assertIn("bd32o4p1o", self.ask())

    def test_the_sentence_quoted_inside_a_result_names_no_run_of_this_agents(self):
        # Reading another transcript or another run's output file brings the sentence in mid-result.
        quoted = started_in_print_mode("bd32o4p1o")
        block = quoted["message"]["content"][0]
        block["content"] = "42\t" + block["content"]
        said = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": TEXT.format(task="bcrt1kt74")}]}}
        self.write(quoted, said)
        self.assertIsNone(self.ask())

    def test_a_result_given_as_text_parts_is_read_too(self):
        parts = started_in_print_mode("bd32o4p1o")
        block = parts["message"]["content"][0]
        block["content"] = [{"type": "text", "text": block["content"]}]
        self.write(parts)
        self.assertIn("bd32o4p1o", self.ask())

    def test_a_run_recorded_in_print_mode_and_finished_is_silent(self):
        self.write(started_in_print_mode("bd32o4p1o"), notified("bd32o4p1o"))
        self.assertIsNone(self.ask())

    def test_a_finished_run_is_silent(self):
        self.write(started("bd32o4p1o"), notified("bd32o4p1o"))
        self.assertIsNone(self.ask())

    def test_a_failed_or_killed_run_has_also_reported(self):
        self.write(started("b1"), notified("b1", "failed"), started("b2"), notified("b2", "killed"))
        self.assertIsNone(self.ask())

    def test_a_run_the_agent_stopped_is_silent(self):
        self.write(started("bd32o4p1o"), task_stopped("bd32o4p1o"))
        self.assertIsNone(self.ask())

    def test_only_the_live_run_is_named(self):
        self.write(started("bdone", "toolu_1"), notified("bdone"), started("blive", "toolu_3"))
        message = self.ask()
        self.assertIn("blive", message)
        self.assertNotIn("bdone", message)

    def test_a_second_attempt_is_let_through(self):
        # stop_hook_active: a command meant to outlive the agent must not trap it in the hook.
        self.write(started("bd32o4p1o"))
        self.assertIsNone(self.ask(stop_hook_active=True))

    def test_the_agents_own_transcript_is_read_when_named_directly(self):
        self.write(started("bd32o4p1o"))
        other = os.path.join(self.root, "elsewhere.jsonl")
        self.assertIn("bd32o4p1o", self.ask(transcript_path=other, agent_transcript_path=self.agent_file))

    def test_the_main_session_is_never_held(self):
        self.write(started("bd32o4p1o"))
        self.assertIsNone(runs.reason({"transcript_path": self.parent, "agent_transcript_path": self.agent_file}))

    def test_a_missing_transcript_is_silent(self):
        self.assertIsNone(self.ask(agent_id="nobody"))
        self.assertIsNone(self.ask(transcript_path=""))

    def test_garbage_lines_are_stepped_over(self):
        with open(self.agent_file, "w") as f:
            f.write("not json at all\n")
            f.write('{"type": "user", "toolUseResult": "a string, not an object"}\n')
            f.write('{"type": "attachment", "attachment": null}\n')
            f.write('{"type": "assistant", "message": {"content": "not a list"}}\n')
            f.write(json.dumps(started("blive")) + "\n")
            f.write('{"toolUseResult": {"backgroundTaskId": 17}}\n')
        self.assertIn("blive", self.ask())

    def test_the_hook_prints_a_block_decision(self):
        self.write(started("bd32o4p1o"))
        payload = {"hook_event_name": "SubagentStop", "transcript_path": self.parent, "agent_id": "a1"}
        result = subprocess.run([sys.executable, HOOK], input=json.dumps(payload).encode("utf-8"), capture_output=True)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        self.assertIn("bd32o4p1o", json.loads(result.stdout)["reason"])

    def test_the_hook_says_nothing_on_an_unreadable_payload(self):
        result = subprocess.run([sys.executable, HOOK], input=b"{ not json", capture_output=True)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
