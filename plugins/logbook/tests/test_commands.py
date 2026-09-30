"""Tests for the model's commands and the index of boards.

Run: python3 -m unittest discover -s plugins/logbook/tests

Each command runs end to end through `board.main(argv)`, with the clock, the environment, the current
directory and the template controlled by the test. Nothing here runs git: the project is not a
repository, and the recorder's own git calls drop every variable that points git elsewhere.
"""
import io
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_board import HERE, NOW, SESSION, Workspace, at

import board  # noqa: E402  (test_board puts the board folder on the path)

OTHER = "99999999-8888-4777-8666-555555555555"


class Commands(Workspace):
    def setUp(self):
        super().setUp()
        self.minutes = 0
        patches = (
            mock.patch.object(board, "DEFAULT_TEMPLATE", self.template),
            mock.patch.object(board, "clock", self.tick),
            mock.patch.dict(os.environ, {board.SESSION_VARIABLE: SESSION}),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in [n for n in os.environ if n.startswith("LOGBOOK_")]:
            self.addCleanup(os.environ.__setitem__, name, os.environ.pop(name))
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.project)
        self.board = self.start()

    def tick(self):
        self.minutes += 1
        return at(self.minutes)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = board.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.run_main(*argv)
        self.assertEqual((code, err), (0, ""), out)
        return out

    def refused(self, *argv, code=1):
        before = len(board.events(self.board))
        got, out, err = self.run_main(*argv)
        self.assertEqual(got, code, err)
        self.assertEqual(out, "")
        if code == 1:  # a refusal is one line; exit 2 is argparse's usage message
            self.assertEqual(err.count("\n"), 1, err)
        self.assertEqual(len(board.events(self.board)), before, "nothing is appended")
        return err

    def state(self):
        return board.read_state(self.board)

    # --- each command -----------------------------------------------------------------------------

    def test_title_sets_the_title_as_the_model(self):
        self.assertEqual(self.ok("title", "Ship the exporter"), "title set\n")
        self.assertEqual((self.state()["title"], self.state()["titleSource"]), ("Ship the exporter", "model"))

    def test_question_records_its_default_and_nulls_what_is_absent(self):
        self.assertEqual(self.ok("question", "Archived too?", "--default", "No."), "Q1 recorded\n")
        out = self.ok("question", "Dates?", "--default", "ISO.", "--affects", "The export.", "--reverse", "Reformat.")
        self.assertEqual(out, "Q2 recorded\n")
        first, second = self.state()["questions"]
        self.assertEqual(
            (first["text"], first["default"], first["affects"], first["reverse"], first["hardStop"], first["status"]),
            ("Archived too?", "No.", None, None, False, "open"),
        )
        self.assertEqual((second["affects"], second["reverse"]), ("The export.", "Reformat."))
        self.assertEqual(first["asked"], board.utc(at(1)))

    def test_question_needs_a_default(self):
        self.refused("question", "Archived too?", code=2)

    def test_stop_is_a_hard_stop_without_a_default(self):
        self.assertEqual(self.ok("stop", "No API key in the environment", "--affects", "The upload."), "Q1 recorded\n")
        question = self.state()["questions"][0]
        self.assertEqual((question["hardStop"], question["default"], question["affects"]), (True, None, "The upload."))
        self.assertEqual(board.events(self.board)[-1]["kind"], "question")

    def test_answer_accepts_each_form_of_id(self):
        for _ in range(3):
            self.ok("question", "Which?", "--default", "This one.")
        self.assertEqual(self.ok("answer", "Q1", "Yes"), "Q1 answered\n")
        self.assertEqual(self.ok("answer", "q2", "No"), "Q2 answered\n")
        self.assertEqual(self.ok("answer", "3", "Maybe"), "Q3 answered\n")
        self.assertEqual([(q["status"], q["answer"]) for q in self.state()["questions"]],
                         [("answered", "Yes"), ("answered", "No"), ("answered", "Maybe")])

    def test_answering_twice_is_refused(self):
        self.ok("question", "Archived too?", "--default", "No.")
        self.ok("answer", "Q1", "Yes")
        self.assertIn("already answered", self.refused("answer", "Q1", "No"))
        self.assertEqual(self.state()["questions"][0]["answer"], "Yes")

    def test_answering_no_question_is_refused(self):
        self.refused("answer", "Q1", "Yes")
        self.ok("question", "Archived too?", "--default", "No.")
        self.refused("answer", "Q2", "Yes")
        self.refused("answer", "D1", "Yes")

    def test_decision(self):
        self.assertEqual(self.ok("decision", "Dates in ISO 8601."), "D1 recorded\n")
        self.assertEqual(self.ok("decision", "One file.", "--why", "Simpler.", "--reverse", "Split it."), "D2 recorded\n")
        first, second = self.state()["decisions"]
        self.assertEqual((first["why"], first["reverse"]), (None, None))
        self.assertEqual((second["text"], second["why"], second["reverse"]), ("One file.", "Simpler.", "Split it."))

    def test_deliverable_takes_a_path_or_a_url(self):
        self.assertEqual(self.ok("deliverable", "The exporter", "--path", "src/export.py", "--step", "2"),
                         "deliverable recorded\n")
        self.assertEqual(self.ok("deliverable", "The pull request", "--url", "https://example.com/pr/1"),
                         "deliverable recorded\n")
        first, second = self.state()["deliverables"]
        self.assertEqual((first["label"], first["path"], first["url"], first["step"]),
                         ("The exporter", "src/export.py", None, "2"))
        self.assertEqual((second["path"], second["url"], second["step"]), (None, "https://example.com/pr/1", None))
        self.refused("deliverable", "Both", "--path", "a", "--url", "https://example.com", code=2)
        self.refused("deliverable", "Neither", code=2)

    def test_check_records_a_model_check(self):
        self.assertEqual(self.ok("check", "One row per report", "--command", "make test", "--result", "pass"),
                         "C1 recorded: pass\n")
        self.assertEqual(self.ok("check", "Empty export", "--command", "make empty", "--result", "fail"),
                         "C2 recorded: fail\n")
        check = self.state()["checks"][0]
        self.assertEqual((check["proves"], check["command"], check["result"], check["source"], check["agent"]),
                         ("One row per report", "make test", "pass", "model", None))
        self.refused("check", "x", "--command", "c", "--result", "maybe", code=2)
        self.refused("check", "x", "--result", "pass", code=2)

    def test_the_id_printed_is_the_one_the_state_gives(self):
        board.append(self.board, "agent-stop", at(0), id="a1", message="G1 the tests pass: passed")
        self.assertEqual(self.ok("check", "Lint is clean", "--command", "make lint", "--result", "pass"),
                         "C2 recorded: pass\n")

    # --- text --------------------------------------------------------------------------------------

    def test_empty_text_is_refused(self):
        self.refused("decision", "", code=2)
        self.refused("decision", "   ", code=2)
        self.refused("question", "Archived?", "--default", "", code=2)
        self.ok("question", "Archived too?", "--default", "No.")
        self.refused("answer", "Q1", "", code=2)

    def test_text_is_cut_at_the_cap_and_otherwise_stored_as_given(self):
        long = "x" * (board.TEXT_CAP + 500)
        self.ok("decision", long, "--why", long)
        self.ok("decision", "<b>as given</b>")
        first, second = self.state()["decisions"]
        self.assertEqual((len(first["text"]), len(first["why"])), (board.TEXT_CAP, board.TEXT_CAP))
        self.assertEqual(second["text"], "<b>as given</b>")

    # --- finding the board ---------------------------------------------------------------------------

    def test_the_board_is_found_from_the_session_in_a_sub_directory(self):
        deep = os.path.join(self.project, "src", "deep")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(self.ok("decision", "Found it."), "D1 recorded\n")
        self.assertEqual(self.state()["decisions"][0]["text"], "Found it.")

    def test_an_explicit_board_wins_over_the_session(self):
        other = board.start(self.project, OTHER, NOW, "other", env={}, template=self.template)
        self.ok("decision", "Here.", "--board", other)
        self.assertEqual(board.read_state(other)["decisions"][0]["text"], "Here.")
        self.assertEqual(self.state()["decisions"], [])

    def test_no_board_for_the_session(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(elsewhere)
        os.chdir(elsewhere)
        for environment in ({board.SESSION_VARIABLE: OTHER}, {}):
            with mock.patch.dict(os.environ, environment):
                if not environment:
                    del os.environ[board.SESSION_VARIABLE]
                err = self.refused("decision", "x")
                self.assertIn("no board is active for this session", err)
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["elsewhere", "project", "template.html"])

    def test_a_bad_session_id_is_refused(self):
        for session in ("../project", "a/b", "..", "has space"):
            with mock.patch.dict(os.environ, {board.SESSION_VARIABLE: session}):
                self.refused("decision", "x")

    def test_a_named_board_that_is_not_one_is_refused(self):
        self.assertIn("no board at", self.refused("decision", "x", "--board", self.tmp.name))

    def test_a_closed_board_takes_nothing_more(self):
        self.ok("question", "Archived too?", "--default", "No.")
        board.close(self.board, at(30), env={}, template=self.template)
        for argv in (("title", "t"), ("question", "q", "--default", "d"), ("stop", "s"), ("answer", "Q1", "a"),
                     ("decision", "d"), ("deliverable", "l", "--path", "p"),
                     ("check", "c", "--command", "c", "--result", "pass")):
            self.assertIn("closed", self.refused(*argv))
        # A second close is not a refusal: it appends nothing and leaves report.html byte-identical.
        report_path = os.path.join(self.board, board.REPORT_FILE)
        before_log = board.events(self.board)
        before_report = self.read(report_path)
        expected = os.path.join(os.path.abspath(os.getcwd()), board.BOARDS_DIR, SESSION, board.REPORT_FILE)
        self.assertEqual(self.ok("close"), f"report written: {expected}\n")
        self.assertEqual(board.events(self.board), before_log)
        self.assertEqual(self.read(report_path), before_report)


