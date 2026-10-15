"""Tests for the scorecard's two rung lines in scripts/agent_cost.py: builder handoffs naming their class, and a
scout's brief before the makers. Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests -p 'test_scorecard_rungs.py'"""
import importlib.machinery
import importlib.util
import os
import unittest
from datetime import timedelta
from unittest import mock

from test_agentcost import (BASE, FixtureRoot, ac, assistant, ts_str, tool_use_block, usage, user_text)

BRIEF = "/tmp/x/briefs/feat-a.md"


class RungFixture(unittest.TestCase):
    def scorecard(self, agents):
        """agents: (agent_type, prompt, start seconds, edits); returns the scorecard's lines."""
        fx = FixtureRoot(self)
        for i, (kind, prompt, start, edits) in enumerate(agents):
            at = lambda s: ts_str(BASE + timedelta(seconds=start + s))
            entries = [user_text(at(0), prompt),
                       assistant(f"a{i}", at(1), usage(input_tokens=1000), [tool_use_block(f"r{i}", "Grep", {"pattern": "x"})])]
            if edits:
                entries.append(assistant(f"e{i}", at(2), usage(input_tokens=1000),
                                         [tool_use_block(f"w{i}", "Edit", {"file_path": "/a", "old_string": "a", "new_string": "b"})]))
            fx.subagent(agent=f"a{i}", agent_type=kind, entries=entries)
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        out = []
        ac.section_rule_scorecard(out, loaded)
        return out

    def find(self, out, rule):
        return next((l for l in out if l.strip().startswith(rule)), None)


class ClassLineTests(RungFixture):
    def line(self, prompts, kind="builder"):
        return self.find(self.scorecard([(kind, p, i, False) for i, p in enumerate(prompts)]),
                         "builder handoffs name a class")

    def test_counts_builders_with_a_class_line_and_the_spend_of_the_rest(self):
        line = self.line(["Do it.\nRung: builder — security", "Do it."])
        self.assertIn("builder handoffs with a class line: 1 of 2 (the 1 without held 50% of builder spend);", line)
        self.assertIn("no baseline", line)

    def test_another_rung_or_prose_does_not_count(self):
        line = self.line(["Rung: builder-lite — security", "Please set the fix aside and restore work."])
        self.assertIn(": 0 of 2 (the 2 without held 100% of builder spend);", line)

    def test_n_a_without_builders(self):
        out = self.scorecard([("builder-lite", "Rung: builder — security", 0, False)])
        self.assertIn(": n/a; n/a", self.find(out, "builder handoffs name a class"))


class ScoutLineTests(RungFixture):
    def line(self, agents):
        return self.find(self.scorecard(agents), "a scout before the makers")

    def test_a_maker_naming_the_scouts_path_is_briefed(self):
        line = self.line([("scout", f"Write a brief to {BRIEF}", 0, False), ("builder-lite", f"Read {BRIEF}", 10, True)])
        self.assertIn("builder-lite briefed $", line)
        self.assertIn("over 1 turns (1)", line)
        self.assertNotIn("not briefed", line)
        self.assertIn("1 scouts, median $", line)

    def test_a_different_path_or_an_earlier_start_is_not_briefed(self):
        line = self.line([("scout", f"Write a brief to {BRIEF}", 10, False),
                          ("builder-lite", "Read /tmp/x/briefs/feat-b.md", 20, True),
                          ("builder-lite", f"Read {BRIEF}", 0, True)])
        self.assertIn("builder-lite not briefed $", line)
        self.assertNotIn("builder-lite briefed", line)
        self.assertIn("(2)", line)
        self.assertTrue(line.rstrip().endswith("n/a"), line)

    def test_verdict_against_the_baseline(self):
        agents = [("scout", f"Brief: {BRIEF}", 0, False), ("builder", f"Read {BRIEF}", 10, True)]
        with mock.patch.dict(ac.BASELINE_RUNUP, {"builder": 1000.0}):
            self.assertTrue(self.line(agents).rstrip().endswith("holds"))
        with mock.patch.dict(ac.BASELINE_RUNUP, {"builder": 0.0}):
            self.assertTrue(self.line(agents).rstrip().endswith("not holding"))



class ClassLinePinTests(unittest.TestCase):
    def test_class_line_equals_the_hook(self):
        here = os.path.dirname(os.path.abspath(__file__))
        hook_path = os.path.join(here, "..", "..", "..", "hooks", "handoff-gates.py")
        loader = importlib.machinery.SourceFileLoader("handoff_gates_hook", hook_path)
        spec = importlib.util.spec_from_loader("handoff_gates_hook", loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)
        self.assertEqual((ac.CLASS_LINE.pattern, ac.CLASS_LINE.flags), (hook.CLASS_LINE.pattern, hook.CLASS_LINE.flags))


if __name__ == "__main__":
    unittest.main()
