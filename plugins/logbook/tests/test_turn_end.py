"""Tests for the end of a turn: a turn that changed something ends with a board, and a turn that
recorded something on an open board ends by showing its page.

Run: python3 -m unittest discover -s plugins/logbook/tests

`gate.sh` writes one byte per work call to `$CLAUDE_PLUGIN_DATA/calls/<session>`: `c` for a call that
changed something (a file tool's `PostToolUse`, or a `Bash` `PostToolUse` whose command runs
`git commit`) and `x` for any other. `Stop` starts a board when that file holds a `c`. Every test runs
the real `gate.sh` through `WorkCalls` from `test_work_trigger.py`, whose `python_shim` proves whether
the gate started Python at all.
"""
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone

from test_hooks import PLUGIN, SESSION, compact, fixture
from test_work_trigger import OTHER_SESSION, WORK_TRANSCRIPT, WorkCalls, work_call

import board  # noqa: E402  (test_hooks puts the board folder on the path)
import board_hook  # noqa: E402  (and the hooks folder)

FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def bash(command, event="PostToolUse", use="toolu_turn_end"):
    payload = work_call("Bash", event=event)
    payload["tool_input"] = {"command": command, "description": "A command"}
    payload["tool_use_id"] = use
    return payload


def stop(**changes):
    payload = fixture("Stop")
    payload.update(changes)
    return payload


class TurnEnd(WorkCalls):

    def count_bytes(self):
        with open(self.counter(), "rb") as f:
            return f.read()

    def byte_of(self, payload):
        """The byte the gate writes for one work call, counted on its own."""
        if os.path.exists(self.counter()):
            os.unlink(self.counter())
        self.assertIsNone(self.hook(payload, self.with_calls("100")))
        return self.count_bytes()

    def work_transcript(self, lines):
        """The first `lines` lines of the captured work transcript as this session's transcript."""
        with open(WORK_TRANSCRIPT, encoding="utf-8") as f:
            kept = f.readlines()[:lines]
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.writelines(kept)

    def started_python(self, started):
        return os.path.exists(started)


