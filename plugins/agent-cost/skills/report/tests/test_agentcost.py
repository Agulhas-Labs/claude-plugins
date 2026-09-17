"""Tests for agentcost.py. Run: python3 -m unittest discover -s skills/report/tests"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "agentcost.py")
spec = importlib.util.spec_from_file_location("agentcost", SCRIPT)
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------

def usage(input_tokens=2, cache_read=0, cache_creation=0, output_tokens=10, split=None):
    u = {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
         "cache_creation_input_tokens": cache_creation, "output_tokens": output_tokens}
    if split is not None:
        u["cache_creation"] = split
    return u


def assistant(mid, ts, u, content=None, model="claude-sonnet-5", version="2.1.272"):
    return {"type": "assistant", "timestamp": ts, "version": version,
            "message": {"id": mid, "usage": u, "model": model, "content": content or []}}


def tool_use_block(tid, name="Bash", input_=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": input_ or {}}


def user_text(ts, text, version="2.1.272"):
    return {"type": "user", "timestamp": ts, "version": version, "message": {"content": text}}


def user_tool_result(ts, tid, text, version="2.1.272"):
    return {"type": "user", "timestamp": ts, "version": version,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                      "content": [{"type": "text", "text": text}]}]}}


def attachment(ts, att, version="2.1.272"):
    return {"type": "attachment", "timestamp": ts, "version": version, "attachment": att}


def instructions_attachment(ts, files):
    return attachment(ts, {"type": "instructions", "files": [{"path": p, "content": c} for p, c in files]})


def deferred_attachment(ts, added):
    return attachment(ts, {"type": "deferred_tools_delta", "addedNames": added})


def ts_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


HOME = os.path.expanduser("~")
BASE = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


class FixtureRoot:
    """A temp ~/.claude/projects-shaped tree."""
    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix="agentcost-test-")
        case.addCleanup(shutil.rmtree, self.root)

    def main_session(self, project="-Users-x-Developer-Proj", session="sess1", entries=()):
        path = os.path.join(self.root, project, f"{session}.jsonl")
        write_jsonl(path, entries)
        return path

    def subagent(self, project="-Users-x-Developer-Proj", session="sess1", agent="a1", entries=(), agent_type="mechanic"):
        path = os.path.join(self.root, project, session, "subagents", f"agent-{agent}.jsonl")
        write_jsonl(path, entries)
        meta = path[:-len(".jsonl")] + ".meta.json"
        with open(meta, "w") as f:
            json.dump({"agentType": agent_type}, f)
        return path


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TurnDedupeTests(unittest.TestCase):
    def test_duplicate_lines_with_one_message_id_count_as_one_turn(self):
        root = tempfile.mkdtemp(prefix="agentcost-dup-")
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, "agent-a1.jsonl")
        t1 = BASE
        entries = [
            assistant("m1", ts_str(t1), usage(output_tokens=7, cache_read=100), content=[{"type": "thinking", "text": "x"}]),
            assistant("m1", ts_str(t1), usage(output_tokens=7, cache_read=100), content=[tool_use_block("t1")]),
            assistant("m1", ts_str(t1), usage(output_tokens=377, cache_read=100), content=[tool_use_block("t2")]),
        ]
        write_jsonl(path, entries)
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual(len(ctx.turns), 1)
        # the later line's usage (more complete output_tokens) wins, not the first
        self.assertEqual(ctx.turns[0]["usage"]["output_tokens"], 377)
        self.assertEqual(ctx.turns[0]["n_tools"], 2)


class PricingTests(unittest.TestCase):
    def test_a_cache_read_on_fable_5_1_weighs_a_fortieth_and_a_tenth_on_everything_else(self):
        u = usage(input_tokens=0, cache_read=1_000_000, cache_creation=0)
        self.assertAlmostEqual(ac.input_equivalent(u, "claude-fable-5-1"), 25_000.0)
        self.assertAlmostEqual(ac.input_equivalent(u, "claude-fable-5-1-20260801"), 25_000.0)
        self.assertAlmostEqual(ac.input_equivalent(u, "claude-fable-5"), 100_000.0)
        self.assertAlmostEqual(ac.input_equivalent(u, "claude-opus-5"), 100_000.0)
        self.assertAlmostEqual(ac.input_equivalent(u), 100_000.0)

    def test_a_loaded_turn_is_priced_with_its_own_models_read_weight(self):
        fx = FixtureRoot(self)
        u = usage(input_tokens=0, cache_read=1_000_000, cache_creation=0)
        fx.main_session(session="fable", entries=[assistant("m1", ts_str(BASE), u, model="claude-fable-5-1")])
        fx.main_session(session="opus", entries=[assistant("m2", ts_str(BASE), u, model="claude-opus-5")])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        spend = sorted(t["ie"] for l in loaded for t in l.window_turns)
        self.assertEqual([round(x) for x in spend], [25_000, 100_000])

    def test_a_cold_rewrite_on_fable_5_1_is_measured_against_its_cheaper_warm_read(self):
        u = usage(input_tokens=0, cache_read=0, cache_creation=100_000,
                  split={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100_000})
        self.assertAlmostEqual(ac.cold_rewrite_cost(u, 100_000, "claude-fable-5-1")[1], 197_500.0)
        self.assertAlmostEqual(ac.cold_rewrite_cost(u, 100_000, "claude-opus-5")[1], 190_000.0)

    def test_input_equivalent_with_5m_1h_split(self):
        u = usage(input_tokens=100, cache_read=1000, split={"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 50})
        # 100*1 + 1000*0.1 + 200*1.25 + 50*2.0 = 100 + 100 + 250 + 100 = 550
        self.assertAlmostEqual(ac.input_equivalent(u), 550.0)

    def test_input_equivalent_fallback_without_split(self):
        u = usage(input_tokens=100, cache_read=1000, cache_creation=400)
        # 100*1 + 1000*0.1 + 400*1.25 = 100 + 100 + 500 = 700
        self.assertAlmostEqual(ac.input_equivalent(u), 700.0)


class WindowTests(unittest.TestCase):
    def test_turns_before_since_excluded_and_straddling_context_has_no_start_metrics(self):
        fx = FixtureRoot(self)
        t_before = BASE - timedelta(hours=2)
        t_after = BASE + timedelta(hours=2)
        entries = [
            instructions_attachment(ts_str(t_before), [("/rules/a.md", "x" * 40)]),
            assistant("m1", ts_str(t_before), usage(cache_read=1000)),
            assistant("m2", ts_str(t_after), usage(cache_read=2000)),
        ]
        path = fx.subagent(entries=entries)
        os.utime(path, (t_after.timestamp(), t_after.timestamp()))
        loaded = ac.load_all(fx.root, None, BASE, BASE + timedelta(hours=4))
        self.assertEqual(len(loaded), 1)
        lc = loaded[0]
        self.assertEqual(len(lc.window_turns), 1)  # only m2 is in-window
        self.assertFalse(lc.first_in_window)       # m1, the true first turn, is before `since`

    def test_old_mtime_file_skipped_recent_mtime_file_kept(self):
        fx = FixtureRoot(self)
        old_path = fx.subagent(agent="old", entries=[assistant("m1", ts_str(BASE), usage(cache_read=1000))])
        recent_path = fx.subagent(agent="recent", entries=[assistant("m1", ts_str(BASE), usage(cache_read=1000))])
        old_time = (BASE - timedelta(days=10)).timestamp()
        recent_time = BASE.timestamp()
        os.utime(old_path, (old_time, old_time))
        os.utime(old_path[:-len(".jsonl")] + ".meta.json", (old_time, old_time))
        os.utime(recent_path, (recent_time, recent_time))
        os.utime(recent_path[:-len(".jsonl")] + ".meta.json", (recent_time, recent_time))
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        agent_ids = {lc.ctx.agent_id for lc in loaded}
        self.assertIn("recent", agent_ids)
        self.assertNotIn("old", agent_ids)


def set_tz(case, name):
    """Set the system TZ for a test and restore it (env + tzset) in cleanup."""
    orig = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    def restore():
        if orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig
        time.tzset()
    case.addCleanup(restore)


class SinceParsingTests(unittest.TestCase):
    def setUp(self):
        # A real system timezone (not a synthetic fixed offset) so `parse_when` resolves naive
        # values against the actual offset in effect for each date, not just `now`'s.
        set_tz(self, "America/New_York")
        self.now = datetime(2026, 9, 15, 18, 30, 0).astimezone()
        self.tz = self.now.tzinfo

    def test_today(self):
        self.assertEqual(ac.parse_when("today", self.now), datetime(2026, 9, 15, 0, 0, 0, tzinfo=self.tz))

    def test_yesterday(self):
        self.assertEqual(ac.parse_when("yesterday", self.now), datetime(2026, 9, 14, 0, 0, 0, tzinfo=self.tz))

    def test_n_days(self):
        self.assertEqual(ac.parse_when("3d", self.now), self.now - timedelta(days=3))

    def test_date_is_local_midnight(self):
        self.assertEqual(ac.parse_when("2026-09-10", self.now), datetime(2026, 9, 10, 0, 0, 0, tzinfo=self.tz))

    def test_local_iso_datetime(self):
        self.assertEqual(ac.parse_when("2026-09-15T16:06", self.now), datetime(2026, 9, 15, 16, 6, 0, tzinfo=self.tz))

    def test_iso_datetime_with_z_is_utc(self):
        self.assertEqual(ac.parse_when("2026-09-15T16:06:00Z", self.now),
                          datetime(2026, 9, 15, 16, 6, 0, tzinfo=timezone.utc))

    def test_iso_datetime_with_explicit_offset(self):
        self.assertEqual(ac.parse_when("2026-09-15T16:06:00+05:30", self.now),
                          datetime(2026, 9, 15, 16, 6, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))


class DSTOffsetTests(unittest.TestCase):
    """A naive date is resolved with the offset in effect on *that* date, not `now`'s — so a
    window spanning a DST transition doesn't silently shift by an hour."""
    def setUp(self):
        set_tz(self, "Australia/Sydney")
        # Sydney DST starts 2026-10-04; `now` is after the transition (+11:00).
        self.now = datetime(2026, 10, 10, 9, 0, 0).astimezone()

    def test_date_before_transition_gets_pre_transition_offset(self):
        dt = ac.parse_when("2026-10-01", self.now)
        self.assertEqual(dt.utcoffset(), timedelta(hours=10))

    def test_date_after_transition_gets_post_transition_offset(self):
        dt = ac.parse_when("2026-10-10", self.now)
        self.assertEqual(dt.utcoffset(), timedelta(hours=11))


