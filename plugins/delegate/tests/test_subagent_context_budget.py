"""Tests for subagent-context-budget.py. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "subagent-context-budget.py")
spec = importlib.util.spec_from_file_location("budget", HOOK)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


def assistant(mid, context, *tool_ids, timestamp=None):
    usage = {"input_tokens": 2, "cache_read_input_tokens": context - 2, "cache_creation_input_tokens": 0}
    content = [{"type": "tool_use", "id": t, "name": "Bash", "input": {}} for t in tool_ids]
    entry = {"type": "assistant", "message": {"id": mid, "usage": usage, "content": content}}
    if timestamp is not None:
        entry["timestamp"] = timestamp
    return entry


# Transcripts write UTC with milliseconds and a trailing "Z".
ISSUED = "2026-01-01T12:00:00.123Z"
ISSUED_AT = 1767268800.123  # ISSUED as epoch seconds


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="budget-test-")
        self.addCleanup(shutil.rmtree, self.root)
        self.parent = os.path.join(self.root, "session.jsonl")
        self.agent_file = os.path.join(self.root, "session", "subagents", "agent-a1.jsonl")
        os.makedirs(os.path.dirname(self.agent_file))

    def write(self, *entries):
        with open(self.agent_file, "w") as f:
            for e in entries:
                # A message with several blocks is written as one line per block, each repeating the usage.
                f.write(json.dumps(e) + "\n")

    def advise(self, tool_id, agent="a1", path=None, env=None, now=None):
        payload = {"transcript_path": path or self.parent, "agent_id": agent, "tool_use_id": tool_id}
        given = {k: v for k, v in (("env", env), ("now", now)) if v is not None}
        return budget.advice(payload, **given)

    def test_the_freeze_tier_fires_at_120k_and_not_before(self):
        self.write(assistant("m1", 110_000, "t1"), assistant("m2", 119_000, "t2"), assistant("m3", 121_000, "t3"))
        self.assertIsNone(self.advise("t2"))
        said = self.advise("t3")
        self.assertIn("121k", said)
        self.assertIn("Scope freeze", said)
        # The point of this tier is knowing what the next one will ask for.
        self.assertIn("150k", said)

    def test_the_hand_back_tier_fires_at_150k_and_not_before(self):
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 149_000, "t2"), assistant("m3", 151_000, "t3"))
        self.assertIsNone(self.advise("t2"))
        said = self.advise("t3")
        self.assertIn("151k", said)
        self.assertIn("Finish only the item in hand", said)

    def test_the_stop_tier_fires_at_200k_and_not_before(self):
        self.write(assistant("m1", 155_000, "t1"), assistant("m2", 199_000, "t2"), assistant("m3", 201_000, "t3"))
        self.assertIsNone(self.advise("t2"))
        said = self.advise("t3")
        self.assertIn("201k", said)
        self.assertIn("Stop here, even mid-item", said)

    def test_each_of_the_first_two_tiers_fires_once(self):
        self.write(
            assistant("m1", 121_000, "t1"),  # freeze
            assistant("m2", 135_000, "t2"),
            assistant("m3", 149_000, "t3"),
            assistant("m4", 151_000, "t4"),  # hand back
            assistant("m5", 175_000, "t5"),
            assistant("m6", 199_000, "t6"),
        )
        self.assertIn("Scope freeze", self.advise("t1"))
        for still_in_tier in ("t2", "t3", "t5", "t6"):
            self.assertIsNone(self.advise(still_in_tier))
        self.assertIn("Finish only the item in hand", self.advise("t4"))

    def test_the_stop_tier_repeats_every_further_50k(self):
        self.write(
            assistant("m1", 201_000, "t1"),
            assistant("m2", 230_000, "t2"),
            assistant("m3", 251_000, "t3"),
            assistant("m4", 280_000, "t4"),
            assistant("m5", 301_000, "t5"),
        )
        for crossing, size in (("t1", "201k"), ("t3", "251k"), ("t5", "301k")):
            said = self.advise(crossing)
            self.assertIn(size, said)
            self.assertIn("Stop here, even mid-item", said)
        self.assertIsNone(self.advise("t2"))
        self.assertIsNone(self.advise("t4"))

    def test_a_turn_that_jumps_past_a_tier_gets_the_tier_it_landed_in(self):
        # One turn can add 100k. The agent is told to stop, not to freeze scope it is already past.
        self.write(assistant("m1", 100_000, "t1"), assistant("m2", 210_000, "t2"))
        said = self.advise("t2")
        self.assertIn("Stop here, even mid-item", said)
        self.assertNotIn("Scope freeze", said)

    def test_parallel_calls_in_one_turn_speak_once(self):
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 151_000, "t2", "t3"))
        self.assertIsNotNone(self.advise("t2"))
        self.assertIsNone(self.advise("t3"))

    def test_a_turn_split_across_lines_is_one_turn(self):
        m2a, m2b = assistant("m2", 151_000, "t2"), assistant("m2", 151_000, "t3")
        self.write(assistant("m1", 125_000, "t1"), m2a, m2b)
        self.assertIsNotNone(self.advise("t2"))
        self.assertIsNone(self.advise("t3"))

    def test_main_session_is_never_told(self):
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 151_000, "t2"))
        self.assertIsNone(budget.advice({"transcript_path": self.parent, "tool_use_id": "t2"}))

    def test_a_payload_naming_the_agent_transcript_itself_is_used_directly(self):
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 151_000, "t2"))
        self.assertIsNotNone(self.advise("t2", path=self.agent_file))

    def test_missing_transcript_or_unknown_call_is_silent(self):
        self.assertIsNone(self.advise("t2", agent="nobody"))
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 151_000, "t2"))
        self.assertIsNone(self.advise("t9"))

    def test_a_call_that_outlived_the_cache_is_told_with_its_minutes_and_context(self):
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp=ISSUED))
        said = self.advise("t2", now=ISSUED_AT + 9 * 60 + 30)
        self.assertIn("Cache expired", said)
        # The time since the turn that issued it, not the call's own run: parallel calls share a turn.
        self.assertIn("returned 9 minutes after the turn that issued it", said)
        self.assertIn("114k", said)
        self.assertIn("report", said)
        self.assertNotIn("Context budget", said)

    def test_a_call_within_the_cache_lifetime_gets_no_line(self):
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp=ISSUED))
        self.assertIsNone(self.advise("t2", now=ISSUED_AT + 299))

    def test_the_cache_lifetime_override_is_honoured(self):
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp=ISSUED))
        longer = {"DELEGATE_SUBAGENT_CACHE_SECONDS": "3600"}
        self.assertIsNone(self.advise("t2", env=longer, now=ISSUED_AT + 9 * 60))
        shorter = {"DELEGATE_SUBAGENT_CACHE_SECONDS": "60"}
        self.assertIn("2 minutes", self.advise("t2", env=shorter, now=ISSUED_AT + 2 * 60 + 5))
        self.assertIn("returned 1 minute after", self.advise("t2", env=shorter, now=ISSUED_AT + 65))
        # Anything that isn't a positive integer means the default.
        for bad in ("soon", "0", "-5", ""):
            self.assertIsNone(self.advise("t2", env={"DELEGATE_SUBAGENT_CACHE_SECONDS": bad}, now=ISSUED_AT + 299))
            self.assertIsNotNone(self.advise("t2", env={"DELEGATE_SUBAGENT_CACHE_SECONDS": bad}, now=ISSUED_AT + 301))

    def test_an_overrun_that_crosses_a_tier_gets_both(self):
        self.write(assistant("m1", 110_000, "t1"), assistant("m2", 121_000, "t2", timestamp=ISSUED))
        said = self.advise("t2", now=ISSUED_AT + 12 * 60)
        self.assertIn("Scope freeze", said)
        self.assertIn("Cache expired", said)
        self.assertIn("12 minutes", said)
        self.assertLess(said.index("Scope freeze"), said.index("Cache expired"))

    def test_the_slow_one_of_parallel_calls_is_told(self):
        m2a = assistant("m2", 114_000, "t2", timestamp=ISSUED)
        m2b = assistant("m2", 114_000, "t3", timestamp="2026-01-01T12:00:00.456Z")
        self.write(assistant("m1", 90_000, "t1"), m2a, m2b)
        self.assertIn("Cache expired", self.advise("t3", now=ISSUED_AT + 10 * 60))

    def test_a_missing_or_unreadable_timestamp_gets_no_line_and_no_error(self):
        self.write(assistant("m1", 110_000, "t1"), assistant("m2", 121_000, "t2"))
        said = self.advise("t2", now=ISSUED_AT + 60 * 60)
        self.assertIn("Scope freeze", said)
        self.assertNotIn("Cache expired", said)
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp="yesterday"))
        self.assertIsNone(self.advise("t2", now=ISSUED_AT + 60 * 60))

    def test_the_hook_reads_the_override_from_its_environment(self):
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp=ISSUED))
        payload = json.dumps({"transcript_path": self.parent, "agent_id": "a1", "tool_use_id": "t2"})
        env = {k: v for k, v in os.environ.items() if k != "DELEGATE_SUBAGENT_CACHE_SECONDS"}
        told = subprocess.run([sys.executable, HOOK], input=payload.encode(), capture_output=True, env=env)
        self.assertIn(b"Cache expired", told.stdout)
        # An issued time long past, so only a lifetime longer than that keeps it quiet.
        env["DELEGATE_SUBAGENT_CACHE_SECONDS"] = str(10 ** 12)
        quiet = subprocess.run([sys.executable, HOOK], input=payload.encode(), capture_output=True, env=env)
        self.assertEqual(b"", quiet.stdout)

    def test_a_failed_call_is_told_like_a_successful_one_and_answers_its_own_event(self):
        # A command that exits non-zero or times out arrives as PostToolUseFailure, with the same
        # tool_use_id and transcript, and a long red suite outlives the cache like a green one.
        self.write(assistant("m1", 90_000, "t1"), assistant("m2", 114_000, "t2", timestamp=ISSUED))
        env = {k: v for k, v in os.environ.items() if k != "DELEGATE_SUBAGENT_CACHE_SECONDS"}
        for event in ("PostToolUse", "PostToolUseFailure"):
            payload = {"hook_event_name": event, "transcript_path": self.parent, "agent_id": "a1",
                       "tool_use_id": "t2", "error": "Exit code 1", "is_interrupt": False}
            told = subprocess.run([sys.executable, HOOK], input=json.dumps(payload).encode(), capture_output=True, env=env)
            answer = json.loads(told.stdout)["hookSpecificOutput"]
            self.assertEqual(answer["hookEventName"], event)
            self.assertIn("Cache expired", answer["additionalContext"])

    def test_a_non_ascii_utf8_payload_is_read_under_a_non_utf8_locale(self):
        # On native Windows Python, sys.stdin decodes with the locale code page rather than UTF-8.
        # PYTHONIOENCODING reproduces that here: force it to ascii and feed a payload with a non-ASCII
        # character. Without explicitly decoding stdin as UTF-8, json.load raises inside main()'s
        # bare except and the nudge never fires (silent success, no output).
        self.write(assistant("m1", 125_000, "t1"), assistant("m2", 151_000, "t2"))
        payload = json.dumps({
            "transcript_path": self.parent,
            "agent_id": "a1",
            "tool_use_id": "t2",
            "note": "café",
        }, ensure_ascii=False)
        result = subprocess.run(
            [sys.executable, HOOK],
            input=payload.encode("utf-8"),
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "ascii"},
        )
        self.assertIn(b"151k", result.stdout)


if __name__ == "__main__":
    unittest.main()
