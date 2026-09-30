"""Tests for the recorder: the event log, the state derived from it, and the files written from the state.

Run: python3 -m unittest discover -s plugins/logbook/tests

Every test works inside its own temporary directory, with a stub template written there: the real
template belongs to the page and is not read here. A test that needs git makes its own repository,
with a local identity, signing off and hooks pointed at an empty folder, so nothing from the machine's
own git configuration reaches it.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "board"))

import board  # noqa: E402
from test_fixtures import ITEM_KEYS, STATE_KEYS, TIME_KEYS, UTC  # noqa: E402

NOW = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
MARKER = "<!-- logbook:state -->"
TEMPLATE = "<!doctype html>\n<html>\n<head>\n  " + MARKER + "\n</head>\n<body></body>\n</html>\n"
SESSION = "11111111-2222-4333-8444-555555555555"


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def event(kind, minutes=0, **fields):
    return dict({"t": board.utc(at(minutes)), "kind": kind}, **fields)


def git(cwd, *args):
    """git in `cwd` and nowhere else. A git hook exports GIT_DIR and GIT_INDEX_FILE, and a suite run from
    one (the pre-commit gate) would otherwise commit, branch and configure the repository being
    committed to, not the one this test made."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


class Workspace(unittest.TestCase):
    """A temporary directory holding a project folder and a stub template."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "project")
        os.mkdir(self.project)
        self.template = self.write(os.path.join(self.tmp.name, "template.html"), TEMPLATE)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def make_repository(self):
        git(self.project, "init", "-q")
        inside = os.path.realpath(git(self.project, "rev-parse", "--absolute-git-dir"))
        self.assertTrue(inside.startswith(os.path.realpath(self.tmp.name) + os.sep), inside)
        git(self.project, "symbolic-ref", "HEAD", "refs/heads/main")
        hooks = os.path.join(self.tmp.name, "no-hooks")
        os.mkdir(hooks)
        for key, value in (
            ("user.name", "Test"), ("user.email", "test@example.com"), ("core.hooksPath", hooks),
            ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
        ):
            git(self.project, "config", key, value)

    def commit(self, subject):
        git(self.project, "commit", "-q", "--allow-empty", "-m", subject)
        return git(self.project, "rev-parse", "--short", "HEAD")

    def start(self, title="Add the export"):
        return board.start(self.project, SESSION, NOW, title, env={}, template=self.template)


def full_log():
    """One event of every kind that adds to a list, so every list in the state has an item."""
    return [
        event("start", 0, session=SESSION, title="the first prompt", project="/p", branch="main", heads={}),
        event("step", 1, id="1", subject="Read the module"),
        event("change", 1, path="/p/src/export.py", tool="Edit", created=False, added=4, removed=1, use="u1"),
        event("command", 1, command="python3 -m pytest", description="Run the tests", result="pass", ms=900, use="u2"),
        event("step-status", 2, id="1", status="in_progress"),
        event("question", 3, text="Include archived reports?", default="No.", affects="The query.",
              reverse="Remove the filter.", hardStop=False),
        event("answer", 4, id="Q1", answer="No is fine."),
        event("decision", 5, text="Dates in ISO 8601.", why="Programs read it.", reverse="Change format_date."),
        event("deliverable", 6, label="The exporter", path="src/export.py", step="1"),
        event("check", 7, proves="One row per report", command="make test", result="pass"),
        event("agent-start", 8, id="a1", type="general-purpose"),
        event("agent-info", 8, id="a1", model="some-model", description="Write the tests"),
        event("agent-stop", 9, id="a1", message="G1 the tests pass: passed"),
    ]


COMMITS = [{"hash": "a1b2c3d", "subject": "Exporter", "branch": "main", "time": "2026-01-05T09:21:40Z", "step": None}]


class Contract(unittest.TestCase):
    def setUp(self):
        self.state = board.derive(full_log(), COMMITS, board.settings({}))

    def test_contract_top_level_and_settings_keys(self):
        self.assertEqual(set(self.state), STATE_KEYS)
        self.assertEqual(set(self.state["settings"]), ITEM_KEYS["settings"])
        self.assertEqual(self.state["schema"], 1)

    def test_contract_every_list_item_has_exactly_its_keys(self):
        for name, keys in ITEM_KEYS.items():
            if name == "settings":
                continue
            self.assertTrue(self.state[name], f"{name} is empty, so this log proves nothing about it")
            for item in self.state[name]:
                self.assertEqual(set(item), keys, name)

    def test_contract_times_are_utc(self):
        seen = []

        def walk(value, key=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k)
            elif isinstance(value, list):
                for v in value:
                    walk(v, key)
            elif key in TIME_KEYS and value is not None:
                seen.append(key)
                self.assertRegex(value, UTC, key)

        walk(self.state)
        self.assertGreater(len(seen), 5)

    def test_contract_state_survives_json(self):
        self.assertEqual(json.loads(json.dumps(self.state)), self.state)


class Derive(unittest.TestCase):
    def derive(self, *log):
        return board.derive(list(log), [], board.settings({}))

    def test_ids_are_assigned_by_order_in_the_log(self):
        state = self.derive(
            event("question", 1, text="first"), event("decision", 2, text="d-one"),
            event("check", 3, proves="x", result="fail"), event("question", 4, text="second"),
            event("agent-stop", 5, id="a", message="- **G1** unit tests — passed"),
            event("decision", 6, text="d-two"), event("check", 7, proves="y", result="pass"),
        )
        self.assertEqual([(q["id"], q["text"]) for q in state["questions"]], [("Q1", "first"), ("Q2", "second")])
        self.assertEqual([d["id"] for d in state["decisions"]], ["D1", "D2"])
        self.assertEqual([(c["id"], c["source"]) for c in state["checks"]], [("C1", "model"), ("C2", "agent"), ("C3", "model")])

    def test_an_answer_names_its_question(self):
        state = self.derive(event("question", 1, text="a"), event("question", 2, text="b"), event("answer", 3, id="Q2", answer="yes"))
        first, second = state["questions"]
        self.assertEqual((first["status"], first["answer"]), ("open", None))
        self.assertEqual((second["status"], second["answer"], second["answered"]), ("answered", "yes", board.utc(at(3))))

    def test_a_hard_stop_carries_no_default(self):
        state = self.derive(event("question", 1, text="key missing", default="guess", hardStop=True))
        self.assertIsNone(state["questions"][0]["default"])
        self.assertTrue(state["questions"][0]["hardStop"])

    def test_live_idle_and_finished(self):
        self.assertEqual(self.derive(event("step", 1, id="1", subject="a"))["state"], "live")
        self.assertEqual(self.derive(event("step", 1, id="1", subject="a"), event("turn-end", 2))["state"], "idle")
        self.assertEqual(self.derive(event("turn-end", 1), event("turn-start", 2))["state"], "live")
        self.assertEqual(self.derive(event("turn-end", 1), event("step", 2, id="1", subject="a"))["state"], "live")
        self.assertEqual(self.derive(event("turn-end", 1), event("close", 2))["state"], "finished")

    def test_started_and_updated(self):
        state = self.derive(event("start", 0, session=SESSION, title="t"), event("step", 3, id="1", subject="a"))
        self.assertEqual((state["started"], state["updated"]), (board.utc(at(0)), board.utc(at(3))))
        self.assertEqual((state["session"], state["title"], state["titleSource"]), (SESSION, "t", "prompt"))

    def test_the_model_title_replaces_the_prompt(self):
        state = self.derive(event("start", 0, title="the prompt"), event("title", 1, title="Short title"))
        self.assertEqual((state["title"], state["titleSource"]), ("Short title", "model"))

    def test_steps_are_ordered_numerically_then_by_log_order(self):
        state = self.derive(
            event("step", 1, id="9", subject="nine"),
            event("step", 2, id="10", subject="ten"),
            event("step", 3, id="2", subject="two"),
            event("step", 4, id="other", subject="lettered"),
            event("step", 5, id="1", subject="one"),
        )
        self.assertEqual([s["id"] for s in state["steps"]], ["1", "2", "9", "10", "other"])

    def test_a_second_step_event_updates_the_subject_but_keeps_the_status(self):
        state = self.derive(
            event("step", 1, id="1", subject="old"),
            event("step-status", 2, id="1", status="in_progress"),
            event("step", 3, id="1", subject="new"),
        )
        self.assertEqual(state["steps"], [{"id": "1", "subject": "new", "status": "in_progress", "updated": board.utc(at(3))}])

    def test_a_status_for_an_unknown_step_adds_it(self):
        state = self.derive(event("step-status", 1, id="7", status="in_progress"))
        self.assertEqual(state["steps"], [{"id": "7", "subject": "", "status": "in_progress", "updated": board.utc(at(1))}])

    def test_steps_replace_takes_the_whole_list(self):
        state = self.derive(
            event("step", 1, id="9", subject="old"),
            event("steps-replace", 2, items=[{"content": "a", "status": "completed"}, {"content": "b", "status": "odd"}]),
        )
        self.assertEqual([(s["id"], s["subject"], s["status"]) for s in state["steps"]], [("1", "a", "completed"), ("2", "b", "pending")])

    def test_unparseable_and_unknown_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, board.EVENTS_FILE), "w", encoding="utf-8") as f:
                f.write("not json\n[1, 2]\n{\"t\": \"2026-01-05T09:01:00Z\", \"kind\": \"mystery\"}\n")
                f.write("{\"kind\": \"step\", \"id\": \"1\", \"subject\": \"no time\"}\n")
                f.write("{\"t\": \"yesterday\", \"kind\": \"step\", \"id\": \"2\", \"subject\": \"bad time\"}\n")
                f.write("{\"t\": \"2026-01-05T09:02:00Z\", \"kind\": \"step\", \"id\": {\"x\": 1}}\n")
                f.write("{\"t\": \"2026-01-05T09:03:00Z\", \"kind\": \"question\"}\n")
                f.write(json.dumps(event("step", 4, id="3", subject="kept")) + "\n")
                f.write("{\"t\": \"2026-01-05T09:0")  # a line cut short
            state = board.derive(board.events(folder), [], board.settings({}))
        self.assertEqual([s["subject"] for s in state["steps"]], ["kept"])
        self.assertEqual(state["questions"], [])
        self.assertEqual(state["updated"], board.utc(at(4)))

    def test_append_then_events_round_trips_one_line_each(self):
        with tempfile.TemporaryDirectory() as folder:
            board.append(folder, "decision", NOW, text="line separator and é")
            board.append(folder, "turn-end", at(1))
            with open(os.path.join(folder, board.EVENTS_FILE), "rb") as f:
                self.assertEqual(f.read().count(b"\n"), 2)
            self.assertEqual(board.events(folder)[0]["text"], "line separator and é")


class Gates(unittest.TestCase):
    def test_gate_lines_and_their_results(self):
        report = "\n".join([
            "## Gates",
            "G1 the plugin's tests pass — `python3 -m unittest` — OK",
            "- **G2** contract held: FAILED, 2 errors",
            "* G3 escape test can fail — FAILS while sabotaged, passes after",
            "1. G4 ran — see above",
            "G5 green — CHECK: `ls -la dir` — EXPECT: `OK`",
            "Not a gate: G6 in the middle of a line passed",
            "G1 repeated later: failed",
        ])
        found = board.gates(report)
        self.assertEqual([(g["id"], g["result"]) for g in found],
                         [("G1", "pass"), ("G2", "fail"), ("G3", "unknown"), ("G4", "unknown"), ("G5", "pass")])
        self.assertEqual(found[4]["command"], "ls -la dir")
        self.assertEqual(found[4]["text"], "green")

    def test_the_expected_token_is_not_a_result(self):
        self.assertEqual(board.gates("G1 tests — CHECK: make test — EXPECT: OK")[0]["result"], "unknown")

    def test_an_agent_fails_when_a_gate_fails_and_passing_gates_become_checks(self):
        state = board.derive([
            event("agent-start", 1, id="a", type="builder"),
            event("agent-stop", 2, id="a", message="G1 build: pass\nG2 tests: fail\nG3 docs: unclear"),
            event("agent-start", 3, id="b", type="builder"),
            event("agent-stop", 4, id="b", message="all done, no gates"),
        ], [], board.settings({}))
        first, second = state["agents"]
        self.assertEqual(first["outcome"], "failed")
        self.assertEqual([g["result"] for g in first["gates"]], ["pass", "fail", "unknown"])
        self.assertEqual((second["outcome"], second["gates"]), ("finished", []))
        self.assertEqual([(c["proves"], c["result"], c["agent"]) for c in state["checks"]],
                         [("build: pass", "pass", "a"), ("tests: fail", "fail", "a")])


class Settings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(board.settings({}), {
            "theme": "system", "accent": None, "density": "comfortable", "refreshSeconds": 10, "stuckAfterSeconds": 600,
        })

    def test_valid_values(self):
        got = board.settings({
            "LOGBOOK_THEME": "Dark", "LOGBOOK_ACCENT": "#1a2B3c",
            "LOGBOOK_DENSITY": "compact", "LOGBOOK_STUCK_MINUTES": "3",
        })
        self.assertEqual((got["theme"], got["accent"], got["density"], got["stuckAfterSeconds"]), ("dark", "#1a2B3c", "compact", 180))
        self.assertEqual(board.settings({"LOGBOOK_ACCENT": " #abc\n"})["accent"], "#abc")

    def test_invalid_values_mean_the_default(self):
        for accent in ("red", "#abcd", "#12345g", "#abc\n;", "#abc;}body{", "abc", ""):
            self.assertIsNone(board.settings({"LOGBOOK_ACCENT": accent})["accent"], accent)
        for minutes in ("0", "-5", "2.5", "ten", ""):
            self.assertEqual(board.settings({"LOGBOOK_STUCK_MINUTES": minutes})["stuckAfterSeconds"], 600, minutes)
        got = board.settings({"LOGBOOK_THEME": "neon", "LOGBOOK_DENSITY": "tight"})
        self.assertEqual((got["theme"], got["density"]), ("system", "comfortable"))


class Files(Workspace):
    def test_the_inlined_state_cannot_close_the_script_or_open_a_comment(self):
        hostile = "</script><script>alert(1)</script> <!-- &   "
        folder = board.start(self.project, SESSION, NOW, hostile, env={}, template=self.template)
        board.close(folder, at(1), env={}, template=self.template)
        state_js = self.read(os.path.join(folder, board.STATE_FILE))
        report = self.read(os.path.join(folder, board.REPORT_FILE))
        inline = report.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        for written in (state_js, inline):
            for bad in ("</script", "<script", "<!--", "&", " ", " "):
                self.assertNotIn(bad, written)
        self.assertEqual(report.count("</script>"), 1)
        self.assertEqual(json.loads(re.match(r"window\.BOARD = (.*);\n$", state_js, re.S).group(1))["title"], hostile)

    def test_the_pages_are_what_the_page_expects(self):
        folder = self.start()
        board.close(folder, at(1), env={}, template=self.template)
        state_js = self.read(os.path.join(folder, board.STATE_FILE))
        self.assertTrue(state_js.startswith("window.BOARD = ") and state_js.endswith(";\n"))
        page = self.read(os.path.join(folder, board.BOARD_FILE))
        self.assertEqual(page, TEMPLATE.replace("  " + MARKER + "\n", ""))
        report = self.read(os.path.join(folder, board.REPORT_FILE))
        self.assertNotIn(MARKER, report)
        self.assertIn("window.BOARD_FROZEN = true;</script>", report)
        frozen = json.loads(re.search(r"window\.BOARD = (.*); window\.BOARD_FROZEN", report).group(1))
        self.assertEqual(frozen["state"], "finished")

    def test_writes_leave_no_temporary_file(self):
        folder = self.start()
        board.render(folder, at(1), env={}, template=self.template)
        board.close(folder, at(2), env={}, template=self.template)
        self.assertEqual(sorted(os.listdir(folder)), sorted([
            board.MARKER_FILE, board.EVENTS_FILE, board.STATE_FILE, board.BOARD_FILE, board.REPORT_FILE,
        ]))

    def test_a_failed_write_removes_its_temporary_file(self):
        target = os.path.join(self.tmp.name, "out.js")
        with mock.patch.object(board.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                board.write_atomic(target, "x")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["project", "template.html"])

    def test_a_template_without_one_marker_line_raises(self):
        for broken in ("<html></html>\n", TEMPLATE + MARKER + "\n", "<div>" + MARKER + "</div>\n"):
            with self.assertRaises(board.TemplateError):
                board.board_page(broken)
            with self.assertRaises(board.TemplateError):
                board.report_page(broken, {})

    def test_a_board_is_not_written_from_a_broken_template(self):
        broken = self.write(os.path.join(self.tmp.name, "broken.html"), TEMPLATE + MARKER + "\n")
        folder = self.start()
        os.remove(os.path.join(folder, board.BOARD_FILE))
        with self.assertRaises(board.TemplateError):
            board.render(folder, at(1), env={}, template=broken)
        self.assertFalse(os.path.exists(os.path.join(folder, board.BOARD_FILE)))

    def test_board_html_is_written_only_when_missing(self):
        folder = self.start()
        page = os.path.join(folder, board.BOARD_FILE)
        self.write(page, "kept")
        board.render(folder, at(1), env={}, template=self.template)
        self.assertEqual(self.read(page), "kept")

    def test_start_twice_keeps_the_first_board(self):
        folder = self.start("first")
        again = board.start(self.project, SESSION, at(5), "second", env={}, template=self.template)
        self.assertIsNone(again)
        log = board.events(folder)
        self.assertEqual([e["kind"] for e in log], ["start"])
        self.assertEqual(log[0]["title"], "first")
        self.assertEqual(self.read(os.path.join(folder, board.MARKER_FILE)), SESSION)

    def test_a_session_id_cannot_leave_the_boards_folder(self):
        for bad in ("../x", "a/b", "..", ""):
            with self.assertRaises(ValueError):
                board.board_dir(self.project, bad)

    def test_the_board_is_invisible_to_git_status(self):
        self.make_repository()
        self.commit("initial")
        self.start()
        self.assertEqual(git(self.project, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.read(os.path.join(self.project, ".logbook", ".gitignore")), "*\n")


class Commits(Workspace):
    def test_commits_since_start_on_its_branch_and_on_new_branches_but_not_older_ones(self):
        self.make_repository()
        self.commit("before the board")
        git(self.project, "branch", "older")
        folder = self.start()
        after = self.commit("on main after the start")
        git(self.project, "checkout", "-q", "older")
        self.commit("another session's work on its older branch")
        git(self.project, "checkout", "-q", "-b", "newer", "main")
        newer = self.commit("on a branch made after the start")
        git(self.project, "checkout", "-q", "main")

        state = board.render(folder, at(10), env={}, template=self.template)
        self.assertEqual([(c["hash"], c["subject"], c["branch"]) for c in state["commits"]], [
            (after, "on main after the start", "main"),
            (newer, "on a branch made after the start", "newer"),
        ])
        for item in state["commits"]:
            self.assertRegex(item["time"], UTC)
            self.assertIsNone(item["step"])

    def test_a_recorded_head_that_no_longer_exists_gives_no_error(self):
        self.make_repository()
        self.commit("before")
        start = {"branch": "main", "heads": {"main": git(self.project, "rev-parse", "HEAD"), "gone": "0" * 40}}
        self.commit("after")
        self.assertEqual([c["subject"] for c in board.commits(self.project, start)], ["after"])

    def test_git_reads_the_project_even_inside_a_git_hook(self):
        self.make_repository()
        self.commit("before")
        start = {"branch": "main", "heads": {"main": git(self.project, "rev-parse", "HEAD")}}
        self.commit("after")
        elsewhere = os.path.join(self.tmp.name, "elsewhere.git")
        with mock.patch.dict(os.environ, {"GIT_DIR": elsewhere, "GIT_INDEX_FILE": os.path.join(elsewhere, "index")}):
            self.assertEqual([c["subject"] for c in board.commits(self.project, start)], ["after"])
            self.assertEqual(board.branch_heads(self.project)[0], "main")
        self.assertFalse(os.path.exists(elsewhere))

    def test_a_project_that_is_not_a_repository_has_no_commits(self):
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp.name}):
            folder = self.start()
            self.assertIsNone(board.events(folder)[0]["branch"])
            self.assertEqual(board.render(folder, at(1), env={}, template=self.template)["commits"], [])
            self.assertEqual(board.commits(self.project, {"branch": "main", "heads": {}}), [])

    def test_no_git_gives_no_commits(self):
        with mock.patch.object(board.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(board.commits(self.project, {"branch": "main", "heads": {}}), [])
        with mock.patch.object(board.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertEqual(board.commits(self.project, {"branch": "main", "heads": {}}), [])


class CommandLine(Workspace):
    def run_main(self, *argv):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = board.main(list(argv))
        return code, err.getvalue()

    def test_render_and_close(self):
        folder = self.start()
        with mock.patch.object(board, "DEFAULT_TEMPLATE", self.template):
            self.assertEqual(self.run_main("render", "--board", folder), (0, ""))
            self.assertEqual(self.run_main("close", "--board", folder), (0, ""))
        self.assertTrue(os.path.exists(os.path.join(folder, board.REPORT_FILE)))
        self.assertEqual(board.events(folder)[-1]["kind"], "close")

    def test_a_missing_board_is_one_line_and_exit_1(self):
        for command in ("render", "close"):
            code, err = self.run_main(command, "--board", os.path.join(self.tmp.name, "nowhere"))
            self.assertEqual(code, 1)
            self.assertEqual(err.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
