"""Tests for scripts/agent_cost.py, the report. Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests"""
import importlib.machinery
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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "agent_cost.py")
spec = importlib.util.spec_from_loader("agentcost", importlib.machinery.SourceFileLoader("agentcost", SCRIPT))
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


def seq_loaded(kind, specs, agent="s0"):
    """One Loaded whose single context has one turn per (n_tools, cats, ie) spec, in order."""
    c = ac.Context(kind, f"/tmp/fake-{agent}.jsonl", "proj", "sess", agent)
    c.turns = [dict(ts=BASE + timedelta(seconds=i), ctx=int(ie), usage=usage(input_tokens=int(ie), output_tokens=1),
                    model="claude-sonnet-5", n_tools=n, tools={}, cats=list(cats))
               for i, (n, cats, ie) in enumerate(specs)]
    return ac.Loaded(c, BASE - timedelta(minutes=1), BASE + timedelta(minutes=1))


R, G, E, B = ["Read (ranged)"], ["Grep/Glob tool"], ["Edit/Write"], ["bash: raw swift build/test"]


def followon_idx(*cat_lists, n_tools=None):
    lc = seq_loaded("main", [(len(c) if n_tools is None else n_tools[i], c, 1) for i, c in enumerate(cat_lists)])
    return [lc.window_turns.index(t) for t in ac.batchable_followons(lc.window_turns)]


class BatchableFollowonTests(unittest.TestCase):
    def test_runs_contribute_every_turn_after_the_first(self):
        self.assertEqual(followon_idx(R, R, G, E, R, R), [1, 2, 5])

    def test_a_two_call_turn_and_a_zero_call_turn_each_break_a_run(self):
        self.assertEqual(followon_idx(R, R + R, R, R), [3])
        self.assertEqual(followon_idx(R, [], R, R), [3])

    def test_a_non_read_only_single_call_breaks_a_run(self):
        self.assertEqual(followon_idx(R, B, R), [])
        self.assertEqual(followon_idx(R, R, B, R, R), [1, 4])

    def test_a_lone_read_contributes_nothing(self):
        self.assertEqual(followon_idx(R), [])
        self.assertEqual(followon_idx(E, R, E), [])

    def test_a_turn_without_cats_does_not_qualify(self):
        t = dict(n_tools=1, tools={})
        self.assertEqual(ac.batchable_followons([t, t]), [])

    def test_runs_do_not_span_contexts(self):
        loaded = [seq_loaded("main", [(1, R, 1)], "a"), seq_loaded("main", [(1, R, 1)], "b")]
        out = []
        ac.section_turn_shape(out, loaded)
        line = next(l for l in out if "following another" in l)
        self.assertIn("  0.0% of turns", line)

    def test_section_line_percentages(self):
        # one context: Read(ie 10), Read(20), Edit(30), Read(40), Read(100), two-call(200).
        # follow-ons are turns 2 and 5: 2 of 6 turns, (20+100) of 400.
        lc = seq_loaded("subagent", [(1, R, 10), (1, R, 20), (1, E, 30), (1, R, 40), (1, R, 100), (2, R + R, 200)])
        out = []
        ac.section_turn_shape(out, [lc])
        line = next(l for l in out if "following another" in l)
        self.assertTrue(line.strip().startswith("subagent"))
        self.assertAlmostEqual(float(re.search(r"([\d.]+)% of turns", line).group(1)), 100 * 2 / 6, places=1)
        self.assertAlmostEqual(float(re.search(r"([\d.]+)% of input-eq spend", line).group(1)), 100 * 120 / 400, places=1)

    def test_load_context_records_each_calls_category(self):
        root = tempfile.mkdtemp(prefix="agentcost-cats-")
        self.addCleanup(shutil.rmtree, root)
        path = os.path.join(root, "agent-a1.jsonl")
        write_jsonl(path, [
            assistant("m1", ts_str(BASE), usage(), content=[
                tool_use_block("t1", "Read", {"file_path": "/x", "limit": 5}),
                tool_use_block("t2", "Grep", {"pattern": "x"})]),
            assistant("m2", ts_str(BASE), usage(), content=[tool_use_block("t3", "Bash", {"command": "git diff"})]),
        ])
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual([t["cats"] for t in ctx.turns], [["Read (ranged)", "Grep/Glob tool"], ["bash: git diff/show"]])

    def test_a_command_that_writes_is_not_a_read(self):
        for command in ("sed -i '' 's/a/b/' f.json", "cat > notes.md <<'EOF'\nx\nEOF", "head -5 a | tee b",
                        "git branch -D old", "git worktree remove ../wt", "cat a >> b"):
            self.assertEqual(ac.turn_shape_category(ac.bash_class(command), {"command": command}), "bash: writes", command)
        for command in ("sed -n 1,5p f", "cat a 2>&1 | head", "grep -n x f >/dev/null", "git log -3", "git branch"):
            self.assertIn(ac.turn_shape_category(ac.bash_class(command), {"command": command}),
                          ac.READ_ONLY_CATEGORIES, command)
        self.assertEqual(followon_idx(R, ["bash: writes"], R), [])


