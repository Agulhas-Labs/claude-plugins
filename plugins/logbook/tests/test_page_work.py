"""The page's Changed and Commands sections, the test runs under Verified, and failed tests under Stuck.

The states are built here from the live fixture plus the three keys the recorder writes for them
(changes, commands, commandsTotal), so these tests do not depend on what the fixtures carry.
"""
import re
import unittest

from test_template import NEEDS_NODE, NODE, fixture, ms, pb, read_template

NOW = ms("2026-01-05T09:45:00Z")


def change(path, first, last, **extra):
    row = {"path": path, "edits": 1, "added": None, "removed": None, "created": False,
           "first": first, "last": last, "agents": []}
    row.update(extra)
    return row


def command(text, time, result="pass", test=False, **extra):
    row = {"command": text, "description": None, "result": result, "exit": 0 if result == "pass" else None,
           "runs": 1, "fails": 1 if result == "fail" else 0, "time": time, "ms": None, "test": test, "agents": []}
    row.update(extra)
    return row


def without_work(state):
    for key in ("changes", "commands", "commandsTotal"):
        state.pop(key, None)
    return state


def live(**work):
    state = without_work(fixture("state-live.json"))
    state.update(work)
    return state


@unittest.skipUnless(NODE, NEEDS_NODE)
class Sections(unittest.TestCase):
    def test_changed_and_commands_take_their_places_in_the_pages_order(self):
        state = live(changes=[change("src/a.py", "2026-01-05T09:10:00Z", "2026-01-05T09:10:00Z")],
                     commands=[command("git status", "2026-01-05T09:11:00Z")], commandsTotal=1)
        self.assertEqual(pb("PB.sections(s, now)", s=state, now=NOW),
                         ["needs", "stuck", "steps", "built", "changed", "verified", "decisions", "commands", "agents"])

    def test_each_is_hidden_when_empty_or_missing(self):
        empty = live(changes=[], commands=[], commandsTotal=0)
        missing = live()
        before = ["needs", "stuck", "steps", "built", "verified", "decisions", "agents"]
        self.assertEqual(pb("PB.sections(s, now)", s=empty, now=NOW), before)
        self.assertEqual(pb("PB.sections(s, now)", s=missing, now=NOW), before)

    def test_commands_is_hidden_when_every_command_is_a_test(self):
        state = live(commands=[command("python -m unittest", "2026-01-05T09:11:00Z", test=True)])
        self.assertNotIn("commands", pb("PB.sections(s, now)", s=state, now=NOW))

    def test_verified_is_shown_for_a_test_command_alone(self):
        state = live(commands=[command("python -m unittest", "2026-01-05T09:11:00Z", test=True)])
        state["checks"] = []
        self.assertIn("verified", pb("PB.sections(s, now)", s=state, now=NOW))

    def test_a_state_without_the_new_keys_renders_the_same_sections_as_before(self):
        cases = {
            "state-started.json": ["steps"],
            "state-live.json": ["needs", "stuck", "steps", "built", "verified", "decisions", "agents"],
            "state-stuck.json": ["stuck", "steps", "built", "verified", "agents"],
            "state-finished.json": ["steps", "built", "verified", "decisions", "agents"],
        }
        for name, expected in cases.items():
            state = without_work(fixture(name))
            now = ms(state["updated"]) + 30_000
            self.assertEqual(pb("PB.sections(s, now)", s=state, now=now), expected, name)


@unittest.skipUnless(NODE, NEEDS_NODE)
class Changed(unittest.TestCase):
    def test_the_most_recently_changed_file_comes_first(self):
        state = live(changes=[
            change("src/a.py", "2026-01-05T09:01:00Z", "2026-01-05T09:30:00Z"),
            change("src/b.py", "2026-01-05T09:02:00Z", "2026-01-05T09:40:00Z"),
            change("src/c.py", "2026-01-05T09:03:00Z", "2026-01-05T09:05:00Z"),
            change("src/d.py", "2026-01-05T09:04:00Z", None),
        ])
        self.assertEqual(pb("PB.changedFiles(s).map(function (f) { return f.path; })", s=state),
                         ["src/b.py", "src/a.py", "src/c.py", "src/d.py"])

    def test_line_counts_show_when_known_and_edits_when_more_than_one(self):
        rows = [change("a", None, None, added=12, removed=3, edits=4), change("b", None, None),
                change("c", None, None, added=5), change("d", None, None, edits=2)]
        self.assertEqual(pb("r.map(PB.changeCounts)", r=rows),
                         [["+12 −3", "4 edits"], [], ["+5"], ["2 edits"]])


