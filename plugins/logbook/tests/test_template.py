"""The page template: its contract with the recorder, and the logic that decides what the page says.

The static tests read the template as text and run everywhere. The logic tests run the template's own
script under node, which the repository's gate does not require, so they are skipped where node is
missing and say why. The script keeps that logic in pure functions (the PB object) for this reason.
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timezone

from test_fixtures import ITEM_KEYS, STATE_KEYS, TIME_KEYS, UTC

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, os.pardir, "board", "template.html")
FIXTURES = os.path.join(HERE, "fixtures")
MARKER = "<!-- logbook:state -->"
STATES = ("state-live.json", "state-started.json", "state-stuck.json", "state-finished.json")
NODE = shutil.which("node")
NEEDS_NODE = "node is not installed; the page's logic runs only under node"


def read_template():
    with open(TEMPLATE, encoding="utf-8") as f:
        return f.read()


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def page_script(html):
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, f"expected the page's one script, found {len(scripts)}"
    return scripts[0]


def ms(iso):
    """A UTC timestamp as the milliseconds a browser clock would read."""
    moment = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def pb(expression, **values):
    """Evaluate a JavaScript expression against the page's PB, with each value bound by name as JSON.

    The program goes to node on stdin, so no file is written."""
    bindings = "".join(f"var {name} = {json.dumps(value)};\n" for name, value in values.items())
    program = (page_script(read_template()) + "\n" + bindings
               + "process.stdout.write(JSON.stringify(" + expression + "));\n")
    run = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True, timeout=60, check=False)
    if run.returncode != 0:
        raise AssertionError(f"node failed on {expression}:\n{run.stderr}")
    return json.loads(run.stdout)


def contrast(a, b):
    def lum(hex_colour):
        h = hex_colour.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class RecorderContract(unittest.TestCase):
    """What the recorder relies on when it turns the template into board.html or report.html."""

    def setUp(self):
        self.html = read_template()

    def test_the_marker_is_one_line_of_its_own(self):
        self.assertEqual(self.html.count(MARKER), 1)
        self.assertEqual([line for line in self.html.splitlines() if line == MARKER], [MARKER])

    def test_the_marker_comes_before_the_pages_script(self):
        # The report inlines the state at the marker, so the state must exist before the script runs.
        self.assertLess(self.html.index(MARKER), self.html.index("<script"))

    def test_the_page_has_one_script_and_no_script_file_of_its_own(self):
        self.assertEqual(self.html.count("</script>"), 1)
        outside = self.html.replace(page_script(self.html), "")
        self.assertNotRegex(outside, r"<script[^>]*\bsrc=")


class NothingFromTheNetwork(unittest.TestCase):
    def test_nothing_loads_from_the_network(self):
        html = read_template()
        for pattern in (
            r"""(src|href)=["']?(https?:)?//""",
            r"@import",
            r"fetch\(",
            r"XMLHttpRequest",
            r"""url\(["']?https?:""",
            r"<link\b",
            r"<img\b",
            r"<iframe\b",
            r"@font-face",
        ):
            self.assertIsNone(re.search(pattern, html), pattern)


class StateIsNeverMarkup(unittest.TestCase):
    """Every string in the state is untrusted, so the script never parses anything as HTML."""

    def setUp(self):
        self.script = page_script(read_template())

    def test_no_html_parsing_api_is_used(self):
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "DOMParser", "createContextualFragment",
                    "eval(", "new Function"):
            self.assertNotIn(api, self.script)

    def test_the_one_document_write_carries_no_state(self):
        writes = re.findall(r"document\.write\((.*)\);", self.script)
        self.assertEqual(writes, [
            """'<script id="pb-state" src="state.js?t=' + Date.now() + '"><\\/script><script>PB.afterState();<\\/script>'"""
        ])

    def test_a_link_is_made_only_through_safe_url(self):
        self.assertEqual(len(re.findall(r"\.href\s*=", self.script)), 1)
        self.assertNotIn("setAttribute('href'", self.script)
        self.assertRegex(self.script, r"var safe = PB\.safeUrl\(url\);\s+if \(!safe\) return null;")

    def test_every_storage_access_is_guarded(self):
        lines = [line for line in self.script.splitlines() if "localStorage" in line]
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("try {", line)