class ConcentrationTests(unittest.TestCase):
    def test_top_10_percent_share(self):
        ies = [100, 90] + [10] * 8   # sum = 270, top 10% (1 context) = 100
        loaded = make_loaded("subagent", ies)
        out = []
        ac.section_concentration(out, loaded)
        line = next(l for l in out if l.strip().startswith("subagent") and "top 10%" in l)
        pct = float(re.search(r": ([\d.]+)%", line).group(1))
        self.assertAlmostEqual(pct, 100.0 * 100 / 270, places=1)
        # Only subagent contexts are loaded, so there is no main row and no combined row repeating it.
        self.assertFalse([l for l in out if l.strip().startswith("main")])
        self.assertFalse([l for l in out if l.strip().startswith("combined")])

    def test_under_ten_contexts_reports_the_largest_not_a_decile(self):
        loaded = make_loaded("main", [60, 30, 10])   # sum = 100, largest = 60%
        out = []
        ac.section_concentration(out, loaded)
        line = next(l for l in out if "largest" in l)
        self.assertIn("too few contexts (3) for a top 10%", line)
        self.assertIn("60.0% of input-eq spend", line)   # 60 of 100, not a "top 10%" of 3 contexts

    def test_a_single_context_says_so_rather_than_quoting_a_share_of_itself(self):
        out = []
        ac.section_concentration(out, make_loaded("main", [42]))
        text = "\n".join(out)
        self.assertIn("one context in this window", text)
        self.assertNotIn("top 10%", text)

    def test_both_kinds_get_their_own_row_plus_a_combined_row(self):
        # main: one context of 100. subagent: two of 50 and 50. combined total 200, largest 100.
        loaded = make_loaded("main", [100]) + make_loaded("subagent", [50, 50])
        out = []
        ac.section_concentration(out, loaded)
        main_line = next(l for l in out if l.strip().startswith("main") and "one context" in l)
        sub_line = next(l for l in out if l.strip().startswith("subagent") and "largest" in l)
        comb_line = next(l for l in out if l.strip().startswith("combined") and "largest" in l)
        self.assertIn("all of the input-eq spend", main_line)
        self.assertIn("50.0% of input-eq spend", sub_line)     # 50 of 100
        self.assertIn("50.0% of input-eq spend", comb_line)    # 100 of 200


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

    def test_a_loop_word_in_a_message_or_a_loop_pacing_its_own_work_is_not_a_wait(self):
        self.assertEqual(ac.bash_class('git commit -m "retry for the flaky sleep test; done"'), "bash: git mutate")
        self.assertEqual(ac.bash_class("for f in *.png; do sips -Z 800 $f; sleep 1; done"), "bash: other")
        self.assertEqual(ac.bash_class('gh pr list | grep -i "fix for" ; sleep 0'), "bash: gh")

    def test_a_pipe_inside_quotes_does_not_split_the_command(self):
        self.assertEqual(ac.bash_class("rg 'cargo build|cargo test' Sources/"), "bash: grep")
        self.assertEqual(ac.bash_class('grep -E "npm test|make" notes.txt | head -3'), "bash: grep")
        self.assertEqual(ac.pipeline_stages("a 'x|y' | b \"p|q\" || c"), ["a 'x|y' ", " b \"p|q\" || c"])

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


class ProjectFilterTests(unittest.TestCase):
    PROJ = "-home-me-work-Proj"
    SCRATCH = "-tmp-home-me-work-Proj-0000-scratchpad"
    OTHER = "-home-me-work-ProjKit"

    def setUp(self):
        self.fx = FixtureRoot(self)
        for project in (self.PROJ, self.SCRATCH, self.OTHER):
            self.fx.main_session(project=project, entries=[assistant("m1", ts_str(BASE), usage(cache_read=5000))])
            self.fx.subagent(project=project, entries=[assistant("m1", ts_str(BASE), usage(cache_read=3000))])

    def projects_loaded(self, project):
        loaded = ac.load_all(self.fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1),
                             project=project)
        return sorted({(l.ctx.project_dir, l.ctx.kind) for l in loaded})

    def test_trailing_name_takes_only_that_project_main_and_subagents(self):
        self.assertEqual(self.projects_loaded("Proj"), [(self.PROJ, "main"), (self.PROJ, "subagent")])

    def test_path_forms_match_the_same_project(self):
        for given in ("work/Proj", "/home/me/work/Proj", "/home/me/work/Proj/"):
            with self.subTest(given=given):
                self.assertEqual(self.projects_loaded(given), [(self.PROJ, "main"), (self.PROJ, "subagent")])

    def test_no_filter_loads_every_project(self):
        self.assertEqual(len(self.projects_loaded(None)), 6)

    def test_unmatched_project_names_the_filter_in_the_empty_message(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", self.fx.root, "--project", "Nope", "--since", ts_str(BASE - timedelta(hours=1)),
                     "--until", ts_str(BASE + timedelta(hours=1))])
        self.assertIn("for project Nope", buf.getvalue())
        self.assertNotIn("=== Totals ===", buf.getvalue())


