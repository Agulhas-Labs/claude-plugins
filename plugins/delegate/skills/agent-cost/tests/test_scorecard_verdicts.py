"""Tests for the scorecard verdict changes in scripts/agent_cost.py: reviewer verdicts read per branch, the
model ceiling leaving out calls that named a model, and no baseline for calls requested together.
Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests -p 'test_scorecard_verdicts.py'"""
import unittest
from datetime import timedelta

from test_agentcost import (BASE, FixtureRoot, ac, assistant, rule_tree, scorecard_lines, section_body,
                            ts_str, tool_use_block, typed_loaded, usage)

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"


class VerdictFormTests(unittest.TestCase):
    def test_the_four_forms_the_reviewer_is_told_to_end_with(self):
        for report, want in (("Findings.\nVerdict: merge-ready", "pass"),
                             ("Findings.\nVerdict: merge-ready with lows", "pass with lows"),
                             ("Findings.\nVerdict: merge-ready after fixes", "pass after fixes"),
                             ("Findings.\nVerdict: not merge-ready", "fail")):
            with self.subTest(report=report):
                self.assertEqual(ac.reviewer_verdict(report), want)

    def test_a_branch_name_fits_the_parenthetical(self):
        name = "fix/a-rather-long-branch-name-for-the-cost-scorecard"
        self.assertEqual(ac.reviewer_verdict(f"Verdict ({name}): not merge-ready"), "fail")
        self.assertEqual(ac.reviewer_verdict(f"Verdict ({name}): merge-ready"), "pass")

    def test_each_branch_gets_its_own_line(self):
        report = ("Verdict (fix/one): merge-ready\nVerdict (fix/two): not merge-ready\n"
                  "Verdict (fix/three): merge-ready after fixes")
        self.assertEqual(ac.reviewer_verdict(report, {"fix/one"}), "pass")
        self.assertEqual(ac.reviewer_verdict(report, {"fix/two"}), "fail")
        self.assertEqual(ac.reviewer_verdict(report, {"fix/three"}), "pass after fixes")
        # no branch given, or none named: the last verdict line, as before
        self.assertEqual(ac.reviewer_verdict(report), "pass after fixes")
        self.assertEqual(ac.reviewer_verdict(report, {"fix/other"}), "pass after fixes")

    def test_the_last_line_naming_the_branch_wins(self):
        report = "Verdict (fix/one): not merge-ready\nVerdict (fix/one): merge-ready\nVerdict (fix/two): not merge-ready"
        self.assertEqual(ac.reviewer_verdict(report, {"fix/one"}), "pass")

    def test_review_by_rung_gives_each_maker_its_own_verdict(self):
        t = lambda kind, typ, agent, secs: _with_times(typed_loaded(kind, typ, [1000], agent), secs)
        a, b = t("subagent", "builder", "a", 0), t("subagent", "builder", "b", 1)
        r = t("subagent", "reviewer", "r", 100)
        # Branch linking reads the handoff text, which loading fills from the first prompt.
        a.ctx.first_prompt = a.ctx.handoff = "Build `fix/one`."
        b.ctx.first_prompt = b.ctx.handoff = "Build `fix/two`."
        r.ctx.first_prompt = r.ctx.handoff = "Review `fix/one` and `fix/two`."
        r.ctx.handback_report = "Verdict (fix/one): merge-ready\nVerdict (fix/two): not merge-ready"
        out = []
        ac.section_review_by_rung(out, [a, b, r])
        row = [line.split() for line in out if line.strip().startswith("builder")][0]
        # rung round makers linked pass lows after-fixes fail unclass
        self.assertEqual(row[2:8], ["2", "2", "1", "0", "0", "1"], out)
        self.assertNotIn("the same verdict", "\n".join(out))


def _with_times(l, secs):
    for t in l.ctx.turns:
        t["ts"] = BASE + timedelta(seconds=secs)
    l.window_turns = list(l.ctx.turns)
    return l


class ModelCeilingTests(unittest.TestCase):
    def _pair(self, named):
        parent = typed_loaded("main", "main", [1000], "m")
        sub = typed_loaded("subagent", "builder", [1000], "s")
        sub.ctx.turns[0]["model"] = OPUS
        sub.ctx.tool_use_id = "toolu_1"
        parent.ctx.agent_calls = {"toolu_1": named}
        return parent, sub

    def test_a_call_that_named_a_model_is_left_out_and_counted(self):
        parent, sub = self._pair("opus")
        judged, above, skipped = ac.above_ceiling([parent, sub])
        self.assertEqual((len(judged), len(above), skipped), (0, 0, 1))

    def test_a_call_that_named_none_is_judged(self):
        parent, sub = self._pair(None)
        judged, above, skipped = ac.above_ceiling([parent, sub])
        self.assertEqual((len(judged), len(above), skipped), (1, 1, 0))

    def test_load_context_records_agent_calls_and_the_meta_reads_the_tool_use_id(self):
        fx = FixtureRoot(self)
        path = fx.main_session(entries=[assistant("m1", ts_str(BASE), usage(), [
            tool_use_block("toolu_a", "Agent", {"model": "opus", "prompt": "x"}),
            tool_use_block("toolu_b", "Agent", None),
            tool_use_block("toolu_c", "Bash", {"model": "ignored"})])])
        ctx = ac.load_context("main", path, "p", "s", "x")
        self.assertEqual(ctx.agent_calls, {"toolu_a": "opus", "toolu_b": None})
        sub = fx.subagent(entries=[assistant("m2", ts_str(BASE), usage())])
        import json
        with open(sub[:-len(".jsonl")] + ".meta.json", "w") as f:
            json.dump({"agentType": "builder", "toolUseId": "toolu_a"}, f)
        self.assertEqual(ac.load_context("subagent", sub, "p", "s", "a1").tool_use_id, "toolu_a")

    def test_the_scorecard_note_says_how_many_were_left_out(self):
        _, loaded = rule_tree(self)
        body = section_body(ac.build_report(loaded, 12, sections=["rule-scorecard"]), "Rule scorecard")
        self.assertIn("note: the model ceiling leaves out 0 subagents whose call named its own model", body)
        main = [l for l in loaded if l.ctx.kind == "main"][0]
        b1 = [l for l in loaded if l.ctx.agent_id == "b1"][0]
        b1.ctx.tool_use_id = "toolu_b1"
        main.ctx.agent_calls = {"toolu_b1": "opus"}
        body = section_body(ac.build_report(loaded, 12, sections=["rule-scorecard"]), "Rule scorecard")
        self.assertIn("leaves out 1 subagent whose call named its own model", body)
        self.assertIn("subagents above their parent session's model: 0 of 11", body)


class CallsTogetherTests(unittest.TestCase):
    def test_no_baseline_and_no_constant(self):
        _, loaded = rule_tree(self)
        lines = scorecard_lines(ac.build_report(loaded, 12, sections=["rule-scorecard"]))
        self.assertEqual(lines["calls requested together"],
                         "single-tool-call share of subagent turns: 89.5% (batchable 5.3%); no baseline")
        self.assertFalse(hasattr(ac, "BASELINE_SINGLE_CALL_SHARE"))


if __name__ == "__main__":
    unittest.main()
