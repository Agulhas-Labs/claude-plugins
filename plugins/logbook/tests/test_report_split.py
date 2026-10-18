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
from contextlib import redirect_stdout
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


class PublishCall(Workspace):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp.name, "data")
        os.mkdir(self.data)
        self.folder = board.start(self.project, SESSION, NOW, "Publish", env={}, template=self.template)
        os.mkdir(os.path.join(self.folder, board.IMAGES_DIR))
        self.write(os.path.join(self.folder, board.IMAGES_DIR, "1-shot.png"), "png")

    def run_cli(self, *argv, data=True):
        env = {"CLAUDE_PLUGIN_DATA": self.data} if data else {}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out):
            if not data:
                os.environ.pop("CLAUDE_PLUGIN_DATA", None)
            code = board.main(list(argv))
        self.assertEqual(code, 0)
        return out.getvalue()

    def close_out(self, data=True):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template):
            return self.run_cli("close", "--board", self.folder, data=data)

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
        self.assertIn("template-url <the published url>, so later reports copy", out)

    def test_a_recorded_url_makes_the_style_and_script_copies_from_that_artifact(self):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template):
            self.run_cli("template-url", "https://example.test/a/1")
        out = self.close_out()
        files = self.files_of(out)
        self.assertEqual(files["logbook.css"], {"artifact": "https://example.test/a/1", "path": "logbook.css"})
        self.assertEqual(files["logbook.js"], {"artifact": "https://example.test/a/1", "path": "logbook.js"})
        self.assertTrue(files["images/1-shot.png"].endswith("1-shot.png"))
        self.assertIn("is refused (it was deleted)", out)
        self.assertNotIn("so later reports copy", out)

    def test_a_changed_template_does_not_use_the_old_url(self):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template):
            self.run_cli("template-url", "https://example.test/a/1")
        changed = self.write(
            os.path.join(self.tmp.name, "changed.html"),
            "<html><head><style>b{}</style>\n  " + board.TEMPLATE_MARKER + "\n<script>var x;</script></head></html>\n",
        )
        with mock.patch.object(board, "DEFAULT_TEMPLATE", changed):
            out = self.run_cli("close", "--board", self.folder)
        self.assertEqual(self.files_of(out)["logbook.css"], os.path.join(os.path.abspath(self.folder), "logbook.css"))

    def test_without_a_data_folder_nothing_is_recorded_and_close_gives_the_local_form(self):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template):
            self.assertIn("no record kept", self.run_cli("template-url", "https://example.test/a/1", data=False))
        self.assertEqual(os.listdir(self.data), [])
        out = self.close_out(data=False)
        self.assertEqual(self.files_of(out)["logbook.js"], os.path.join(os.path.abspath(self.folder), "logbook.js"))

    def test_a_url_with_a_space_is_refused(self):
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template), mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data}):
            self.assertEqual(board.main(["template-url", "a b"]), 1)
        self.assertEqual(os.listdir(self.data), [])


if __name__ == "__main__":
    unittest.main()