class TranscriptArgumentTests(unittest.TestCase):
    """--transcript opens only a file that a listing of its folder for `*.jsonl` returned."""
    def setUp(self):
        self.fx = FixtureRoot(self)
        self.session = self.fx.main_session(entries=[assistant("m1", ts_str(BASE), usage(cache_read=5000))])
        self.fx.subagent(agent_type="builder", entries=[assistant("m1", ts_str(BASE), usage(cache_read=3000))])

    def test_a_session_named_by_path_is_reported_with_its_subagents(self):
        loaded = ac.load_all(None, self.session, BASE, BASE)
        self.assertEqual(sorted((l.ctx.kind, l.ctx.agent_type) for l in loaded),
                         [("main", "main"), ("subagent", "builder")])

    def test_a_name_that_is_not_a_transcripts_is_refused_before_it_is_read(self):
        # The same content under another name: a transcript is a .jsonl file, and the report never
        # opens a file that a listing of its folder for that pattern did not return.
        copy = self.session[:-len(".jsonl")] + ".txt"
        shutil.copyfile(self.session, copy)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            ac.main(["--transcript", copy, "--sections", "totals"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("is not a transcript", err.getvalue())
        self.assertNotIn("=== Totals ===", out.getvalue())

    def test_a_transcript_that_does_not_exist_reports_nothing_rather_than_failing(self):
        missing = os.path.join(os.path.dirname(self.session), "gone.jsonl")
        out = io.StringIO()
        with redirect_stdout(out):
            ac.main(["--transcript", missing, "--sections", "totals"])
        self.assertIn("nothing to report", out.getvalue())
        self.assertIn(missing, out.getvalue())


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


def sections_of(report):
    """The report split into its `=== X ===` sections: header text -> body text."""
    bodies = {}
    header = None
    for line in report.split("\n"):
        if line.startswith("==="):
            header = line.strip("= ").strip()
            bodies[header] = []
        elif header:
            bodies[header].append(line)
    return {h: "\n".join(lines) for h, lines in bodies.items()}


def section_body(report, name):
    """The body of the one section whose header starts with `name`."""
    bodies = sections_of(report)
    matches = [b for h, b in bodies.items() if h.startswith(name)]
    assert len(matches) == 1, f"{name}: {list(bodies)}"
    return matches[0]


def loaded_context(kind, sizes, events=(), name="c"):
    """A Loaded over one context with `sizes` as its per-turn context sizes (input-eq == size, since
    the usage is pure uncached input) and `events` as its (turn_index, category, chars) list."""
    c = ac.Context(kind, f"/tmp/{name}.jsonl", "proj", "sess", name)
    c.turns = build_turns([(BASE, s) for s in sizes])
    c.events = list(events)
    return ac.Loaded(c, BASE - timedelta(minutes=1), BASE + timedelta(minutes=1))


class MainOnlyReportTests(unittest.TestCase):
    """A window with no subagent context is reported as that person's own picture: no zero-filled
    subagent rows, no combined row that only repeats main, no subagent cold-cache breakdown."""

    def _main_only_report(self):
        fx = FixtureRoot(self)
        fx.main_session(session="sess1", entries=[
            instructions_attachment(ts_str(BASE), [("/rules/a.md", "x" * 400)]),
            assistant("m1", ts_str(BASE), usage(input_tokens=0, cache_read=20000),
                      content=[tool_use_block("t1")]),
            user_tool_result(ts_str(BASE + timedelta(seconds=1)), "t1", "result " * 200),
            assistant("m2", ts_str(BASE + timedelta(seconds=30)), usage(input_tokens=0, cache_read=26000)),
            assistant("m3", ts_str(BASE + timedelta(seconds=60)), usage(input_tokens=0, cache_read=30000)),
        ])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        self.assertEqual([l.ctx.kind for l in loaded], ["main"])
        return ac.build_report(loaded, 12)

    def test_no_subagent_rows_and_the_session_is_listed_under_main_sessions(self):
        report = self._main_only_report()
        for name in ("Totals", "Per day", "Concentration", "Turn shape", "Cold cache",
                     "What fills the context"):
            self.assertNotIn("subagent", section_body(report, name).lower(),
                             f"{name} still mentions subagents in a main-only window")
        self.assertNotIn("combined", section_body(report, "Turn shape"))
        main_sessions = section_body(report, "Main sessions")
        self.assertIn("sess1", main_sessions)
        self.assertIn("-Users-x-Developer-Proj", main_sessions)

    def test_a_day_with_under_ten_contexts_claims_no_top_decile(self):
        per_day = section_body(self._main_only_report(), "Per day")
        self.assertNotIn("100.0%", per_day)
        self.assertTrue(per_day.rstrip().endswith("-"), per_day)


class SubagentOnlyReportTests(unittest.TestCase):
    def test_no_main_sessions_section_and_no_main_rows_when_only_agents_ran(self):
        fx = FixtureRoot(self)
        fx.subagent(agent="a1", entries=[
            assistant("s1", ts_str(BASE), usage(input_tokens=0, cache_read=5000)),
            assistant("s2", ts_str(BASE + timedelta(seconds=30)), usage(input_tokens=0, cache_read=6000)),
        ])
        report = ac.build_report(ac.load_all(fx.root, None, BASE - timedelta(hours=1),
                                             BASE + timedelta(hours=1)), 12)
        self.assertNotIn("=== Main sessions ===", report)
        for name in ("Totals", "Concentration", "Turn shape"):
            body = section_body(report, name)
            self.assertFalse([l for l in body.split("\n") if l.strip().startswith("main")], name)
        self.assertNotIn("by gap and context size", section_body(report, "Cold cache"))


class KindSplitTests(unittest.TestCase):
    """Both kinds in the window: the sections that were combined figures now carry each kind's own."""

    def _loaded(self):
        # main A: 3 turns (1000, 2000, 3000) -> input-eq 6000; one event of 2000 chars first sent in
        #   window-turn 1, so ratio (3000-1000)/2000 == 1 and it is re-sent 3-1-1 == 1 time -> 2000.
        #   Its base (1000) is re-sent n-1 == 2 times -> 2000. main's own re-sent total: 4000.
        # sub C: 3 turns (1000, 2000, 5000) -> input-eq 8000; event 4000 chars, ratio 1, re-sent once
        #   -> 4000, base re-sent 2000. subagent's own re-sent total: 6000.
        # main B and sub D are single-turn contexts: concentration counts them, the fills table skips
        #   them (it needs two turns to see anything re-sent).
        return [loaded_context("main", [1000, 2000, 3000], [(1, "cat-M", 2000)], "A"),
                loaded_context("main", [500], name="B"),
                loaded_context("subagent", [1000, 2000, 5000], [(1, "cat-S", 4000)], "C"),
                loaded_context("subagent", [2000], name="D")]

    def test_concentration_reports_each_kind_and_the_combination(self):
        out = []
        ac.section_concentration(out, self._loaded())
        main_line = next(l for l in out if l.strip().startswith("main") and "largest" in l)
        sub_line = next(l for l in out if l.strip().startswith("subagent") and "largest" in l)
        comb_line = next(l for l in out if l.strip().startswith("combined") and "largest" in l)
        self.assertIn("(2) for a top 10%", main_line)
        self.assertIn("92.3% of input-eq spend", main_line)   # 6000 of 6500
        self.assertIn("80.0% of input-eq spend", sub_line)    # 8000 of 10000
        self.assertIn("(4) for a top 10%", comb_line)
        self.assertIn("48.5% of input-eq spend", comb_line)   # 8000 of 16500

    def test_fills_context_carries_a_share_column_per_kind(self):
        out = []
        ac.section_fills_context(out, self._loaded())
        header = next(l for l in out if "category" in l)
        self.assertTrue(header.rstrip().endswith("main %   sub %"), header)
        shares = {}
        for line in out:
            for cat in ("cat-M", "cat-S", "(base)"):
                if line.strip().startswith(cat):
                    shares[cat] = (float(line.split()[-2]), float(line.split()[-1]))
        self.assertEqual(shares["cat-M"], (50.0, 0.0))     # 2000 of main's 4000; none of it subagent
        self.assertEqual(shares["cat-S"], (0.0, 66.7))     # 4000 of subagent's 6000
        self.assertEqual(shares["(base)"], (50.0, 33.3))   # 2000 of 4000, 2000 of 6000

    def test_per_day_splits_the_day_total_into_main_and_subagent(self):
        out = []
        ac.section_per_day(out, self._loaded())
        header = next(l for l in out if l.strip().startswith("day"))
        self.assertIn("main", header)
        self.assertIn("subagent", header)
        row = out[-2]
        # day total 16500 ("16k" at this scale), main 6500, subagent 10000.
        self.assertIn(ac.fmt_tok(16500), row)
        self.assertIn(ac.fmt_tok(6500), row)
        self.assertIn(ac.fmt_tok(10000), row)

    def test_per_day_has_no_split_columns_with_one_kind(self):
        out = []
        ac.section_per_day(out, [loaded_context("main", [1000, 2000], name="A")])
        header = next(l for l in out if l.strip().startswith("day"))
        self.assertNotIn("subagent", header)


class MainSessionsSectionTests(unittest.TestCase):
    def _loaded(self):
        fx = FixtureRoot(self)
        alpha = "-Users-x-Developer-Alpha"
        fx.main_session(project=alpha, session="s1", entries=[
            assistant("a1", ts_str(BASE), usage(input_tokens=3000, output_tokens=10)),
            assistant("a2", ts_str(BASE + timedelta(minutes=1)), usage(input_tokens=2000, output_tokens=10)),
        ])
        fx.main_session(project=alpha, session="s2", entries=[
            assistant("b1", ts_str(BASE), usage(input_tokens=1000, output_tokens=5))])
        fx.main_session(project="-Users-x-Developer-Beta", session="s3", entries=[
            assistant("c1", ts_str(BASE), usage(input_tokens=10000, output_tokens=10))])
        return ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))

    def test_per_project_totals_and_top_n_sessions(self):
        out = []
        ac.section_main_sessions(out, self._loaded(), 2)
        text = "\n".join(out)
        alpha = next(l for l in out if "Alpha" in l)
        beta = next(l for l in out if "Beta" in l)
        self.assertLess(out.index(beta), out.index(alpha))   # 10k before 6k
        self.assertRegex(alpha, r"Alpha\s+2\s+3\s+6k\s+25")  # 2 sessions, 3 turns, 6k input-eq, 25 out
        self.assertRegex(beta, r"Beta\s+1\s+1\s+10k\s+10")
        self.assertIn("top 2 sessions by input-eq", text)
        self.assertIn("s3", text)
        self.assertIn("s1", text)
        self.assertNotIn("s2", text)                          # --top 2 stops at two sessions

    def test_session_row_carries_model_turns_peak_and_input_eq(self):
        out = []
        ac.section_main_sessions(out, self._loaded(), 12)
        row = next(l for l in out if "s1" in l)
        self.assertRegex(row, r"Alpha\s+s1\s+claude-sonnet-5\s+2\s+3k\s+5k")   # peak 3000, input-eq 5000


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




