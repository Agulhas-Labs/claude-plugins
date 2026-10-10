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


class ImagesStayLocal(unittest.TestCase):
    """The page shows images from its own folder and nowhere else."""

    def test_images_may_come_only_from_a_file_or_the_pages_own_origin(self):
        policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', read_template()).group(1)
        directives = dict(d.strip().split(" ", 1) for d in policy.split(";") if d.strip())
        self.assertEqual(set(directives["img-src"].split()), {"'self'", "file:"})
        self.assertEqual(directives["connect-src"], "'none'")

    def test_an_image_source_is_set_only_from_safe_image(self):
        script = page_script(read_template())
        sources = re.findall(r"^\s*(\w+)\.src\s*=\s*(.*);$", script, re.M)
        self.assertEqual(sources, [("img", "src"), ("tag", "'state.js?t=' + Date.now()")])
        self.assertRegex(script, r"var src = PB\.safeImage\(d\.image\);\s+if \(!src\) return;")


# The fields the rulings page reads beyond the reference contract. A board written before them lacks them.
RULINGS_STATE_KEYS = STATE_KEYS | {"summary"}
RULINGS_ITEM_KEYS = dict(ITEM_KEYS, decisions=ITEM_KEYS["decisions"] | {"group", "yours", "revised"},
                         deliverables=ITEM_KEYS["deliverables"] | {"image"})


class RulingsFixture(unittest.TestCase):
    def test_the_rulings_fixture_carries_the_contract_and_the_rulings_fields(self):
        state = fixture("state-rulings.json")
        self.assertEqual(set(state), RULINGS_STATE_KEYS)
        self.assertEqual(set(state["summary"]), {"text", "facts", "time"})
        for key, keys in RULINGS_ITEM_KEYS.items():
            if key == "settings":
                continue
            for item in state[key]:
                self.assertEqual(set(item), keys, key)

    def test_it_holds_what_the_rulings_view_is_proven_on(self):
        state = fixture("state-rulings.json")
        decisions = state["decisions"]
        self.assertGreaterEqual(len(decisions), 4)
        self.assertGreaterEqual(len({d["group"] for d in decisions if d["group"]}), 2)
        self.assertTrue(any(d["yours"] for d in decisions))
        self.assertTrue(any(d["group"] is None for d in decisions))
        statuses = [(q["status"], q["hardStop"]) for q in state["questions"]]
        self.assertIn(("answered", False), statuses)
        self.assertIn(("open", False), statuses)
        self.assertIn(("open", True), statuses)
        self.assertEqual(len([d for d in state["deliverables"] if d["image"]]), 2)


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
                "needs", "rulings", "stuck", "steps", "built", "changed", "verified", "commands", "agents",
            ],
            "state-stuck.json": ["stuck", "steps", "built", "verified", "agents"],
            "state-finished.json": ["rulings", "steps", "built", "verified", "agents"],
            "state-rulings.json": [
                "needs", "looks", "rulings", "stuck", "steps", "built", "changed", "verified", "commands", "agents",
            ],
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


