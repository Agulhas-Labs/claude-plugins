"""Tests for the list-price figure in scripts/agent_cost.py. Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests"""
import importlib.machinery
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "agent_cost.py")
spec = importlib.util.spec_from_loader("agentcost_price", importlib.machinery.SourceFileLoader("agentcost_price", SCRIPT))
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

BASE = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
M = 1_000_000


def loaded_for(kind, agent_type, agent, turns):
    """One Loaded; `turns` is a list of (model, usage dict)."""
    c = ac.Context(kind, f"/tmp/fake-{agent}.jsonl", "proj", "sess", agent)
    c.agent_type = agent_type
    c.turns = [dict(ts=BASE + timedelta(seconds=i), ctx=u.get("input_tokens", 0), usage=u, model=m, n_tools=1, tools={})
               for i, (m, u) in enumerate(turns)]
    return ac.Loaded(c, BASE - timedelta(minutes=1), BASE + timedelta(minutes=1))


def pure(input_tokens, output_tokens=0):
    return {"input_tokens": input_tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "output_tokens": output_tokens}


class PriceLookupTests(unittest.TestCase):
    def test_the_more_specific_family_wins_and_dated_ids_match(self):
        self.assertEqual(ac.input_price("claude-opus-5-5"), 4.00)
        self.assertEqual(ac.input_price("claude-opus-4-1-20250805"), 5.00)
        self.assertEqual(ac.input_price("claude-sonnet-5-5"), 2.00)
        self.assertEqual(ac.input_price("claude-haiku-5-5"), 0.10)
        self.assertEqual(ac.input_price("claude-fable-5-1"), 10.00)

    def test_an_unplaceable_model_has_no_price(self):
        self.assertIsNone(ac.input_price("mystery-model"))
        self.assertIsNone(ac.input_price(None))
        self.assertIsNone(ac.list_dollars(1000, 10, "<synthetic>"))

    def test_a_turn_costs_input_eq_plus_five_times_output_at_the_input_price(self):
        self.assertAlmostEqual(ac.list_dollars(M, 0, "claude-opus-5-5"), 4.00)
        self.assertAlmostEqual(ac.list_dollars(0, M, "claude-sonnet-5-5"), 10.00)

    def test_dollars_format_compactly(self):
        self.assertEqual([ac.fmt_usd(v) for v in (1234, 85.4, 0.4, None)], ["$1.2k", "$85", "$0.40", "-"])


class PriceAcrossModelsTests(unittest.TestCase):
    def _loaded(self):
        return [
            loaded_for("subagent", "builder", "b", [("claude-opus-5-5", pure(M))]),             # $4.00
            loaded_for("subagent", "builder-lite", "l", [("claude-sonnet-5-5", pure(M))]),      # $2.00, same tokens
            loaded_for("subagent", "odd", "o", [("mystery-model", pure(3 * M))]),               # unpriced, biggest
        ]

    def test_equal_tokens_rank_by_price_and_unpriced_types_sort_last(self):
        out = []
        ac.section_spend_by_type(out, self._loaded())
        self.assertIn("$/ctx", out[1].split())
        rows = {l.split()[0]: l.split() for l in out[2:6]}
        self.assertEqual([l.split()[0] for l in out[2:6]], ["builder", "builder-lite", "odd", "total"])
        self.assertIn("$4.00", rows["builder"])
        self.assertIn("$2.00", rows["builder-lite"])
        self.assertEqual(rows["odd"].count("-"), 2)   # list $ and $/ctx
        self.assertIn("$6.00", rows["total"])   # unpriced turns add nothing
        self.assertTrue(any("not a bill" in l for l in out))

    def test_totals_rows_and_model_rows_carry_list_dollars(self):
        out = []
        ac.section_totals(out, self._loaded())
        text = "\n".join(out)
        sub = next(l for l in out if l.startswith("  subagent"))
        self.assertIn("list $   $6.00", sub)
        by_model = {l.split()[0]: l for l in out if l.startswith("    ")}
        self.assertIn("list $   $4.00", by_model["claude-opus-5-5"])
        self.assertIn("list $   $2.00", by_model["claude-sonnet-5-5"])
        self.assertIn("list $       -", by_model["mystery-model"])
        self.assertIn("list $", text)


if __name__ == "__main__":
    unittest.main()
