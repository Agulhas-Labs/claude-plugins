"""Tests for the commits a session made before its board started: read back from the transcript as the
hash its own `git commit` printed, and shown with what git says of them.

Run: python3 -m unittest discover -s plugins/logbook/tests

Each test makes a real repository inside its own temporary directory, through the `git` helper and
`make_repository` of `test_board`, which clear every `GIT_*` variable first and check the repository's
git directory is inside the temporary directory before anything is written to it. The board starts
through `board_hook.handle`, in-process, from a transcript written beside the project.
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PLUGIN, "board"))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))

import board  # noqa: E402
import board_hook  # noqa: E402
from test_board import NOW, SESSION, Workspace, at, git  # noqa: E402

PAYLOADS = os.path.join(HERE, "fixtures", "payloads")
PROMPT = {"type": "user", "message": {"role": "user", "content": "Make the change"}, "isSidechain": False}


def fixture(name):
    with open(os.path.join(PAYLOADS, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def call_lines(use, command, stdout="", failed=False, background=False):
    """The two transcript lines of one `Bash` call: its tool use, then its result."""
    tool_input = {"command": command, "description": "Run it"}
    if background:
        tool_input["run_in_background"] = True
    asked = {
        "type": "assistant", "isSidechain": False, "timestamp": "2026-01-05T08:59:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "id": use, "name": "Bash", "input": tool_input}]},
    }
    if failed:
        result, content = "Error: Exit code 1\n" + stdout, "Exit code 1\n" + stdout
    else:
        result = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
        content = stdout
    answered = {
        "type": "user", "isSidechain": False, "timestamp": "2026-01-05T08:59:01.000Z", "toolUseResult": result,
        "message": {"role": "user", "content": [
            {"tool_use_id": use, "type": "tool_result", "content": content, "is_error": failed},
        ]},
    }
    return [asked, answered]


class EarlyCommits(Workspace):
    """A repository in the project, and a transcript of the session beside it."""

    def setUp(self):
        super().setUp()
        self.project = os.path.realpath(self.project)
        self.transcript = os.path.join(self.tmp.name, "transcript.jsonl")
        self.env = {"CLAUDE_PROJECT_DIR": self.project, "CLAUDE_PLUGIN_ROOT": PLUGIN}
        self.folder = board.board_dir(self.project, SESSION)
        self.lines = [PROMPT]
        self.write_transcript()

    def write_transcript(self):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(line) + "\n" for line in self.lines)

    def ran(self, use, command, stdout="", **kinds):
        """Add one `Bash` call to the transcript."""
        self.lines += call_lines(use, command, stdout, **kinds)
        self.write_transcript()

    def session_commit(self, subject, use):
        """A commit the session makes: git's own output of it goes into the transcript. Returns its hash."""
        printed = git(self.project, "commit", "--allow-empty", "-m", subject)
        self.ran(use, f'git commit -m "{subject}"', printed + "\n 0 files changed\n")
        return git(self.project, "rev-parse", "--short", "HEAD")

    def hook(self, payload):
        payload = copy.deepcopy(payload)
        payload.update(session_id=SESSION, cwd=self.project, transcript_path=self.transcript)
        return board_hook.handle(payload, self.env, NOW)

    def start(self):
        """Start the board as the first subagent does, which starts one at any count of calls."""
        self.assertIsNone(self.hook(fixture("SubagentStart")), "a subagent start carries no context and prints nothing")
        self.assertTrue(board.is_board(self.folder))
        return self.folder

    def state(self):
        return board.current_state(self.folder, at(1), self.env)

    def shown(self):
        return [(c["hash"], c["subject"], c["branch"]) for c in self.state()["commits"]]

    def logged(self, kind="commit"):
        return [event for event in board.events(self.folder) if event["kind"] == kind]

    def committed_at(self, sha):
        seconds = int(git(self.project, "log", "-1", "--format=%ct", sha))
        return board.utc(datetime.fromtimestamp(seconds, timezone.utc))


