"""Tests for what happens when a session starts, and for starting and closing a board by hand.

Run: python3 -m unittest discover -s plugins/logbook/tests

The hook tests run the real `gate.sh` through the helpers in `test_hooks.py`, whose environment has
every `GIT_*` variable removed. The hand commands run `board.py` as the skill does, as a program, in
that same environment. The pruning tests call `board.prune` with the clock of `test_board.py`, on
boards whose files they write by hand, so nothing there runs git; every age is an event time or a
modification time the test writes.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from test_board import NOW, Workspace
from test_hooks import GATE, HOOKS, PLUGIN, SESSION, Hooks, fixture

import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402  (test_hooks puts the hooks folder on the path)

BOARD_PY = os.path.join(PLUGIN, "board", "board.py")
OTHER = "99999999-8888-4777-8666-555555555555"
# Every file a board may hold, the temporary file of an atomic write among them.
BOARD_FILES = (".logbook", "announced", "catch-up", "events.jsonl", "state.js", "board.html", "report.html")
TEMPORARY = ".state.js.a1b2c3d4.tmp"
LONG_AGO = datetime(2000, 1, 3, 12, 0, 0, tzinfo=timezone.utc)


def days_ago(days):
    return NOW - timedelta(days=days)


def write_board(boards, name, last, marker=True, log=True):
    """A board folder written by hand: every file a board holds, and a log whose last event is at `last`."""
    folder = os.path.join(boards, name)
    os.makedirs(folder)
    for file in BOARD_FILES[1:] + (TEMPORARY,):
        if file != "events.jsonl":
            with open(os.path.join(folder, file), "w", encoding="utf-8") as f:
                f.write("x")
    if marker:
        with open(os.path.join(folder, ".logbook"), "w", encoding="utf-8") as f:
            f.write(name)
    if log:
        with open(os.path.join(folder, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": board.utc(last - timedelta(days=1)), "kind": "start", "title": "Old"}) + "\n")
            f.write(json.dumps({"t": board.utc(last), "kind": "turn-end"}) + "\n")
            f.write("not json\n")
    return folder


# ---------------------------------------------------------------------------------------------------
# The hook


class SessionStartHook(Hooks):

    def session_start(self, source="compact", session=SESSION, env=None):
        payload = fixture("SessionStart")
        payload.update(source=source, session_id=session)
        return self.hook(payload, env)

    def test_a_project_without_a_boards_folder_prints_nothing_and_never_starts_python(self):
        # The same `python3` that notes each start as in `test_hooks`: the gate must answer by itself.
        bin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bin_dir.cleanup)
        started = os.path.join(bin_dir.name, "python-started")
        with open(os.path.join(bin_dir.name, "python3"), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\necho started >> "{started}"\nexec "{sys.executable}" "$@"\n')
        os.chmod(os.path.join(bin_dir.name, "python3"), 0o755)
        env = dict(self.env, PATH=bin_dir.name + os.pathsep + self.env.get("PATH", ""))

        for source in ("startup", "resume", "clear", "compact"):
            self.assertIsNone(self.session_start(source, env=env))
        self.assertEqual(self.created_anything(), [])
        self.assertFalse(os.path.exists(started), "the gate started Python in a project with no boards folder")

        # Once the project has a boards folder, the same PATH does reach Python.
        os.mkdir(os.path.join(self.project, ".logbook"))
        self.assertIsNone(self.session_start("startup", env=env))
        self.assertTrue(os.path.exists(started))

    def test_a_compaction_or_a_resume_gets_the_context_and_the_path_back_every_time(self):
        self.start_with_subagent()
        self.assertIn("hookSpecificOutput", self.hook(fixture("UserPromptSubmit")))  # announced once already
        for source in ("compact", "resume", "compact"):
            with self.subTest(source=source):
                output = self.session_start(source)
                self.assertEqual(list(output), ["hookSpecificOutput"])
                self.assertEqual(output["hookSpecificOutput"], {
                    "hookEventName": "SessionStart", "additionalContext": board_hook.context(self.folder, self.env),
                })

    def test_the_context_asks_for_decisions_checks_and_deliverables_after_questions(self):
        text = board_hook.context(self.folder, self.env)
        self.assertIn(
            "unless it is a hard stop. Record decisions worth a look, checks and deliverables as you make them. "
            "A line from the user like Q3:", text,
        )

    def test_a_closed_board_or_no_board_gives_nothing(self):
        # A resume reopens a closed board instead (test_resume_and_gate.py); every other source leaves
        # a board closed by hand closed (one the session's end closed is reopened by any later event
        # from the session, test_lifecycle.py), and a session with no board of its own gives nothing.
        self.start_with_subagent()
        self.assertIsNone(self.session_start("compact", session=OTHER))
        board.close(self.folder, board.clock(), self.env)
        self.assertIsNone(self.session_start("compact"))
        self.assertIsNone(self.session_start("startup"))

    def test_a_session_start_prunes_old_boards_and_keeps_its_own(self):
        boards = os.path.join(self.project, ".logbook")
        old = write_board(boards, OTHER, LONG_AGO)
        own = write_board(boards, SESSION, LONG_AGO)
        output = self.session_start("resume")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(own))
        self.assertIn("hookSpecificOutput", output)


# ---------------------------------------------------------------------------------------------------
# Pruning


class Pruning(Workspace):

    def setUp(self):
        super().setUp()
        self.boards = os.path.join(self.project, ".logbook")
        os.mkdir(self.boards)

    def prune(self, env=None, keep=None):
        return board.prune(self.project, NOW, {} if env is None else env, keep=keep)

    def test_a_marked_board_older_than_the_retention_is_removed_with_every_file_it_holds(self):
        folder = write_board(self.boards, OTHER, days_ago(15))
        young = write_board(self.boards, "young", days_ago(13))
        self.assertEqual(self.prune(), [folder])
        self.assertFalse(os.path.lexists(folder))
        self.assertEqual(sorted(os.listdir(young)), sorted(BOARD_FILES + (TEMPORARY,)))
        with open(os.path.join(self.boards, "index.html"), encoding="utf-8") as f:
            index = f.read()
        self.assertNotIn(OTHER, index)
        self.assertIn("young/", index)

    def test_the_current_sessions_board_is_kept_however_old(self):
        folder = write_board(self.boards, SESSION, days_ago(400))
        self.assertEqual(self.prune(keep=board.board_dir(self.project, SESSION)), [])
        self.assertEqual(sorted(os.listdir(folder)), sorted(BOARD_FILES + (TEMPORARY,)))

    def test_an_unmarked_old_folder_survives(self):
        folder = write_board(self.boards, OTHER, days_ago(400), marker=False)
        self.assertEqual(self.prune(), [])
        self.assertEqual(sorted(os.listdir(folder)), sorted(BOARD_FILES[1:] + (TEMPORARY,)))
        self.assertFalse(os.path.exists(os.path.join(self.boards, "index.html")), "nothing pruned, no index written")

    def test_a_marker_that_is_a_link_or_a_folder_is_no_marker(self):
        target = os.path.join(self.tmp.name, "a-marker")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x")
        linked = write_board(self.boards, "linked-marker", days_ago(400), marker=False)
        os.symlink(target, os.path.join(linked, ".logbook"))
        folder = write_board(self.boards, "folder-marker", days_ago(400), marker=False)
        os.mkdir(os.path.join(folder, ".logbook"))
        self.assertEqual(self.prune(), [])
        self.assertIn("events.jsonl", os.listdir(linked))
        self.assertIn("events.jsonl", os.listdir(folder))

    def test_a_link_to_an_old_marked_board_elsewhere_survives_and_so_do_its_files(self):
        target = write_board(os.path.join(self.tmp.name, "elsewhere"), OTHER, days_ago(400))
        link = os.path.join(self.boards, OTHER)
        os.symlink(target, link)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.islink(link))
        self.assertEqual(sorted(os.listdir(target)), sorted(BOARD_FILES + (TEMPORARY,)))

    def test_an_unknown_file_keeps_the_folder_and_the_known_files_go(self):
        folder = write_board(self.boards, OTHER, days_ago(400))
        for name in ("notes.txt", ".state.js.tmp", ".state.js.a1b2c3d4e5.tmp"):
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write("mine")
        self.assertEqual(self.prune(), [])
        self.assertEqual(sorted(os.listdir(folder)), [".state.js.a1b2c3d4e5.tmp", ".state.js.tmp", "notes.txt"])

    def test_a_sub_directory_keeps_the_folder_and_is_not_entered(self):
        folder = write_board(self.boards, OTHER, days_ago(400))
        inner = os.path.join(folder, "inner")
        os.mkdir(inner)
        with open(os.path.join(inner, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.mkdir(os.path.join(folder, "state.js.d"))
        self.assertEqual(self.prune(), [])
        self.assertEqual(sorted(os.listdir(folder)), ["inner", "state.js.d"])
        self.assertEqual(os.listdir(inner), ["events.jsonl"])

    def test_a_file_named_as_a_board_file_but_a_link_is_not_followed(self):
        outside = os.path.join(self.tmp.name, "precious")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("keep me")
        folder = write_board(self.boards, OTHER, days_ago(400))
        os.unlink(os.path.join(folder, "report.html"))
        os.symlink(outside, os.path.join(folder, "report.html"))
        self.assertEqual(self.prune(), [])
        self.assertEqual(os.listdir(folder), ["report.html"])
        self.assertEqual(self.read(outside), "keep me")

    def test_names_that_are_not_session_ids_are_not_candidates(self):
        folder = write_board(self.boards, "has space", days_ago(400))
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.isdir(folder))

    def test_without_a_readable_log_the_age_is_the_markers(self):
        old = write_board(self.boards, "old", days_ago(400), log=False)
        young = write_board(self.boards, "young", days_ago(400), log=False)
        for folder, days in ((old, 20), (young, 1)):
            moment = days_ago(days).timestamp()
            os.utime(os.path.join(folder, ".logbook"), (moment, moment))
        self.assertEqual(self.prune(), [old])
        self.assertTrue(os.path.isdir(young))

    def test_the_retention_is_a_setting_and_anything_but_a_positive_integer_means_fourteen(self):
        for value, kept in (("3", False), ("7", True), (" 3 ", False)):
            with self.subTest(value=value):
                folder = write_board(self.boards, f"five-{len(os.listdir(self.boards))}", days_ago(5))
                self.prune({"LOGBOOK_RETENTION_DAYS": value})
                self.assertEqual(os.path.isdir(folder), kept)
        for value in ("0", "-3", "abc", "", "2.5", "1e3"):
            with self.subTest(value=value):
                self.assertEqual(board.retention_days({"LOGBOOK_RETENTION_DAYS": value}), 14)
                young = write_board(self.boards, f"young-{len(os.listdir(self.boards))}", days_ago(13))
                old = write_board(self.boards, f"old-{len(os.listdir(self.boards))}", days_ago(15))
                self.prune({"LOGBOOK_RETENTION_DAYS": value})
                self.assertTrue(os.path.isdir(young))
                self.assertFalse(os.path.lexists(old))
        self.assertEqual(board.retention_days({}), 14)

    def test_pruning_never_raises(self):
        folder = write_board(self.boards, OTHER, days_ago(400))
        with mock.patch.object(board.os, "unlink", side_effect=PermissionError("no")):
            self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(board.prune(os.path.join(self.tmp.name, "nowhere"), NOW, {}), [])

    def test_the_prune_command_prints_one_line_and_keeps_the_sessions_board(self):
        write_board(self.boards, OTHER, days_ago(400))
        own = write_board(self.boards, SESSION, days_ago(400))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board, "clock", lambda: NOW), \
                mock.patch.dict(os.environ, {board.SESSION_VARIABLE: SESSION}), \
                redirect_stdout(out), redirect_stderr(err):
            code = board.main(["prune", "--project", self.project])
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, "pruned 1 boards\n", ""))
        self.assertTrue(os.path.isdir(own))


# ---------------------------------------------------------------------------------------------------
# Starting and closing by hand


class ByHand(Hooks):

    def cli(self, *argv, session=SESSION, cwd=None, env=None):
        """board.py run as the skill runs it, in the hooks' environment with the session's id set."""
        env = dict(self.env if env is None else env, CLAUDE_CODE_SESSION_ID=session)
        done = subprocess.run(
            [sys.executable, BOARD_PY, *argv], cwd=cwd or self.project, env=env, capture_output=True, timeout=60,
        )
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def page(self, name="board.html"):
        return os.path.join(self.folder, name)

    def test_start_prints_the_page_and_the_title_is_the_models(self):
        self.assertEqual(self.cli("start", "Hand test"), (0, f"board started: {self.page()}\n", ""))
        state = self.state()
        self.assertEqual((state["title"], state["titleSource"]), ("Hand test", "model"))
        self.assertTrue(os.path.isfile(self.page()))
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "announced")))

    def test_start_without_a_title_is_untitled_from_the_prompt(self):
        self.assertEqual(self.cli("start"), (0, f"board started: {self.page()}\n", ""))
        state = self.state()
        self.assertEqual((state["title"], state["titleSource"]), ("Untitled task", "prompt"))

    def test_the_project_is_the_option_else_the_setting_else_the_current_directory(self):
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        page = os.path.join(other, ".logbook", SESSION, "board.html")
        self.assertEqual(self.cli("start", "--project", other), (0, f"board started: {page}\n", ""))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".logbook")))
        third = os.path.join(self.tmp, "third")
        os.mkdir(third)
        env = {key: value for key, value in self.env.items() if key != "CLAUDE_PROJECT_DIR"}
        page = os.path.join(third, ".logbook", SESSION, "board.html")
        self.assertEqual(self.cli("start", cwd=third, env=env), (0, f"board started: {page}\n", ""))

    def test_a_running_board_is_reported_and_only_its_title_changes(self):
        self.cli("start", "First")
        before = len(board.events(self.folder))
        self.assertEqual(self.cli("start"), (0, f"board already running: {self.page()}\n", ""))
        self.assertEqual(len(board.events(self.folder)), before)
        self.assertEqual(self.cli("start", "Second"), (0, f"board already running: {self.page()}\n", ""))
        self.assertEqual(self.kinds()[before:], ["title"])
        self.assertEqual(self.state()["title"], "Second")

    def test_a_board_started_by_a_hook_is_running_too(self):
        self.start_with_subagent()
        self.assertEqual(self.cli("start", "Named"), (0, f"board already running: {self.page()}\n", ""))

    def test_a_closed_board_is_not_started_again(self):
        self.cli("start", "Done soon")
        self.assertEqual(self.cli("close")[0], 0)
        before = len(board.events(self.folder))
        code, out, err = self.cli("start", "Again")
        self.assertEqual((code, out, err.count("\n")), (1, "", 1), err)
        self.assertIn("closed", err)
        self.assertEqual(len(board.events(self.folder)), before)

    def test_a_bad_session_id_is_one_line_and_exit_1(self):
        for session in ("../project", "a/b", "..", "has space", ""):
            with self.subTest(session=session):
                code, out, err = self.cli("start", "x", session=session)
                self.assertEqual((code, out, err.count("\n")), (1, "", 1), err)
        self.assertEqual(self.created_anything(), [])

    def test_a_board_started_by_hand_is_not_announced_again(self):
        self.cli("start", "Hand test")
        self.assertIsNone(self.hook(fixture("UserPromptSubmit")))
        recorded = [e["kind"] for e in board.events(self.folder) if not e.get("early")]
        self.assertEqual(recorded[-1], "turn-start")

    def test_close_finds_the_sessions_board_and_prints_the_report(self):
        self.cli("start", "Hand test")
        inner = os.path.join(self.project, "src")
        os.mkdir(inner)
        report = self.page("report.html")
        self.assertEqual(self.cli("close", cwd=inner), (0, f"report written: {report}\n", ""))
        self.assertTrue(os.path.isfile(report))
        self.assertEqual(self.kinds()[-1], "close")

    def test_close_with_a_board_prints_the_same_line(self):
        self.cli("start", "Hand test")
        report = self.page("report.html")
        self.assertEqual(self.cli("close", "--board", self.folder, session=OTHER), (0, f"report written: {report}\n", ""))

    def test_close_without_a_board_is_one_line_and_exit_1(self):
        code, out, err = self.cli("close")
        self.assertEqual((code, out, err.count("\n")), (1, "", 1), err)


class Manifest(unittest.TestCase):

    def test_session_start_runs_the_gate_and_hooks_json_names_only_files_that_exist(self):
        with open(os.path.join(HOOKS, "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        self.assertEqual(
            hooks["SessionStart"],
            [{"hooks": [{"type": "command", "command": 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/gate.sh"'}]}],
        )
        for event, groups in hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    named = hook["command"].split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN)
                    self.assertEqual(named, GATE, event)
                    self.assertTrue(os.path.isfile(named), named)


if __name__ == "__main__":
    unittest.main()
