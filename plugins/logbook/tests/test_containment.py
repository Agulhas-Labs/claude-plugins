"""Tests for where the plugin will write: a project or data folder from the environment or a hook's
payload is used only inside the home folder or the temporary folder, unless `LOGBOOK_ALLOW_ANY_PATH=1`.

Run: python3 -m unittest discover -s plugins/logbook/tests
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from test_board import SESSION

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402

ALLOW_ANY_PATH = "LOGBOOK_ALLOW_ANY_PATH"
OPT_OUT = {ALLOW_ANY_PATH: "1"}
# A path outside both roots, never created: only its text is checked.
OUTSIDE = os.path.join(os.path.abspath(os.sep), "logbook-containment-test-outside")


class Contained(unittest.TestCase):
    def test_a_path_under_home_or_the_temporary_folder_is_its_real_path(self):
        for root in (os.path.expanduser("~"), tempfile.gettempdir()):
            path = os.path.join(root, "logbook-containment-test", "project")
            with self.subTest(root=root):
                self.assertEqual(board.contained(path, {}), os.path.realpath(path))

    def test_a_path_outside_them_is_refused_unless_the_setting_allows_any(self):
        self.assertIsNone(board.contained(OUTSIDE, {}))
        self.assertIsNone(board.contained(OUTSIDE, {ALLOW_ANY_PATH: "yes"}))
        self.assertEqual(board.contained(OUTSIDE, OPT_OUT), os.path.realpath(OUTSIDE))


class Outside(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = os.path.join(os.path.realpath(tmp.name), "data")
        os.makedirs(os.path.join(self.data, board.CALLS_DIR))
        self.count = os.path.join(self.data, board.CALLS_DIR, SESSION)
        with open(self.count, "wb") as f:
            f.write(b"c" * 20)

    def test_the_count_file_is_used_only_in_a_data_folder_inside_the_roots(self):
        self.assertEqual(board.calls_file({"CLAUDE_PLUGIN_DATA": self.data}, SESSION), self.count)
        self.assertIsNone(board.calls_file({"CLAUDE_PLUGIN_DATA": OUTSIDE}, SESSION))
        allowed = board.calls_file(dict(OPT_OUT, CLAUDE_PLUGIN_DATA=OUTSIDE), SESSION)
        self.assertEqual(allowed, os.path.join(os.path.realpath(OUTSIDE), board.CALLS_DIR, SESSION))

    def test_the_hook_treats_a_project_outside_as_unusable_and_forgets_the_count(self):
        env = {"CLAUDE_PROJECT_DIR": OUTSIDE, "CLAUDE_PLUGIN_DATA": self.data}
        payload = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "hello"}
        self.assertIsNone(board_hook.handle(payload, env, datetime.now(timezone.utc)))
        self.assertFalse(os.path.lexists(self.count))
        self.assertFalse(os.path.lexists(OUTSIDE))

    def test_the_command_line_refuses_a_project_outside_and_names_the_setting(self):
        for command in ("start", "prune"):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(command=command), mock.patch.dict(os.environ, {board.SESSION_VARIABLE: SESSION}), \
                    redirect_stdout(out), redirect_stderr(err):
                os.environ.pop(ALLOW_ANY_PATH, None)
                code = board.main([command, "--project", OUTSIDE])
            self.assertEqual((code, out.getvalue()), (1, ""))
            self.assertIn(ALLOW_ANY_PATH, err.getvalue())
        self.assertFalse(os.path.lexists(OUTSIDE))


if __name__ == "__main__":
    unittest.main()