def make_loaded(kind, ies, n_tools_list=None, ts=BASE):
    """A Loaded wrapping a single-turn-per-call context list, for hand-computed math tests."""
    out = []
    n_tools_list = n_tools_list or [1] * len(ies)
    for i, (ie, n_tools) in enumerate(zip(ies, n_tools_list)):
        c = ac.Context(kind, f"/tmp/fake-{i}.jsonl", "proj", "sess", f"a{i}")
        # Back out a usage whose input_equivalent equals `ie` exactly (pure uncached input).
        c.turns = [dict(ts=ts, ctx=int(ie), usage=usage(input_tokens=int(ie), output_tokens=1), model="claude-sonnet-5", n_tools=n_tools, tools={})]
        lc = ac.Loaded(c, ts - timedelta(minutes=1), ts + timedelta(minutes=1))
        out.append(lc)
    return out


class TurnShapeTests(unittest.TestCase):
    def test_one_tool_call_turn_share_and_spend_share(self):
        # 3 turns with exactly one tool call (ie 10, 10, 80) and 1 turn with two tool calls (ie 100).
        loaded = make_loaded("subagent", [10, 10, 80, 100], n_tools_list=[1, 1, 1, 2])
        out = []
        ac.section_turn_shape(out, loaded)
        line = next(l for l in out if l.strip().startswith("subagent"))
        turns_pct = float(re.search(r"([\d.]+)% of turns", line).group(1))
        spend_pct = float(re.search(r"([\d.]+)% of input-eq spend", line).group(1))
        self.assertAlmostEqual(turns_pct, 75.0, places=1)          # 3 of 4 turns
        self.assertAlmostEqual(spend_pct, 100.0 * 100 / 200, places=1)  # (10+10+80) of 200


class ConcentrationTests(unittest.TestCase):
    def test_top_10_percent_share(self):
        ies = [100, 90] + [10] * 8   # sum = 270, top 10% (1 context) = 100
        loaded = make_loaded("subagent", ies)
        out = []
        ac.section_concentration(out, loaded)
        line = next(l for l in out if l.strip().startswith("top 10%"))
        pct = float(re.search(r": ([\d.]+)%", line).group(1))
        self.assertAlmostEqual(pct, 100.0 * 100 / 270, places=1)


def mcp_instructions_attachment(ts, added_names, added_blocks, removed_names=None):
    return attachment(ts, {"type": "mcp_instructions_delta", "addedNames": added_names,
                            "addedBlocks": added_blocks, "removedNames": removed_names or []})


