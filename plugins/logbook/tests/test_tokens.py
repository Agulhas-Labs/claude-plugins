"""Tests for the token totals: a subagent's, read from its transcript when it stops, and the main
session's, read from its own transcript at the end of each turn.

Run: python3 -m unittest discover -s plugins/logbook/tests
"""
import json
import os
import tempfile
import unittest

from test_board import NOW, event
import test_hooks  # noqa: E402,F401  (puts the hooks folder on the path)
import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402


def usage(message_id, **counts):
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": counts}})


class TranscriptTokens(unittest.TestCase):
    def read(self, *lines, raw=None):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n" if raw is None else raw)
            return board_hook.transcript_tokens(path)

    def test_every_kind_of_token_is_counted(self):
        line = usage("m1", input_tokens=10, cache_creation_input_tokens=200, cache_read_input_tokens=3000, output_tokens=4)
        self.assertEqual(self.read(line), 3214)

    def test_a_message_written_in_blocks_counts_once(self):
        self.assertEqual(self.read(usage("m1", input_tokens=5, output_tokens=1), usage("m1", input_tokens=5, output_tokens=9)), 14)

    def test_lines_that_are_not_usage_are_skipped(self):
        self.assertEqual(self.read("not json with \"usage\" in it", '{"type":"user"}', usage("m2", output_tokens=7)), 7)

    def test_a_missing_file_or_no_usage_is_none(self):
        self.assertIsNone(board_hook.transcript_tokens("/nonexistent/x.jsonl"))
        self.assertIsNone(board_hook.transcript_tokens(None))
        self.assertIsNone(self.read('{"type":"user"}'))


class Derived(unittest.TestCase):
    def state(self, *events):
        derivation = board.Derivation()
        for one in events:
            derivation.apply(one)
        return derivation.state([], {})

    def test_an_agent_and_the_session_carry_their_tokens(self):
        state = self.state(
            event("start", 0, session="s", title="t"),
            event("agent-start", 1, id="a1", type="builder"),
            event("agent-stop", 2, id="a1", message="done", tokens=4200),
            event("session-tokens", 3, tokens=900),
            event("session-tokens", 4, tokens=1500),
        )
        self.assertEqual(state["tokens"], 1500)
        self.assertEqual(state["agents"][0]["tokens"], 4200)

    def test_unread_tokens_stay_none(self):
        state = self.state(event("start", 0, session="s"), event("agent-start", 1, id="a1"), event("agent-stop", 2, id="a1", message=""))
        self.assertIsNone(state["tokens"])
        self.assertIsNone(state["agents"][0]["tokens"])

    def test_a_token_event_is_not_recorded_work(self):
        log = [event("turn-end", 1), event("session-tokens", 2, tokens=5)]
        self.assertFalse(board_hook.recorded_this_turn(log))


if __name__ == "__main__":
    unittest.main()
