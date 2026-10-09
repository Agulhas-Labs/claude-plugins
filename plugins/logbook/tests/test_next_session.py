"""Tests for next-session mode: its commands, what a session start says about the brief, and `next facts`.

Run: python3 -m unittest discover -s plugins/logbook/tests

The commands run `board.py` as the skill does, as a program, and the session starts run the real
`gate.sh`, both in the environment of `test_hooks.py`, which has every `GIT_*` variable removed. The
Bash tool's environment has no `CLAUDE_PROJECT_DIR`, so the tests of where the setting is found run the
commands without it. The project is not a repository, except where a test makes it one with `git` below
(every `GIT_*` variable removed, the repository checked to be inside the test's folder), with a linked
worktree nested under it and one beside it.
"""
import json
import os
import re
import subprocess
import sys

from test_hooks import PLUGIN, SESSION, Hooks, fixture

import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402  (test_hooks puts the hooks folder on the path)

BOARD_PY = os.path.join(PLUGIN, "board", "board.py")
# A line only the brief holds: the session start must never carry it.
SECRET = "only the brief holds this line"


class NextSession(Hooks):

    def cli(self, *argv, cwd=None, project_variable=True):
        """board.py run as the skill runs it, in the hooks' environment with the session's id set.

        Without `project_variable`, `CLAUDE_PROJECT_DIR` is left out, as it is from the Bash tool's environment.
        """
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SESSION)
        if not project_variable:
            del env["CLAUDE_PROJECT_DIR"]
        done = subprocess.run(
            [sys.executable, BOARD_PY, *argv], cwd=cwd or self.project, env=env, capture_output=True, timeout=60,
        )
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def session_start(self, source="startup", project=None):
        payload = fixture("SessionStart")
        payload.update(source=source, session_id=SESSION)
        return self.hook(payload, env=dict(self.env, CLAUDE_PROJECT_DIR=project) if project else None)

    def git(self, cwd, *args):
        """git in `cwd` and nowhere else: every `GIT_*` variable a git hook exports is removed first."""
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()

    def make_repository(self):
        """The project as a repository with one commit, and two linked worktrees: one nested under the
        project, where a session's isolation worktree goes, and one beside it. Returns the worktrees."""
        self.git(self.project, "init", "-q")
        inside = os.path.realpath(self.git(self.project, "rev-parse", "--absolute-git-dir"))
        self.assertTrue(inside.startswith(self.tmp + os.sep), inside)
        hooks = os.path.join(self.tmp, "no-hooks")
        os.mkdir(hooks)
        for key, value in (
            ("user.name", "Test"), ("user.email", "test@example.com"), ("core.hooksPath", hooks),
            ("commit.gpgsign", "false"),
        ):
            self.git(self.project, "config", key, value)
        self.git(self.project, "commit", "-q", "--allow-empty", "-m", "first")
        nested = os.path.join(self.project, ".claude", "worktrees", "x")
        beside = os.path.join(self.tmp, "beside")
        self.git(self.project, "worktree", "add", "-q", "-b", "x", nested)
        self.git(self.project, "worktree", "add", "-q", "-b", "y", beside)
        return nested, beside

    def status_line(self, name="NEXT_SESSION.md"):
        return f"next-session mode on\nbrief: {self.brief(name)} (not written yet)\n"

    def brief(self, name="NEXT_SESSION.md"):
        return os.path.join(self.project, name)

    def setting(self):
        return os.path.join(self.project, ".logbook", "next-session.json")

    def write_brief(self, lines, name="NEXT_SESSION.md"):
        path = self.brief(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
        return path

    # -----------------------------------------------------------------------------------------------
    # on, off, status

    def test_on_defaults_to_next_session_md_and_says_it_is_not_written_yet(self):
        self.assertEqual(
            self.cli("next", "on"),
            (0, f"next-session mode on\nbrief: {self.brief()} (not written yet)\n", ""),
        )
        with open(self.setting(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"path": "NEXT_SESSION.md"})
        with open(os.path.join(self.project, ".logbook", ".gitignore"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "*\n")
        self.assertFalse(os.path.exists(self.brief()), "turning the mode on wrote the brief")

    def test_on_with_a_path_names_it_relative_to_the_project_and_says_it_exists(self):
        """Run from a subfolder with no `CLAUDE_PROJECT_DIR`, as the Bash tool runs it, in a project that has a
        boards folder: the setting goes in the project's, and every reader finds it from either folder."""
        os.mkdir(os.path.join(self.project, ".logbook"))
        path = self.write_brief(["# Brief", "one", "two"], name=os.path.join("notes", "brief.md"))
        notes = os.path.join(self.project, "notes")
        code, out, err = self.cli("next", "on", "notes/brief.md", cwd=notes, project_variable=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "next-session mode on")
        self.assertRegex(out.splitlines()[1], rf"^brief: {re.escape(path)} \(written \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ, 3 lines\)$")
        with open(self.setting(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"path": os.path.join("notes", "brief.md")})
        self.assertFalse(os.path.exists(os.path.join(notes, ".logbook")))
        for cwd in (self.project, notes):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.cli("next", "status", cwd=cwd, project_variable=False), (0, out, ""))
                self.assertEqual(board.next_session(cwd)["path"], path)
        self.assertIn(path, self.session_start()["hookSpecificOutput"]["additionalContext"])

    def test_from_a_linked_worktree_the_setting_goes_in_the_main_checkout_and_every_entry_point_reads_it(self):
        nested, beside = self.make_repository()
        inner = os.path.join(nested, "sub")
        os.mkdir(inner)
        sub = os.path.join(self.project, "sub")
        os.mkdir(sub)
        self.assertIsNone(self.session_start(project=beside))
        self.assertEqual(self.cli("next", "on", cwd=inner, project_variable=False), (0, self.status_line(), ""))
        with open(self.setting(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"path": "NEXT_SESSION.md"})
        for worktree in (nested, beside):
            self.assertFalse(os.path.exists(os.path.join(worktree, ".logbook")), worktree)
        for cwd in (self.project, sub, nested, inner, beside):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.cli("next", "status", cwd=cwd, project_variable=False), (0, self.status_line(), ""))
                self.assertEqual(board.next_session(cwd)["path"], self.brief())
        for project in (self.project, nested, beside):
            with self.subTest(session_start=project):
                text = self.session_start(project=project)["hookSpecificOutput"]["additionalContext"]
                self.assertTrue(text.startswith(f"Next-session brief: {self.brief()} "), text)
        self.assertEqual(
            self.cli("next", "off", cwd=beside, project_variable=False),
            (0, "next-session mode off; the brief was left as it is\n", ""),
        )
        self.assertFalse(os.path.exists(os.path.join(self.project, ".logbook")))
        self.assertEqual(self.cli("next", "status", cwd=nested, project_variable=False), (0, "next-session mode off\n", ""))

    def test_from_a_subfolder_of_a_repository_the_setting_goes_in_its_top(self):
        self.make_repository()
        sub = os.path.join(self.project, "sub")
        os.mkdir(sub)
        self.assertEqual(self.cli("next", "on", cwd=sub, project_variable=False), (0, self.status_line(), ""))
        self.assertTrue(os.path.isfile(self.setting()))
        self.assertFalse(os.path.exists(os.path.join(sub, ".logbook")))

    def test_status_is_off_then_on_with_the_path_the_time_and_the_line_count(self):
        self.assertEqual(self.cli("next", "status"), (0, "next-session mode off\n", ""))
        self.cli("next", "on")
        self.assertEqual(
            self.cli("next", "status"), (0, f"next-session mode on\nbrief: {self.brief()} (not written yet)\n", ""),
        )
        self.write_brief(["a", "b", "c", "d"])
        code, out, _ = self.cli("next", "status")
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"^next-session mode on\nbrief: {re.escape(self.brief())} \(written [0-9T:-]+Z, 4 lines\)\n$")

    def test_a_path_outside_the_project_is_refused_and_nothing_is_written(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.project, "out"))
        for path in ("../elsewhere.md", os.path.join(outside, "brief.md"), "out/brief.md", ".", "sub/../.."):
            with self.subTest(path=path):
                code, out, err = self.cli("next", "on", path)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("is not a file path inside the project", err)
                self.assertFalse(os.path.exists(self.setting()))

    def test_a_path_inside_the_boards_folder_is_refused(self):
        for path in (".logbook/next-session.json", ".logbook/brief.md", "notes/../.logbook/brief.md", ".logbook"):
            with self.subTest(path=path):
                code, out, err = self.cli("next", "on", path)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("is not a file path inside the project", err)
                self.assertFalse(os.path.exists(self.setting()))

    def test_off_removes_the_setting_and_leaves_the_brief_alone(self):
        self.cli("next", "on")
        self.write_brief([SECRET])
        self.assertEqual(self.cli("next", "off"), (0, "next-session mode off; the brief was left as it is\n", ""))
        self.assertFalse(os.path.exists(self.setting()))
        self.assertFalse(os.path.exists(os.path.join(self.project, ".logbook")), "off left the boards folder behind")
        with open(self.brief(), encoding="utf-8") as f:
            self.assertEqual(f.read(), SECRET + "\n")
        self.assertEqual(self.cli("next", "status"), (0, "next-session mode off\n", ""))
        self.assertEqual(self.cli("next", "off"), (0, "next-session mode was already off\n", ""))

    def test_off_keeps_a_boards_folder_that_holds_anything_else(self):
        self.assertEqual(self.cli("start", "Work")[0], 0)
        self.cli("next", "on")
        self.cli("next", "off")
        self.assertFalse(os.path.exists(self.setting()))
        self.assertTrue(board.is_board(self.folder))
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".logbook", ".gitignore")))

    def test_a_setting_that_is_not_json_is_unreadable_and_read_as_off(self):
        os.mkdir(os.path.join(self.project, ".logbook"))
        with open(self.setting(), "w", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertEqual(
            self.cli("next", "status"), (0, f"next-session setting unreadable: {self.setting()} (it is not JSON)\n", ""),
        )
        self.assertIsNone(board.next_session(self.project))
        self.assertIsNone(self.session_start())
        self.assertEqual(self.cli("next", "on"), (0, self.status_line(), ""))

    def test_a_hand_edited_setting_naming_no_file_in_the_project_is_unreadable_and_read_as_off(self):
        os.mkdir(os.path.join(self.project, ".logbook"))
        why = f"it names no file path inside {self.project} outside its .logbook folder"
        for value in ({"path": "../elsewhere.md"}, {"path": os.path.join(self.tmp, "x.md")},
                      {"path": ".logbook/next-session.json"}, {"path": 3}, ["NEXT_SESSION.md"]):
            with self.subTest(value=value):
                with open(self.setting(), "w", encoding="utf-8") as f:
                    json.dump(value, f)
                self.assertEqual(
                    self.cli("next", "status"), (0, f"next-session setting unreadable: {self.setting()} ({why})\n", ""),
                )
                self.assertIsNone(board.next_session(self.project))
                self.assertIsNone(self.session_start())

    def test_a_linked_boards_folder_is_refused(self):
        elsewhere = os.path.join(self.tmp, "boards")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, os.path.join(self.project, ".logbook"))
        self.assertEqual(self.cli("next", "on"), (1, "", f"logbook: {board.LINKED_BOARDS}\n"))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_python_reads_the_setting_as_off_or_the_brief_it_names(self):
        self.assertIsNone(board.next_session(self.project))
        self.cli("next", "on")
        self.assertEqual(
            board.next_session(self.project), {"path": self.brief(), "exists": False, "written": None, "lines": None},
        )
        self.write_brief(["a", "b"])
        found = board.next_session(self.project)
        self.assertEqual((found["exists"], found["lines"]), (True, 2))
        self.assertRegex(found["written"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    # -----------------------------------------------------------------------------------------------
    # The session start

    def test_a_session_start_names_the_brief_and_never_its_contents_on_every_source(self):
        self.cli("next", "on")
        self.write_brief(["# Next session", SECRET, "three"])
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                output = self.session_start(source)
                self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
                text = output["hookSpecificOutput"]["additionalContext"]
                lines = text.splitlines()
                self.assertLessEqual(len(lines), 3)
                self.assertRegex(
                    lines[0], rf"^Next-session brief: {re.escape(self.brief())} \(written [0-9T:-]+Z, 3 lines\)\.$",
                )
                self.assertEqual(lines[1], "Read it when the user asks you to pick up or carry on the work, not otherwise.")
                self.assertEqual(
                    lines[2],
                    "When the work it describes is done, or the user asks, rewrite it whole for the session after, "
                    "using /logbook next.",
                )
                self.assertNotIn(SECRET, text)
                self.assertNotIn("# Next session", text)

    def test_a_brief_not_written_yet_is_said_and_the_model_is_told_to_write_it_when_done(self):
        self.cli("next", "on")
        text = self.session_start()["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(text, (
            f"Next-session brief: {self.brief()} does not exist yet.\n"
            "When the work is done, write it for the session after, using /logbook next."
        ))

    def test_with_the_mode_off_a_session_start_says_nothing_about_a_brief(self):
        self.write_brief([SECRET])
        os.mkdir(os.path.join(self.project, ".logbook"))
        self.assertIsNone(self.session_start())
        self.cli("next", "on")
        self.cli("next", "off")
        self.assertIsNone(self.session_start())

    def test_an_open_board_keeps_its_context_and_the_brief_follows_it(self):
        self.start_with_subagent()
        own = board_hook.context(self.folder, self.env)
        self.assertEqual(self.session_start("compact")["hookSpecificOutput"]["additionalContext"], own)
        self.cli("next", "on")
        text = self.session_start("compact")["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(text.startswith(own + "\n\nNext-session brief: "), text)

    # -----------------------------------------------------------------------------------------------
    # next facts

    def test_facts_print_nothing_without_a_board(self):
        self.assertEqual(self.cli("next", "facts"), (0, "", ""))

    def test_facts_carry_open_questions_stops_decisions_failed_checks_and_deliverables(self):
        self.assertEqual(self.cli("start", "Facts")[0], 0)
        for argv in (
            ("question", "Keep the old file?", "--default", "keep it"),
            ("question", "Rename the table?", "--default", "no"),
            ("answer", "Q2", "yes"),
            ("stop", "Which account?"),
            ("decision", "Store as JSON", "--why", "no schema yet"),
            ("check", "the export round-trips", "--command", "make test", "--result", "fail"),
            ("check", "the build compiles", "--command", "make", "--result", "pass"),
            ("deliverable", "Export", "--path", "out/export.json"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0], 0)
        self.assertEqual(self.cli("next", "facts"), (0, (
            "Q1 open: Keep the old file? (default: keep it)\n"
            "Q3 stop: Which account?\n"
            "D1 decision: Store as JSON (why: no schema yet)\n"
            "C1 failed: the export round-trips (command: make test)\n"
            "deliverable: Export (out/export.json)\n"
        ), ""))

    def test_facts_drop_a_failure_a_later_pass_fixed_and_keep_one_none_did(self):
        self.assertEqual(self.cli("start", "Facts")[0], 0)
        for argv in (
            ("check", "the docs build", "--command", "make docs", "--result", "pass"),
            ("check", "the export round-trips", "--command", "make test", "--result", "fail"),
            ("check", "the lint is clean", "--command", "make lint", "--result", "fail"),
            ("check", "the docs build", "--command", "make docs", "--result", "fail"),
            ("check", "the schema migrates", "--command", "make migrate", "--result", "fail"),
            ("check", "the export round-trips after the fix", "--command", "make  test", "--result", "pass"),
            ("check", "the  schema migrates", "--command", "make migrate --dry-run", "--result", "pass"),
            ("check", "the lint is clean", "--command", "make lint", "--result", "fail"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[0], 0)
        self.assertEqual(self.cli("next", "facts"), (0, (
            "C3 failed: the lint is clean (command: make lint)\n"
            "C4 failed: the docs build (command: make docs)\n"
            "C8 failed: the lint is clean (command: make lint)\n"
        ), ""))

    def test_facts_list_an_unanswered_question_with_its_default(self):
        self.cli("start", "Facts")
        self.cli("question", "Keep the old file?", "--default", "keep it")
        self.assertEqual(self.cli("next", "facts"), (0, "Q1 open: Keep the old file? (default: keep it)\n", ""))
