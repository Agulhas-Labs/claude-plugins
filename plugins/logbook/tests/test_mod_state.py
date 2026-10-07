"""Tests for `board/mod_state.py`, the one line of JSON the band and pane read, and the early start it offers.

Run: python3 -m unittest discover -s plugins/logbook/tests

The helper runs as the mod runs it, as a child process in the hooks' environment (`GIT_*`, `LOGBOOK_*` and
`CLAUDE_*` stripped, `CLAUDE_PROJECT_DIR` unset unless a test sets it). The early start is checked against the
captured work transcript and the real `gate.sh`: the board it starts must read the earlier calls back as the
hooks' own start does, and must leave the model's context to the next hook.
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from test_hooks import HERE, PLUGIN, SESSION, Hooks
from test_work_trigger import work_call

import board  # noqa: E402  (test_hooks puts the board folder on the path)
import mod_state  # noqa: E402

HELPER = os.path.join(PLUGIN, "board", "mod_state.py")
BOARD_PY = os.path.join(PLUGIN, "board", "board.py")
WORK_TRANSCRIPT = os.path.join(HERE, "fixtures", "transcript-work.jsonl")


class ModState(Hooks):

    def setUp(self):
        super().setUp()
        self.env.pop("CLAUDE_PROJECT_DIR")  # the mod's process gets the project from its arguments
        self.data = os.path.join(self.tmp, "data")
        self.env["CLAUDE_PLUGIN_DATA"] = self.data

    def run_helper(self, *extra, project=None, session=SESSION, env=None):
        done = subprocess.run(
            [sys.executable, HELPER, "--project", project or self.project, "--session", session, *extra],
            env=env or self.env, capture_output=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, b"")
        lines = done.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1, "one line of JSON")
        return json.loads(lines[0])

    def start_by_hand(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SESSION)
        subprocess.run([sys.executable, BOARD_PY, "start", "--project", self.project], env=env, check=True, capture_output=True)

    def use_work_transcript(self):
        shutil.copy(WORK_TRANSCRIPT, self.transcript)


class Reading(ModState):

    def test_no_board_is_an_empty_object(self):
        self.assertEqual(self.run_helper(), {})

    def test_the_board_and_its_state(self):
        self.start_by_hand()
        found = self.run_helper()
        self.assertEqual(found["board"], self.folder)
        self.assertEqual(found["state"], board.read_state(self.folder))
        self.assertEqual(found["state"]["session"], SESSION)

    def test_a_board_is_found_from_a_folder_below_the_project(self):
        self.start_by_hand()
        below = os.path.join(self.project, "src", "deep")
        os.makedirs(below)
        self.assertEqual(self.run_helper(project=below)["board"], self.folder)

    def test_another_sessions_board_is_not_this_ones(self):
        self.start_by_hand()
        self.assertEqual(self.run_helper(session="99999999-8888-4777-8666-555555555555"), {})

    def test_a_session_id_that_cannot_be_a_folder_name_is_an_empty_object(self):
        self.start_by_hand()
        self.assertEqual(self.run_helper(session=".."), {})

    def test_a_state_file_that_does_not_parse_is_an_empty_object(self):
        self.start_by_hand()
        with open(os.path.join(self.folder, board.STATE_FILE), "w", encoding="utf-8") as f:
            f.write("window.BOARD = {not json;\n")
        self.assertEqual(self.run_helper(), {})

    def test_a_linked_boards_folder_is_refused(self):
        elsewhere = os.path.join(self.tmp, "elsewhere", SESSION)
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, board.MARKER_FILE), "w", encoding="utf-8") as f:
            f.write(SESSION)
        os.symlink(os.path.dirname(elsewhere), os.path.join(self.project, board.BOARDS_DIR))
        self.assertEqual(self.run_helper(), {})

    def test_a_project_outside_the_home_and_temporary_folders_needs_the_setting(self):
        self.start_by_hand()
        roots = [mock.patch("board.tempfile.gettempdir", return_value=os.path.join(os.sep, "no-such-tmp")),
                 mock.patch("board.os.path.expanduser", return_value=os.path.join(os.sep, "no-such-home"))]
        for patch in roots:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(mod_state.board_and_state(self.project, SESSION, {}), {})
        found = mod_state.board_and_state(self.project, SESSION, {board.ALLOW_ANY_PATH: "1"})
        self.assertEqual(found["board"], self.folder)

    def test_the_hooks_project_variable_wins_over_the_argument(self):
        self.start_by_hand()
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        env = dict(self.env, CLAUDE_PROJECT_DIR=self.project)
        self.assertEqual(self.run_helper(project=other, env=env)["board"], self.folder)


class EarlyStart(ModState):

    def test_start_reads_the_earlier_calls_back_from_the_transcript_and_titles_the_board(self):
        self.use_work_transcript()
        found = self.run_helper("--start", "--transcript", self.transcript)
        self.assertEqual(found["board"], self.folder)
        log = board.events(self.folder)
        self.assertEqual(log[0]["kind"], "start")
        early = [e for e in log[1:] if e.get("early") is True]
        self.assertEqual({e["kind"] for e in early}, {"change", "command"})
        self.assertTrue(all(e["use"].startswith("toolu_") for e in early))
        self.assertEqual(len(early), len(log) - 1)
        self.assertEqual(found["state"]["title"], log[0]["title"])
        self.assertNotEqual(found["state"]["title"], board.UNTITLED)
        self.assertGreater(len(found["state"]["changes"]), 0)
        self.assertGreater(found["state"]["commandsTotal"], 0)

    def test_the_starts_title_and_events_are_the_ones_the_hooks_start_would_have_made(self):
        # The same transcript, started by the hooks at their threshold, on a second project.
        self.use_work_transcript()
        self.run_helper("--start", "--transcript", self.transcript)
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        env = dict(self.env, CLAUDE_PROJECT_DIR=other, LOGBOOK_CALLS="1")
        payload = work_call("Bash")
        payload["transcript_path"] = self.transcript
        payload["tool_use_id"] = "toolu_not_in_the_transcript"
        self.hook(payload, env)
        theirs = board.events(os.path.join(other, board.BOARDS_DIR, SESSION))
        mine = board.events(self.folder)
        shape = lambda log: [(e["kind"], e.get("use"), e.get("title"), e.get("early")) for e in log]
        # Theirs also holds the call that started it, last; everything before it is what the transcript gave.
        self.assertEqual(shape(mine), shape(theirs)[:len(mine)])

    def test_the_next_hook_still_gives_the_model_its_context_once(self):
        self.use_work_transcript()
        self.run_helper("--start", "--transcript", self.transcript)
        self.assertFalse(os.path.exists(os.path.join(self.folder, board.ANNOUNCED_FILE)))
        env = dict(self.env, CLAUDE_PROJECT_DIR=self.project)
        payload = work_call("Bash")
        payload["transcript_path"] = self.transcript
        after = self.hook(payload, env)
        self.assertIn(os.path.join(self.folder, "board.html"), after["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook(payload, env))

    def test_board_py_start_marks_the_board_announced_so_the_model_would_never_be_told(self):
        # Why the helper does not run `board.py start`: it writes `announced`, and `announce` is exclusive.
        self.start_by_hand()
        self.assertTrue(os.path.exists(os.path.join(self.folder, board.ANNOUNCED_FILE)))
        env = dict(self.env, CLAUDE_PROJECT_DIR=self.project)
        self.assertIsNone(self.hook(work_call("Bash"), env))

    def test_start_does_nothing_to_a_board_that_exists(self):
        self.use_work_transcript()
        self.start_by_hand()
        before = board.events(self.folder)
        found = self.run_helper("--start", "--transcript", self.transcript)
        self.assertEqual(board.events(self.folder), before)
        self.assertEqual(found["board"], self.folder)

    def test_start_without_a_readable_transcript_still_starts_an_untitled_board(self):
        found = self.run_helper("--start", "--transcript", os.path.join(self.tmp, "missing.jsonl"))
        self.assertEqual(found["state"]["title"], board.UNTITLED)

    def plant_transcript(self, session=SESSION, project="-p"):
        # The config folder is the test's own, so the real one is never searched.
        self.env["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "config")
        folder = os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", project)
        os.makedirs(folder, exist_ok=True)
        shutil.copy(WORK_TRANSCRIPT, os.path.join(folder, session + ".jsonl"))

    def test_start_without_a_transcript_path_finds_the_transcript_by_the_session_id(self):
        self.plant_transcript()
        found = self.run_helper("--start")
        self.assertNotEqual(found["state"]["title"], board.UNTITLED)
        self.assertGreater(len(found["state"]["changes"]), 0)

    def test_no_transcript_for_the_session_id_starts_an_untitled_board(self):
        self.plant_transcript(session="99999999-2222-4333-8444-555555555555")
        self.assertEqual(self.run_helper("--start")["state"]["title"], board.UNTITLED)

    def test_two_transcripts_for_the_session_id_start_an_untitled_board(self):
        self.plant_transcript(project="-a")
        self.plant_transcript(project="-b")
        self.assertEqual(self.run_helper("--start")["state"]["title"], board.UNTITLED)

    def test_a_directory_named_like_the_transcript_is_not_a_match(self):
        self.plant_transcript()
        os.makedirs(os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "-b", SESSION + ".jsonl"))
        self.assertIsNotNone(mod_state.find_transcript(SESSION, self.env))  # the one file; the directory is not a second
        shutil.rmtree(os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "-p"))
        self.assertIsNone(mod_state.find_transcript(SESSION, self.env))  # a directory alone is no transcript

    def test_a_config_folder_named_with_glob_characters_still_finds_the_transcript(self):
        self.plant_transcript()
        odd = os.path.join(self.tmp, "cfg[a]*")
        os.rename(self.env["CLAUDE_CONFIG_DIR"], odd)
        self.env["CLAUDE_CONFIG_DIR"] = odd
        self.assertEqual(mod_state.find_transcript(SESSION, self.env), os.path.join(odd, "projects", "-p", SESSION + ".jsonl"))

    def test_a_session_id_that_is_not_a_uuid_is_never_looked_up(self):
        self.plant_transcript(session="not-a-uuid")
        found = self.run_helper("--start", session="not-a-uuid")
        self.assertEqual(found["state"]["title"], board.UNTITLED)

    def test_start_in_a_linked_boards_folder_starts_nothing(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, os.path.join(self.project, board.BOARDS_DIR))
        self.assertEqual(self.run_helper("--start", "--transcript", self.transcript), {})
        self.assertEqual(os.listdir(elsewhere), [])


class Opening(ModState):

    def run_in_process(self, *extra):
        argv = ["--project", self.project, "--session", SESSION, *extra]
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(out):
            mod_state.main(argv)
        return json.loads(out.getvalue())

    def test_open_hands_the_boards_page_to_the_browser(self):
        self.start_by_hand()
        with mock.patch.object(mod_state.webbrowser, "open", return_value=True) as browser:
            found = self.run_in_process("--open")
        browser.assert_called_once_with(pathlib.Path(self.folder, board.BOARD_FILE).as_uri())
        self.assertTrue(found["opened"])
        self.assertEqual(found["board"], self.folder)

    def test_open_says_so_when_the_browser_would_not(self):
        self.start_by_hand()
        with mock.patch.object(mod_state.webbrowser, "open", return_value=False):
            self.assertFalse(self.run_in_process("--open")["opened"])

    def test_open_with_no_board_opens_nothing(self):
        with mock.patch.object(mod_state.webbrowser, "open") as browser:
            found = self.run_in_process("--open")
        browser.assert_not_called()
        self.assertFalse(found["opened"])

    def test_reading_without_open_never_touches_the_browser(self):
        self.start_by_hand()
        with mock.patch.object(mod_state.webbrowser, "open") as browser:
            self.run_in_process()
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