class FixedStartCompositionTests(unittest.TestCase):
    def test_per_file_instruction_sizes_and_per_server_deferred_counts(self):
        fx = FixtureRoot(self)
        entries = [
            instructions_attachment(ts_str(BASE), [("/rules/a.md", "x" * 100), ("/rules/b.md", "y" * 40)]),
            deferred_attachment(ts_str(BASE), ["mcp__codeindex__digest", "mcp__codeindex__where", "WebFetch"]),
            assistant("m1", ts_str(BASE), usage(cache_read=5000)),
        ]
        path = fx.subagent(entries=entries)
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual(dict(ctx.start["instructions"]), {"/rules/a.md": 100, "/rules/b.md": 40})
        self.assertEqual(ctx.start["deferred_counts"], {"codeindex": 2})
        # "mcp__codeindex__digest" (22 chars) + "mcp__codeindex__where" (21 chars) = 43
        self.assertEqual(ctx.start["deferred_chars"], {"codeindex": 43})
        self.assertEqual(ctx.start["deferred_nonmcp_count"], 1)
        self.assertEqual(ctx.start["deferred_nonmcp_chars"], len("WebFetch"))

    def test_mcp_instructions_delta_parsed_per_server(self):
        fx = FixtureRoot(self)
        entries = [
            mcp_instructions_attachment(ts_str(BASE), ["codeindex", "xcode"], ["x" * 200, "y" * 50]),
            assistant("m1", ts_str(BASE), usage(cache_read=5000)),
        ]
        path = fx.subagent(entries=entries)
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual(ctx.start["mcp_instr"], {"codeindex": 200, "xcode": 50})

    def test_mcp_instructions_delta_removal_before_first_turn_drops_server(self):
        fx = FixtureRoot(self)
        entries = [
            mcp_instructions_attachment(ts_str(BASE), ["codeindex"], ["x" * 200]),
            mcp_instructions_attachment(ts_str(BASE), [], [], removed_names=["codeindex"]),
            assistant("m1", ts_str(BASE), usage(cache_read=5000)),
        ]
        path = fx.subagent(entries=entries)
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual(ctx.start["mcp_instr"], {})


class ContextRemainderTests(unittest.TestCase):
    """Each context's remainder is its own first-turn context minus its own itemised estimate —
    never a blend of one context's start against another context's composition."""

    def _ctx(self, first_ctx, instructions, deferred_names=(), mcp_names_blocks=(),
              skill_chars=0, hook_chars=0, prompt_chars=0):
        entries = []
        if instructions:
            entries.append(instructions_attachment(ts_str(BASE), instructions))
        if deferred_names:
            entries.append(deferred_attachment(ts_str(BASE), list(deferred_names)))
        if mcp_names_blocks:
            names, blocks = zip(*mcp_names_blocks)
            entries.append(mcp_instructions_attachment(ts_str(BASE), list(names), list(blocks)))
        if skill_chars:
            entries.append(attachment(ts_str(BASE), {"type": "skill_listing", "content": "s" * skill_chars}))
        if hook_chars:
            entries.append(attachment(ts_str(BASE), {"type": "hook_additional_context",
                                                       "content": [{"content": "h" * hook_chars}]}))
        if prompt_chars:
            entries.append(user_text(ts_str(BASE), "p" * prompt_chars))
        entries.append(assistant("m1", ts_str(BASE), usage(input_tokens=first_ctx)))
        return entries

    def test_two_contexts_different_instructions_and_agent_types_exact_median_remainder(self):
        fx = FixtureRoot(self)
        # Context A: reviewer, first-turn ctx 12000, one instructions file of 4000 chars (=1000 tok).
        a_entries = self._ctx(12000, [("/rules/a.md", "x" * 4000)])
        # Context B: builder, first-turn ctx 40000, two instructions files totalling 8000 chars (=2000 tok).
        b_entries = self._ctx(40000, [("/rules/a.md", "x" * 4000), ("/rules/b.md", "y" * 4000)])
        fx.subagent(agent="a1", agent_type="reviewer", entries=a_entries)
        fx.subagent(agent="b1", agent_type="builder", entries=b_entries)
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        by_agent = {lc.ctx.agent_id: lc for lc in loaded}
        self.assertAlmostEqual(ac.context_remainder(by_agent["a1"]), 12000 - 1000)
        self.assertAlmostEqual(ac.context_remainder(by_agent["b1"]), 40000 - 2000)

    def test_deferred_names_and_mcp_instructions_reduce_the_remainder(self):
        fx = FixtureRoot(self)
        bare_entries = self._ctx(20000, [])
        loaded_entries = self._ctx(20000, [],
                                    deferred_names=["mcp__codeindex__digest", "mcp__codeindex__where"],
                                    mcp_names_blocks=[("codeindex", "z" * 400)])
        fx.subagent(agent="bare", agent_type="mechanic", entries=bare_entries)
        fx.subagent(agent="loaded", agent_type="mechanic", entries=loaded_entries)
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        by_agent = {lc.ctx.agent_id: lc for lc in loaded}
        bare_remainder = ac.context_remainder(by_agent["bare"])
        loaded_remainder = ac.context_remainder(by_agent["loaded"])
        # Exact values, so dropping either the deferred-name or the MCP-instructions subtraction fails.
        self.assertAlmostEqual(bare_remainder, 20000)
        self.assertAlmostEqual(loaded_remainder,
                               20000 - (len("mcp__codeindex__digest") + len("mcp__codeindex__where") + 400) / 4)


def build_turns(specs):
    """specs: list of (ts, ctx_size) -> ctx.turns-shaped dicts."""
    return [dict(ts=ts, ctx=ctx_size, usage=usage(input_tokens=ctx_size, output_tokens=1),
                 model="claude-sonnet-5", n_tools=0) for ts, ctx_size in specs]


class FillsContextTests(unittest.TestCase):
    def _resident(self, out, label):
        line = next(l for l in out if l.strip().startswith(label))
        return line

    def test_carried_in_row_gets_straddling_contexts_first_in_window_size(self):
        since = BASE
        until = BASE + timedelta(hours=4)
        t0 = BASE - timedelta(hours=2)   # before the window: Y's true first turn
        t1 = BASE + timedelta(hours=1)
        t2 = BASE + timedelta(hours=2)

        cx = ac.Context("subagent", "/tmp/x.jsonl", "proj", "sess", "x")
        cx.turns = build_turns([(t1, 5000), (t2, 9000)])   # X starts inside the window
        cx.events = []
        lx = ac.Loaded(cx, since, until)

        cy = ac.Context("subagent", "/tmp/y.jsonl", "proj", "sess", "y")
        cy.turns = build_turns([(t0, 3000), (t1, 50000), (t2, 54000)])  # Y straddles `since`
        cy.events = []
        ly = ac.Loaded(cy, since, until)

        self.assertTrue(lx.first_in_window)
        self.assertFalse(ly.first_in_window)

        out = []
        ac.section_fills_context(out, [lx, ly])
        base_line = self._resident(out, "(base) system prompt")
        carried_line = self._resident(out, "(carried in)")
        self.assertIn(ac.fmt_tok(5000), base_line)      # only X's first-in-window size
        self.assertIn(ac.fmt_tok(50000), carried_line)  # Y's history-laden first-in-window size

    def test_resent_multiplier_is_off_by_one_from_first_send(self):
        # A single context, 4 window turns, one event first sent in window-turn index 1 (i.e. the
        # event's turn_index recorded as 1 in ctx.events, meaning it appears starting the 2nd turn).
        ts = BASE
        c = ac.Context("subagent", "/tmp/z.jsonl", "proj", "sess", "z")
        c.turns = build_turns([(ts, 1000), (ts, 3000), (ts, 6000), (ts, 9000)])
        c.events = [(1, "cat-under-test", 8000)]  # chars == last-base, so ratio == 1 and tok == 8000
        l = ac.Loaded(c, ts - timedelta(minutes=1), ts + timedelta(minutes=1))
        self.assertTrue(l.first_in_window)

        out = []
        ac.section_fills_context(out, [l])
        line = self._resident(out, "cat-under-test")
        # n=4, ti=1 -> multiplier max(4-1-1, 0) = 2 -> re-sent = 8000*2 = 16k (not 24k, the old
        # off-by-one that used max(n-ti, 0)). The re-sent column is the second-last field.
        self.assertEqual(line.split()[-2], "16k")