class Index(Workspace):
    def index(self):
        return self.read(os.path.join(self.project, board.BOARDS_DIR, board.INDEX_FILE))

    def links(self):
        return re.findall(r'<a href="([^"]*)">', self.index())

    def test_boards_are_listed_newest_first_and_a_closed_one_links_its_report(self):
        first = self.start("The first task")
        second = board.start(self.project, OTHER, at(5), "The second task", env={}, template=self.template)
        self.assertEqual(self.links(), [f"{OTHER}/board.html", f"{SESSION}/board.html"])
        board.close(first, at(10), env={}, template=self.template)
        self.assertEqual(self.links(), [f"{OTHER}/board.html", f"{SESSION}/report.html"])
        page = self.index()
        self.assertLess(page.index("The second task"), page.index("The first task"))
        self.assertIn('<time datetime="2026-01-05T09:00:00Z">2026-01-05 09:00 UTC</time>', page)
        self.assertRegex(page, r"The first task</a></td><td><span class=\"state-finished\">finished</span>")
        self.assertTrue(board.is_board(second))

    def test_the_index_follows_a_change_of_state_or_title(self):
        folder = self.start("The first task")
        self.assertIn(">live<", self.index())
        board.append(folder, "turn-end", at(1))
        board.render(folder, at(1), env={}, template=self.template)
        self.assertIn(">idle<", self.index())
        board.append(folder, "title", at(2), title="A better title")
        board.render(folder, at(2), env={}, template=self.template)
        self.assertIn("A better title", self.index())

    def test_the_index_escapes_a_hostile_title(self):
        self.start('<script>alert("x")</script> & <img src=x>')
        page = self.index()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;img src=x&gt;", page)

    def test_a_folder_without_the_marker_is_not_listed(self):
        self.start()
        stray = os.path.join(self.project, board.BOARDS_DIR, "stray")
        os.mkdir(stray)
        self.write(os.path.join(stray, board.STATE_FILE), 'window.BOARD = {"title": "stray"};\n')
        board.update_index(os.path.join(self.project, board.BOARDS_DIR, SESSION))
        self.assertEqual(self.links(), [f"{SESSION}/board.html"])
        self.assertNotIn("stray", self.index())

    def test_a_board_whose_state_cannot_be_read_is_listed_by_its_folder_name(self):
        folder = self.start("Readable")
        other = board.start(self.project, OTHER, at(5), "Soon unreadable", env={}, template=self.template)
        self.write(os.path.join(other, board.STATE_FILE), "window.BOARD = {not json")
        board.update_index(folder)
        page = self.index()
        self.assertEqual(self.links(), [f"{SESSION}/board.html", f"{OTHER}/board.html"])
        self.assertIn(f">{OTHER}</a>", page)
        self.assertNotIn("Soon unreadable", page)

    def test_the_index_needs_nothing_from_the_network_and_reads_the_page_theme_key(self):
        self.start()
        page = self.index()
        self.assertNotRegex(page, r"(?i)https?://|<link|src=")
        template = self.read(os.path.join(HERE, "..", "board", "template.html"))
        key = re.search(r"var STORE = '([^']+)'", template).group(1)
        self.assertIn(f"localStorage.getItem('{key}')", page)
        self.assertIn('@media (prefers-color-scheme: dark)', page)
        self.assertIn(':root:not([data-theme="light"])', page)
        self.assertIn(':root[data-theme="dark"]', page)
        self.assertRegex(page, r"body \{[^}]*background: var\(--bg\)")

    def test_the_theme_setting_is_the_index_default(self):
        folder = self.start()
        board.update_index(folder, env={"LOGBOOK_THEME": "dark"})
        self.assertIn("mode = 'dark';", self.index())

    def test_an_index_is_written_only_where_boards_live(self):
        loose = os.path.join(self.tmp.name, "loose")
        os.mkdir(loose)
        self.write(os.path.join(loose, board.MARKER_FILE), SESSION)
        board.render(loose, NOW, env={}, template=self.template)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, board.INDEX_FILE)))


if __name__ == "__main__":
    unittest.main()
