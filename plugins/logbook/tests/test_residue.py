"""Tests for three small gaps left behind: calls caught up on `Stop` landing after that turn's
`turn-end`, an unlisted board's render rewriting the index every time, and a count file the gate wrote
before a board existed outliving that board's start.

Run: python3 -m unittest discover -s plugins/logbook/tests
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))

from test_board import NOW, Workspace  # noqa: E402
from test_turn_end import TurnEnd, stop  # noqa: E402
from test_work_recording import SESSION, OpenBoard, fixture  # noqa: E402

import board  # noqa: E402


class CalledUpOnStop(TurnEnd):
    """What `catch_up` recovers on `Stop` must land before that `Stop`'s own `turn-end`, and count
    towards the turn it ends."""

    def setUp(self):
        super().setUp()
        # As `TheEndOfATurnOnATurn` does: nothing is on disk yet but the prompt, so the board's own
        # start reads nothing a test did not send.
        self.write_prompt_only()

    def test_calls_caught_up_on_stop_land_before_turn_end_and_the_turn_ends_with_the_page(self):
        self.start_with_subagent()
        # The host flushes the Write, Read and Edit calls to disk only once `Stop` arrives; none of
        # their hooks ran, so only catch-up can put the `change` they made on the board.
        self.work_transcript(9)

        output = self.hook(stop())

        self.assertEqual(output, self.page())
        kinds = self.kinds()
        self.assertIn("change", kinds)
        self.assertLess(kinds.index("change"), kinds.index("turn-end"))

        # Nothing new: the following Stop, with no fresh work, says nothing.
        self.assertIsNone(self.hook(stop()))


class AnUnlistedBoardsIndex(Workspace):
    """A board `board_rows` leaves out of the index (here, one whose marker is gone) must not have its
    render rewrite an index that already exists."""

    def test_rendering_a_board_with_no_marker_does_not_rewrite_an_index_that_leaves_it_out(self):
        folder = self.start()
        index = os.path.join(self.project, ".logbook", "index.html")
        # An index that does not list this board: `index_lists` alone would have `render` rewrite it
        # on every pass, whether or not `board_rows` would ever put it there.
        board.write_atomic(index, board.index_page([], "system"))
        os.unlink(os.path.join(folder, board.MARKER_FILE))
        past = os.stat(index).st_mtime - 3600
        os.utime(index, (past, past))

        board.render(folder, NOW, env={}, template=self.template)

        self.assertEqual(os.stat(index).st_mtime, past)


class ACountFileOnAnOpenBoard(OpenBoard):
    """A count file the gate wrote before this session had a board must not outlive its start."""

    def test_a_handled_event_on_an_open_board_leaves_no_count_file(self):
        self.env["CLAUDE_PLUGIN_DATA"] = os.path.join(self.tmp, "data")
        path = board.calls_file(self.env, SESSION)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("cccccccccc")

        self.hook(fixture("PostToolUse-Write"))

        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
