"""End-to-end tests of scripts/check.sh against a throwaway repository of stub suites."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SUITES = [
    "plugins/delegate/tests",
    "plugins/delegate/skills/agent-cost/tests",
    "plugins/cache-guard/tests",
]
PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
SKIPPING = (
    "import unittest\nclass T(unittest.TestCase):\n"
    "    def test_a(self):\n        pass\n"
    "    @unittest.skip('x')\n    def test_b(self):\n        pass\n"
    "    @unittest.expectedFailure\n    def test_c(self):\n        self.fail()\n"
    "    @unittest.expectedFailure\n    def test_d(self):\n        self.fail()\n"
)
EMPTY = "import unittest\n"
FAKE_CLAUDE = """#!/bin/sh
if [ "$2" = test ]; then printf '%s\\n' "$FAKE_MOD_SUMMARY"; fi
exit 0
"""
COLOURED = "\x1b[32m 3 pass\x1b[0m\n\x1b[2m 0 fail\x1b[0m"


class CheckShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        with open(os.path.join(ROOT, "scripts/check.sh"), encoding="utf-8") as f:
            self.write("scripts/check.sh", f.read())
        self.write(".claude-plugin/marketplace.json", '{"plugins": [{"name": "mod", "version": "1"}]}')
        self.bin = os.path.join(self.tmp, "bin")
        self.write("../bin/claude", FAKE_CLAUDE)
        os.chmod(os.path.join(self.bin, "claude"), 0o755)
        for suite in SUITES:
            self.write(suite + "/test_stub.py", PASSING)

    def write(self, rel, text):
        path = os.path.normpath(os.path.join(self.repo, rel))
        self.assertTrue(path.startswith(self.tmp + os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def run_check(self, *args, summary=""):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            HOME=self.tmp,
            PATH=os.pathsep.join([self.bin, os.path.dirname(sys.executable), "/usr/bin", "/bin"]),
            FAKE_MOD_SUMMARY=summary,
        )
        env.pop("PRIVATE_TERMS", None)
        return subprocess.run(
            ["sh", "scripts/check.sh", *args], cwd=self.repo, env=env,
            capture_output=True, text=True, check=False,
        )

    def test_passing_stubs_are_green_in_both_modes(self):
        for args in ([], ["--verbose"]):
            self.assertEqual(self.run_check(*args).returncode, 0, args)

    def test_zero_test_suite_fails_in_verbose_mode(self):
        self.write(SUITES[0] + "/test_stub.py", EMPTY)
        for args in ([], ["--verbose"]):
            self.assertNotEqual(self.run_check(*args).returncode, 0, args)

    def test_failing_suite_fails_in_verbose_mode_and_shows_output(self):
        self.write(SUITES[0] + "/test_stub.py", PASSING.replace("pass\n", "self.fail('boom')\n", 1))
        result = self.run_check("--verbose")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("boom", result.stdout + result.stderr)

    def test_skips_are_counted_beside_expected_failures(self):
        self.write(SUITES[0] + "/test_stub.py", SKIPPING)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 skipped", result.stdout)

    def _with_mod_tests(self):
        self.write("plugins/mod/.claude-plugin/plugin.json", '{"name": "mod", "version": "1"}')
        self.write("plugins/mod/a.test.ts", "")

    def test_coloured_mod_summary_reads_as_a_pass(self):
        self._with_mod_tests()
        for args in ([], ["--verbose"]):
            result = self.run_check(*args, summary=COLOURED)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mod 3 pass", self.run_check(summary=COLOURED).stdout)

    def test_failing_mod_summary_is_red_even_when_coloured(self):
        self._with_mod_tests()
        for args in ([], ["--verbose"]):
            result = self.run_check(*args, summary="\x1b[32m 3 pass\x1b[0m\n\x1b[31m 1 fail\x1b[0m")
            self.assertNotEqual(result.returncode, 0, args)


if __name__ == "__main__":
    unittest.main()