class NameTailTests(unittest.TestCase):
    def test_a_long_name_keeps_the_end_that_tells_a_worktree_from_its_repository(self):
        repo = "Developer-Some-Long-Repository-Name"
        a, b = ac.name_tail(repo), ac.name_tail(repo + "--claude-worktrees-agent-1")
        self.assertNotEqual(a, b)
        self.assertEqual(len(b), 30)
        self.assertTrue(b.endswith("worktrees-agent-1"))
        self.assertEqual(ac.name_tail("short"), "short")


# the heading each --sections name prints, written out rather than read back from the registry: a test
# that recomputes the name from the code under test pins nothing.
SECTION_HEADINGS = {
    "totals": "=== Totals ===",
    "spend-by-type": "=== Spend by agent type ===",
    "per-day": "=== Per day",
    "main-sessions": "=== Main sessions ===",
    "concentration": "=== Concentration ===",
    "turn-shape": "=== Turn shape ===",
    "cold-cache": "=== Cold cache ===",
    "what-fills-the-context": "=== What fills the context ===",
    "fixed-start": "=== Fixed start ===",
    "rule-scorecard": "=== Rule scorecard ===",
    "largest-contexts": "=== Largest contexts",
    "tools-called": "=== Tools called",
}


class SectionSelectionTests(unittest.TestCase):
    """--sections: a follow-up question costs the lines it needs, not the whole report."""

    def _loaded(self):
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
        self.fx = fx
        return ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))

    def test_every_section_can_be_asked_for_on_its_own(self):
        loaded = self._loaded()
        self.assertEqual(sorted(SECTION_HEADINGS), sorted(ac.SECTION_NAMES))
        for name, heading in SECTION_HEADINGS.items():
            with self.subTest(section=name):
                report = ac.build_report(loaded, 12, sections=[name])
                self.assertIn(heading, report)
                headings = [l for l in report.splitlines() if l.startswith("=== ")]
                self.assertEqual(len(headings), 1, headings)

    def test_default_report_is_every_section_but_tools(self):
        report = ac.build_report(self._loaded(), 12)
        for name, heading in SECTION_HEADINGS.items():
            if name == "tools-called":
                self.assertNotIn(heading, report)
            else:
                self.assertIn(heading, report)

    def test_names_match_by_case_insensitive_prefix(self):
        self.assertEqual(ac.resolve_sections("TOTALS, Main ,cold"),
                         ["totals", "main-sessions", "cold-cache"])

    def test_selection_prints_in_report_order_not_the_order_given(self):
        self.assertEqual(ac.resolve_sections("cold,totals"), ["totals", "cold-cache"])
        report = ac.build_report(self._loaded(), 12, sections=ac.resolve_sections("cold,totals"))
        self.assertLess(report.index("=== Totals ==="), report.index("=== Cold cache ==="))

    def test_a_name_repeated_renders_its_section_once(self):
        self.assertEqual(ac.resolve_sections("cold,cold-cache"), ["cold-cache"])

    def test_an_unknown_name_lists_the_sections_that_exist(self):
        with self.assertRaises(ValueError) as e:
            ac.resolve_sections("totals,collder")
        self.assertIn("'collder'", str(e.exception))
        for name in ac.SECTION_NAMES:
            self.assertIn(name, str(e.exception))

    def test_an_ambiguous_prefix_names_the_sections_it_matched(self):
        with self.assertRaises(ValueError) as e:
            ac.resolve_sections("t")
        for name in ("totals", "turn-shape", "tools-called"):
            self.assertIn(name, str(e.exception))

    def test_an_empty_selection_is_refused(self):
        with self.assertRaises(ValueError):
            ac.resolve_sections(" , ")

    def test_tools_flag_adds_its_section_to_a_selection(self):
        report = ac.build_report(self._loaded(), 12, tools=True, sections=["totals"])
        self.assertIn("=== Totals ===", report)
        self.assertIn("=== Tools called", report)

    def test_main_prints_only_the_sections_asked_for(self):
        loaded = self._loaded()  # for its fixture tree
        del loaded
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", self.fx.root, "--since", ts_str(BASE - timedelta(hours=1)),
                     "--until", ts_str(BASE + timedelta(hours=1)), "--sections", "totals,cold"])
        output = buf.getvalue()
        self.assertIn("=== Totals ===", output)
        self.assertIn("=== Cold cache ===", output)
        self.assertNotIn("=== Per day", output)
        self.assertNotIn("=== Fixed start ===", output)
        self.assertIn("note: input-eq is", output)  # the unit still explains itself

    def test_a_section_asked_for_by_name_says_so_when_the_window_holds_nothing_for_it(self):
        fx = FixtureRoot(self)
        fx.subagent(agent="a1", entries=[
            assistant("s1", ts_str(BASE), usage(input_tokens=0, cache_read=5000)),
            assistant("s2", ts_str(BASE + timedelta(seconds=30)), usage(input_tokens=0, cache_read=6000)),
        ])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        asked = ac.build_report(loaded, 12, sections=["main-sessions"])
        self.assertIn("=== Main sessions ===", asked)
        self.assertIn("none in this window", asked)
        # ... while the whole report still leaves out a kind the window does not hold
        self.assertNotIn("=== Main sessions ===", ac.build_report(loaded, 12))

    def test_main_refuses_an_unknown_name_before_it_reads_a_transcript(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            ac.main(["--projects", "/nonexistent-projects-dir", "--sections", "collder"])
        self.assertIn("collder", err.getvalue())
        self.assertIn("what-fills-the-context", err.getvalue())


def typed_loaded(kind, agent_type, ctxs, agent):
    """One Loaded whose context has one turn per size in `ctxs`, each priced at its size (pure uncached
    input) with 1 output token, and the agent type set by hand."""
    c = ac.Context(kind, f"/tmp/fake-{agent}.jsonl", "proj", "sess", agent)
    c.agent_type = agent_type
    c.turns = [dict(ts=BASE + timedelta(seconds=i), ctx=n, usage=usage(input_tokens=n, output_tokens=1),
                    model="claude-sonnet-5", n_tools=1, tools={}) for i, n in enumerate(ctxs)]
    return ac.Loaded(c, BASE - timedelta(minutes=1), BASE + timedelta(minutes=1))


class SpendByTypeTests(unittest.TestCase):
    """One row per agent type, the plugin prefix folded, so each rung's cost can be set against the next."""

    def _rows(self, loaded):
        out = []
        ac.section_spend_by_type(out, loaded)
        self.assertEqual(out[0], "=== Spend by agent type ===")
        return {line.split()[0]: line.split()[1:] for line in out[2:] if line.strip()}, [
            line.split()[0] for line in out[2:] if line.strip()]

    def test_rows_fold_the_plugin_prefix_count_peaks_and_shares_add_to_100(self):
        loaded = [
            typed_loaded("subagent", "delegate:builder", [100_000, 150_000], "b1"),  # peak exactly 150k
            typed_loaded("subagent", "old-name:builder", [210_000], "b2"),
            typed_loaded("subagent", "builder", [199_999], "b3"),
            typed_loaded("subagent", "reviewer", [50_000], "r1"),
            typed_loaded("main", "main", [120_000, 200_000, 180_000], "m1"),
        ]
        rows, order = self._rows(loaded)
        # builder 659,999 input-eq, main 500,000, reviewer 50,000: sorted by input-eq, then the total
        self.assertEqual(order, ["builder", "main", "reviewer", "total"])
        # columns: contexts, median turns, median peak, input-eq, share, output, >=150k, >=200k
        self.assertEqual(rows["builder"], ["3", "1", "200k", "660k", "54.5%", "4", "3", "1"])
        self.assertEqual(rows["main"], ["1", "3", "200k", "500k", "41.3%", "3", "1", "1"])
        self.assertEqual(rows["reviewer"], ["1", "1", "50k", "50k", "4.1%", "1", "0", "0"])
        self.assertEqual(rows["total"], ["5", "1", "200k", "1.2M", "100.0%", "8", "4", "2"])
        shares = sum(float(rows[name][4].rstrip("%")) for name in ("builder", "main", "reviewer"))
        self.assertAlmostEqual(shares, 100.0, delta=0.15)

    def _fixture(self):
        fx = FixtureRoot(self)
        fx.main_session(entries=[assistant("m1", ts_str(BASE), usage(input_tokens=5000))])
        fx.subagent(agent="a1", agent_type="delegate:builder",
                    entries=[assistant("s1", ts_str(BASE), usage(input_tokens=3000))])
        fx.subagent(agent="a2", agent_type="reviewer",
                    entries=[assistant("s2", ts_str(BASE), usage(input_tokens=1000))])
        return fx

    def test_in_the_default_report_straight_after_totals(self):
        fx = self._fixture()
        report = ac.build_report(ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1)), 12)
        headings = [l for l in report.splitlines() if l.startswith("=== ")]
        self.assertEqual(headings[:2], ["=== Totals ===", "=== Spend by agent type ==="])

    def test_asked_for_alone_with_sections(self):
        fx = self._fixture()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", fx.root, "--since", ts_str(BASE - timedelta(hours=1)),
                     "--until", ts_str(BASE + timedelta(hours=1)), "--sections", "spend-by-type"])
        lines = buf.getvalue().splitlines()
        self.assertEqual([l for l in lines if l.startswith("=== ")], ["=== Spend by agent type ==="])
        self.assertEqual([l.split()[0] for l in lines[2:6]], ["main", "builder", "reviewer", "total"])