class ColdCacheTests(unittest.TestCase):
    """A turn is cold when it follows a gap of at least 5 minutes and read back under half of the
    previous context from cache — it pays to write that context in again."""

    def _turn(self, ts, u):
        return dict(ts=ts, ctx=ac.context_size(u), usage=u, model="claude-sonnet-5", n_tools=0,
                    tools={})

    def _context(self, turns, kind="main"):
        c = ac.Context(kind, "/tmp/cold.jsonl", "proj", "sess", "c1")
        c.turns = turns
        return c

    def test_a_warm_turn_after_a_long_gap_is_not_cold(self):
        prev = self._turn(BASE, usage(input_tokens=0, cache_read=50000))
        warm = self._turn(BASE + timedelta(hours=2), usage(input_tokens=0, cache_read=50000))
        self.assertEqual(ac.cold_turns([prev, warm]), [])

    def test_a_gap_over_five_minutes_with_the_context_rewritten_at_the_1h_rate_is_cold(self):
        prev_ctx = 50000
        prev = self._turn(BASE, usage(input_tokens=0, cache_read=prev_ctx))
        cold = self._turn(BASE + timedelta(minutes=10),
                          usage(input_tokens=0, cache_read=0, cache_creation=prev_ctx,
                                split={"ephemeral_5m_input_tokens": 0,
                                       "ephemeral_1h_input_tokens": prev_ctx}))
        records = ac.cold_turns([prev, cold])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["prev_ctx"], prev_ctx)
        self.assertEqual(records[0]["rewritten"], prev_ctx)
        # what the rewrite cost (x2.0, the 1-hour write rate) above a warm cache read (x0.1)
        self.assertAlmostEqual(records[0]["extra"], prev_ctx * (2.0 - 0.1))

    def test_a_gap_under_five_minutes_with_a_low_cache_read_is_not_cold(self):
        # This is what compaction looks like: the context is rewritten, but not because it expired.
        prev = self._turn(BASE, usage(input_tokens=0, cache_read=50000))
        compacted = self._turn(BASE + timedelta(seconds=299),
                               usage(input_tokens=0, cache_read=0, cache_creation=20000))
        self.assertEqual(ac.cold_turns([prev, compacted]), [])

    def test_the_first_turn_of_a_context_is_never_cold(self):
        first = self._turn(BASE, usage(input_tokens=0, cache_read=0, cache_creation=50000))
        self.assertEqual(ac.cold_turns([first]), [])

    def test_a_previous_turn_outside_the_window_still_counts_as_the_previous_turn(self):
        since = BASE
        prev_ctx = 40000
        prev = self._turn(BASE - timedelta(hours=1), usage(input_tokens=0, cache_read=prev_ctx))
        cold = self._turn(BASE + timedelta(minutes=10),
                          usage(input_tokens=0, cache_read=0, cache_creation=prev_ctx))
        l = ac.Loaded(self._context([prev, cold]), since, since + timedelta(hours=4))
        self.assertEqual(len(l.window_turns), 1)          # the previous turn is out of the window
        records = ac.cold_turns(l.ctx.turns)
        self.assertEqual(len(records), 1)
        self.assertIs(records[0]["prev"], prev)

        out = []
        ac.section_cold_cache(out, [l])
        line = next(x for x in out if x.strip().startswith("main") and "cold turns" in x)
        self.assertRegex(line, r"cold turns\s+1 of\s+1\b")

    def test_cache_write_lifetime_reported_per_kind(self):
        main_turn = self._turn(BASE, usage(input_tokens=0, cache_creation=30000,
                                           split={"ephemeral_5m_input_tokens": 0,
                                                  "ephemeral_1h_input_tokens": 30000}))
        sub_turn = self._turn(BASE, usage(input_tokens=0, cache_creation=20000,
                                          split={"ephemeral_5m_input_tokens": 20000,
                                                 "ephemeral_1h_input_tokens": 0}))
        window = (BASE - timedelta(minutes=1), BASE + timedelta(minutes=1))
        lm = ac.Loaded(self._context([main_turn], kind="main"), *window)
        ls = ac.Loaded(self._context([sub_turn], kind="subagent"), *window)
        out = []
        ac.section_cold_cache(out, [lm, ls])
        main_line = next(x for x in out if x.strip().startswith("main") and "cache writes" in x)
        sub_line = next(x for x in out if x.strip().startswith("subagent") and "cache writes" in x)
        self.assertIn("1-hour 100% (30k)", main_line)
        self.assertIn("5-minute 0%", main_line)
        self.assertIn("5-minute 100% (20k)", sub_line)
        self.assertIn("1-hour 0%", sub_line)

    def test_no_split_recorded_says_so(self):
        turn = self._turn(BASE, usage(input_tokens=0, cache_creation=5000))
        l = ac.Loaded(self._context([turn], kind="main"), BASE - timedelta(minutes=1),
                      BASE + timedelta(minutes=1))
        out = []
        ac.section_cold_cache(out, [l])
        line = next(x for x in out if x.strip().startswith("main") and "cache writes" in x)
        self.assertIn("no lifetime recorded", line)

    def test_report_says_none_in_this_window_when_nothing_is_cold(self):
        fx = FixtureRoot(self)
        fx.main_session(entries=[
            assistant("m1", ts_str(BASE), usage(input_tokens=0, cache_read=5000)),
            assistant("m2", ts_str(BASE + timedelta(seconds=30)), usage(input_tokens=0, cache_read=6000)),
        ])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        report = ac.build_report(loaded, 12)
        self.assertIn("=== Cold cache ===", report)
        self.assertIn("none in this window", report)

    def test_gap_boundary_299_not_cold_300_and_301_are(self):
        # prev_ctx = 1000; cache_read 0 in the test turn throughout, so only the gap varies.
        prev = self._turn(BASE, usage(input_tokens=1000, cache_read=0))
        just_under = self._turn(BASE + timedelta(seconds=299), usage(input_tokens=0, cache_read=0))
        at_gap = self._turn(BASE + timedelta(seconds=300), usage(input_tokens=0, cache_read=0))
        over_gap = self._turn(BASE + timedelta(seconds=301), usage(input_tokens=0, cache_read=0))
        self.assertEqual(ac.cold_turns([prev, just_under]), [])
        self.assertEqual(len(ac.cold_turns([prev, at_gap])), 1)
        self.assertEqual(len(ac.cold_turns([prev, over_gap])), 1)

    def test_read_boundary_half_of_previous_context(self):
        # prev_ctx = 100000, gap 600s (well over the 300s floor) throughout; only cache_read varies.
        prev = self._turn(BASE, usage(input_tokens=100000, cache_read=0))
        half_read = self._turn(BASE + timedelta(seconds=600), usage(input_tokens=0, cache_read=50000))
        just_under_half = self._turn(BASE + timedelta(seconds=600), usage(input_tokens=0, cache_read=49999))
        self.assertEqual(ac.cold_turns([prev, half_read]), [])
        self.assertEqual(len(ac.cold_turns([prev, just_under_half])), 1)

    def test_pricing_pro_rata_split_when_written_exceeds_previous_context(self):
        u = usage(input_tokens=0, cache_read=0, cache_creation=150000,
                   split={"ephemeral_5m_input_tokens": 60000, "ephemeral_1h_input_tokens": 90000})
        rewritten, extra = ac.cold_rewrite_cost(u, 120000)
        self.assertEqual(rewritten, 120000)
        self.assertAlmostEqual(extra, 120000 * (0.4 * 1.25 + 0.6 * 2.0) - 120000 * 0.1)

    def test_pricing_no_cache_write_uses_uncached_rate(self):
        u = usage(input_tokens=80000, cache_read=0, cache_creation=0)
        rewritten, extra = ac.cold_rewrite_cost(u, 150000)
        self.assertEqual(rewritten, 80000)
        self.assertAlmostEqual(extra, 72000.0)


