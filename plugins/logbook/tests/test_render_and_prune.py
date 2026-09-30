"""Tests for a render that must not leave an older state behind, an index a killed start left without its
board, and a building folder a killed start left without its marker.

Run: python3 -m unittest discover -s plugins/logbook/tests

No test here runs git. Every folder is made inside the temporary directory `Workspace` creates and removes.
"""
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402
from test_board import NOW, SESSION, Workspace, at  # noqa: E402

# Twice the hour after which a building folder without its marker counts as abandoned.
OLD = 2 * 60 * 60


class RenderLeavesTheNewestState(Workspace):
    def test_a_writer_that_appends_before_every_pass_keeps_its_newer_state(self):
        """Before each time the render would replace `state.js`, another writer appends an event and renders
        whole. The render must never replace that writer's newer state with the one it read earlier."""
        folder = self.start()
        real = board.write_atomic
        appended, inside = [0], [False]

        def write(path, text, **kwargs):
            if os.path.basename(path) == board.STATE_FILE and not inside[0]:
                inside[0] = True
                try:
                    appended[0] += 1
                    n = appended[0]
                    board.append(folder, "step", at(n), id=str(n), subject=f"step {n}")
                    board.render(folder, at(n), env={}, template=self.template)
                finally:
                    inside[0] = False
            return real(path, text, **kwargs)

        with mock.patch.object(board, "write_atomic", side_effect=write):
            board.render(folder, at(0), env={}, template=self.template)
        self.assertEqual(appended[0], board.RENDER_PASSES)
        shown = [step["id"] for step in board.read_state(folder)["steps"]]
        self.assertEqual(shown, [str(n) for n in range(1, board.RENDER_PASSES + 1)])

    def test_a_write_the_check_declines_leaves_the_file_and_no_temporary_one(self):
        path = self.write(os.path.join(self.tmp.name, board.STATE_FILE), "before\n")
        self.assertFalse(board.write_atomic(path, "after\n", unless=lambda: True))
        self.assertEqual(self.read(path), "before\n")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), sorted([board.STATE_FILE, "project", "template.html"]))
        self.assertTrue(board.write_atomic(path, "after\n", unless=lambda: False))
        self.assertEqual(self.read(path), "after\n")


class RenderPutsItsBoardInTheIndex(Workspace):
    def index(self, folder):
        return os.path.join(board.boards_of(folder), board.INDEX_FILE)

    def test_an_index_a_killed_start_left_without_the_board_gets_it_back(self):
        folder = self.start()
        board.render(folder, at(1), env={}, template=self.template)
        before = board.read_state(folder)
        board.write_atomic(self.index(folder), board.index_page([], "system"))
        self.assertNotIn(f'href="{SESSION}/', self.read(self.index(folder)))
        board.render(folder, at(1), env={}, template=self.template)
        after = board.read_state(folder)
        self.assertEqual((before["title"], before["state"]), (after["title"], after["state"]))
        self.assertIn(f'href="{SESSION}/{board.BOARD_FILE}"', self.read(self.index(folder)))

    def test_an_index_that_lists_the_board_is_not_rewritten(self):
        folder = self.start()
        board.render(folder, at(1), env={}, template=self.template)
        listed = self.read(self.index(folder)) + "<!-- left alone -->\n"
        board.write_atomic(self.index(folder), listed)
        board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(self.read(self.index(folder)), listed)

    def test_an_index_that_cannot_be_read_does_not_list_the_board(self):
        folder = self.start()
        os.unlink(self.index(folder))
        self.assertFalse(board.index_lists(folder))


class PruneTakesAbandonedStarts(Workspace):
    def setUp(self):
        super().setUp()
        self.boards = os.path.join(self.project, board.BOARDS_DIR)
        os.mkdir(self.boards)

    def building(self, age_seconds, files=()):
        """A folder named as a start builds one, holding `files`, last modified `age_seconds` before NOW."""
        folder = os.path.join(self.boards, f"{SESSION}{board.STARTING}123-abcdef01")
        os.mkdir(folder)
        for name in files:
            self.write(os.path.join(folder, name), "{}\n")
        self.age(folder, age_seconds)
        return folder

    def age(self, path, seconds):
        t = NOW.timestamp() - seconds
        os.utime(path, (t, t), follow_symlinks=False)

    def prune(self):
        return board.prune(self.project, NOW, env={})

    def test_an_old_empty_one_goes(self):
        folder = self.building(OLD)
        self.assertEqual(self.prune(), [folder])
        self.assertFalse(os.path.lexists(folder))

    def test_an_old_one_holding_only_its_log_goes(self):
        folder = self.building(OLD, [board.EVENTS_FILE])
        self.assertEqual(self.prune(), [folder])
        self.assertFalse(os.path.lexists(folder))

    def test_an_old_one_holding_a_stranger_file_stays_with_it(self):
        folder = self.building(OLD, [board.EVENTS_FILE, "notes.txt"])
        self.assertEqual(self.prune(), [])
        self.assertEqual(sorted(os.listdir(folder)), [board.EVENTS_FILE, "notes.txt"])

    def test_an_old_one_holding_a_link_named_as_a_board_file_stays(self):
        target = self.write(os.path.join(self.tmp.name, "elsewhere"), "kept\n")
        folder = os.path.join(self.boards, f"{SESSION}{board.STARTING}123-abcdef01")
        os.mkdir(folder)
        os.symlink(target, os.path.join(folder, board.EVENTS_FILE))
        self.age(folder, OLD)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.islink(os.path.join(folder, board.EVENTS_FILE)))
        self.assertEqual(self.read(target), "kept\n")

    def test_an_old_one_whose_marker_is_not_a_file_stays(self):
        folder = os.path.join(self.boards, f"{SESSION}{board.STARTING}123-abcdef01")
        os.mkdir(folder)
        os.mkdir(os.path.join(folder, board.MARKER_FILE))
        self.age(folder, OLD)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.isdir(os.path.join(folder, board.MARKER_FILE)))

    def test_an_old_link_named_as_one_stays(self):
        target = os.path.join(self.tmp.name, "empty")
        os.mkdir(target)
        self.age(target, OLD)
        link = os.path.join(self.boards, f"{SESSION}{board.STARTING}123-abcdef01")
        os.symlink(target, link)
        self.age(link, OLD)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.islink(link) and os.path.isdir(target))

    def test_a_fresh_one_stays(self):
        folder = self.building(60)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.isdir(folder))

    def test_an_old_one_with_its_marker_follows_the_retention(self):
        folder = self.building(OLD, [board.MARKER_FILE])
        self.age(os.path.join(folder, board.MARKER_FILE), 60)
        self.age(folder, OLD)
        self.assertEqual(self.prune(), [])
        self.assertTrue(os.path.isdir(folder))
        retention = timedelta(days=board.DEFAULT_RETENTION_DAYS + 1).total_seconds()
        self.age(os.path.join(folder, board.MARKER_FILE), retention)
        self.assertEqual(self.prune(), [folder])
        self.assertFalse(os.path.lexists(folder))


if __name__ == "__main__":
    unittest.main()
