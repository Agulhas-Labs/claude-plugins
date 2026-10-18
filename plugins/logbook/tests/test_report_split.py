"""Tests for the closed report: its style and script beside it, its commands cut down, and the publish call
`close` prints.

Run: python3 -m unittest discover -s plugins/logbook/tests

Every folder is made inside the temporary directory `Workspace` creates and removes.
"""
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402
from test_board import NOW, SESSION, Workspace, at  # noqa: E402

REAL = board.read_text(board.DEFAULT_TEMPLATE)


def commands(count, length=10):
    return [
        {"command": "c" * length + str(i), "runs": 1, "fails": 0, "latestRun": i + 1}
        for i in range(count)
    ]


class ReportFiles(Workspace):
    def closed(self, template=None):
        folder = board.start(self.project, SESSION, NOW, "Split", env={}, template=template or self.template)
        board.close(folder, at(1), env={}, template=template or self.template)
        return folder

    def test_the_report_holds_the_data_and_links_the_style_and_script_it_no_longer_carries(self):
        real = self.write(os.path.join(self.tmp.name, "real.html"), REAL)
        folder = self.closed(real)
        report = self.read(os.path.join(folder, board.REPORT_FILE))
        self.assertNotIn("<style>", report)
        self.assertIn('<link rel="stylesheet" href="logbook.css">', report)
        self.assertIn('<script src="logbook.js"></script>', report)
        self.assertEqual(len(re.findall(r"<script>", report)), 1)
        self.assertNotIn("var PB = ", report)
        data = report.index("window.BOARD_FROZEN = true;")
        self.assertLess(data, report.index('<script src="logbook.js">'))
        self.assertEqual(json.loads(re.search(r"window\.BOARD = (.*); window\.BOARD_FROZEN", report).group(1))["state"], "finished")

    def test_the_files_beside_it_are_the_templates_blocks_byte_for_byte(self):
        real = self.write(os.path.join(self.tmp.name, "real.html"), REAL)
        folder = self.closed(real)
        css = self.read(os.path.join(folder, board.CSS_FILE))
        js = self.read(os.path.join(folder, board.JS_FILE))
        self.assertEqual(css, REAL[REAL.index("<style>") + 7:REAL.index("</style>")])
        marker = REAL.index(board.TEMPLATE_MARKER)
        start = REAL.index("<script>", marker)
        self.assertEqual(js, REAL[start + 8:REAL.index("</script>", start)])
        # Put back in place, the three files are the old inlined page with its data.
        report = self.read(os.path.join(folder, board.REPORT_FILE))
        rebuilt = report.replace('<link rel="stylesheet" href="logbook.css">', f"<style>{css}</style>")
        rebuilt = rebuilt.replace('<script src="logbook.js"></script>', f"<script>{js}</script>")
        state = board.read_state(folder)
        old = board.fill_template(
            REAL, f"<script>window.BOARD = {board.inline_json(board.report_state(state))}; window.BOARD_FROZEN = true;</script>"
        )
        self.assertEqual(rebuilt, old)

    def test_a_lost_stylesheet_is_written_back_by_a_second_close(self):
        real = self.write(os.path.join(self.tmp.name, "real.html"), REAL)
        folder = self.closed(real)
        os.unlink(os.path.join(folder, board.CSS_FILE))
        board.close(folder, at(2), env={}, template=real)
        self.assertTrue(os.path.isfile(os.path.join(folder, board.CSS_FILE)))

    def test_the_reports_commands_are_cut_in_number_and_length_but_the_total_stands(self):
        state = {"commands": commands(120, length=500), "commandsTotal": 300}
        slim = board.report_state(state)
        self.assertEqual(len(slim["commands"]), board.REPORT_COMMAND_ROWS)
        self.assertEqual([c["latestRun"] for c in slim["commands"]], list(range(71, 121)))
        for row in slim["commands"]:
            self.assertEqual(len(row["command"]), board.REPORT_COMMAND_CHARS + 1)
            self.assertTrue(row["command"].endswith("…"))
        self.assertEqual(slim["commandsTotal"], 300)
        self.assertEqual(len(state["commands"]), 120)

    def test_a_short_command_and_a_short_list_pass_whole(self):
        state = {"commands": commands(3), "commandsTotal": 3}
        self.assertEqual(board.report_state(state), state)

    def test_every_test_row_stays_in_the_report_and_only_the_others_are_cut(self):
        rows = commands(60)
        rows[0].update(test=True, fails=1, result="fail", command="pytest")
        slim = board.report_state({"commands": rows, "commandsTotal": 60})
        self.assertEqual(len(slim["commands"]), board.REPORT_COMMAND_ROWS + 1)
        self.assertEqual(slim["commands"][0]["command"], "pytest")
        self.assertEqual(slim["commandsTotal"], 60)
        others = [c for c in slim["commands"] if c.get("test") is not True]
        self.assertEqual([c["latestRun"] for c in others], list(range(11, 61)))

    def test_a_failing_early_test_run_still_counts_in_the_reports_verified(self):
        rows = commands(60)
        rows[0].update(test=True, result="fail", command="pytest")
        slim = board.report_state({"commands": rows, "commandsTotal": 60})
        self.assertEqual(sum(1 for c in slim["commands"] if c.get("test") is True and c.get("result") == "fail"), 1)