# ---------------------------------------------------------------------------
# rule scorecard
# ---------------------------------------------------------------------------

SONNET, OPUS, HAIKU = "claude-sonnet-5", "claude-opus-5", "claude-haiku-5"
SPLIT_5M = {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 0}


def budget_nudge(ts, size_k, tier_text):
    """The budget hook's words as a subagent transcript records them: a hook_success attachment whose
    stdout carries the additionalContext."""
    stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                         f"Context budget: this agent's context is now {size_k}k tokens, and every "
                         f"further turn re-sends all of it. {tier_text}"}})
    return attachment(ts, {"type": "hook_success", "hookEvent": "PostToolUse", "stdout": stdout})


def handback(tid, message):
    return tool_use_block(tid, "SubagentHandback", {"message": message})


def meta_prompt(ts, text, origin=None):
    e = user_text(ts, text)
    e["isMeta"] = True
    if origin:
        e["origin"] = {"kind": origin}
    return e


def rule_tree(case):
    """A window that touches every scorecard line: a Sonnet session with 12 subagents (so a top 10%
    exists), one Opus builder above its parent that is nudged at 150k, goes cold after a Bash call,
    hands back and is prompted again; a reviewer that fails and one that passes; and one Opus agent
    whose parent session is not in the window."""
    fx = FixtureRoot(case)
    at = lambda s: ts_str(BASE + timedelta(seconds=s))
    fx.main_session(entries=[
        assistant("m1", at(0), usage(cache_read=5000), [tool_use_block("t1", "Grep", {"pattern": "x"})], model=SONNET),
        user_tool_result(at(1), "t1", "hit"),
        assistant("m2", at(10), usage(cache_read=6000), [tool_use_block("t2", "Grep", {"pattern": "y"})], model=SONNET),
        user_tool_result(at(11), "t2", "hit"),
        assistant("m3", at(1200), usage(cache_creation=7000, split=dict(SPLIT_5M, ephemeral_5m_input_tokens=7000)),
                  model=SONNET),
    ])
    fx.subagent(agent="b1", agent_type="delegate:builder", entries=[
        assistant("s1", at(60), usage(cache_creation=140_000), [tool_use_block("u1", "Bash", {"command": "make test"})], model=OPUS),
        user_tool_result(at(61), "u1", "ok"),
        budget_nudge(at(61), 155, "Finish only the item in hand: get it to a verified commit."),
        assistant("s2", at(120), usage(input_tokens=15_000, cache_read=140_000), [tool_use_block("u2", "Bash", {"command": "make check"})], model=OPUS),
        user_tool_result(at(121), "u2", "ok"),
        assistant("s3", at(900), usage(cache_creation=160_000), [tool_use_block("u3", "Read", {"file_path": "/a"})], model=OPUS),
        user_tool_result(at(901), "u3", "text"),
        assistant("s4", at(910), usage(input_tokens=45_000, cache_read=160_000), [handback("u4", "Done: one commit.")], model=OPUS),
        user_tool_result(at(911), "u4", "delivered"),
        meta_prompt(at(960), "The coordinator sent a message while you were working: one more fix.", "coordinator"),
        assistant("s5", at(970), usage(input_tokens=2, cache_read=205_000), model=OPUS),
    ])
    fx.subagent(agent="r1", agent_type="reviewer", entries=[
        assistant("s1", at(60), usage(input_tokens=20_000), [tool_use_block("v1", "Read", {"file_path": "/hook.py"})], model=SONNET),
        # a file that quotes the hook's words is not the hook speaking
        user_tool_result(at(61), "v1", "Context budget: this agent's context is now 150k tokens. Finish only the item in hand"),
        assistant("s1b", at(65), usage(input_tokens=20_500), [tool_use_block("v3", "Grep", {"pattern": "q"})], model=SONNET),
        user_tool_result(at(66), "v3", "hit"),
        assistant("s2", at(70), usage(input_tokens=21_000), [handback("v2", "Verdict: FAIL. One blocker before merge.")], model=SONNET),
        user_tool_result(at(71), "v2", "delivered"),
        meta_prompt(at(72), "Stop hook feedback:\nsomething"),  # a hook talking, not a second prompt
    ])
    fx.subagent(agent="r2", agent_type="delegate:reviewer", entries=[
        assistant("s1", at(60), usage(input_tokens=18_000), [handback("w1", "Verdict: pass. Merge-ready.")], model=HAIKU),
    ])
    for i in range(9):
        fx.subagent(agent=f"k{i}", agent_type="mechanic", entries=[
            assistant(f"s{i}", at(30 + i), usage(input_tokens=3000 + 1000 * i), [tool_use_block(f"k{i}", "Grep", {"pattern": "z"})], model=HAIKU),
            user_tool_result(at(31 + i), f"k{i}", "hit"),
        ])
    fx.subagent(session="orphan", agent="o1", agent_type="mechanic", entries=[
        assistant("s1", at(60), usage(input_tokens=4000), model=OPUS),
    ])
    return fx, ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))


