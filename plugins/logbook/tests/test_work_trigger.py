"""Tests for the work-call trigger: a board starts at the session's `LOGBOOK_CALLS`th work call.

Run: python3 -m unittest discover -s plugins/logbook/tests

A work call is a main-session `Write`, `Edit`, `MultiEdit`, `NotebookEdit` or `Bash`, arriving as
`PostToolUse` or `PostToolUseFailure`. `gate.sh` alone counts them, one byte a call in
`$CLAUDE_PLUGIN_DATA/calls/<session>`, and hands over to the handler at the threshold. Every test runs
the real `gate.sh` through `Hooks` from `test_hooks.py` (a temporary project, `GIT_*`,
`LOGBOOK_*` and `CLAUDE_*` stripped from the environment first), with `CLAUDE_PLUGIN_DATA`
pointing at a folder inside the test's temporary directory that the gate creates when it first counts.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_hooks import HERE, HOOKS, SESSION, Hooks, compact, fixture

import board  # noqa: E402  (test_hooks puts the board folder on the path)

HANDLER = os.path.join(HOOKS, "board_hook.py")
WORK_TRANSCRIPT = os.path.join(HERE, "fixtures", "transcript-work.jsonl")
OTHER_SESSION = "99999999-8888-4777-8666-555555555555"
WORK_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")


def work_call(tool="Bash", session=SESSION, event="PostToolUse"):
    """A work-call payload for `tool`: a captured one, or built from the captured `Edit` for the two
    file tools not captured."""
    if event == "PostToolUseFailure":
        payload = fixture("PostToolUseFailure-Bash")
        payload["tool_name"] = tool
    elif tool in ("Bash", "Write", "Edit"):
        payload = fixture("PostToolUse-" + tool)
    else:
        payload = fixture("PostToolUse-Edit")
        payload["tool_name"] = tool
        if tool == "NotebookEdit":
            payload["tool_input"] = {"notebook_path": "/home/user/project/notes.ipynb", "new_source": "x"}
    payload["session_id"] = session
    return payload


class WorkCalls(Hooks):
    """`Hooks` with a plugin data folder, and ways to read the gate's count."""

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, "data")
        self.env["CLAUDE_PLUGIN_DATA"] = self.data

    def counter(self, session=SESSION):
        return os.path.join(self.data, "calls", session)

    def counted(self, session=SESSION):
        """The count the gate keeps for a session: its file's length, or None when there is no file."""
        path = self.counter(session)
        return os.path.getsize(path) if os.path.exists(path) else None

    def calls(self, count, env=None, session=SESSION, tool="Bash"):
        """Send `count` work calls, each of which must print nothing."""
        for _ in range(count):
            self.assertIsNone(self.hook(work_call(tool, session), env))

    def with_calls(self, value):
        return dict(self.env, LOGBOOK_CALLS=value)

    def python_shim(self):
        """(env, file) where `env` puts first on the PATH a `python3` that notes each start in `file`,
        then runs the real one: the technique `test_hooks.py` uses to prove the gate answered alone."""
        bin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bin_dir.cleanup)
        started = os.path.join(bin_dir.name, "python-started")
        with open(os.path.join(bin_dir.name, "python3"), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho started >> "{started}"\nexec "{sys.executable}" "$@"\n')
        os.chmod(os.path.join(bin_dir.name, "python3"), 0o755)
        return dict(self.env, PATH=bin_dir.name + os.pathsep + self.env.get("PATH", "")), started

    def boards(self):
        return os.path.join(self.project, ".logbook")

    def assert_started(self, output, folder=None):
        folder = folder or self.folder
        self.assertTrue(board.is_board(folder), "no board on disk: the call did not publish one")
        self.assertNotIn("systemMessage", output or {}, "the hook printed to the terminal")


class ShortSessions(WorkCalls):

    def test_a_short_session_creates_nothing_and_never_starts_python(self):
        env, started = self.python_shim()
        tools = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Edit", "Write", "Bash"]
        for number, tool in enumerate(tools, 1):
            self.assertIsNone(self.hook(work_call(tool), env), f"call {number} printed something")
            self.assertEqual(self.counted(), number)
        self.assertFalse(os.path.exists(self.boards()))
        self.assertFalse(os.path.exists(started), "the gate started Python before the tenth work call")
        data_calls = os.path.join(self.data, "calls")
        self.assertEqual(self.created_anything(), [self.data, data_calls, self.counter()])

        # The same PATH does reach Python at the tenth call, so the check above can see a start.
        self.assert_started(self.hook(work_call("Bash"), env))
        self.assertTrue(os.path.exists(started))


class TheThreshold(WorkCalls):

    def test_the_tenth_work_call_starts_the_board_and_removes_the_count(self):
        self.calls(9)
        self.assertEqual(self.counted(), 9)
        output = self.hook(work_call("Edit"))
        self.assert_started(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(os.path.join(self.folder, "board.html"), output["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.counted())

    def test_a_failed_call_at_the_threshold_starts_with_a_message_and_the_next_call_gives_the_context_once(self):
        self.calls(9)
        output = self.hook(work_call("Bash", event="PostToolUseFailure"))
        self.assert_started(output)
        self.assertIsNone(output, "a failed call carries no context and the hook prints nothing")
        self.assertIsNone(self.counted())

        after = self.hook(work_call("Bash"))
        self.assertEqual(list(after), ["hookSpecificOutput"])
        self.assertEqual(after["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(os.path.join(self.folder, "board.html"), after["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(work_call("Bash")))
        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        self.assertIsNone(self.counted())

    def test_logbook_calls_of_three_starts_at_the_third_call(self):
        env = self.with_calls("3")
        self.calls(2, env)
        self.assertFalse(os.path.exists(self.boards()))
        self.assert_started(self.hook(work_call("Bash"), env))

    def test_a_setting_that_is_not_a_positive_integer_means_ten(self):
        for number, value in enumerate(("0", "-1", "abc", "")):
            with self.subTest(value=value):
                session = f"11111111-2222-4333-8444-00000000000{number}"
                env = self.with_calls(value)
                self.calls(9, env, session)
                folder = os.path.join(self.boards(), session)
                self.assertFalse(os.path.exists(folder))
                self.assertEqual(self.counted(session), 9)
                self.assert_started(self.hook(work_call("Bash", session), env), folder)


class NoDataFolder(WorkCalls):

    def test_without_a_plugin_data_folder_twenty_work_calls_start_nothing_and_create_nothing(self):
        unset = {key: value for key, value in self.env.items() if key != "CLAUDE_PLUGIN_DATA"}
        for name, env in (("unset", unset), ("empty", dict(self.env, CLAUDE_PLUGIN_DATA=""))):
            with self.subTest(name):
                for number in range(20):
                    self.assertIsNone(self.hook(work_call(WORK_TOOLS[number % len(WORK_TOOLS)]), env))
                self.assertEqual(self.created_anything(), [])


class WhatCounts(WorkCalls):

    def test_each_work_tool_counts_as_a_success_and_as_a_failure(self):
        made, env = 0, self.with_calls("100")
        for event in ("PostToolUse", "PostToolUseFailure"):
            for tool in WORK_TOOLS:
                with self.subTest(event=event, tool=tool):
                    self.assertIsNone(self.hook(work_call(tool, event=event), env))
                    made += 1
                    self.assertEqual(self.counted(), made)

    def test_read_grep_and_task_update_do_not_count(self):
        for tool in ("Read", "Grep", "TaskUpdate"):
            with self.subTest(tool=tool):
                payload = fixture("PostToolUse-Bash")
                payload["tool_name"] = tool
                for _ in range(12):
                    self.assertIsNone(self.hook(payload))
                self.assertIsNone(self.counted())
        self.assertFalse(os.path.exists(self.boards()))

    def test_a_call_inside_a_subagent_is_not_counted_and_starts_nothing_at_the_threshold(self):
        self.calls(9)
        inside = work_call("Bash")
        inside["agent_id"] = "a1b2c3d4e5f607182"
        for _ in range(3):
            self.assertIsNone(self.hook(inside))
        self.assertEqual(self.counted(), 9)
        self.assertFalse(os.path.exists(self.boards()))
        self.assert_started(self.hook(work_call("Bash")))

    def test_a_subagents_call_as_the_host_sends_it_is_not_counted(self):
        # Captured payloads: the host writes `agent_id` ahead of the event's name, inside what the
        # gate reads, however large the call's input and response are.
        self.calls(9)
        for name in ("PostToolUse-Bash-subagent", "PostToolUse-Write-subagent"):
            payload = fixture(name)
            self.assertLess(compact(payload).index('"agent_id"'), compact(payload).index('"hook_event_name"'))
            self.assertIsNone(self.hook(payload))
        self.assertEqual(self.counted(), 9)
        self.assertFalse(os.path.exists(self.boards()))

    def test_a_subagents_call_as_the_host_sends_it_is_recorded_with_its_agent_on_a_board(self):
        self.calls(9)
        self.assert_started(self.hook(work_call("Bash")))
        self.assertIsNone(self.hook(fixture("PostToolUse-Write-subagent")))
        made = [e for e in board.events(self.folder) if e["kind"] == "change" and e.get("agent")]
        self.assertEqual([(e["path"], e["agent"]) for e in made], [("/home/user/project/from-subagent.txt", "aa8a3f0e94aaf9080")])

    def test_a_session_that_has_a_board_is_not_counted(self):
        self.start_with_subagent()
        for tool in WORK_TOOLS:
            self.hook(work_call(tool))
        self.assertIsNone(self.counted())
        self.assertFalse(os.path.exists(os.path.join(self.data, "calls")))
        self.assertIn("command", self.kinds())

    def test_a_cwd_with_a_backslash_is_counted_before_the_handler_decides(self):
        project = os.path.join(self.tmp, "back\\slash")
        os.mkdir(project)
        env = {key: value for key, value in self.with_calls("3").items() if key != "CLAUDE_PROJECT_DIR"}
        payload = work_call("Bash")
        payload["cwd"] = project
        self.assertIn("\\\\", compact(payload))
        for number in (1, 2):
            self.assertIsNone(self.hook(payload, env))
            self.assertEqual(self.counted(), number)
        self.assertFalse(os.path.exists(os.path.join(project, ".logbook")))
        self.assert_started(self.hook(payload, env), os.path.join(project, ".logbook", SESSION))
        self.assertIsNone(self.counted())

    def test_two_sessions_in_one_project_count_separately(self):
        env = self.with_calls("4")
        self.calls(3, env)
        self.calls(2, env, OTHER_SESSION, "Edit")
        self.assertEqual((self.counted(), self.counted(OTHER_SESSION)), (3, 2))
        self.assert_started(self.hook(work_call("Write"), env))
        self.assertIsNone(self.counted())
        self.assertEqual(self.counted(OTHER_SESSION), 2)
        self.assertFalse(os.path.exists(os.path.join(self.boards(), OTHER_SESSION)))


class SessionEnd(WorkCalls):

    def test_session_end_removes_the_count_and_nothing_else(self):
        self.calls(3)
        self.calls(2, session=OTHER_SESSION)
        other = os.path.join(self.data, "other-file")
        with open(other, "w", encoding="utf-8") as f:
            f.write("kept")
        before = self.created_anything()
        self.assertIsNone(self.hook(fixture("SessionEnd")))
        self.assertEqual(self.created_anything(), [p for p in before if p != self.counter()])
        self.assertEqual(self.counted(OTHER_SESSION), 2)

    def test_session_end_without_a_count_does_nothing_and_prints_nothing(self):
        self.assertIsNone(self.hook(fixture("SessionEnd")))
        self.assertEqual(self.created_anything(), [])
        self.calls(1, session=OTHER_SESSION)
        before = self.created_anything()
        self.assertIsNone(self.hook(fixture("SessionEnd")))
        self.assertEqual(self.created_anything(), before)


class AStartThatCannotHappen(WorkCalls):
    """The gate starts the handler while the count is at the threshold, so a start that fails must
    take the count with it: the next try is a threshold of calls later, not the next call."""

    def starts(self, started):
        if not os.path.exists(started):
            return 0
        with open(started, encoding="utf-8") as f:
            return len(f.read().splitlines())

    def tried_once(self, env, started):
        env = dict(env, LOGBOOK_CALLS="3")
        for _ in range(3):
            self.assertIsNone(self.hook(work_call(), env))
        tried = self.starts(started)
        self.assertGreater(tried, 0, "the third call did not reach the handler")
        self.assertIsNone(self.counted(), "the count outlived the start that failed")
        for _ in range(2):
            self.assertIsNone(self.hook(work_call(), env))
        self.assertEqual(self.starts(started), tried, "a call under the threshold started Python")
        self.assertEqual(self.counted(), 2)

    def test_a_boards_folder_that_is_a_file_is_tried_once_a_threshold_not_on_every_call(self):
        with open(self.boards(), "w", encoding="utf-8") as f:
            f.write("not a folder\n")
        self.tried_once(*self.python_shim())
        self.assertTrue(os.path.isfile(self.boards()))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes to a read-only folder")
    def test_a_project_that_cannot_be_written_to_is_tried_once_a_threshold_not_on_every_call(self):
        os.chmod(self.project, 0o555)
        self.addCleanup(os.chmod, self.project, 0o755)
        self.tried_once(*self.python_shim())
        self.assertFalse(os.path.exists(self.boards()))


class ALinkedCountFolder(WorkCalls):
    """A folder of counts that is a symbolic link is never used, by the gate or by the handler."""

    def setUp(self):
        super().setUp()
        self.elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(self.elsewhere)
        os.mkdir(self.data)
        os.symlink(self.elsewhere, os.path.join(self.data, "calls"))

    def test_nothing_is_counted_through_it_and_no_board_starts(self):
        self.calls(12)
        self.assertEqual(os.listdir(self.elsewhere), [])
        self.assertFalse(os.path.exists(self.boards()))

    def test_session_end_removes_nothing_through_it(self):
        kept = os.path.join(self.elsewhere, SESSION)
        with open(kept, "w", encoding="utf-8") as f:
            f.write("xxx")
        payload = fixture("SessionEnd")
        payload["session_id"] = SESSION
        self.assertIsNone(self.hook(payload))
        self.assertTrue(os.path.isfile(kept))

    def test_the_handler_neither_reads_nor_removes_a_count_through_it(self):
        kept = os.path.join(self.elsewhere, SESSION)
        with open(kept, "w", encoding="utf-8") as f:
            f.write("x" * 20)
        self.assertIsNone(board.calls_file(self.env, SESSION))
        self.assertEqual(board.calls_made(self.env, SESSION), 0)
        self.assertTrue(os.path.isfile(kept))


class TheCountsName(unittest.TestCase):

    def test_a_session_id_that_is_not_one_names_no_count_file(self):
        nowhere = os.path.join(tempfile.gettempdir(), "nowhere")
        env = {"CLAUDE_PLUGIN_DATA": nowhere}
        for session in ("../escape", "a/b", "..", ".", "", "a b", "x\n"):
            self.assertIsNone(board.calls_file(env, session), repr(session))
        self.assertEqual(board.calls_file(env, SESSION), os.path.join(os.path.realpath(nowhere), "calls", SESSION))


class CountsLeftBehind(WorkCalls):

    def test_a_board_that_starts_takes_the_old_counts_of_sessions_that_never_ended(self):
        self.calls(9)
        old, fresh = self.counter(OTHER_SESSION), self.counter("77777777-6666-4555-8444-333333333333")
        for path in (old, fresh):
            with open(path, "w", encoding="utf-8") as f:
                f.write("xxx")
        long_ago = os.path.getmtime(old) - 30 * 24 * 60 * 60
        os.utime(old, (long_ago, long_ago))
        self.assert_started(self.hook(work_call()))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(fresh))


class TheHandler(WorkCalls):

    def run_handler(self, payload):
        done = subprocess.run(
            [sys.executable, HANDLER], input=compact(payload).encode("utf-8"), env=self.env,
            capture_output=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_the_handler_never_adds_to_the_count(self):
        self.calls(3)
        with open(self.counter(), "rb") as f:
            before = f.read()
        for tool in WORK_TOOLS:
            self.assertEqual(self.run_handler(work_call(tool)), b"")
        with open(self.counter(), "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.boards()))

        # Nor does it make a count for a session that has none.
        self.assertEqual(self.run_handler(work_call("Bash", OTHER_SESSION)), b"")
        self.assertIsNone(self.counted(OTHER_SESSION))


class EarlyCalls(WorkCalls):

    def test_the_calls_before_the_start_are_on_the_board_and_the_starting_call_once(self):
        shutil.copyfile(WORK_TRANSCRIPT, self.transcript)
        env = self.with_calls("5")
        for payload in (work_call("Write"), work_call("Edit"), work_call("Bash"),
                        work_call("Bash", event="PostToolUseFailure")):
            self.assertIsNone(self.hook(payload, env))
        self.assertFalse(os.path.exists(self.boards()))
        starting = fixture("PostToolUse-Bash-2")

        self.assert_started(self.hook(starting, env))

        state = board.read_state(self.folder)
        changes = {os.path.basename(row["path"]): row for row in state["changes"]}
        self.assertEqual(sorted(changes), ["hello.txt", "notes.txt"])
        self.assertEqual(changes["hello.txt"]["edits"], 1)
        self.assertTrue(changes["hello.txt"]["created"])
        self.assertEqual(changes["notes.txt"]["edits"], 1)
        commands = {row["command"]: row for row in state["commands"]}
        self.assertEqual(
            {command: (row["runs"], row["result"]) for command, row in commands.items()},
            {
                "echo ok && true": (1, "pass"),
                'sh -c "echo bad >&2; exit 3"': (1, "fail"),
                starting["tool_input"]["command"]: (1, "pass"),
            },
        )
        uses = [event.get("use") for event in board.events(self.folder)]
        self.assertEqual(uses.count(starting["tool_use_id"]), 1)


if __name__ == "__main__":
    unittest.main()