@unittest.skipUnless(NODE, NEEDS_NODE)
class Commands(unittest.TestCase):
    def state(self):
        return live(commands=[
            command("git status", "2026-01-05T09:10:00Z"),
            command("ls missing", "2026-01-05T09:12:00Z", result="fail", exit=1),
            command("python -m unittest", "2026-01-05T09:20:00Z", test=True),
            command("git diff", "2026-01-05T09:30:00Z"),
            command("npm run dev", "2026-01-05T09:31:00Z", result="background"),
            command("cat nope", "2026-01-05T09:25:00Z", result="fail", exit=1),
        ])

    def test_failed_commands_come_first_then_the_rest_newest_first(self):
        self.assertEqual(pb("PB.otherCommands(s).map(function (c) { return c.command; })", s=self.state()),
                         ["cat nope", "ls missing", "npm run dev", "git diff", "git status"])

    def test_test_commands_are_not_in_commands_but_in_verified_after_the_checks(self):
        state = self.state()
        state["commands"].append(command("pytest -q", "2026-01-05T09:35:00Z", test=True))
        rows = pb("PB.verifiedRows(s).map(function (r) { return [r.kind, r.item.id || r.item.command]; })", s=state)
        self.assertEqual(rows, [["check", "C1"], ["check", "C2"], ["test", "pytest -q"], ["test", "python -m unittest"]])

    def test_the_result_says_only_what_is_known(self):
        rows = [{"result": "pass", "exit": 0}, {"result": "fail", "exit": 2}, {"result": "fail", "exit": None},
                {"result": "background", "exit": None}]
        statuses = pb("r.map(PB.commandStatus)", r=rows)
        self.assertEqual([s["word"] for s in statuses], ["exited 0", "failed, exit 2", "failed", "started in the background"])
        # Never told by colour alone.
        for status in statuses + [pb("PB.commandStatus({result: 'odd'})")]:
            self.assertTrue(status["glyph"].strip())
            self.assertTrue(status["word"].strip())

    def test_runs_are_counted_only_when_there_was_more_than_one(self):
        rows = [{"runs": 1, "fails": 0}, {"runs": 3, "fails": 1}, {"runs": 2, "fails": 0}, {}]
        self.assertEqual(pb("r.map(PB.runsText)", r=rows), ["", "ran 3 times, 1 failed", "ran 2 times, none failed", ""])

    def test_the_page_says_so_when_the_rows_do_not_account_for_every_run(self):
        state = self.state()
        state["commands"][0]["runs"] = 4
        listed = 4 + 5
        state["commandsTotal"] = listed
        self.assertIsNone(pb("PB.commandsNote(s)", s=state))
        state["commandsTotal"] = 250
        self.assertIn(f"{listed} of 250", pb("PB.commandsNote(s)", s=state))
        del state["commandsTotal"]
        self.assertIsNone(pb("PB.commandsNote(s)", s=state))


@unittest.skipUnless(NODE, NEEDS_NODE)
class Stuck(unittest.TestCase):
    def kinds(self, commands):
        state = live(commands=commands)
        return [(i["kind"], i["id"]) for i in pb("PB.stuck(s, now).items", s=state, now=NOW)]

    def test_a_failed_test_is_stuck(self):
        items = self.kinds([command("pytest", "2026-01-05T09:20:00Z", result="fail", exit=1, test=True)])
        self.assertIn(("test", "pytest"), items)

    def test_a_failed_test_followed_by_a_passing_test_is_not(self):
        items = self.kinds([command("pytest -k quoting", "2026-01-05T09:20:00Z", result="fail", exit=1, test=True),
                            command("pytest", "2026-01-05T09:25:00Z", test=True)])
        self.assertNotIn("test", [kind for kind, _ in items])

    def test_a_pass_before_the_failure_does_not_clear_it(self):
        items = self.kinds([command("pytest", "2026-01-05T09:10:00Z", test=True),
                            command("pytest -k quoting", "2026-01-05T09:20:00Z", result="fail", test=True)])
        self.assertIn(("test", "pytest -k quoting"), items)

    def test_a_failed_command_that_is_not_a_test_is_not(self):
        items = self.kinds([command("ls missing", "2026-01-05T09:20:00Z", result="fail", exit=1)])
        self.assertEqual([kind for kind, _ in items], ["check", "agent"])


class NewCodeIsNeverMarkup(unittest.TestCase):
    """The new sections and their rows put state in as text, like the rest of the page."""

    def test_the_new_code_uses_no_html_parsing_api(self):
        script = read_template()
        pieces = [
            re.search(r"\n  function changeItem\(.*?\n  \}\n", script, re.S),
            re.search(r"\n  function commandItem\(.*?\n  \}\n", script, re.S),
            re.search(r"\n    changed: function .*?\n    \},\n", script, re.S),
            re.search(r"\n    commands: function .*?\n    \},\n", script, re.S),
        ]
        for piece in pieces:
            self.assertIsNotNone(piece)
            for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "DOMParser", "createContextualFragment",
                        "eval(", "new Function", "document.write", ".href"):
                self.assertNotIn(api, piece.group(0))


if __name__ == "__main__":
    unittest.main()
