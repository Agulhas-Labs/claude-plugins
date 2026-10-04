"""Tests for the hooks: `gate.sh` in front, `board_hook.py` behind it, driven the way the host drives them.

Run: python3 -m unittest discover -s plugins/logbook/tests

Every test runs the real `gate.sh` with `sh`, feeding it a payload as the host sends one: compact JSON
on one line. The payloads start from the ones captured in `fixtures/payloads`. The environment is the
test's own: `CLAUDE_PROJECT_DIR` points at a temporary project, no `LOGBOOK_*` setting leaks in
from the machine, and every `GIT_*` variable is removed, because the board reads git and a git hook
running this suite exports `GIT_DIR` and `GIT_INDEX_FILE` for the repository being committed to.
"""
import copy
import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
HOOKS = os.path.join(PLUGIN, "hooks")
GATE = os.path.join(HOOKS, "gate.sh")
PAYLOADS = os.path.join(HERE, "fixtures", "payloads")
TRANSCRIPT = os.path.join(HERE, "fixtures", "transcript.jsonl")
sys.path.insert(0, os.path.join(PLUGIN, "board"))
sys.path.insert(0, HOOKS)

import board  # noqa: E402
import board_hook  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
FIRST_PROMPT = "Add CSV export to the reports page, with tests."
GATE_REPORT = "Done.\n\nG1 the suite passes — PASS\nG2 the latency target — FAIL, 240 ms\n"


def fixture(name):
    with open(os.path.join(PAYLOADS, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def compact(payload):
    return json.dumps(payload, separators=(",", ":"))


def task_create(number, subject=None):
    payload = fixture("PostToolUse-TaskCreate")
    subject = subject or f"step {number}"
    payload["tool_input"] = {"subject": subject, "description": f"Task {subject}"}
    payload["tool_response"] = {"task": {"id": str(number), "subject": subject}}
    payload["tool_use_id"] = f"toolu_create_{number}"
    return payload


def todo_write(count):
    """Written from the tool's documentation, not captured: `TodoWrite` was not observed on the version
    the other payloads came from. Its input carries the whole list each time."""
    payload = fixture("PostToolUse-TaskCreate")
    payload["tool_name"] = "TodoWrite"
    payload["tool_input"] = {"todos": [
        {"content": f"item {n}", "status": "completed" if n == 1 else "pending", "activeForm": f"Doing item {n}"}
        for n in range(1, count + 1)
    ]}
    payload["tool_response"] = {"oldTodos": [], "newTodos": payload["tool_input"]["todos"]}
    payload["tool_use_id"] = "toolu_todo"
    return payload


def transcript_lines(*creates):
    """A transcript's lines for more `TaskCreate` calls, in the shape of the captured one."""
    lines = []
    for number, subject in creates:
        use_id = f"toolu_create_{number}"
        lines.append({"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": use_id, "name": "TaskCreate", "input": {"subject": subject}},
        ]}})
        lines.append({"type": "user", "isSidechain": False, "toolUseResult": {"task": {"id": str(number), "subject": subject}},
                      "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": use_id, "content": f"Task #{number} created"},
                      ]}})
    return [json.dumps(line, separators=(",", ":")) for line in lines]


