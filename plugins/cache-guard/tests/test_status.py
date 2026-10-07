import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
STATUS = os.path.abspath(os.path.join(HOOKS, "status.py"))


def run(request, **env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CACHE_GUARD_")}
    done = subprocess.run([sys.executable, STATUS], input=json.dumps(request), capture_output=True,
                          text=True, env=dict(clean, **env), check=True)
    return json.loads(done.stdout)


class StatusTest(unittest.TestCase):
    def test_reads_the_last_main_turn_and_prices_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            usage = {"input_tokens": 0, "cache_read_input_tokens": 190_000, "cache_creation_input_tokens": 10_000,
                     "cache_creation": {"ephemeral_1h_input_tokens": 10_000}}
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "timestamp": "2026-10-01T12:00:00Z",
                                    "message": {"model": "claude-opus-5", "usage": usage}}) + "\n")
                f.write(json.dumps({"type": "assistant", "isSidechain": True, "timestamp": "2026-10-01T12:05:00Z",
                                    "message": {"model": "claude-haiku-4-5", "usage": usage}}) + "\n")
            out = run({"transcript_path": path, "agents": [
                {"model": "claude-haiku-4-5", "input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                {"model": "mystery", "input_tokens": 5},
            ]})
        self.assertEqual(out["last_turn_at"], 1_790_856_000_000)  # 2026-10-01T12:00:00Z, not the sidechain's
        self.assertEqual(out["lifetime_s"], 3600)
        self.assertEqual(out["context_tokens"], 200_000)
        self.assertAlmostEqual(out["cold_usd"], 2.0)   # 200k at $5 x 2 for the one-hour write
        self.assertAlmostEqual(out["warm_usd"], 0.1)   # 200k at $0.50 cache read
        self.assertAlmostEqual(out["agents_usd"], 1.0)  # 1M Haiku input at $1
        self.assertEqual(out["agents_unpriced"], 1)
        self.assertFalse(out["disabled"])

    def test_no_transcript_is_no_reading(self):
        out = run({"transcript_path": "/nonexistent/t.jsonl"}, CACHE_GUARD_DISABLE="1")
        self.assertIsNone(out["last_turn_at"])
        self.assertIsNone(out["agents_usd"])
        self.assertTrue(out["disabled"])


SESSION = "0b6f2c1e-4a7d-4c3b-9e21-5f8a7d6c4b3a"
TURN = {"type": "assistant", "timestamp": "2026-10-01T12:00:00Z",
        "message": {"model": "claude-opus-5", "usage": {"input_tokens": 0, "cache_read_input_tokens": 90_000,
                                                          "cache_creation_input_tokens": 10_000}}}


class TranscriptLookupTest(unittest.TestCase):
    """Without a transcript_path, the transcript is the one projects/*/<session_id>.jsonl in the config dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-status-test-")
        self.addCleanup(self.tmp.cleanup)
        self.projects = os.path.join(self.tmp.name, "projects")

    def write(self, *parts):
        path = os.path.join(self.projects, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(TURN) + "\n")
        return path

    def lookup(self, session_id):
        return run({"session_id": session_id}, CLAUDE_CONFIG_DIR=self.tmp.name)

    def test_the_session_id_finds_its_transcript_under_the_config_dir(self):
        self.write("-work-project", SESSION + ".jsonl")
        out = self.lookup(SESSION)
        self.assertEqual(out["last_turn_at"], 1_790_856_000_000)
        self.assertEqual(out["context_tokens"], 100_000)

    def test_an_id_that_is_not_a_session_id_finds_nothing(self):
        # Each of these would match a file below if it reached the pattern unchecked.
        self.write("x.jsonl")                  # projects/*/../x.jsonl
        self.write("-work-project", ".jsonl")  # projects/*/.jsonl, an empty id
        self.write("-work-project", "*.jsonl")
        for bad in ("../x", "", "*", None, SESSION + "/../../x"):
            with self.subTest(session_id=bad):
                self.assertIsNone(self.lookup(bad)["last_turn_at"])

    def test_the_same_id_in_two_projects_is_no_transcript_rather_than_a_guess(self):
        self.write("-work-one", SESSION + ".jsonl")
        self.write("-work-two", SESSION + ".jsonl")
        self.assertIsNone(self.lookup(SESSION)["last_turn_at"])

    def test_a_transcript_path_given_is_used_and_never_looked_up(self):
        self.write("-work-project", SESSION + ".jsonl")
        out = run({"transcript_path": os.path.join(self.tmp.name, "missing.jsonl"), "session_id": SESSION},
                  CLAUDE_CONFIG_DIR=self.tmp.name)
        self.assertIsNone(out["last_turn_at"])


if __name__ == "__main__":
    unittest.main()