# The four sections the scorecard draws on, rendered from `rule_tree` by the code before the scorecard
# was added: factoring their helpers out for it must leave every byte of them as it was.
SECTIONS_BEFORE_SCORECARD = {
    'spend-by-type': (
        '=== Spend by agent type ===\n'
        '  type                 contexts med turns  med peak  input-eq  share    output  >=150k  >=200k\n'
        '  builder                     1         5      205k      486k  75.6%        50       1       1\n'
        '  reviewer                    2         2       20k       80k  12.4%        40       0       0\n'
        '  mechanic                   10         1        6k       67k  10.4%       100       0       0\n'
        '  main                        1         3        7k       10k   1.5%        30       0       0\n'
        '  total                      14         1        8k      642k 100.0%       220       1       1\n'
        '\n'
        'harness versions in this window: 2.1.272\n'
        'note: input-eq is a price comparison against the uncached input rate, not a token count.'
    ),
    'concentration': (
        '=== Concentration ===\n'
        '  main       one context in this window: all of the input-eq spend, 3 turns\n'
        '  main       contexts under 50 turns: 100.0% of input-eq spend\n'
        '  subagent   top 10% of contexts (1 of 13): 76.8% of input-eq spend, median turns 5\n'
        '  subagent   contexts under 50 turns: 100.0% of input-eq spend\n'
        '  combined   top 10% of contexts (1 of 14): 75.6% of input-eq spend, median turns 5\n'
        '  combined   contexts under 50 turns: 100.0% of input-eq spend\n'
        '\n'
        'harness versions in this window: 2.1.272\n'
        'note: input-eq is a price comparison against the uncached input rate, not a token count.'
    ),
    'turn-shape': (
        '=== Turn shape ===\n'
        '  main       turns carrying exactly one tool call:  66.7% of turns,  11.2% of input-eq spend\n'
        '  main       one read-only call, following another:  33.3% of turns,   6.1% of input-eq spend — the most that requesting them together could save\n'
        '  subagent   turns carrying exactly one tool call:  89.5% of turns,  96.1% of input-eq spend\n'
        '  subagent   one read-only call, following another:   5.3% of turns,   3.2% of input-eq spend — the most that requesting them together could save\n'
        '  combined   turns carrying exactly one tool call:  86.4% of turns,  94.8% of input-eq spend\n'
        '  combined   one read-only call, following another:   9.1% of turns,   3.3% of input-eq spend — the most that requesting them together could save\n'
        '\n'
        'harness versions in this window: 2.1.272\n'
        'note: input-eq is a price comparison against the uncached input rate, not a token count.'
    ),
    'cold-cache': (
        '=== Cold cache ===\n'
        '  a turn is cold when it follows a gap of 5 minutes or more and read under half of the previous context from cache\n'
        '  main       cache writes  1-hour 0%              5-minute 100% (7k)\n'
        '  subagent   cache writes  no lifetime recorded\n'
        '  main       cold turns    1 of      3  input-eq     9k  (88.8% of main spend)      avoidable     7k (70.0%)\n'
        '  subagent   cold turns    1 of     19  input-eq   200k  (31.6% of subagent spend)  avoidable   178k (28.2%)\n'
        '\n'
        '  main, by gap and context size (avoidable input-eq):\n'
        '    gap            under 100k       100k and over\n'
        '    5m to 1h       7k (1 turn)      0 (0 turns)\n'
        '    over 1h        0 (0 turns)      0 (0 turns)\n'
        '\n'
        '  subagent, what the cold turns were waiting on (avoidable input-eq):\n'
        '    after a Bash call              178k (1 turn)   median wait 13m, median context 155k\n'
        '    after another tool                0 (0 turns)\n'
        '    after no tool call                0 (0 turns)\n'
        '    by command:  bash: build/test 178k (1)\n'
        '    agents with a cold turn: 1 of 13; the 0 with two or more hold 0% of it\n'
        '\n'
        'harness versions in this window: 2.1.272\n'
        'note: input-eq is a price comparison against the uncached input rate, not a token count.'
    ),
}


