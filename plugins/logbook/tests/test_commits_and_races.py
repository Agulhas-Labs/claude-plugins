"""Tests for commits that outlive their branch, heads git could not read, renders that race, and a start
cut short.

Run: python3 -m unittest discover -s plugins/logbook/tests

Git runs only through the `git` helper and `make_repository` of `test_board`, which clear git's
environment first, so a suite run from inside a git hook still works on its own repository.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402
from test_board import NOW, SESSION, Workspace, at, git  # noqa: E402


def timing_out(*args, **kwargs):
    raise subprocess.TimeoutExpired("git", board.GIT_TIMEOUT_SECONDS)


class ReviewKeptCommits(Workspace):
    def render(self, folder, minutes):
        return board.render(folder, at(minutes), env={}, template=self.template)

    def hashes(self, state):
        return [item["hash"] for item in state["commits"]]

    def test_work_merged_into_a_recorded_branch_stays_after_its_branch_is_deleted(self):
        self.make_repository()
        before = self.commit("on main before the board")
        git(self.project, "checkout", "-q", "-b", "feat")
        folder = self.start()
        self.assertIs(board.events(folder)[0]["headsRead"], True)
        work = self.commit("the work")
        self.assertEqual(self.hashes(self.render(folder, 1)), [work])
        git(self.project, "checkout", "-q", "main")
        git(self.project, "merge", "-q", "--no-ff", "-m", "Merge the work", "feat")
        merge = git(self.project, "rev-parse", "--short", "HEAD")
        git(self.project, "branch", "-q", "-d", "feat")

        self.render(folder, 2)
        shown = self.hashes(board.read_state(folder))
        self.assertIn(work, shown)
        self.assertIn(merge, shown)
        self.assertNotIn(before, shown)

    def test_a_commit_shown_once_keeps_its_branch_after_the_branch_is_gone(self):
        self.make_repository()
        self.commit("on main before the board")
        git(self.project, "checkout", "-q", "-b", "feat")
        folder = self.start()
        work = self.commit("the work")
        self.render(folder, 1)
        git(self.project, "checkout", "-q", "main")
        git(self.project, "branch", "-q", "-D", "feat")

        state = self.render(folder, 2)
        self.assertEqual([(c["hash"], c["subject"], c["branch"]) for c in state["commits"]], [
            (work, "the work", "feat"),
        ])

    def test_a_missing_or_unreadable_previous_state_gives_no_error(self):
        self.make_repository()
        self.commit("before")
        folder = self.start()
        self.commit("after")
        os.remove(os.path.join(folder, board.STATE_FILE))
        self.assertEqual([c["subject"] for c in self.render(folder, 1)["commits"]], ["after"])
        self.write(os.path.join(folder, board.STATE_FILE), 'window.BOARD = {"commits": [1, {"hash": "zz"}, null]};\n')
        self.assertEqual([c["subject"] for c in self.render(folder, 2)["commits"]], ["after"])
        self.write(os.path.join(folder, board.STATE_FILE), "not a state at all")
        self.assertEqual([c["subject"] for c in self.render(folder, 3)["commits"]], ["after"])

    def test_a_longer_abbreviation_of_a_shown_hash_is_the_same_commit(self):
        shown = [{"hash": "a1b2c3d", "subject": "s", "branch": "feat", "time": "2026-01-05T09:00:00Z", "step": None}]
        found = [{"hash": "a1b2c3d4", "subject": "s", "branch": "main", "time": "2026-01-05T09:00:00Z", "step": None}]
        self.assertEqual(board.with_shown(found, board.shown_before({"commits": shown})), found)

    def test_kept_and_new_commits_are_ordered_by_time_newest_last(self):
        early = {"hash": "aaaaaaa", "subject": "early", "branch": "feat", "time": "2026-01-05T09:00:00Z", "step": None}
        late = {"hash": "bbbbbbb", "subject": "late", "branch": "main", "time": "2026-01-05T09:05:00Z", "step": None}
        self.assertEqual(board.with_shown([late], [early]), [early, late])


class ReviewUnreadHeads(Workspace):
    def test_heads_not_read_at_the_start_show_no_commits(self):
        self.make_repository()
        for subject in ("one", "two", "three"):
            self.commit(subject)
        self.assertEqual(board.commits(self.project, {"branch": "main", "heads": {}, "headsRead": False}), [])
        self.assertEqual(board.commits(self.project, {"branch": "main", "heads": {}}), [])
        self.assertEqual(len(board.commits(self.project, {"branch": "main", "heads": {}, "headsRead": True})), 3)

    def test_a_start_that_cannot_read_the_heads_records_so_and_shows_no_history(self):
        self.make_repository()
        self.commit("history")
        with mock.patch.object(board.subprocess, "run", side_effect=timing_out):
            folder = self.start()
        first = board.events(folder)[0]
        self.assertEqual((first["heads"], first["headsRead"]), ({}, False))
        self.commit("after")
        self.assertEqual(board.render(folder, at(1), env={}, template=self.template)["commits"], [])

    def test_a_failed_git_read_at_render_keeps_the_commits_already_shown(self):
        self.make_repository()
        self.commit("before")
        folder = self.start()
        shown = self.commit("shown")
        board.render(folder, at(1), env={}, template=self.template)
        self.commit("later")
        with mock.patch.object(board.subprocess, "run", side_effect=timing_out):
            state = board.render(folder, at(2), env={}, template=self.template)
        self.assertEqual([c["hash"] for c in state["commits"]], [shown])

    def test_a_failed_read_of_the_recorded_heads_adds_nothing(self):
        self.make_repository()
        self.commit("before")
        folder = self.start()
        self.commit("after")
        real = subprocess.run

        def run(args, **kwargs):
            if "cat-file" in args:
                timing_out()
            return real(args, **kwargs)

        with mock.patch.object(board.subprocess, "run", side_effect=run):
            state = board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(state["commits"], [])


class ReviewRenderRace(Workspace):
    def writing_state(self, on_write):
        """`write_atomic`, calling `on_write(n)` just before the n-th `state.js` replaces the last."""
        real = board.write_atomic
        count = [0]

        def write(path, text, **kwargs):
            if os.path.basename(path) == board.STATE_FILE:
                count[0] += 1
                on_write(count[0])
            return real(path, text, **kwargs)

        return mock.patch.object(board, "write_atomic", side_effect=write), count

    def test_an_event_appended_while_rendering_is_in_the_state_left_behind(self):
        folder = self.start()

        def on_write(n):
            if n == 1:
                board.append(folder, "turn-end", at(2))

        patch, count = self.writing_state(on_write)
        with patch:
            board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(board.read_state(folder)["state"], "idle")
        self.assertEqual(count[0], 2)

    def test_a_log_that_grows_on_every_pass_stops_after_three(self):
        folder = self.start()
        patch, count = self.writing_state(lambda n: board.append(folder, "step", at(n), id=str(n), subject="s"))
        with patch:
            board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(count[0], 3)

    def test_a_board_closed_during_the_last_pass_is_left_finished(self):
        folder = self.start()

        def on_write(n):
            if n < 3:
                board.append(folder, "step", at(n), id=str(n), subject="s")
            elif n == 3:
                board.append(folder, "close", at(n))

        patch, _ = self.writing_state(on_write)
        with patch:
            board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(board.read_state(folder)["state"], "finished")
        self.assertEqual(board.board_row(board.boards_of(folder), SESSION)["state"], "finished")


class ReviewStartRepair(Workspace):
    def test_a_marker_without_a_start_event_gets_one_from_the_next_start(self):
        folder = board.board_dir(self.project, SESSION)
        os.makedirs(folder)
        self.write(os.path.join(folder, board.MARKER_FILE), SESSION)

        self.assertEqual(self.start("Repaired"), folder)
        log = board.events(folder)
        self.assertEqual([e["kind"] for e in log], ["start"])
        self.assertEqual((log[0]["title"], log[0]["project"]), ("Repaired", self.project))
        state = board.read_state(folder)
        self.assertEqual((state["title"], state["started"]), ("Repaired", board.utc(NOW)))

        self.start("Again")
        self.assertEqual(board.events(folder), log)

    def test_the_heads_are_read_before_the_marker_is_made(self):
        marker = os.path.join(board.board_dir(self.project, SESSION), board.MARKER_FILE)
        real, seen = board.branch_heads, []

        def heads(project):
            seen.append(os.path.exists(marker))
            return real(project)

        with mock.patch.object(board, "branch_heads", side_effect=heads):
            self.start()
        self.assertFalse(seen[0])


if __name__ == "__main__":
    unittest.main()