class Hooks(unittest.TestCase):
    """A temporary project, a transcript beside it, and the gate run in a controlled environment."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.project = os.path.join(self.tmp, "project")
        os.mkdir(self.project)
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        self.write_transcript([])
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "LOGBOOK_", "CLAUDE_"))
        }
        self.env.update(CLAUDE_PROJECT_DIR=self.project, CLAUDE_PLUGIN_ROOT=PLUGIN)
        self.folder = os.path.join(self.project, ".logbook", SESSION)

    def write_transcript(self, extra):
        with open(TRANSCRIPT, encoding="utf-8") as f:
            text = f.read()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(text + "".join(line + "\n" for line in extra))

    def write_prompt_only(self):
        """What the host has on disk before it flushes the earlier tool calls: the prompt, nothing else."""
        with open(TRANSCRIPT, encoding="utf-8") as f:
            first_line = f.readline()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(first_line)

    def hook(self, payload, env=None):
        """Run the gate on one payload (a dict, or raw text) and return its stdout, parsed when there is any."""
        if isinstance(payload, dict):
            payload = copy.deepcopy(payload)
            if "transcript_path" in payload:
                payload["transcript_path"] = self.transcript
            payload = compact(payload)
        done = subprocess.run(
            ["sh", GATE], input=payload.encode("utf-8"), env=env or self.env, capture_output=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b"")
        out = done.stdout.decode("utf-8").strip()
        return json.loads(out) if out else None

    def state(self):
        return board.derive(board.events(self.folder), [], board.settings({}))

    def kinds(self):
        return [event["kind"] for event in board.events(self.folder)]

    def start_with_subagent(self):
        output = self.hook(fixture("SubagentStart"))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, ".logbook")))
        return output

    def created_anything(self):
        found = []
        for root, dirs, files in os.walk(self.tmp):
            found.extend(os.path.join(root, name) for name in dirs + files)
        return sorted(p for p in found if p not in (self.project, self.transcript))


class Starting(Hooks):

    def test_a_two_step_session_creates_no_board_and_never_starts_python(self):
        # A `python3` first on the PATH that notes each start, then runs the real one: the gate must
        # answer all three hooks by itself.
        bin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bin_dir.cleanup)
        started = os.path.join(bin_dir.name, "python-started")
        with open(os.path.join(bin_dir.name, "python3"), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho started >> "{started}"\nexec "{sys.executable}" "$@"\n')
        os.chmod(os.path.join(bin_dir.name, "python3"), 0o755)
        env = dict(self.env, PATH=bin_dir.name + os.pathsep + self.env.get("PATH", ""))

        self.assertIsNone(self.hook(task_create(1), env))
        self.assertIsNone(self.hook(task_create(2), env))
        self.assertIsNone(self.hook(fixture("Stop"), env))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".logbook")))
        self.assertEqual(self.created_anything(), [])
        self.assertFalse(os.path.exists(started), "the gate started Python for a session with no board")

        # The same PATH does reach Python once a board can start, so the check above can see a start.
        self.hook(task_create(5), env)
        self.assertTrue(os.path.exists(started))

    def test_the_fifth_step_starts_a_board_with_the_first_four_read_from_the_transcript(self):
        # The captured transcript holds steps 1-3 and three status changes; step 4 is added in its shape,
        # with lines the reader must step over around it.
        self.write_transcript(
            ["not json at all", '{"type":"summary","summary":"x"}', "[1,2]"]
            + transcript_lines((4, "delta"))
            + ['{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"unknown"}]}}']
        )
        for number in range(1, 5):
            self.assertIsNone(self.hook(task_create(number)))
        self.assertFalse(os.path.exists(self.folder))

        output = self.hook(task_create(5, "epsilon"))

        page = os.path.join(self.folder, "board.html")
        self.assertNotIn("systemMessage", output)
        self.assertEqual(list(output), ["hookSpecificOutput"])
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(page, context)
        self.assertIn(f'python3 "{os.path.join(PLUGIN, "board", "board.py")}" <command> --board "{self.folder}"', context)
        self.assertTrue(os.path.isfile(page))
        state = self.state()
        self.assertEqual(state["title"], FIRST_PROMPT)
        self.assertEqual(
            [(s["id"], s["subject"], s["status"]) for s in state["steps"]],
            [("1", "alpha", "completed"), ("2", "beta", "completed"), ("3", "gamma", "pending"),
             ("4", "delta", "pending"), ("5", "epsilon", "pending")],
        )

    def test_the_handler_keeps_the_threshold_itself_behind_the_gate(self):
        now = board_hook.datetime.now(board_hook.timezone.utc)
        env = {"CLAUDE_PROJECT_DIR": self.project}
        for number in range(1, 5):
            self.assertIsNone(board_hook.handle(task_create(number), env, now))
        self.assertIsNone(board_hook.handle(todo_write(4), env, now))
        self.assertIsNone(board_hook.handle(fixture("Stop"), env, now))
        self.assertEqual(self.created_anything(), [])

    def test_the_fifth_step_already_in_the_transcript_is_recorded_once(self):
        self.write_transcript(transcript_lines((4, "delta"), (5, "epsilon")))
        self.hook(task_create(5, "epsilon"))
        steps = [e["id"] for e in board.events(self.folder) if e["kind"] == "step"]
        self.assertEqual(sorted(steps), ["1", "2", "3", "4", "5"])

    def test_the_threshold_is_a_setting_and_anything_but_a_positive_integer_means_five(self):
        for setting, first in (("3", 3), ("12", 12), ("0", 5), ("-2", 5), ("three", 5), ("", 5), (" 3", 5)):
            with self.subTest(setting=setting):
                env = dict(self.env, LOGBOOK_STEPS=setting)
                self.assertIsNone(self.hook(task_create(first - 1), env))
                self.assertFalse(os.path.exists(self.folder))
                output = self.hook(task_create(first), env)
                self.assertTrue(board.is_board(self.folder), "the board did not start at the threshold")
                self.assertNotIn("systemMessage", output)
                os.rename(os.path.join(self.project, ".logbook"), os.path.join(self.tmp, "done-" + setting))

    def test_a_subagent_starts_a_board_and_the_next_main_session_call_tells_the_model_once(self):
        output = self.start_with_subagent()
        self.assertIsNone(output, "a subagent start carries no context and prints nothing")
        self.assertTrue(board.is_board(self.folder))
        self.assertFalse(os.path.exists(os.path.join(self.folder, "announced")))
        self.assertEqual(self.state()["agents"][0]["type"], "general-purpose")

        # A call from inside the subagent cannot carry it: its context is the subagent's.
        inside = dict(fixture("PostToolUse-TaskUpdate"), agent_id="ac91f2c94fb8193d6")
        self.assertIsNone(self.hook(inside))

        delivered = self.hook(fixture("PostToolUse-Agent"))
        self.assertNotIn("systemMessage", delivered)
        self.assertEqual(delivered["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(self.folder, delivered["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(fixture("PostToolUse-TaskUpdate")))
        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))

    def test_the_context_is_delivered_on_a_prompt_too(self):
        self.start_with_subagent()
        delivered = self.hook(fixture("UserPromptSubmit"))
        self.assertEqual(delivered["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_without_a_transcript_the_title_is_untitled(self):
        payload = fixture("SubagentStart")
        payload["transcript_path"] = os.path.join(self.tmp, "missing.jsonl")
        self.hook(compact(payload))
        self.assertEqual(self.state()["title"], "Untitled task")

    def test_a_long_first_prompt_is_cut_at_a_word_with_an_ellipsis(self):
        words = "Rebuild the export pipeline so that every report can be downloaded as CSV and also as JSON files"
        self.assertEqual(board_hook.title_from(words), "Rebuild the export pipeline so that every report can be downloaded as CSV and…")
        self.assertLessEqual(len(board_hook.title_from(words)), 80)
        self.assertEqual(board_hook.title_from("x" * 100), "x" * 79 + "…")
        self.assertEqual(board_hook.title_from("  two\n lines "), "two lines")
        self.assertEqual(board_hook.title_from(None), "Untitled task")

    def test_without_a_project_setting_the_payload_cwd_is_the_project(self):
        env = {key: value for key, value in self.env.items() if key != "CLAUDE_PROJECT_DIR"}
        payload = fixture("SubagentStart")
        payload["cwd"] = self.project
        self.hook(payload, env)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, ".logbook")))

    def test_a_task_list_call_from_inside_a_subagent_is_ignored(self):
        inside = dict(task_create(5), agent_id="ac91f2c94fb8193d6")
        self.assertIsNone(self.hook(inside))
        self.assertFalse(os.path.exists(self.folder))

        self.start_with_subagent()
        before = self.kinds()
        self.hook(dict(task_create(6), agent_id="ac91f2c94fb8193d6"))
        self.hook(dict(fixture("PostToolUse-TaskUpdate"), agent_id="ac91f2c94fb8193d6"))
        self.hook(dict(todo_write(5), agent_id="ac91f2c94fb8193d6"))
        self.hook(dict(fixture("PostToolUse-Agent"), agent_id="ac91f2c94fb8193d6"))
        self.assertEqual(self.kinds(), before)


class CatchingUp(Hooks):
    """The host writes the earlier tool calls to the transcript late; the handler reads it again."""

    def test_late_calls_written_to_the_transcript_are_caught_up(self):
        # As measured: the transcript on disk holds only the prompt when the fifth `TaskCreate` arrives.
        self.write_prompt_only()
        for number in range(1, 5):
            self.assertIsNone(self.hook(task_create(number)))
        self.hook(task_create(5, "epsilon"))
        self.assertEqual([s["id"] for s in self.state()["steps"]], ["5"])

        # The host flushes the earlier calls late, and the sixth `TaskCreate` arrives.
        self.write_transcript(transcript_lines((4, "delta")))
        self.hook(task_create(6, "zeta"))

        steps = self.state()["steps"]
        self.assertEqual(
            [(s["id"], s["subject"]) for s in steps],
            [("1", "alpha"), ("2", "beta"), ("3", "gamma"), ("4", "delta"), ("5", "epsilon"), ("6", "zeta")],
        )
        self.assertEqual([e["kind"] for e in board.events(self.folder)].count("step"), 6)

    def test_a_status_recorded_before_catch_up_is_kept(self):
        self.write_prompt_only()
        self.start_with_subagent()
        update = fixture("PostToolUse-TaskUpdate")
        update["tool_input"] = {"taskId": "1", "status": "completed"}
        self.hook(update)
        step = next(s for s in self.state()["steps"] if s["id"] == "1")
        self.assertEqual((step["subject"], step["status"]), ("", "completed"))

        self.write_transcript(transcript_lines((1, "alpha")))
        self.hook(fixture("Stop"))
        step = next(s for s in self.state()["steps"] if s["id"] == "1")
        self.assertEqual((step["subject"], step["status"]), ("alpha", "completed"))

    def test_catch_up_gives_up_after_five_reads(self):
        self.write_prompt_only()
        for number in range(1, 5):
            self.assertIsNone(self.hook(task_create(number)))
        self.hook(task_create(5, "epsilon"))
        catch_up_file = os.path.join(self.folder, "catch-up")

        for _ in range(5):
            self.hook(fixture("Stop"))
        with open(catch_up_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "5")

        self.hook(fixture("Stop"))
        with open(catch_up_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "5")
        self.assertEqual([s["id"] for s in self.state()["steps"]], ["5"])


class Recording(Hooks):

    def setUp(self):
        super().setUp()
        self.start_with_subagent()

    def test_task_update_changes_a_status_and_a_subject(self):
        self.hook(task_create(7, "write the parser"))
        update = fixture("PostToolUse-TaskUpdate")
        update["tool_input"] = {"taskId": "7", "status": "in_progress"}
        self.hook(update)
        step = next(s for s in self.state()["steps"] if s["id"] == "7")
        self.assertEqual((step["subject"], step["status"]), ("write the parser", "in_progress"))

        update["tool_input"] = {"taskId": "7", "subject": "write the streaming parser", "status": "completed"}
        self.hook(update)
        step = next(s for s in self.state()["steps"] if s["id"] == "7")
        self.assertEqual((step["subject"], step["status"]), ("write the streaming parser", "completed"))

        update["tool_input"] = {"taskId": "7", "status": "deleted"}
        self.hook(update)
        self.assertNotIn("7", [s["id"] for s in self.state()["steps"]])

    def test_agent_fills_model_and_description(self):
        self.hook(fixture("PostToolUse-Agent"))
        agent = self.state()["agents"][0]
        self.assertEqual(
            (agent["id"], agent["model"], agent["description"]),
            ("ac91f2c94fb8193d6", "claude-haiku-4-5-20251001", "Simple reply task"),
        )

    def test_an_agent_response_without_those_fields_records_nothing(self):
        before = self.kinds()
        payload = fixture("PostToolUse-Agent")
        payload["tool_response"] = [{"type": "text", "text": "done"}]
        self.hook(payload)
        payload["tool_response"] = {"status": "completed", "agentId": "ac91f2c94fb8193d6"}
        payload["tool_input"] = {}
        self.hook(payload)
        self.assertEqual(self.kinds(), before)

    def test_subagent_stop_with_gate_lines_gives_gates_and_an_outcome(self):
        stop = fixture("SubagentStop")
        stop["last_assistant_message"] = GATE_REPORT
        self.hook(stop)
        agent = self.state()["agents"][0]
        self.assertEqual(agent["outcome"], "failed")
        self.assertEqual([(g["id"], g["result"]) for g in agent["gates"]], [("G1", "pass"), ("G2", "fail")])
        self.assertIsNotNone(agent["ended"])

    def test_a_subagent_report_is_capped(self):
        stop = fixture("SubagentStop")
        stop["last_assistant_message"] = "G1 passes\n" + "x" * 30000
        self.hook(stop)
        message = next(e for e in board.events(self.folder) if e["kind"] == "agent-stop")["message"]
        self.assertEqual(len(message), 20000)

    def test_stop_then_a_prompt_gives_idle_then_live(self):
        self.hook(fixture("Stop"))
        self.assertEqual(self.state()["state"], "idle")
        self.hook(fixture("UserPromptSubmit"))
        self.assertEqual(self.state()["state"], "live")

    def test_session_end_writes_the_report_and_a_second_session_end_records_nothing(self):
        # Any other event from the session reopens a board its end closed (test_lifecycle.py).
        self.hook(fixture("SessionEnd"))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "report.html")))
        self.assertEqual(self.state()["state"], "finished")
        logged = self.kinds()
        self.hook(fixture("SessionEnd"))
        self.assertEqual(self.kinds(), logged)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "announced")))

    def test_todo_write_replaces_the_steps(self):
        self.hook(todo_write(3))
        self.assertEqual(
            [(s["subject"], s["status"]) for s in self.state()["steps"]],
            [("item 1", "completed"), ("item 2", "pending"), ("item 3", "pending")],
        )


class TodoWriteStarts(Hooks):

    def test_todo_write_starts_a_board_at_the_threshold(self):
        # The TodoWrite payload is written from the tool's documentation, not captured (see todo_write).
        self.assertIsNone(self.hook(todo_write(4)))
        self.assertFalse(os.path.exists(self.folder))
        output = self.hook(todo_write(5))
        self.assertTrue(board.is_board(self.folder))
        self.assertNotIn("systemMessage", output)
        self.assertEqual(len(self.state()["steps"]), 5)


class NeverInTheWay(Hooks):

    def assert_silent_and_nothing_made(self, text):
        self.assertIsNone(self.hook(text))
        self.assertEqual(self.created_anything(), [])

    def test_a_payload_that_is_not_json_exits_silently(self):
        self.assert_silent_and_nothing_made("this is not json")
        self.assert_silent_and_nothing_made("")
        # Enough for the gate to hand it on, but not JSON for the handler.
        self.assert_silent_and_nothing_made(compact(task_create(5))[:-40])

    def test_a_payload_without_a_session_id_exits_silently(self):
        payload = task_create(5)
        del payload["session_id"]
        self.assert_silent_and_nothing_made(compact(payload))
        subagent = fixture("SubagentStart")
        del subagent["session_id"]
        self.assert_silent_and_nothing_made(compact(subagent))

    def test_a_session_id_with_a_path_separator_creates_nothing(self):
        for session in ("../escape", "a/b", "..", "a\\\\b", "a b"):
            with self.subTest(session=session):
                for payload in (task_create(5), fixture("SubagentStart")):
                    payload["session_id"] = session
                    self.assertIsNone(self.hook(payload))
                self.assertEqual(self.created_anything(), [])
        # The handler refuses one too, if a payload ever reaches it past the gate.
        payload = dict(fixture("SubagentStart"), session_id="../escape")
        with self.assertRaises(ValueError):
            board_hook.handle(payload, {"CLAUDE_PROJECT_DIR": self.project}, board_hook.datetime.now(board_hook.timezone.utc))
        self.assertEqual(self.created_anything(), [])


class Manifest(unittest.TestCase):

    def test_hooks_json_parses_and_names_only_files_that_exist(self):
        with open(os.path.join(HOOKS, "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        self.assertEqual(
            set(hooks),
            {"PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop", "UserPromptSubmit", "Stop",
             "SessionStart", "SessionEnd"},
        )
        self.assertEqual(
            hooks["PostToolUse"][0]["matcher"], "TaskCreate|TaskUpdate|TodoWrite|Agent|Task|Write|Edit|MultiEdit|NotebookEdit|Bash",
        )
        self.assertEqual(hooks["PostToolUseFailure"][0]["matcher"], "Write|Edit|MultiEdit|NotebookEdit|Bash")
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertEqual(hook["type"], "command")
                        self.assertEqual(hook["command"], 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/gate.sh"')
                        named = hook["command"].split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN)
                        self.assertTrue(os.path.isfile(named), named)
        for name in ("run-python.sh", "board_hook.py"):
            self.assertTrue(os.path.isfile(os.path.join(HOOKS, name)), name)

    def test_run_python_is_the_same_launcher_the_other_plugins_use(self):
        with open(os.path.join(HOOKS, "run-python.sh"), "rb") as f:
            ours = f.read()
        others = glob.glob(os.path.join(PLUGIN, "..", "*", "hooks", "run-python.sh"))
        others = [path for path in others if os.path.realpath(path) != os.path.realpath(os.path.join(HOOKS, "run-python.sh"))]
        self.assertTrue(others)
        for path in others:
            with open(path, "rb") as f:
                self.assertEqual(f.read(), ours, path)


if __name__ == "__main__":
    unittest.main()
