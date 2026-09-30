"""Tests for a shown commit that git no longer finds on any local branch: rewritten by an amend or a
rebase, or left behind by a branch a parallel session made and then abandoned.

Run: python3 -m unittest discover -s plugins/logbook/tests

Git runs only through the `git` helper and `make_repository` of `test_board`, which clear git's
environment first, so a suite run from inside a git hook still works on its own repository.
"""
import subprocess
import sys
import os
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402
from test_board import Workspace, at, git  # noqa: E402


class KeptCommits(Workspace):
    def render(self, folder, minutes):
        return board.render(folder, at(minutes), env={}, template=self.template)

    def hashes(self, state):
        return [item["hash"] for item in state["commits"]]

    def test_amending_a_shown_commit_drops_the_original(self):
        self.make_repository()
        folder = self.start()
        work = self.commit("work")
        self.assertEqual(self.hashes(self.render(folder, 1)), [work])

        git(self.project, "commit", "--amend", "--allow-empty", "-q", "-m", "work, reworded")
        amended = git(self.project, "rev-parse", "--short", "HEAD")

        shown = self.hashes(self.render(folder, 2))
        self.assertIn(amended, shown)
        self.assertNotIn(work, shown)

    def test_merged_and_deleted_branch_still_shows_its_commit(self):
        self.make_repository()
        self.commit("on main before the board")
        git(self.project, "checkout", "-q", "-b", "feat")
        folder = self.start()
        work = self.commit("the work")
        self.assertEqual(self.hashes(self.render(folder, 1)), [work])

        git(self.project, "checkout", "-q", "main")
        git(self.project, "merge", "-q", "--no-ff", "-m", "Merge the work", "feat")
        git(self.project, "branch", "-q", "-d", "feat")

        self.assertIn(work, self.hashes(self.render(folder, 2)))

    def test_a_branch_made_after_the_start_loses_its_commit_once_deleted_unmerged(self):
        self.make_repository()
        self.commit("on main before the board")
        folder = self.start()
        git(self.project, "checkout", "-q", "-b", "temp")
        work = self.commit("temp work")
        self.assertEqual(self.hashes(self.render(folder, 1)), [work])

        git(self.project, "checkout", "-q", "main")
        git(self.project, "branch", "-q", "-D", "temp")

        self.assertNotIn(work, self.hashes(self.render(folder, 2)))

    def test_a_reachability_failure_keeps_the_commit(self):
        self.make_repository()
        self.commit("on main before the board")
        folder = self.start()
        git(self.project, "checkout", "-q", "-b", "temp")
        work = self.commit("temp work")
        self.assertEqual(self.hashes(self.render(folder, 1)), [work])

        git(self.project, "checkout", "-q", "main")
        git(self.project, "branch", "-q", "-D", "temp")

        real_run = subprocess.run

        def flaky(args, **kwargs):
            if "--contains" in args:
                raise subprocess.TimeoutExpired(args, board.GIT_TIMEOUT_SECONDS)
            return real_run(args, **kwargs)

        with mock.patch("board.subprocess.run", side_effect=flaky):
            shown = self.hashes(self.render(folder, 2))
        self.assertIn(work, shown)


if __name__ == "__main__":
    unittest.main()