class ShownFromTheTranscript(EarlyCommits):

    def test_a_commit_the_session_made_before_the_board_is_shown_with_gits_subject_and_time(self):
        self.make_repository()
        sha = self.session_commit("The session's first commit", "toolu_commit")
        self.start()

        self.assertEqual(self.state()["commits"], [{
            "hash": sha, "subject": "The session's first commit", "branch": "main",
            "time": self.committed_at(sha), "step": None,
        }])
        self.assertEqual(
            [(e["hash"], e["branch"], e["use"], e["early"]) for e in self.logged()],
            [(sha, "main", "toolu_commit", True)],
        )

    def test_the_subject_comes_from_git_and_not_from_the_transcripts_text(self):
        self.make_repository()
        git(self.project, "commit", "--allow-empty", "-m", "What git holds")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        self.ran("toolu_commit", "git commit -m x", f"[main {sha}] What the transcript says\n")
        self.start()
        self.assertEqual(self.shown(), [(sha, "What git holds", "main")])

    def test_a_commit_made_by_someone_else_before_the_start_is_not_shown(self):
        self.make_repository()
        self.commit("someone else's, before the session")
        mine = self.session_commit("the session's", "toolu_commit")
        self.commit("someone else's, after the session's")
        self.start()
        self.assertEqual(self.shown(), [(mine, "the session's", "main")])

    def test_a_commit_after_the_start_that_the_transcript_also_names_is_shown_once(self):
        self.make_repository()
        self.commit("history")
        self.start()
        sha = self.session_commit("after the start", "toolu_after")
        # The transcript names it by a longer abbreviation than git's own: still the same commit.
        full = git(self.project, "rev-parse", "HEAD")
        self.lines[-1]["toolUseResult"]["stdout"] = f"[main {full[:12]}] after the start\n"
        self.write_transcript()
        self.hook(fixture("Stop"))

        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(self.shown(), [(sha, "after the start", "main")])

    def test_the_root_commit_a_slashed_branch_and_a_detached_head(self):
        self.make_repository()
        root = self.session_commit("the root", "toolu_root")
        git(self.project, "checkout", "-q", "-b", "feat/early.fix-1")
        slashed = self.session_commit("on a slashed branch", "toolu_slashed")
        git(self.project, "checkout", "-q", "--detach")
        detached = self.session_commit("on a detached head", "toolu_detached")
        git(self.project, "branch", "kept-detached", "HEAD")
        self.assertIn("(root-commit)", self.lines[2]["toolUseResult"]["stdout"])
        self.assertTrue(self.lines[-1]["toolUseResult"]["stdout"].startswith("[detached HEAD "))
        self.start()

        events = {e["use"]: e for e in self.logged()}
        self.assertEqual({use: (e["hash"], e.get("branch")) for use, e in events.items()}, {
            "toolu_root": (root, "main"), "toolu_slashed": (slashed, "feat/early.fix-1"),
            "toolu_detached": (detached, None),
        })
        self.assertNotIn("branch", events["toolu_detached"])
        self.assertEqual(sorted(self.shown()), sorted([
            (root, "the root", "main"), (slashed, "on a slashed branch", "feat/early.fix-1"),
            (detached, "on a detached head", None),
        ]))

    def test_a_commit_line_that_reaches_the_transcript_after_the_start_is_recorded_once(self):
        self.make_repository()
        printed = git(self.project, "commit", "--allow-empty", "-m", "made before the start")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        self.start()
        self.assertEqual((self.logged(), self.shown()), ([], []))

        self.ran("toolu_late", 'git commit -m "made before the start"', printed + "\n")
        self.hook(fixture("Stop"))
        self.hook(fixture("Stop"))

        self.assertEqual([(e["hash"], e["use"]) for e in self.logged()], [(sha, "toolu_late")])
        self.assertEqual(self.shown(), [(sha, "made before the start", "main")])

    def test_a_commit_read_back_does_not_stop_a_later_catch_up(self):
        # The catch-up stops at the first call a hook recorded; a `commit` read back is not one.
        self.make_repository()
        self.start()
        self.ran("toolu_commit", "git commit -m x", "[main abc1234] x\n")
        self.hook(fixture("Stop"))
        self.ran("toolu_later", "echo later", "later\n")
        self.hook(fixture("Stop"))
        self.assertEqual([e["use"] for e in self.logged("command")], ["toolu_commit", "toolu_later"])


def commit_call(use, command, stdout):
    """The `PostToolUse` of a `Bash` call that ran `command` and printed `stdout`."""
    payload = fixture("PostToolUse-Bash")
    payload["tool_input"] = {"command": command, "description": "Commit it"}
    payload["tool_response"]["stdout"] = stdout
    payload["tool_use_id"] = use
    return payload