class PublishCall(Workspace):
    def setUp(self):
        super().setUp()
        self.folder = board.start(self.project, SESSION, NOW, "Publish", env={}, template=self.template)
        os.mkdir(os.path.join(self.folder, board.IMAGES_DIR))
        self.write(os.path.join(self.folder, board.IMAGES_DIR, "1-shot.png"), "png")

    def run_cli(self, *argv, expect=0):
        """Run a command the way the model's Bash tool does: no plugin variable in the environment."""
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = board.main(list(argv))
        self.assertEqual(code, expect)
        return out.getvalue()

    def close_out(self, folder=None, template=None):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", template or self.template):
            return self.run_cli("close", "--board", folder or self.folder)

    def second_board(self, template=None):
        folder = board.start(self.project, "22222222-2222-4222-8222-222222222222", NOW, "Second", env={}, template=template or self.template)
        return folder

    def files_of(self, out):
        return json.loads(re.search(r"^files = (.*)$", out, re.M).group(1))

    def test_without_a_record_the_style_and_script_go_as_local_paths_and_the_url_is_asked_for(self):
        out = self.close_out()
        files = self.files_of(out)
        folder = os.path.abspath(self.folder)
        self.assertEqual(files, {
            "images/1-shot.png": os.path.join(folder, "images", "1-shot.png"),
            "logbook.css": os.path.join(folder, "logbook.css"),
            "logbook.js": os.path.join(folder, "logbook.js"),
        })
        self.assertIn(f"file_path = {os.path.join(folder, 'report.html')}", out)
        self.assertIn(f'template-url --board "{folder}" <the published url>, so later reports copy', out)

    def test_with_no_plugin_variable_the_recorded_url_reaches_the_next_boards_close(self):
        self.close_out()
        self.run_cli("template-url", "--board", self.folder, "https://example.test/a/1")
        self.assertTrue(os.path.isfile(os.path.join(self.project, board.BOARDS_DIR, board.TEMPLATE_URL_FILE)))
        second = self.second_board()
        out = self.close_out(second)
        files = self.files_of(out)
        self.assertEqual(files["logbook.css"], {"artifact": "https://example.test/a/1", "path": "logbook.css"})
        self.assertEqual(files["logbook.js"], {"artifact": "https://example.test/a/1", "path": "logbook.js"})
        self.assertIn("is refused (it was deleted)", out)
        self.assertNotIn("so later reports copy", out)

    def test_the_record_is_not_a_board_and_pruning_keeps_it(self):
        self.close_out()
        self.run_cli("template-url", "--board", self.folder, "https://example.test/a/1")
        boards = os.path.join(self.project, board.BOARDS_DIR)
        self.assertNotIn(board.TEMPLATE_URL_FILE, [r["name"] for r in board.board_rows(boards)])
        board.prune(self.project, NOW.replace(year=NOW.year + 5), env={})
        self.assertTrue(os.path.isfile(os.path.join(boards, board.TEMPLATE_URL_FILE)))

    def test_a_url_recorded_for_one_template_is_not_used_by_a_board_holding_another(self):
        self.close_out()
        self.run_cli("template-url", "--board", self.folder, "https://example.test/a/1")
        changed = self.write(
            os.path.join(self.tmp.name, "changed.html"),
            "<html><head><style>b{}</style>\n  " + board.TEMPLATE_MARKER + "\n<script>var x;</script></head></html>\n",
        )
        second = self.second_board(changed)
        out = self.close_out(second, template=changed)
        self.assertEqual(self.files_of(out)["logbook.css"], os.path.join(os.path.abspath(second), "logbook.css"))

    def test_the_hash_is_of_the_files_on_disk_not_of_the_current_template(self):
        self.close_out()  # this board keeps its v1 logbook.css and logbook.js
        v2 = self.write(
            os.path.join(self.tmp.name, "v2.html"),
            "<html><head><style>b{}</style>\n  " + board.TEMPLATE_MARKER + "\n<script>var x;</script></head></html>\n",
        )
        # After a plugin update the old board's artifact is recorded: it must be recorded as v1's.
        with mock.patch.object(board, "DEFAULT_TEMPLATE", v2):
            self.run_cli("template-url", "--board", self.folder, "https://example.test/v1")
        second = self.second_board(v2)
        out = self.close_out(second, template=v2)
        self.assertEqual(self.files_of(out)["logbook.js"], os.path.join(os.path.abspath(second), "logbook.js"))
        # A v1 board's own repeat close still finds the v1 record.
        again = self.close_out()
        self.assertEqual(self.files_of(again)["logbook.js"]["artifact"], "https://example.test/v1")

    def test_a_url_that_is_not_https_is_refused_and_nothing_is_recorded(self):
        self.close_out()
        for url in ("a b", "http://example.test/a", "ftp://x", "example.test/a", "https://a b", "https://"):
            self.run_cli("template-url", "--board", self.folder, url, expect=1)
        self.assertFalse(os.path.exists(os.path.join(self.project, board.BOARDS_DIR, board.TEMPLATE_URL_FILE)))


if __name__ == "__main__":
    unittest.main()