class ColdCacheTableAndWindowTests(unittest.TestCase):
    """The main-only gap x size table, and rendering when a context's only cold arrival falls
    outside the reporting window."""

    def test_table_quadrants_and_boundaries(self):
        fx = FixtureRoot(self)
        t0 = BASE
        t1 = t0 + timedelta(seconds=600)
        t2 = t1 + timedelta(seconds=600)
        t3 = t2 + timedelta(seconds=7200)
        t4 = t3 + timedelta(seconds=7200)
        t5 = t4 + timedelta(seconds=3600)   # boundary: gap exactly 3600s -> "over 1h"
        t6 = t5 + timedelta(seconds=600)    # boundary: prev ctx exactly 100000 -> "100k and over"
        entries = [
            assistant("m0", ts_str(t0), usage(input_tokens=0, cache_creation=50000)),   # ctx 50000
            assistant("m1", ts_str(t1), usage(input_tokens=0, cache_creation=150000, cache_read=0)),  # gap 600, prev 50000
            assistant("m2", ts_str(t2), usage(input_tokens=0, cache_creation=50000, cache_read=0)),   # gap 600, prev 150000
            assistant("m3", ts_str(t3), usage(input_tokens=0, cache_creation=150000, cache_read=0)),  # gap 7200, prev 50000
            assistant("m4", ts_str(t4), usage(input_tokens=0, cache_creation=20000, cache_read=0)),   # gap 7200, prev 150000
            assistant("m5", ts_str(t5), usage(input_tokens=0, cache_creation=100000, cache_read=0)),  # gap 3600, prev 20000
            assistant("m6", ts_str(t6), usage(input_tokens=0, cache_creation=1, cache_read=0)),       # gap 600, prev 100000
        ]
        fx.main_session(entries=entries)
        loaded = ac.load_all(fx.root, None, t0 - timedelta(minutes=1), t6 + timedelta(hours=1))
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertIn("cold turns    6 of      7", report)

        header = next(l for l in out if l.strip().startswith("gap"))
        cols = header.split()
        self.assertEqual(cols[1:], ["under", "100k", "100k", "and", "over"])  # sanity on the header text

        row_5m = next(l for l in out if l.strip().startswith("5m to 1h"))
        row_over = next(l for l in out if l.strip().startswith("over 1h"))
        # (gap, prev): (600,50000)->under100k/5m-1h; (600,150000)->100k-and-over/5m-1h (+ boundary m6, gap600/prev100000)
        # (7200,50000)->under100k/over1h (+ boundary m5, gap3600/prev20000); (7200,150000)->100k-and-over/over1h
        self.assertRegex(row_5m, r"\(1 turn\).*\(2 turns\)")
        self.assertRegex(row_over, r"\(2 turns\).*\(1 turn\)")

    def test_arrival_outside_window_renders_none_in_this_window(self):
        fx = FixtureRoot(self)
        t_cold = BASE - timedelta(hours=4)
        t_warm = BASE + timedelta(minutes=10)
        entries = [
            assistant("m0", ts_str(t_cold - timedelta(hours=1)), usage(input_tokens=0, cache_read=100000)),
            assistant("m1", ts_str(t_cold), usage(input_tokens=0, cache_read=0, cache_creation=100000)),
            assistant("m2", ts_str(t_warm), usage(input_tokens=0, cache_read=100000)),
        ]
        fx.main_session(entries=entries)
        loaded = ac.load_all(fx.root, None, BASE, BASE + timedelta(hours=1))
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertIn("none in this window", report)

    def test_zero_cold_records_for_main_prints_the_exact_spacing(self):
        # Add a subagent with a genuine in-window cold turn so the report doesn't take the
        # "none in this window" shortcut, and main's own zero-record line can be checked.
        fx = FixtureRoot(self)
        t_cold = BASE - timedelta(hours=4)
        t_warm = BASE + timedelta(minutes=10)
        fx.main_session(entries=[
            assistant("m0", ts_str(t_cold - timedelta(hours=1)), usage(input_tokens=0, cache_read=100000)),
            assistant("m1", ts_str(t_cold), usage(input_tokens=0, cache_read=0, cache_creation=100000)),
            assistant("m2", ts_str(t_warm), usage(input_tokens=0, cache_read=100000)),
        ])
        fx.subagent(agent="a1", entries=[
            assistant("s0", ts_str(t_warm), usage(input_tokens=0, cache_read=100000)),
            assistant("s1", ts_str(t_warm + timedelta(seconds=600)), usage(input_tokens=0, cache_read=0, cache_creation=100000)),
        ])
        loaded = ac.load_all(fx.root, None, BASE, BASE + timedelta(hours=1))
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertIn("cold turns    0 of", report)
        main_line = next(l for l in out if l.strip().startswith("main") and "cold turns" in l)
        self.assertIn("cold turns    0 of", main_line)