class ATurnThatChangedSomething(TurnEnd):

    def test_three_calls_with_an_edit_end_with_a_board_that_holds_them(self):
        # The transcript's first nine lines are the prompt, a `Write`, a `Read`, an `Edit` and a `Bash`.
        self.work_transcript(9)
        for payload in (work_call("Write"), work_call("Edit"), work_call("Bash")):
            self.assertIsNone(self.hook(payload))
        self.assertEqual(self.counted(), 3)
        self.assertFalse(os.path.exists(self.boards()))

        output = self.hook(stop())

        self.assert_started(output)
        self.assertIsNone(output, "a Stop carries no context and the hook prints nothing")
        state = board.read_state(self.folder)
        self.assertEqual(sorted(os.path.basename(row["path"]) for row in state["changes"]), ["hello.txt", "notes.txt"])
        self.assertEqual([row["command"] for row in state["commands"]], ["echo ok && true"])
        self.assertEqual(self.kinds()[-1], "turn-end")
        self.assertIsNone(self.counted())

    def test_a_stop_hook_active_stop_behaves_the_same(self):
        self.assertIsNone(self.hook(work_call("Edit")))
        self.assert_started(self.hook(stop(stop_hook_active=True)))
        self.assertIsNone(self.counted())

    def test_a_git_commit_alone_ends_with_a_board(self):
        self.assertIsNone(self.hook(bash("git commit -m x")))
        self.assert_started(self.hook(stop()))

    def test_the_next_prompt_after_a_board_started_by_stop_gives_the_context_once(self):
        self.assertIsNone(self.hook(work_call("Edit")))
        self.assert_started(self.hook(stop()))

        after = self.hook(fixture("UserPromptSubmit"))
        self.assertEqual(list(after), ["hookSpecificOutput"])
        self.assertEqual(after["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(os.path.join(self.folder, "board.html"), after["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        self.assertIsNone(self.hook(work_call("Bash")))


class ATurnThatChangedNothing(TurnEnd):

    def test_nine_calls_that_change_nothing_end_with_no_board_and_no_python(self):
        env, started = self.python_shim()
        for number in range(9):
            self.assertIsNone(self.hook(bash("git status" if number % 2 else "ls"), env))
        self.assertEqual(self.count_bytes(), b"x" * 9)

        self.assertIsNone(self.hook(stop(), env))

        self.assertFalse(self.started_python(started), "Python started on a Stop after a turn that changed nothing")
        self.assertFalse(os.path.exists(self.boards()))
        self.assertEqual(self.counted(), 9)

    def test_a_stop_with_no_count_starts_nothing_and_no_python(self):
        env, started = self.python_shim()
        for payload in (stop(), stop(stop_hook_active=True)):
            self.assertIsNone(self.hook(payload, env))
        self.assertFalse(self.started_python(started), "Python started on a Stop with no count")
        self.assertEqual(self.created_anything(), [])

    def test_a_stop_hook_active_stop_after_no_change_starts_nothing(self):
        env, started = self.python_shim()
        self.assertIsNone(self.hook(bash("ls"), env))
        self.assertIsNone(self.hook(stop(stop_hook_active=True), env))
        self.assertFalse(self.started_python(started))
        self.assertFalse(os.path.exists(self.boards()))


class WhatEachCallWrites(TurnEnd):

    def test_a_file_tool_that_succeeds_writes_c_and_one_that_fails_writes_x(self):
        for tool in FILE_TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(self.byte_of(work_call(tool)), b"c")
                self.assertEqual(self.byte_of(work_call(tool, event="PostToolUseFailure")), b"x")

    def test_a_command_that_runs_git_commit_writes_c(self):
        for command in ("git commit -m x", "git -C sub commit -m x", "cd sub && git commit -m x",
                        "git -c user.name=n --no-pager commit -m x", "(cd sub && git commit)"):
            with self.subTest(command=command):
                self.assertEqual(self.byte_of(bash(command)), b"c")

    def test_a_command_that_only_mentions_commit_writes_x(self):
        # `commit` has to be git's subcommand: a search for the word, or git's name inside a quoted
        # string, is not a commit.
        for command in ("git status", "git log --grep commit", 'echo "git commit"',
                        "git status; echo commit", "git commitx", 'git log -m "a \\" git commit"'):
            with self.subTest(command=command):
                self.assertEqual(self.byte_of(bash(command)), b"x")

    def test_a_command_that_ran_git_commit_and_failed_writes_c(self):
        # It may have made its commit and failed afterwards: the push that follows it, refused.
        self.assertEqual(self.byte_of(bash("git commit -m x && git push", event="PostToolUseFailure")), b"c")
        self.assertEqual(self.byte_of(bash("git status", event="PostToolUseFailure")), b"x")

    def test_sudo_and_a_path_to_git_are_read_as_git(self):
        self.assertEqual(self.byte_of(bash("sudo git commit -m x")), b"c")
        self.assertEqual(self.byte_of(bash("/usr/bin/git commit -m x")), b"c")

    def test_a_command_key_far_into_the_payload_is_not_read(self):
        # Finding the key costs the shell time that grows with the square of how far in it sits.
        payload = bash("git commit -m x")
        payload["cwd"] = "/home/user/" + "deep/" * 300 + "project"
        self.assertGreater(compact(payload).index('"command":"'), 1200)
        env = dict(self.with_calls("100"), CLAUDE_PROJECT_DIR=self.project)
        if os.path.exists(self.counter()):
            os.unlink(self.counter())
        self.assertIsNone(self.hook(payload, env))
        self.assertEqual(self.count_bytes(), b"x")

    def test_a_commit_whose_message_runs_past_what_the_gate_reads_writes_c(self):
        # Only the command's first 600 bytes are read, and a long command is long in its message.
        body = 'Fix the parser\n\n' + 'It said "no" where it meant "yes". ' * 120
        self.assertEqual(self.byte_of(bash('git commit -m "' + body + '"')), b"c")
        self.assertEqual(self.byte_of(bash("cd sub && git commit -F - <<'EOF'\n" + body + "\nEOF")), b"c")

    def test_a_commit_named_past_what_the_gate_reads_writes_x(self):
        self.assertEqual(self.byte_of(bash("echo " + "a " * 400 + "&& git commit -m x")), b"x")

    def test_a_command_the_gate_cuts_in_the_middle_of_a_word_is_not_read_as_a_commit(self):
        # 600 bytes end inside `commit-tree`: the half word is dropped, not read as `commit`.
        lead = "echo " + "a" * 581 + " && git "
        self.assertEqual(len(lead) + len("commit"), 600)
        self.assertEqual(self.byte_of(bash(lead + "commit-tree HEAD^{tree}")), b"x")

    def test_a_command_longer_than_the_head_writes_x_and_nothing_breaks(self):
        payload = bash("echo " + "a" * 5000 + " && git commit -m x")
        self.assertEqual(self.byte_of(payload), b"x")
        env, started = self.python_shim()
        self.assertIsNone(self.hook(stop(), env))
        self.assertFalse(self.started_python(started))
        self.assertFalse(os.path.exists(self.boards()))

    def test_the_count_is_still_the_length_whatever_the_bytes(self):
        order = "cxxcxcxxc"
        for number, kind in enumerate(order, 1):
            payload = work_call("Edit") if kind == "c" else bash("ls")
            self.assertIsNone(self.hook(payload))
            self.assertEqual(self.counted(), number)
        self.assertEqual(self.count_bytes(), order.encode())
        self.assertFalse(os.path.exists(self.boards()))

        output = self.hook(bash("ls"))

        self.assert_started(output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIsNone(self.counted())

    def test_the_handler_never_writes_to_the_count_on_a_stop(self):
        self.calls(3)
        before = self.count_bytes()
        now = datetime.now(timezone.utc)
        self.assertIsNone(board_hook.handle(dict(stop(), transcript_path=self.transcript), self.env, now))
        self.assertEqual(self.count_bytes(), before)
        self.assertFalse(os.path.exists(self.boards()))


class TheEndOfATurnOnABoard(TurnEnd):

    def setUp(self):
        super().setUp()
        # A transcript with the prompt alone, so reading it back records nothing a test did not send.
        self.write_prompt_only()

    def test_a_turn_that_recorded_something_ends_with_the_page_and_one_that_did_not_with_nothing(self):
        self.start_with_subagent()
        self.assertIn("hookSpecificOutput", self.hook(fixture("UserPromptSubmit")))
        self.assertIsNone(self.hook(bash("ls")))
        self.assertIsNone(self.hook(stop()), "a Stop prints nothing")

        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        self.assertIsNone(self.hook(stop()))

        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        now = datetime.now(timezone.utc)
        board.recorded(self.folder, now, "question", text="Which format?", default="CSV", affects=None,
                       reverse=None, hardStop=False)
        self.assertIsNone(self.hook(stop()), "a Stop prints nothing")

    def test_a_question_recorded_through_the_command_line_ends_with_the_page(self):
        self.start_with_subagent()
        self.hook(fixture("UserPromptSubmit"))
        self.assertIsNone(self.hook(stop()))
        self.hook(fixture("UserPromptSubmit"))
        done = subprocess.run(
            [sys.executable, os.path.join(PLUGIN, "board", "board.py"), "question", "Which format?",
             "--default", "CSV", "--board", self.folder],
            env=self.env, capture_output=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(self.hook(stop()), "a Stop prints nothing")

    def test_the_stop_that_starts_a_board_says_so_once(self):
        self.assertIsNone(self.hook(work_call("Edit")))
        self.assert_started(self.hook(stop()))
        self.assertIsNone(self.hook(stop()))

    def test_a_closed_board_says_nothing(self):
        self.start_with_subagent()
        self.hook(bash("ls"))
        board.close(self.folder, datetime.now(timezone.utc))
        self.assertIsNone(self.hook(stop()))

    def test_a_board_closed_by_the_session_end_reopens_on_stop_and_says_nothing(self):
        self.start_with_subagent()
        self.hook(bash("ls"))
        self.assertIsNone(self.hook(fixture("SessionEnd")))
        self.assertIsNone(self.hook(stop()))
        self.assertIn("reopen", self.kinds())


class NothingIsPrinted(TurnEnd):
    """The hook once printed `Logbook: <path>` through `systemMessage` wherever it started a board, ended a
    turn that recorded something, or began a session. The band above the prompt points at the board now:
    no output of the hook carries a `systemMessage`, and the model is still told, once, where the board is."""

    THIRD_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def folder_of(self, session):
        return os.path.join(self.boards(), session)

    def announced(self, folder):
        return os.path.exists(os.path.join(folder, "announced"))

    def test_no_start_turn_end_or_session_start_prints_and_the_context_is_still_given_once(self):
        outputs = []

        def send(payload):
            output = self.hook(payload)
            outputs.append(output)
            return output

        # A start at the threshold, on a call that carries context: the context, and no message.
        self.calls(9)
        started = send(work_call("Edit"))
        self.assertTrue(board.is_board(self.folder))
        self.assertEqual(list(started), ["hookSpecificOutput"])
        self.assertIn(os.path.join(self.folder, "board.html"), started["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.announced(self.folder))

        # The end of a turn that recorded something on that board prints nothing.
        self.assertIsNone(send(bash("ls")))
        self.assertIsNone(send(stop()))
        self.assertIn("turn-end", self.kinds())

        # A session start gives the context back every time, as the context alone.
        for source in ("startup", "compact", "resume"):
            with self.subTest(source=source):
                payload = fixture("SessionStart")
                payload["source"] = source
                begun = send(payload)
                self.assertEqual(list(begun), ["hookSpecificOutput"])
                self.assertEqual(begun["hookSpecificOutput"]["hookEventName"], "SessionStart")
                self.assertIn(os.path.join(self.folder, "board.html"), begun["hookSpecificOutput"]["additionalContext"])

        # A board a failed call starts: nothing is printed, and the next call that can carry context does.
        self.calls(9, session=OTHER_SESSION)
        other = self.folder_of(OTHER_SESSION)
        self.assertIsNone(send(work_call("Bash", OTHER_SESSION, "PostToolUseFailure")))
        self.assertTrue(board.is_board(other))
        self.assertFalse(self.announced(other))
        delivered = send(work_call("Bash", OTHER_SESSION))
        self.assertEqual(list(delivered), ["hookSpecificOutput"])
        self.assertIn(os.path.join(other, "board.html"), delivered["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(send(work_call("Bash", OTHER_SESSION)))

        # A board a Stop starts: nothing is printed, and the next prompt carries the context.
        third = self.folder_of(self.THIRD_SESSION)
        self.assertIsNone(send(work_call("Edit", self.THIRD_SESSION)))
        self.assertIsNone(send(stop(session_id=self.THIRD_SESSION)))
        self.assertTrue(board.is_board(third))
        self.assertFalse(self.announced(third))
        prompt = fixture("UserPromptSubmit")
        prompt["session_id"] = self.THIRD_SESSION
        prompted = send(prompt)
        self.assertEqual(list(prompted), ["hookSpecificOutput"])
        self.assertIn(os.path.join(third, "board.html"), prompted["hookSpecificOutput"]["additionalContext"])

        for output in outputs:
            self.assertNotIn("systemMessage", output or {})


class NoTrigger(TurnEnd):

    def test_without_a_plugin_data_folder_a_stop_after_changes_starts_nothing(self):
        unset = {key: value for key, value in self.env.items() if key != "CLAUDE_PLUGIN_DATA"}
        for tool in FILE_TOOLS:
            self.assertIsNone(self.hook(work_call(tool), unset))
        self.assertIsNone(self.hook(bash("git commit -m x"), unset))
        self.assertIsNone(self.hook(stop(), unset))
        self.assertEqual(self.created_anything(), [])

    def test_nothing_is_read_through_a_linked_count_folder_on_stop(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        os.mkdir(self.data)
        os.symlink(elsewhere, os.path.join(self.data, "calls"))
        kept = os.path.join(elsewhere, SESSION)
        with open(kept, "w", encoding="utf-8") as f:
            f.write("ccc")
        env, started = self.python_shim()

        self.assertIsNone(self.hook(stop(), env))

        self.assertFalse(self.started_python(started), "the gate read a count through a linked folder")
        self.assertFalse(os.path.exists(self.boards()))
        self.assertFalse(board.calls_changed(self.env, SESSION))
        with open(kept, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ccc")


if __name__ == "__main__":
    unittest.main()
