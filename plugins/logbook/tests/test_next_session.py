"""Tests for next-session mode: its commands, what a session start says about the brief, and `next facts`.

Run: python3 -m unittest discover -s plugins/logbook/tests

The commands run `board.py` as the skill does, as a program, and the session starts run the real
`gate.sh`, both in the environment of `test_hooks.py`, which has every `GIT_*` variable removed. The
project is not a repository; the only git the recorder runs is its own read of branch heads, which
finds none.
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

    def cli(self, *argv, cwd=None):
        """board.py run as the skill runs it, in the hooks' environment with the session's id set."""
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=SESSION)
        done = subprocess.run(
            [sys.executable, BOARD_PY, *argv], cwd=cwd or self.project, env=env, capture_output=True, timeout=60,
        )
        return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")

    def session_start(self, source="startup"):
        payload = fixture("SessionStart")
        payload.update(source=source, session_id=SESSION)
        return self.hook(payload)

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
        path = self.write_brief(["# Brief", "one", "two"], name=os.path.join("notes", "brief.md"))
        code, out, err = self.cli("next", "on", "notes/brief.md", cwd=os.path.join(self.project, "notes"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], "next-session mode on")
        self.assertRegex(out.splitlines()[1], rf"^brief: {re.escape(path)} \(written \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ, 3 lines\)$")
        with open(self.setting(), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"path": os.path.join("notes", "brief.md")})

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

    def test_off_removes_the_setting_and_leaves_the_brief_alone(self):
        self.cli("next", "on")
        self.write_brief([SECRET])
        self.assertEqual(self.cli("next", "off"), (0, "next-session mode off; the brief was left as it is\n", ""))
        self.assertFalse(os.path.exists(self.setting()))
        with open(self.brief(), encoding="utf-8") as f:
            self.assertEqual(f.read(), SECRET + "\n")
        self.assertEqual(self.cli("next", "status"), (0, "next-session mode off\n", ""))
        self.assertEqual(self.cli("next", "off"), (0, "next-session mode was already off\n", ""))

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

    def test_facts_list_an_unanswered_question_with_its_default(self):
        self.cli("start", "Facts")
        self.cli("question", "Keep the old file?", "--default", "keep it")
        self.assertEqual(self.cli("next", "facts"), (0, "Q1 open: Keep the old file? (default: keep it)\n", ""))