class WindowBoundaryTests(unittest.TestCase):
    def test_until_is_exclusive_so_back_to_back_windows_never_double_count(self):
        c = ac.Context("subagent", "/tmp/w.jsonl", "proj", "sess", "w")
        boundary = BASE
        c.turns = build_turns([(boundary, 1000)])
        first_window = ac.Loaded(c, boundary - timedelta(hours=1), boundary)
        second_window = ac.Loaded(c, boundary, boundary + timedelta(hours=1))
        self.assertEqual(len(first_window.window_turns), 0)
        self.assertEqual(len(second_window.window_turns), 1)


class DisplayPathTests(unittest.TestCase):
    def test_home_directory_replaced_with_tilde(self):
        path = os.path.join(HOME, "Developer", "CLAUDE.md")
        self.assertEqual(ac.display_path(path), "~/Developer/CLAUDE.md")

    def test_encoded_home_in_a_project_folder_name_replaced(self):
        encoded = HOME.replace("/", "-")
        path = os.path.join(HOME, ".claude", "projects", encoded + "-Developer-App", "memory", "MEMORY.md")
        shown = ac.display_path(path)
        self.assertEqual(shown, "~/.claude/projects/~-Developer-App/memory/MEMORY.md")
        self.assertNotIn(os.path.basename(HOME), shown)

    def test_unrelated_path_left_alone(self):
        self.assertEqual(ac.display_path("/etc/hosts"), "/etc/hosts")


class ClassificationTests(unittest.TestCase):
    def test_main_vs_subagent_and_agent_type_from_meta(self):
        fx = FixtureRoot(self)
        fx.main_session(entries=[assistant("m1", ts_str(BASE), usage(cache_read=1000))])
        fx.subagent(agent="b1", agent_type="builder", entries=[assistant("m1", ts_str(BASE), usage(cache_read=1000))])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        kinds = {lc.ctx.kind: lc.ctx.agent_type for lc in loaded}
        self.assertEqual(kinds["main"], "main")
        self.assertEqual(kinds["subagent"], "builder")


class ClassifyMcpNameTests(unittest.TestCase):
    def test_server_and_tool_name_is_split(self):
        self.assertEqual(ac.classify("mcp__codeindex__digest", {}), "MCP codeindex digest")

    def test_a_name_with_no_tool_part_falls_back_instead_of_raising(self):
        self.assertEqual(ac.classify("mcp__foo", {}), "MCP foo")


class BashClassTests(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(ac.bash_class("swift test --package-path ."), "bash: raw swift build/test")
        self.assertEqual(ac.bash_class("git status"), "bash: git log/status/etc")
        self.assertEqual(ac.bash_class("git commit -m 'x'"), "bash: git mutate")
        self.assertEqual(ac.bash_class("grep -rn Foo Sources/"), "bash: grep")
        self.assertEqual(ac.bash_class("cat file.txt"), "bash: cat/sed/head window")
        self.assertEqual(ac.bash_class("python3 script.py"), "bash: python/jq")
        self.assertEqual(ac.bash_class("echo hello"), "bash: other")
        # non-Swift ecosystems: one build/test example and one lint example per tool family
        self.assertEqual(ac.bash_class("npm test"), "bash: build/test")
        self.assertEqual(ac.bash_class("npm run lint"), "bash: lint/format/gates")
        self.assertEqual(ac.bash_class("cargo build"), "bash: build/test")
        self.assertEqual(ac.bash_class("cargo clippy"), "bash: lint/format/gates")

    def test_a_pipeline_is_classified_by_the_stage_that_feeds_its_filters(self):
        self.assertEqual(ac.bash_class("swift build 2>&1 | grep -E 'error|warning'"), "bash: raw swift build/test")
        self.assertEqual(ac.bash_class("npm test | tail -20"), "bash: build/test")
        self.assertEqual(ac.bash_class("cd pkg && cargo test 2>&1 | grep FAILED | head -5"), "bash: build/test")
        self.assertEqual(ac.bash_class("git log --oneline | grep fix"), "bash: git log/status/etc")

    def test_a_loop_that_sleeps_until_a_log_shows_a_verdict_is_a_wait_not_a_grep(self):
        poll = 'L=run.log; for i in $(seq 1 58); do if grep -q "Test run with" "$L"; then break; fi; sleep 10; done; tail -3 "$L"'
        self.assertEqual(ac.bash_class(poll), "bash: wait loop (polling a run)")
        self.assertEqual(ac.bash_class("until grep -q DONE out.log; do caffeinate -t 20; done"),
                         "bash: wait loop (polling a run)")
        self.assertEqual(ac.bash_class("for f in a b; do grep -c x $f; done"), "bash: grep")

    def test_a_cd_on_its_own_line_is_transparent(self):
        self.assertEqual(ac.bash_class("cd /some/dir\nmake test | tail -5"), "bash: build/test")

    def test_a_pipeline_of_filters_only_keeps_the_whole_command_rule(self):
        self.assertEqual(ac.bash_class("grep -rn Foo Sources/ | head -5"), "bash: grep")
        self.assertEqual(ac.bash_class("grep -E 'a|b' file.txt"), "bash: grep")
        self.assertEqual(ac.bash_class("echo hello | head -1"), "bash: cat/sed/head window")
        self.assertEqual(ac.bash_class("ls || grep x f"), "bash: grep")
        self.assertEqual(ac.bash_class("go test ./..."), "bash: build/test")
        self.assertEqual(ac.bash_class("go vet ./..."), "bash: lint/format/gates")
        self.assertEqual(ac.bash_class("pytest -k foo"), "bash: build/test")
        self.assertEqual(ac.bash_class("dotnet test"), "bash: build/test")
        self.assertEqual(ac.bash_class("./gradlew build"), "bash: build/test")
        self.assertEqual(ac.bash_class("make test"), "bash: build/test")
        self.assertEqual(ac.bash_class("eslint ."), "bash: lint/format/gates")
        self.assertEqual(ac.bash_class("prettier --check ."), "bash: lint/format/gates")
        self.assertEqual(ac.bash_class("git commit -m 'make the build faster'"), "bash: git mutate")
        # the build/lint tool patterns are anchored to the start of the command, so a tool name that
        # merely appears inside a git/grep command doesn't misclassify it
        self.assertEqual(ac.bash_class('git commit -m "bump gradle"'), "bash: git mutate")
        self.assertEqual(ac.bash_class('git commit -m "fix black text"'), "bash: git mutate")
        self.assertEqual(ac.bash_class("grep -rn eslint ."), "bash: grep")


class ProjectsDirTests(unittest.TestCase):
    def _env(self, value):
        orig = os.environ.get("CLAUDE_CONFIG_DIR")
        if value is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = value
        self.addCleanup(lambda: os.environ.__setitem__("CLAUDE_CONFIG_DIR", orig) if orig is not None
                        else os.environ.pop("CLAUDE_CONFIG_DIR", None))

    def test_config_dir_variable_moves_the_default(self):
        self._env("/opt/claude-config")
        self.assertEqual(ac.default_projects_dir(), "/opt/claude-config/projects")

    def test_home_default_without_the_variable(self):
        self._env(None)
        self.assertEqual(ac.default_projects_dir(), os.path.join(HOME, ".claude", "projects"))


class ProjectDisplayNameTests(unittest.TestCase):
    def test_strips_encoded_home_prefix(self):
        encoded = HOME.replace("/", "-")
        self.assertEqual(ac.project_display_name(f"{encoded}-Developer-Foo"), "Developer-Foo")

    def test_leaves_unrelated_name_alone(self):
        self.assertEqual(ac.project_display_name("-some-other-path"), "-some-other-path")


class EndToEndTests(unittest.TestCase):
    def test_main_prints_every_section_header(self):
        fx = FixtureRoot(self)
        fx.main_session(entries=[
            instructions_attachment(ts_str(BASE), [("/rules/a.md", "x" * 100)]),
            assistant("m1", ts_str(BASE), usage(cache_read=5000), content=[tool_use_block("t1")]),
            user_tool_result(ts_str(BASE + timedelta(seconds=1)), "t1", "result text"),
            assistant("m2", ts_str(BASE + timedelta(seconds=2)), usage(cache_read=6000)),
        ])
        fx.subagent(agent_type="mechanic", entries=[
            instructions_attachment(ts_str(BASE), [("/rules/a.md", "x" * 100)]),
            deferred_attachment(ts_str(BASE), ["mcp__codeindex__digest"]),
            assistant("m1", ts_str(BASE), usage(cache_read=3000), content=[tool_use_block("t1")]),
            user_tool_result(ts_str(BASE + timedelta(seconds=1)), "t1", "result text"),
            assistant("m2", ts_str(BASE + timedelta(seconds=2)), usage(cache_read=4000)),
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", fx.root, "--since", str(ts_str(BASE - timedelta(hours=1))),
                     "--until", str(ts_str(BASE + timedelta(hours=1)))])
        output = buf.getvalue()
        for header in ("=== Totals ===", "=== Per day", "=== Concentration ===", "=== Turn shape ===",
                       "=== Cold cache ===", "=== What fills the context ===", "=== Fixed start ===",
                       "=== Largest contexts"):
            self.assertIn(header, output)


class ToolsSectionTests(unittest.TestCase):
    """--tools is the evidence an agent definition's `tools:` list is built from."""

    def _calls(self, *names):
        return [{"type": "tool_use", "id": f"t{i}", "name": n, "input": {}} for i, n in enumerate(names)]

    def test_counts_calls_per_agent_type_and_omits_types_that_called_nothing_extra(self):
        fx = FixtureRoot(self)
        fx.subagent(agent="m1", agent_type="mechanic", entries=[
            assistant("m1", ts_str(BASE), usage(cache_read=1000), self._calls("Bash", "Bash", "Read"))])
        fx.subagent(agent="r1", agent_type="reviewer", entries=[
            assistant("r1", ts_str(BASE), usage(cache_read=1000), self._calls("Grep"))])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))

        out = []
        ac.section_tools(out, loaded)
        text = "\n".join(out)
        self.assertIn("mechanic", text)
        self.assertIn("Bash 2", text)
        self.assertIn("Read 1", text)
        self.assertIn("Grep 1", text)
        self.assertNotIn("Grep 1, Bash", text)  # counted per type, not pooled

    def test_section_is_absent_unless_asked_for(self):
        fx = FixtureRoot(self)
        fx.subagent(agent="m1", agent_type="mechanic", entries=[
            assistant("m1", ts_str(BASE), usage(cache_read=1000), self._calls("Bash"))])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        self.assertNotIn("=== Tools called", ac.build_report(loaded, 12))
        self.assertIn("=== Tools called", ac.build_report(loaded, 12, tools=True))