class TheCallThatStartsTheBoard(EarlyCommits):
    """Its hook runs after its command, so its commit is behind the heads the start records, and the
    transcript is read without it."""

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(self.data, "calls"))
        self.env["CLAUDE_PLUGIN_DATA"] = self.data
        with open(os.path.join(self.data, "calls", SESSION), "w", encoding="utf-8") as f:
            f.write("x" * 10)

    def test_a_commit_made_by_the_call_that_starts_the_board_is_shown(self):
        self.make_repository()
        printed = git(self.project, "commit", "--allow-empty", "-m", "Made by the tenth call")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        self.assertIsNotNone(self.hook(commit_call("toolu_tenth", 'git commit -m "Made by the tenth call"', printed)))
        self.assertTrue(board.is_board(self.folder))
        self.assertEqual(self.shown(), [(sha, "Made by the tenth call", "main")])
        self.assertEqual([(e["hash"], e["use"], e["early"]) for e in self.logged()], [(sha, "toolu_tenth", True)])

    def test_the_next_calls_commit_is_found_by_git_and_records_no_event(self):
        self.make_repository()
        self.assertIsNotNone(self.hook(commit_call("toolu_tenth", "git status --short", "")))
        printed = git(self.project, "commit", "--allow-empty", "-m", "Made once the board exists")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        self.hook(commit_call("toolu_next", 'git commit -m "Made once the board exists"', printed))
        self.assertEqual(self.logged(), [])
        self.assertEqual(self.shown(), [(sha, "Made once the board exists", "main")])


class ACommandThatFailedAfterItsCommit(EarlyCommits):
    """`git commit && git push`, the push refused: the commit was made, and the command failed."""

    def test_its_commit_is_shown(self):
        self.make_repository()
        printed = git(self.project, "commit", "--allow-empty", "-m", "Made, then the push was refused")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        self.ran("toolu_refused", 'git commit -m "Made" && git push', printed + "\nerror: failed to push\n", failed=True)
        self.start()
        self.assertEqual(self.shown(), [(sha, "Made, then the push was refused", "main")])
        self.assertEqual([(e["hash"], e["use"]) for e in self.logged()], [(sha, "toolu_refused")])

    def test_the_call_that_starts_the_board_shows_its_commit_when_it_failed(self):
        self.make_repository()
        data = os.path.join(self.tmp.name, "data")
        os.makedirs(os.path.join(data, "calls"))
        self.env["CLAUDE_PLUGIN_DATA"] = data
        with open(os.path.join(data, "calls", SESSION), "w", encoding="utf-8") as f:
            f.write("x" * 10)
        printed = git(self.project, "commit", "--allow-empty", "-m", "The tenth call's")
        sha = git(self.project, "rev-parse", "--short", "HEAD")
        payload = fixture("PostToolUseFailure-Bash")
        payload["tool_input"] = {"command": 'git commit -m "The tenth" && git push', "description": "Commit and push"}
        payload["error"] = "Exit code 1\n" + printed + "\nerror: failed to push"
        payload["tool_use_id"] = "toolu_tenth"
        self.assertIsNone(self.hook(payload), "a failed call carries no context and prints nothing")
        self.assertTrue(board.is_board(self.folder))
        self.assertEqual(self.shown(), [(sha, "The tenth call's", "main")])


class ManyAtOnce(EarlyCommits):

    def test_git_is_asked_about_the_latest_ten_a_command_named_and_no_more(self):
        self.make_repository()
        printed, hashes = "", []
        for number in range(1, 15):
            printed += git(self.project, "commit", "--allow-empty", "-m", f"picked {number}") + "\n"
            hashes.append(git(self.project, "rev-parse", "--short", "HEAD"))
        self.ran("toolu_pick", "git cherry-pick one..fourteen", printed)
        self.start()
        self.assertEqual(len(self.logged()), 14)
        with mock.patch.object(board, "on_some_branch", wraps=board.on_some_branch) as asked:
            shown = [c["hash"] for c in self.state()["commits"]]
        self.assertEqual(shown, hashes[-10:])
        # The cap is on what git is asked, not only on what is shown: each hash is a call to git.
        self.assertEqual([len(call.args[1]) for call in asked.call_args_list], [10])


