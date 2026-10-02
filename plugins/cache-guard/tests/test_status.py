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


if __name__ == "__main__":
    unittest.main()