def scorecard_lines(report):
    """The scorecard's rule lines, keyed by the rule's words."""
    body = section_body(report, "Rule scorecard")
    rules = ("one job per agent, budget tiers", "a fresh agent per round", "calls requested together",
             "the slow check goes to a runner", "the model ceiling", "the reviewer pays")
    found = {}
    for raw in body.splitlines():
        for rule in rules:
            if raw.strip().startswith(rule):
                found[rule] = raw.strip()[len(rule):].strip()
    return found


class RuleScorecardTests(unittest.TestCase):
    """One line per rule the plugin states, each with its metric, value, baseline and judgement."""

    def test_the_sections_it_draws_on_render_as_they_did_before_it(self):
        _, loaded = rule_tree(self)
        for name, before in SECTIONS_BEFORE_SCORECARD.items():
            with self.subTest(section=name):
                self.assertEqual(ac.build_report(loaded, 12, sections=[name]), before)

    def test_each_rule_on_the_fixture(self):
        _, loaded = rule_tree(self)
        lines = scorecard_lines(ac.build_report(loaded, 12, sections=["rule-scorecard"]))
        self.assertEqual(len(lines), 6, lines)
        # 13 subagents: the builder's 486k of 633k is the top 10%; it peaked at 205k and took 4 turns
        # after its nudge; the reviewer's quoted hook text in a tool result is no nudge
        self.assertEqual(lines["one job per agent, budget tiers"],
                         "top 10% of subagents' share of subagent spend: 76.8% (past 150k 1, past 200k 1, "
                         "median 4 turns after the hand-back nudge, 1 nudged); baseline 45%: not holding")
        # the builder was prompted again after handing back; the reviewer's hook feedback is no prompt
        self.assertEqual(lines["a fresh agent per round"],
                         "subagents prompted again after their hand-back: 1 of 3 that handed back; no baseline")
        self.assertEqual(lines["calls requested together"],
                         "single-tool-call share of subagent turns: 89.5% (batchable 5.3%); baseline 70%: not holding")
        self.assertEqual(lines["the slow check goes to a runner"],
                         "cold turns after a Bash call, share of subagent spend: 31.6%; baseline 7.5%: not holding")
        # the Opus builder under a Sonnet session is above; the Opus agent with no parent in the window is
        # not judged, so 12 of the 13
        self.assertEqual(lines["the model ceiling"],
                         "subagents above their parent session's model: 1 of 12, 76.8% of subagent spend; "
                         "target 0: not holding")
        self.assertEqual(lines["the reviewer pays"],
                         "reviewer verdicts from the hand-back report: fail 1, pass 1 of 2; no baseline")
        self.assertIn("not scored: two review rounds at most", section_body(
            ac.build_report(loaded, 12, sections=["rule-scorecard"]), "Rule scorecard"))

    def test_load_context_keeps_the_nudge_turn_the_handback_and_a_second_prompt(self):
        _, loaded = rule_tree(self)
        by_agent = {l.ctx.agent_id: l.ctx for l in loaded}
        self.assertEqual(by_agent["b1"].hand_back_nudge_turn, 1)
        self.assertEqual(by_agent["b1"].handback_report, "Done: one commit.")
        self.assertTrue(by_agent["b1"].prompted_after_handback)
        self.assertIsNone(by_agent["r1"].hand_back_nudge_turn)
        self.assertFalse(by_agent["r1"].prompted_after_handback)
        self.assertIsNone(by_agent["k0"].handback_report)

    def test_a_subagent_on_its_parents_family_or_below_is_within_the_ceiling(self):
        _, loaded = rule_tree(self)
        judged, above = ac.above_ceiling(loaded)
        self.assertEqual([l.ctx.agent_id for l in above], ["b1"])
        self.assertIn("r1", [l.ctx.agent_id for l in judged])   # Sonnet under Sonnet
        self.assertNotIn("o1", [l.ctx.agent_id for l in judged])  # no parent in the window

    def test_reviewer_verdicts(self):
        for report, verdict in (("Verdict: FAIL. One blocker before merge.", "fail"),
                                ("Not merge-ready: two findings.", "fail"),
                                ("Merge-ready after the named fix.", "pass after fixes"),
                                ("Pass with lows: one naming nit.", "pass with lows"),
                                ("Verdict: pass. Merge-ready.", "pass"),
                                ("I looked at the diff.", "unclassified"),
                                (None, "unclassified")):
            with self.subTest(report=report):
                self.assertEqual(ac.reviewer_verdict(report), verdict)

    def test_negative_gate_text_and_other_verdict_wordings(self):
        notes = "cleanup note. " * 90   # over 1,200 characters
        for report, verdict in (
                ("Verdict: merge-ready. Gate: name the test; it should fail before the fix.", "pass"),
                ("Merge-ready after the named fixes. Finding 1 needs a one-line fix", "pass after fixes"),
                ("Fix first: the hook swallows errors", "fail"),
                ("Merge after fixes.", "pass after fixes"),
                ("Publish after fixes.", "pass after fixes"),
                (notes + "\nVerdict: not merge-ready, fix needed before merge.", "fail"),
                (notes + "\n**Verdict** - merge after the named fixes\n" + notes, "pass after fixes"),
                ("Verdict: fail\nnotes: the pass here is a pass on lint only", "fail"),
                ("It should fail before the fix.", "unclassified")):
            with self.subTest(report=report[-60:]):
                self.assertEqual(ac.reviewer_verdict(report), verdict)

    def test_a_subagent_is_judged_against_the_model_its_parent_used_at_its_first_turn(self):
        # (parent model before, after the subagent's first turn; the subagent's model; above?)
        for before, after, own, expect_above in (
                ("claude-sonnet-5", "claude-opus-5", "claude-sonnet-5", False),
                ("claude-opus-5", "claude-sonnet-5", "claude-opus-5", False),   # a last-turn read says above
                ("claude-sonnet-5", "claude-opus-5", "claude-opus-5", True)):   # a last-turn read says not
            with self.subTest(before=before, after=after, own=own):
                parent = typed_loaded("main", "main", [1000, 1000], "m")
                sub = typed_loaded("subagent", "builder", [1000], "s")
                parent.ctx.turns[0]["model"] = before
                parent.ctx.turns[1]["model"] = after
                parent.ctx.turns[1]["ts"] = BASE + timedelta(seconds=30)
                sub.ctx.turns[0]["ts"] = BASE + timedelta(seconds=10)
                sub.ctx.turns[0]["model"] = own
                judged, above = ac.above_ceiling([parent, sub])
                self.assertEqual(len(judged), 1)
                self.assertEqual(len(above), 1 if expect_above else 0)

    def test_judgement_words(self):
        self.assertEqual(ac.judgement(30.0, 45.0), "holds")
        self.assertEqual(ac.judgement(45.0, 45.0), "holds")
        self.assertEqual(ac.judgement(45.1, 45.0), "not holding")
        self.assertEqual(ac.judgement(None, 45.0), "n/a")

    def test_an_even_spread_holds_against_the_concentration_baseline(self):
        loaded = [typed_loaded("subagent", "mechanic", [1000], f"s{i}") for i in range(10)]
        lines = scorecard_lines(ac.build_report(loaded, 12, sections=["rule-scorecard"]))
        self.assertTrue(lines["one job per agent, budget tiers"].startswith(
            "top 10% of subagents' share of subagent spend: 10.0% "), lines)
        self.assertTrue(lines["one job per agent, budget tiers"].endswith("baseline 45%: holds"))

    def test_a_window_without_subagents_says_none_in_this_window(self):
        loaded = [typed_loaded("main", "main", [1000, 2000], "m")]
        lines = scorecard_lines(ac.build_report(loaded, 12, sections=["rule-scorecard"]))
        self.assertEqual(lines, {})
        report = ac.build_report(loaded, 12, sections=["rule-scorecard"])
        self.assertEqual(section_body(report, "Rule scorecard").splitlines()[0].strip(), "none in this window")

    def test_in_the_default_report_just_before_largest_contexts(self):
        _, loaded = rule_tree(self)
        headings = [l for l in ac.build_report(loaded, 12).splitlines() if l.startswith("=== ")]
        self.assertEqual(headings[-2], "=== Rule scorecard ===")
        self.assertTrue(headings[-1].startswith("=== Largest contexts"))

    def test_asked_for_alone_with_sections(self):
        fx, _ = rule_tree(self)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(["--projects", fx.root, "--since", ts_str(BASE - timedelta(hours=1)),
                     "--until", ts_str(BASE + timedelta(hours=1)), "--sections", "rule-scorecard"])
        output = buf.getvalue()
        self.assertEqual([l for l in output.splitlines() if l.startswith("=== ")], ["=== Rule scorecard ==="])
        self.assertEqual(len(scorecard_lines(output)), 6)


if __name__ == "__main__":
    unittest.main()


class VerdictLabelFormTests(unittest.TestCase):
    def test_a_dash_after_the_verdict_label_still_classifies(self):
        cases = (("Verdict - fail", "fail"), ("**Verdict** \u2014 fail: the hook swallows errors", "fail"),
                 ("Verdict \u2014 pass", "pass"), ("Verdict: pass with low-severity items only", "pass with lows"))
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(ac.reviewer_verdict(text), want)

    def test_not_ready_in_the_body_is_not_a_failing_verdict(self):
        report = "Final verdict: merge-ready.\nThe Windows path is not ready yet; file it."
        self.assertEqual(ac.reviewer_verdict(report), "pass")
        self.assertEqual(ac.reviewer_verdict("Verdict: not ready to merge."), "fail")
