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
        self.assertEqual(ac.input_price("claude-sonnet-5-5"), 2.00)
        self.assertEqual(ac.input_price("claude-haiku-5-5"), 0.10)
        self.assertEqual(ac.input_price("claude-fable-5-1"), 10.00)

    def test_opus_four_and_four_one_list_at_three_times_the_later_generations(self):
        for model, price in (("claude-opus-4-20250514", 15.00), ("claude-opus-4-1-20250805", 15.00),
                             ("claude-opus-4-5-20251101", 5.00), ("claude-opus-4-6", 5.00),
                             ("claude-opus-4-7", 5.00), ("claude-opus-5", 5.00), ("claude-opus-5-5", 4.00)):
            with self.subTest(model=model):
                self.assertEqual(ac.input_price(model), price)

    def test_an_unplaceable_model_has_no_price(self):
        self.assertIsNone(ac.input_price("mystery-model"))
        self.assertIsNone(ac.input_price(None))
        self.assertIsNone(ac.list_dollars(1000, 10, "<synthetic>"))

    def test_a_turn_costs_input_eq_plus_five_times_output_at_the_input_price(self):
        self.assertAlmostEqual(ac.list_dollars(M, 0, "claude-opus-5-5"), 4.00)
        self.assertAlmostEqual(ac.list_dollars(0, M, "claude-sonnet-5-5"), 10.00)

    def test_cache_reads_and_writes_are_priced_as_input_eq_not_as_raw_context(self):
        sonnet = {"input_tokens": 100_000, "cache_read_input_tokens": M, "cache_creation_input_tokens": 200_000,
                  "output_tokens": 10_000}
        opus = {"input_tokens": 0, "cache_read_input_tokens": 2 * M, "output_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 400_000, "ephemeral_1h_input_tokens": 0}}
        loaded = loaded_for("subagent", "builder", "c", [("claude-sonnet-5-5", sonnet), ("claude-opus-5-5", opus)])
        # Sonnet 5.5: 100k + 1M read at a tenth + 200k written at 1.25 = 450k at $2, plus 10k output at $10.
        # Opus 5.5: 2M read at a twentieth + 400k written at 1.25 = 600k at $4. Raw context would be $5.70 more.
        self.assertAlmostEqual(loaded.window_turns[0]["usd"], 1.00)
        self.assertAlmostEqual(loaded.window_turns[1]["usd"], 2.40)
        out = []
        ac.section_totals(out, [loaded])
        self.assertIn("list $   $3.40", next(l for l in out if l.startswith("  subagent")))

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