class RecordsNothing(EarlyCommits):

    def read(self):
        _, found = board_hook.read_transcript(self.transcript, set(), env=self.env)
        return found

    def kinds(self):
        return [kind for kind, _ in self.read()]

    def test_a_bracket_line_from_a_command_that_does_not_name_git_records_no_commit(self):
        self.ran("toolu_echo", 'echo "[main abc1234] x"', "[main abc1234] x\n")
        self.ran("toolu_legit", "legit-tool commit", "[main abc1234] x\n")
        self.assertEqual(self.kinds(), ["command", "command"])

    def test_a_command_left_running_records_no_commit(self):
        self.ran("toolu_background", "git commit -m x", "[main abc1234] x\n", background=True)
        self.assertEqual(self.kinds(), ["command"])

    def test_a_command_that_failed_without_naming_a_commit_records_none(self):
        self.ran("toolu_failed", "git commit -m x", "nothing to commit, working tree clean\n", failed=True)
        self.assertEqual(self.kinds(), ["command"])

    def test_a_line_that_only_resembles_gits_records_no_commit(self):
        self.ran("toolu_odd", "git log --oneline", "  [main abc1234] indented\n[main ABC1234] upper\n"
                 "[main abc12] short\n[main abc1234]glued\n[two words abc1234] x\n")
        self.assertEqual(self.kinds(), ["command"])

    def test_a_result_of_an_unexpected_shape_raises_nothing_and_records_no_commit(self):
        for stdout in (None, 7, ["[main abc1234] x"], {"a": 1}):
            self.lines += call_lines("toolu_%d" % len(self.lines), "git commit -m x")
            self.lines[-1]["toolUseResult"]["stdout"] = stdout
        self.lines += call_lines("toolu_string", "git commit -m x")
        self.lines[-1]["toolUseResult"] = "[main abc1234] x"
        self.write_transcript()
        self.assertNotIn("commit", self.kinds())

    def test_the_hooks_own_call_on_an_existing_board_records_no_commit(self):
        self.make_repository()
        self.commit("history")
        self.start()
        printed = git(self.project, "commit", "--allow-empty", "-m", "made on the board")
        payload = fixture("PostToolUse-Bash")
        payload.update(tool_input={"command": "git commit -m x", "description": "Commit"}, tool_use_id="toolu_hooked")
        payload["tool_response"]["stdout"] = printed + "\n"
        self.hook(payload)

        self.assertEqual([e["use"] for e in self.logged("command")], ["toolu_hooked"])
        self.assertEqual(self.logged(), [])
        self.assertEqual([c["subject"] for c in self.state()["commits"]], ["made on the board"])


class ShownOnlyWhenGitConfirms(EarlyCommits):

    def test_a_hash_git_cannot_confirm_as_a_commit_on_a_branch_is_not_shown(self):
        self.make_repository()
        self.commit("history")
        blob = git(self.project, "hash-object", "-w", self.write(os.path.join(self.project, "a-file"), "text\n"))
        amended = self.session_commit("amended away", "toolu_amended")
        git(self.project, "commit", "-q", "--amend", "--allow-empty", "-m", "the amendment")
        self.ran("toolu_nowhere", "git commit -m x", "[main 0123456789abcdef] names nothing here\n")
        self.ran("toolu_blob", "git commit -m x", f"[main {blob[:10]}] a blob, not a commit\n")
        self.start()
        self.assertEqual(len(self.logged()), 3)
        for sha, branch in (("zzzzzzz", "main"), ("ABCDEF1", "main"), ("abc1234; rm -rf x", "main"),
                            (1234567, "main"), (None, None), ("abc1234", ["main"])):
            board.append(self.folder, "commit", NOW, hash=sha, branch=branch, early=True)

        state = self.state()
        self.assertEqual(state["commits"], [])
        self.assertNotIn(amended, [c["hash"] for c in state["commits"]])
        self.assertEqual(board.render(self.folder, at(2), self.env)["commits"], [])

    def test_a_project_that_is_not_a_repository_shows_nothing_and_raises_nothing(self):
        self.ran("toolu_commit", "git commit -m x", "[main abc1234] x\n")
        self.start()
        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(self.state()["commits"], [])

    def test_a_board_without_a_commit_event_asks_git_nothing_more_to_render(self):
        self.make_repository()
        self.commit("history")
        self.ran("toolu_echo", "echo hello", "hello\n")
        self.start()
        self.commit("after the start")
        log = board.events(self.folder)
        first = board.start_event(log)
        before = {"commits": [{"hash": "abcdef1", "subject": "gone", "branch": "old", "time": board.utc(NOW)}]}

        with mock.patch.object(board, "git", wraps=board.git) as counted:
            board.with_shown(board.commits(self.project, first), board.shown_before(before), self.project, "main")
            without = counted.call_count
            counted.reset_mock()
            board.current_state(self.folder, at(1), self.env, before)
            self.assertEqual(counted.call_count, without)


if __name__ == "__main__":
    unittest.main()