class WindowedToolsTests(unittest.TestCase):
    def test_tools_section_counts_only_calls_inside_the_window(self):
        fx = FixtureRoot(self)
        t_before = BASE - timedelta(hours=2)
        t_after = BASE + timedelta(hours=2)
        fx.subagent(agent_type="mechanic", entries=[
            assistant("m1", ts_str(t_before), usage(cache_read=1000), [tool_use_block("t1", "Read")]),
            assistant("m2", ts_str(t_after), usage(cache_read=2000), [tool_use_block("t2", "Bash")]),
        ])
        loaded = ac.load_all(fx.root, None, BASE, BASE + timedelta(hours=4))
        out = []
        ac.section_tools(out, loaded)
        text = "\n".join(out)
        self.assertIn("Bash 1", text)
        self.assertNotIn("Read 1", text)

    def test_a_tool_use_streamed_on_two_lines_counts_once(self):
        fx = FixtureRoot(self)
        line = assistant("m1", ts_str(BASE), usage(cache_read=1000), [tool_use_block("t1", "Bash")])
        fx.subagent(agent_type="mechanic", entries=[line, line])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        out = []
        ac.section_tools(out, loaded)
        self.assertIn("Bash 1", "\n".join(out))


class EncodingTests(unittest.TestCase):
    def test_transcripts_are_read_as_utf8_under_an_ascii_locale(self):
        fx = FixtureRoot(self)
        path = fx.subagent(agent_type="mechanic", entries=[])
        entries = [
            assistant("m1", ts_str(BASE), usage(cache_read=1000), [tool_use_block("t1")]),
            user_tool_result(ts_str(BASE + timedelta(seconds=1)), "t1", "café — naïve ×"),
        ]
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        run = subprocess.run(
            [sys.executable, SCRIPT, "--projects", fx.root,
             "--since", ts_str(BASE - timedelta(hours=1)), "--until", ts_str(BASE + timedelta(hours=1))],
            capture_output=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        self.assertRegex(run.stdout, r"subagent\s+contexts\s+1\b")


class SubagentColdBreakdownTests(unittest.TestCase):
    """What subagent cold turns were waiting on: after a Bash call, after another tool, after no
    tool call — and, for the Bash group, which commands and how concentrated across agents."""

    def _turn(self, ts, tools, u):
        return dict(ts=ts, ctx=ac.context_size(u), usage=u, model="claude-sonnet-5", n_tools=len(tools),
                    tools=tools)

    def _context(self, turns, events, kind="subagent"):
        c = ac.Context(kind, "/tmp/subcold.jsonl", "proj", "sess", "c")
        c.turns = turns
        c.events = events
        return c

    def _records_by_context(self, contexts):
        out = []
        for ctx in contexts:
            in_window = {id(t) for t in ctx.turns}
            recs = [r for r in ac.cold_turns(ctx.turns) if id(r["turn"]) in in_window]
            out.append((ctx, recs))
        return out

    def setUp(self):
        t0 = BASE
        # Context A: one cold turn following a Bash call, prev_ctx 50000, gap 300s, extra 45000.
        a_prev = self._turn(t0, {"Bash": 1}, usage(input_tokens=0, cache_read=50000))
        a_cold = self._turn(t0 + timedelta(seconds=300), {},
                            usage(input_tokens=50000, cache_read=0, cache_creation=0))
        self.ctx_a = self._context([a_prev, a_cold], [(1, "bash: raw swift build/test", 500)])

        # Context D: two cold turns, both following Bash, gaps 600s/900s, prev_ctx 100000/150000,
        # extras 90000/135000 — exercises the "two or more" grouping.
        d0 = self._turn(t0, {"Bash": 1}, usage(input_tokens=0, cache_read=100000))
        d1 = self._turn(t0 + timedelta(seconds=600), {},
                        usage(input_tokens=100000, cache_read=0, cache_creation=0))
        d2 = self._turn(t0 + timedelta(seconds=700), {"Bash": 1}, usage(input_tokens=0, cache_read=150000))
        d3 = self._turn(t0 + timedelta(seconds=1600), {},
                        usage(input_tokens=150000, cache_read=0, cache_creation=0))
        self.ctx_d = self._context([d0, d1, d2, d3],
                                    [(1, "bash: grep", 500), (3, "bash: raw swift build/test", 500)])

        # Context B: one cold turn following a Read (a tool, but not Bash). prev_ctx 20000, extra 18000.
        b_prev = self._turn(t0, {"Read": 1}, usage(input_tokens=0, cache_read=20000))
        b_cold = self._turn(t0 + timedelta(seconds=400), {},
                            usage(input_tokens=20000, cache_read=0, cache_creation=0))
        self.ctx_b = self._context([b_prev, b_cold], [])

        # Context C: one cold turn following a text-only turn (no tool call). prev_ctx 10000, extra 9000.
        c_prev = self._turn(t0, {}, usage(input_tokens=0, cache_read=10000))
        c_cold = self._turn(t0 + timedelta(seconds=350), {},
                            usage(input_tokens=10000, cache_read=0, cache_creation=0))
        self.ctx_c = self._context([c_prev, c_cold], [])

        self.records_by_context = self._records_by_context(
            [self.ctx_a, self.ctx_b, self.ctx_c, self.ctx_d])

    def test_group_counts_and_avoidable(self):
        bd = ac.subagent_cold_breakdown(self.records_by_context)
        self.assertEqual(len(bd["bash"]), 3)
        self.assertAlmostEqual(sum(r["extra"] for r in bd["bash"]), 270000)
        self.assertEqual(len(bd["other_tool"]), 1)
        self.assertAlmostEqual(sum(r["extra"] for r in bd["other_tool"]), 18000)
        self.assertEqual(len(bd["no_tool"]), 1)
        self.assertAlmostEqual(sum(r["extra"] for r in bd["no_tool"]), 9000)

    def test_bash_group_median_wait_and_context(self):
        bd = ac.subagent_cold_breakdown(self.records_by_context)
        self.assertEqual(ac.median(r["gap"] for r in bd["bash"]), 600)
        self.assertEqual(ac.median(r["prev_ctx"] for r in bd["bash"]), 100000)

    def test_by_command_top_categories(self):
        bd = ac.subagent_cold_breakdown(self.records_by_context)
        by_command = {cat: (av, n) for cat, av, n in bd["by_command"]}
        self.assertEqual(by_command["bash: raw swift build/test"], (45000 + 135000, 2))
        self.assertEqual(by_command["bash: grep"], (90000, 1))

    def test_contexts_with_cold_and_two_or_more_share(self):
        bd = ac.subagent_cold_breakdown(self.records_by_context)
        self.assertEqual(bd["total_contexts"], 4)
        self.assertEqual(bd["contexts_with_cold"], 4)
        self.assertEqual(bd["contexts_multi"], 1)
        self.assertAlmostEqual(bd["total_avoidable"], 297000)
        self.assertAlmostEqual(bd["multi_avoidable"], 225000)

    def test_section_renders_the_breakdown_for_subagent_cold_turns(self):
        loaded = [ac.Loaded(ctx, BASE - timedelta(minutes=1), BASE + timedelta(hours=1))
                  for ctx in (self.ctx_a, self.ctx_b, self.ctx_c, self.ctx_d)]
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertIn("what the cold turns were waiting on", report)
        self.assertIn("after a Bash call", report)
        self.assertIn("median wait 10m, median context 100k", report)
        self.assertIn("agents with a cold turn: 4 of 4", report)

    def test_a_cold_turn_outside_its_contexts_window_is_not_counted(self):
        t_early = BASE - timedelta(hours=2)
        t_cold = BASE - timedelta(hours=1)
        prev = self._turn(t_early, {"Bash": 1}, usage(input_tokens=0, cache_read=50000))
        cold = self._turn(t_cold, {}, usage(input_tokens=50000, cache_read=0, cache_creation=0))
        ctx_outside = self._context([prev, cold], [(1, "bash: raw swift build/test", 500)])

        loaded = [ac.Loaded(ctx, BASE - timedelta(minutes=1), BASE + timedelta(hours=1))
                  for ctx in (self.ctx_a, self.ctx_b, self.ctx_c, self.ctx_d)]
        # This context's only turns are both before the window, so it has no window turns at all —
        # its cold turn must not be counted toward the breakdown.
        loaded.append(ac.Loaded(ctx_outside, BASE, BASE + timedelta(hours=1)))
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertIn("agents with a cold turn: 4 of 5", report)

    def test_main_only_cold_turns_do_not_print_the_subagent_breakdown(self):
        t0 = BASE
        main_prev = self._turn(t0, {}, usage(input_tokens=0, cache_read=5000))
        main_cold = self._turn(t0 + timedelta(seconds=400), {},
                               usage(input_tokens=5000, cache_read=0, cache_creation=0))
        ctx_main = self._context([main_prev, main_cold], [], kind="main")
        loaded = [ac.Loaded(ctx_main, BASE - timedelta(minutes=1), BASE + timedelta(hours=1))]
        out = []
        ac.section_cold_cache(out, loaded)
        report = "\n".join(out)
        self.assertNotIn("what the cold turns were waiting on", report)


class EmptyWindowTests(unittest.TestCase):
    def test_no_turns_in_window_says_so_instead_of_a_report_of_zeros(self):
        fx = FixtureRoot(self)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", fx.root, "--since", ts_str(BASE - timedelta(hours=1)),
                     "--until", ts_str(BASE + timedelta(hours=1))])
        output = buf.getvalue()
        self.assertIn("no turns between", output)
        self.assertNotIn("=== Totals ===", output)


if __name__ == "__main__":
    unittest.main()