@unittest.skipUnless(NODE, NEEDS_NODE)
class Rulings(unittest.TestCase):
    """The rulings page's logic: the lede, the images, the rulings and the line the viewer copies."""

    def setUp(self):
        self.state = fixture("state-rulings.json")

    def test_rulings_are_grouped_in_order_of_first_appearance_and_the_ungrouped_go_last(self):
        groups = pb("PB.rulings(s).map(function (g) { return [g.name, g.items.map(function (i) { return i.id; })]; })",
                    s=self.state)
        self.assertEqual(groups, [["Data format", ["D1", "D3"]], ["Page layout", ["D2", "D5"]], [None, ["D4", "Q3"]]])

    def test_a_ruling_carries_its_title_why_reversal_and_whose_call_it_was(self):
        items = {i["id"]: i for g in pb("PB.rulings(s)", s=self.state) for i in g["items"]}
        self.assertEqual(items["D2"]["title"], "The download button sits in the page header.")
        self.assertTrue(items["D2"]["yours"])
        self.assertFalse(items["D1"]["yours"])
        self.assertEqual(items["D1"]["reverse"], "Change format_date in export.py.")
        self.assertEqual(items["D3"]["revised"], "2026-01-05T13:10:00Z")

    def test_an_answered_question_is_a_ruling_of_yours(self):
        q3 = pb("PB.rulings(s)[2].items[1]", s=self.state)
        self.assertEqual(q3, {"id": "Q3", "title": "Comma or semicolon as the separator?", "why": "Answered: Comma is fine.",
                              "reverse": "Change SEPARATOR in export.py.", "yours": True, "group": None, "revised": None})
        self.state["questions"][2]["reverse"] = None
        self.assertEqual(pb("PB.rulings(s)[2].items[1].reverse", s=self.state), "was running on: Comma.")

    def test_open_questions_stay_in_needs_you_and_are_not_rulings(self):
        self.assertEqual(pb("PB.openQuestions(s).map(function (q) { return q.id; })", s=self.state), ["Q1", "Q2"])
        ids = pb("[].concat.apply([], PB.rulings(s).map(function (g) { return g.items.map(function (i) { return i.id; }); }))",
                 s=self.state)
        self.assertNotIn("Q1", ids)
        self.assertNotIn("Q2", ids)

    def test_a_board_from_before_groups_has_its_rulings_in_one_unnamed_group(self):
        live = fixture("state-live.json")
        self.assertEqual(pb("PB.rulings(s)", s=live), [{"name": None, "items": [
            {"id": "D1", "title": live["decisions"][0]["text"], "why": live["decisions"][0]["why"],
             "reverse": live["decisions"][0]["reverse"], "yours": False, "group": None, "revised": None},
            {"id": "Q3", "title": "Comma or semicolon as the separator?", "why": "Answered: Comma is fine.",
             "reverse": "Change SEPARATOR in export.py.", "yours": True, "group": None, "revised": None},
        ]}])
        self.assertIsNone(pb("PB.summaryOf(s)", s=live))

    def test_the_answers_line_says_keep_all_until_a_ruling_is_unticked(self):
        head = 'Logbook "CSV export for the reports page" 5 Jan: '
        self.assertEqual(pb("PB.answersLine(s, {}, '')", s=self.state), head + "keep all")
        self.assertEqual(pb("PB.answersLine(s, {D2: false}, '')", s=self.state), head + "reverse D2")
        # In the order the page shows them, which is by group, and only what is false counts.
        self.assertEqual(pb("PB.answersLine(s, {Q3: false, D4: true, D5: false, D2: false}, '')", s=self.state),
                         head + "reverse D2, D5, Q3")
        self.assertEqual(pb("PB.answersLine(s, {D2: false}, n)", s=self.state, n="  Keep the  icon,\nbut bigger. "),
                         head + "reverse D2 | note: Keep the icon, but bigger.")
        self.assertEqual(pb("PB.answersLine(s, {}, 'Looks good')", s=self.state), head + "keep all | note: Looks good")

    def test_the_lede_and_the_facts_line_come_from_the_summary(self):
        self.assertEqual(pb("PB.summaryOf(s)", s=self.state),
                         {"text": self.state["summary"]["text"], "facts": self.state["summary"]["facts"]})
        self.assertIsNone(pb("PB.summaryOf({summary: null})"))
        self.assertIsNone(pb("PB.summaryOf({summary: {text: 3, facts: null}})"))

    def test_images_go_to_what_it_looks_like_and_the_other_deliverables_stay_in_built(self):
        self.assertEqual(pb("PB.gallery(s).map(function (d) { return d.label; })", s=self.state),
                         ["The reports page with the Download CSV button", "The same page on a phone"])
        self.assertEqual(pb("PB.builtDeliverables(s).map(function (d) { return d.label; })", s=self.state), ["The exporter"])
        built = pb("PB.tiles(s, now)[0]", s=self.state, now=ms(self.state["updated"]))
        self.assertEqual(built["sub"], "commits · 1 deliverable")

    def test_only_a_plain_relative_path_inside_the_board_is_an_image(self):
        paths = ["images/3-x.png", "shot.png", "images/3 x.png", "../secret.png", "images/../../x.png", "/etc/x.png",
                 "https://example.com/x.png", "file:///etc/x.png", "javascript:alert(1)", " //example.com/x.png",
                 "\t//example.com/x.png", "images\\..\\x.png", "\\\\host\\x.png", "images/%2e%2e/%2E%2E/x.png",
                 "./x.png", "C:/x.png", "", None, 3]
        self.assertEqual(pb("p.map(PB.safeImage)", p=paths),
                         ["images/3-x.png", "shot.png", "images/3 x.png"] + [None] * (len(paths) - 3))
        unsafe = dict(self.state, deliverables=[dict(self.state["deliverables"][1], image=p) for p in paths[3:]])
        self.assertEqual(pb("PB.gallery(s).length", s=unsafe), 0)
        self.assertEqual(pb("PB.builtDeliverables(s).length", s=unsafe), len(paths) - 3)


if __name__ == "__main__":
    unittest.main()
