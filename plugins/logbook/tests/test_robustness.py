"""Tests for four robustness findings in the recorder: a lone surrogate that would otherwise stop a
board rendering for good, a `close` on an already-closed board, a torn or short write to the event
log, and pruning's recognition of `write_atomic`'s own leftovers.

Run: python3 -m unittest discover -s plugins/logbook/tests
"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_board import SESSION, Workspace, at

import board  # noqa: E402  (test_board puts the board folder on the path)


class Surrogates(Workspace):
    def test_an_event_holding_a_lone_surrogate_still_renders(self):
        folder = self.start()
        board.append(folder, "decision", at(1), text="a\ud83db")
        state = board.render(folder, at(2), env={}, template=self.template)
        board.close(folder, at(3), env={}, template=self.template)
        self.assertEqual(state["decisions"][0]["text"], "a�b")
        for name in (board.STATE_FILE, board.REPORT_FILE):
            with open(os.path.join(folder, name), "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")  # raises if any byte sequence is not valid UTF-8
            self.assertIn("�", text)
            self.assertNotIn("\ud83d", text)

    def test_a_surrogate_from_argv_is_recorded_and_prints_its_one_line(self):
        folder = self.start()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template), \
             mock.patch.object(board, "clock", lambda: at(1)), \
             redirect_stdout(out), redirect_stderr(err):
            code = board.main(["decision", "a\udcffb", "--board", folder])
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertEqual(board.events(folder)[-1]["text"], "a�b")

    def test_a_hand_written_split_escape_heals_on_read_and_renders(self):
        folder = self.start()
        with open(os.path.join(folder, board.EVENTS_FILE), "a", encoding="utf-8") as f:
            f.write('{"t": "%s", "kind": "decision", "text": "a\\ud83db"}\n' % board.utc(at(1)))
        state = board.render(folder, at(2), env={}, template=self.template)
        self.assertEqual(state["decisions"][0]["text"], "a�b")
        with open(os.path.join(folder, board.STATE_FILE), "rb") as f:
            f.read().decode("utf-8")  # raises if any byte sequence is not valid UTF-8


class SecondClose(Workspace):
    def test_a_second_close_replaces_a_missing_report(self):
        folder = self.start()
        board.close(folder, at(1), env={}, template=self.template)
        report = os.path.join(folder, board.REPORT_FILE)
        os.remove(report)
        board.close(folder, at(2), env={}, template=self.template)
        self.assertTrue(os.path.isfile(report))


class TornLog(Workspace):
    def test_a_torn_last_line_loses_only_itself(self):
        folder = self.start()
        path = os.path.join(folder, board.EVENTS_FILE)
        with open(path, "ab") as f:
            f.write(b'{"t": "2026-01-05T09:00:01Z", "kind": "decision", "text": "torn"')  # no closing brace, no newline
        board.append(folder, "decision", at(2), text="whole")
        events = board.events(folder)
        self.assertEqual([e.get("text") for e in events if e.get("kind") == "decision"], ["whole"])
        with open(path, "rb") as f:
            raw = f.read()
        lines = raw.split(b"\n")
        torn = lines[1]  # lines[0] is the board's own `start` event
        self.assertTrue(torn.startswith(b'{"t": "2026-01-05T09:00:01Z"'))
        self.assertNotIn(b"whole", torn)

    def test_a_short_write_still_leaves_one_complete_line(self):
        folder = self.start()
        real_write = os.write
        calls = []

        def flaky(fd, data):
            if not calls:
                calls.append(1)
                n = min(4, len(data))
                return real_write(fd, data[:n])
            return real_write(fd, data)

        with mock.patch.object(board.os, "write", side_effect=flaky):
            board.append(folder, "decision", at(1), text="whole line")
        events = [e for e in board.events(folder) if e.get("kind") == "decision"]
        self.assertEqual(events[-1]["text"], "whole line")


class PruneLeftover(Workspace):
    def test_a_write_atomic_leftover_is_pruned(self):
        # A bare board folder (no events), so last_activity falls back to the marker file's own
        # mtime, which is set below to fall outside the retention window.
        folder = board.board_dir(self.project, SESSION)
        os.makedirs(folder)
        with open(os.path.join(folder, board.MARKER_FILE), "w", encoding="utf-8") as f:
            f.write(SESSION)
        state_path = os.path.join(folder, board.STATE_FILE)
        fd, leftover = tempfile.mkstemp(**board.temp_mkstemp_kwargs(state_path))
        os.close(fd)
        self.assertRegex(os.path.basename(leftover), board.TEMPORARY_FILE)
        old = at(-60 * 24 * 60)  # far outside the retention window
        os.utime(os.path.join(folder, board.MARKER_FILE), (old.timestamp(), old.timestamp()))
        removed = board.prune(self.project, at(0), env={})
        self.assertEqual(removed, [folder])
        self.assertFalse(os.path.exists(folder))


if __name__ == "__main__":
    unittest.main()