class Styles(unittest.TestCase):
    def setUp(self):
        self.css = re.search(r"<style>(.*?)</style>", read_template(), re.S).group(1)

    def dark_blocks(self):
        system = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\}", self.css, re.S)
        chosen = re.search(r":root\[data-theme=\"dark\"\]\s*\{(.*?)\}", self.css, re.S)
        return system, chosen

    def test_dark_is_defined_for_the_system_and_for_the_switch_alike(self):
        system, chosen = self.dark_blocks()
        self.assertIsNotNone(system)
        self.assertIsNotNone(chosen)

        def tokens(block):
            return sorted(d.strip() for d in block.group(1).split(";") if d.strip())
        self.assertEqual(tokens(system), tokens(chosen))

    def test_body_has_an_explicit_background(self):
        body = re.search(r"\nbody\s*\{(.*?)\}", self.css, re.S).group(1)
        self.assertIn("background: var(--bg)", body)

    def test_motion_and_print_are_provided_for(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("@media print", self.css)

    def test_group_title_wraps_a_long_unbroken_subject(self):
        rule = re.search(r"\.group-title\s*\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(rule)
        self.assertIn("overflow-wrap: anywhere", rule.group(1))


class StateFixtures(unittest.TestCase):
    """The three extra states the page is tested on hold to the same contract as the reference."""

    def test_every_state_fixture_carries_exactly_the_contract_keys(self):
        for name in STATES:
            state = fixture(name)
            self.assertEqual(set(state), STATE_KEYS, name)
            self.assertEqual(set(state["settings"]), ITEM_KEYS["settings"], name)
            for key, keys in ITEM_KEYS.items():
                if key == "settings":
                    continue
                for item in state[key]:
                    self.assertEqual(set(item), keys, f"{name}: {key}")

    def test_times_are_utc(self):
        def walk(value, key, name):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k, name)
            elif isinstance(value, list):
                for v in value:
                    walk(v, key, name)
            elif key in TIME_KEYS and value is not None:
                self.assertRegex(value, UTC, f"{name}: {key}")
        for name in STATES:
            walk(fixture(name), None, name)

    def test_started_is_seconds_old_with_five_pending_steps(self):
        state = fixture("state-started.json")
        self.assertEqual(state["titleSource"], "prompt")
        self.assertLess(ms(state["updated"]) - ms(state["started"]), 60_000)
        self.assertEqual([s["status"] for s in state["steps"]], ["pending"] * 5)
        for key in ("questions", "commits", "deliverables", "checks", "decisions", "agents"):
            self.assertEqual(state[key], [], key)

    def test_stuck_has_a_step_in_progress_a_failed_check_and_a_failed_agent(self):
        state = fixture("state-stuck.json")
        self.assertEqual(state["state"], "live")
        self.assertIn("in_progress", [s["status"] for s in state["steps"]])
        self.assertIn("fail", [c["result"] for c in state["checks"]])
        self.assertIn("failed", [a["outcome"] for a in state["agents"]])

    def test_finished_has_every_step_done_and_every_question_answered(self):
        state = fixture("state-finished.json")
        self.assertEqual(state["state"], "finished")
        self.assertEqual({s["status"] for s in state["steps"]}, {"completed"})
        self.assertTrue(state["questions"])
        self.assertEqual({q["status"] for q in state["questions"]}, {"answered"})


@unittest.skipUnless(NODE, NEEDS_NODE)
class PageLogic(unittest.TestCase):
    """The page's pure functions, run as the browser would run them, on the fixture states."""

    def test_ages_read_the_way_a_person_would_say_them(self):
        spans = [0, 3_000, 12_000, 59_000, 60_000, 270_000, 3_600_000, 3_900_000, 86_400_000, 3 * 86_400_000, -5_000]
        self.assertEqual(pb("spans.map(PB.formatAge)", spans=spans), [
            "just now", "just now", "12 s ago", "59 s ago", "1 min ago", "4 min ago", "1 h ago", "1 h 5 min ago",
            "1 day ago", "3 days ago", "just now",
        ])

    def test_durations_keep_their_seconds_under_an_hour(self):
        self.assertEqual(pb("spans.map(PB.formatDuration)", spans=[45_000, 240_000, 270_000, 3_900_000]),
                         ["45 s", "4 min", "4 min 30 s", "1 h 5 min"])

    def test_a_finished_agent_is_timed_by_its_ends_and_a_running_one_by_the_clock(self):
        state = fixture("state-live.json")
        now = ms("2026-01-05T09:45:00Z")
        self.assertEqual(pb("s.agents.map(function (a) { return PB.agentDuration(a, now); })", s=state, now=now),
                         [270_000, 300_000])

    def test_a_quiet_live_board_has_its_step_in_progress_stuck(self):
        state = fixture("state-stuck.json")
        now = ms(state["updated"]) + 601_000
        result = pb("PB.stuck(s, now)", s=state, now=now)
        self.assertEqual(result["title"], "Stuck")
        self.assertEqual([(i["kind"], i["id"]) for i in result["items"]],
                         [("step", "3"), ("check", "C2"), ("agent", "ef56ab78cab12cd34")])

    def test_a_step_is_not_stuck_before_the_threshold(self):
        state = fixture("state-stuck.json")
        now = ms(state["updated"]) + 599_000
        kinds = pb("PB.stuck(s, now).items.map(function (i) { return i.kind; })", s=state, now=now)
        self.assertEqual(kinds, ["check", "agent"])

    def test_an_idle_board_is_waiting_for_you_not_stuck(self):
        state = dict(fixture("state-stuck.json"), state="idle")
        now = ms(state["updated"]) + 3_600_000
        kinds = pb("PB.stuck(s, now).items.map(function (i) { return i.kind; })", s=state, now=now)
        self.assertEqual(kinds, ["check", "agent"])

    def test_a_finished_board_shows_its_failures_as_failures(self):
        state = dict(fixture("state-stuck.json"), state="finished")
        now = ms(state["updated"]) + 3_600_000
        result = pb("PB.stuck(s, now)", s=state, now=now)
        self.assertEqual(result["title"], "Failures")
        self.assertEqual([i["kind"] for i in result["items"]], ["check", "agent"])

    def test_empty_sections_are_hidden_and_the_rest_keep_the_pages_order(self):
        cases = {
            "state-started.json": ["steps"],
            "state-live.json": [
                "needs", "stuck", "steps", "built", "changed", "verified", "decisions", "commands", "agents",
            ],
            "state-stuck.json": ["stuck", "steps", "built", "verified", "agents"],
            "state-finished.json": ["steps", "built", "verified", "decisions", "agents"],
        }
        for name, expected in cases.items():
            state = fixture(name)
            now = ms(state["updated"]) + 30_000
            self.assertEqual(pb("PB.sections(s, now)", s=state, now=now), expected, name)

    def test_a_tile_per_tab_with_its_big_number_and_what_it_counts(self):
        state = fixture("state-live.json")
        now = ms(state["updated"]) + 30_000
        tiles = pb("PB.tiles(s, now)", s=state, now=now)
        self.assertEqual([t["id"] for t in tiles], ["built", "changed", "verified", "commands", "agents"])
        by = {t["id"]: t for t in tiles}
        self.assertEqual(by["built"]["big"], str(len(state["commits"])))
        self.assertEqual(by["changed"]["big"], str(len(state["changes"])))
        self.assertEqual(by["agents"]["big"], str(len(state["agents"])))
        self.assertIn("tok", by["agents"]["sub"])
        self.assertTrue(all(t["label"] and t["big"].isdigit() for t in tiles))

    def test_a_board_without_a_tabs_content_has_no_tile_for_it(self):
        state = fixture("state-started.json")
        self.assertEqual(pb("PB.tiles(s, now)", s=state, now=ms(state["updated"])), [])

    def test_answered_questions_leave_needs_you(self):
        state = fixture("state-live.json")
        self.assertEqual(pb("PB.openQuestions(s).map(function (q) { return q.id; })", s=state), ["Q1", "Q2"])
        self.assertEqual(pb("PB.answeredQuestions(s).map(function (q) { return q.id; })", s=state), ["Q3"])

    def test_hard_stops_come_first(self):
        state = fixture("state-live.json")
        state["questions"].reverse()
        self.assertEqual(pb("PB.openQuestions(s).map(function (q) { return q.id; })", s=state), ["Q1", "Q2"])

    def test_a_live_board_says_when_it_has_stopped_receiving_updates(self):
        state = fixture("state-live.json")
        updated = ms(state["updated"])
        threshold = state["settings"]["stuckAfterSeconds"] * 1000
        self.assertFalse(pb("PB.isStale(s, now, false)", s=state, now=updated + threshold))
        self.assertTrue(pb("PB.isStale(s, now, false)", s=state, now=updated + threshold + 1_000))
        # Frozen, idle and finished boards never say so, however old.
        self.assertFalse(pb("PB.isStale(s, now, true)", s=state, now=updated + threshold + 1_000))
        idle = dict(state, state="idle")
        self.assertFalse(pb("PB.isStale(s, now, false)", s=idle, now=updated + 3_600_000))
        finished = dict(state, state="finished")
        self.assertFalse(pb("PB.isStale(s, now, false)", s=finished, now=updated + 3_600_000))

    def test_the_viewers_choice_overrides_the_boards_theme(self):
        cases = [
            [None, "dark", False, {"mode": "dark", "applied": "dark"}],
            ["light", "dark", True, {"mode": "light", "applied": "light"}],
            [None, "system", True, {"mode": "system", "applied": "dark"}],
            [None, "system", False, {"mode": "system", "applied": "light"}],
            ["bogus", "sepia", True, {"mode": "system", "applied": "dark"}],
        ]
        for stored, setting, dark, expected in cases:
            self.assertEqual(pb("PB.resolveTheme(a, b, c)", a=stored, b=setting, c=dark), expected)

    def test_only_web_links_become_links(self):
        urls = ["https://example.com/pull/12", "HTTP://example.com/", "javascript:alert(1)", "JavaScript:alert(1)",
                " javascript:alert(1)", "data:text/html,<b>x</b>", "//example.com/", "src/reports/export.py", None]
        self.assertEqual(pb("u.map(PB.safeUrl)", u=urls),
                         ["https://example.com/pull/12", "HTTP://example.com/", None, None, None, None, None, None, None])

    def test_the_accent_is_hex_or_nothing(self):
        values = ["#0f766e", "#FFF", "red", "#12345", "#fff;background:url(x)", "url(x)", None]
        self.assertEqual(pb("v.map(PB.safeAccent)", v=values), ["#0f766e", "#fff", None, None, None, None, None])

    def test_text_on_any_accent_stays_readable(self):
        accents = ["#ffff00", "#0f766e", "#000080", "#2f64c8", "#777777", "#767676", "#808080", "#ff0000",
                   "#00ff00", "#f0f", "#fff", "#000"]
        inks = pb("v.map(PB.accentInk)", v=accents)
        for accent, ink in zip(accents, inks):
            self.assertGreaterEqual(contrast(accent, ink), 4.5, f"{ink} on {accent}")
        self.assertIsNone(pb("PB.accentInk('red')"))

    def test_the_copy_button_copies_the_id_a_colon_and_a_space(self):
        self.assertEqual(pb("PB.copyText({id: 'Q3'})"), "Q3: ")

    def test_built_items_sit_under_their_step_and_the_rest_last(self):
        state = fixture("state-live.json")
        state["commits"].append(dict(state["commits"][0], hash="9f9f9f9", step="99"))
        groups = pb("PB.builtGroups(s).map(function (g) { return [g.step && g.step.id,"
                    " g.commits.map(function (c) { return c.hash; }),"
                    " g.deliverables.map(function (d) { return d.label; })]; })", s=state)
        self.assertEqual(groups, [["2", ["a1b2c3d"], ["The exporter"]], [None, ["e4f5a6b", "9f9f9f9"], []]])

    def test_running_agents_come_first(self):
        state = fixture("state-live.json")
        self.assertEqual(pb("PB.orderedAgents(s).map(function (a) { return a.outcome; })", s=state),
                         ["running", "failed"])

    def test_no_status_is_told_by_colour_alone(self):
        cases = [["board", "live"], ["board", "idle"], ["board", "finished"], ["step", "completed"],
                 ["step", "in_progress"], ["step", "pending"], ["check", "pass"], ["check", "fail"],
                 ["agent", "running"], ["agent", "finished"], ["agent", "failed"], ["agent", "cancelled"]]
        for status in pb("c.map(function (x) { return PB.statusOf(x[0], x[1]); })", c=cases):
            self.assertTrue(status["glyph"].strip())
            self.assertTrue(status["word"].strip())
        self.assertEqual(pb("PB.statusOf('agent', 'cancelled').word"), "cancelled")

    def test_list_drops_entries_that_are_not_objects(self):
        self.assertEqual(pb("PB.list(s, 'steps')", s={"steps": [None, {"id": "1"}, "x", 3]}),
                          [{"id": "1"}])
        self.assertEqual(pb("PB.list(s, 'steps')", s={"steps": None}), [])
        self.assertEqual(pb("PB.list(s, 'steps')", s={}), [])

    def test_a_links_key_comes_from_its_position_not_its_text(self):
        self.assertEqual(pb("PB.linkKey(step, 2)", step={"id": "3"}), "link-3-2")
        self.assertEqual(pb("PB.linkKey(step, 0)", step=None), "link-none-0")

    def test_a_schema_other_than_one_is_flagged(self):
        self.assertFalse(pb("PB.schemaMismatch(s)", s={"schema": 1}))
        self.assertTrue(pb("PB.schemaMismatch(s)", s={"schema": 2}))
        self.assertTrue(pb("PB.schemaMismatch(s)", s={}))

    def test_settings_fall_back_to_their_defaults(self):
        self.assertEqual(pb("[PB.densityOf('compact'), PB.densityOf('roomy'), PB.densityOf(null)]"),
                         ["compact", "comfortable", "comfortable"])
        self.assertEqual(pb("[PB.refreshSeconds({refreshSeconds: 10}), PB.refreshSeconds({}),"
                            " PB.refreshSeconds({refreshSeconds: 0.5}), PB.refreshSeconds(null)]"), [10, 10, 2, 10])


if __name__ == "__main__":
    unittest.main()
