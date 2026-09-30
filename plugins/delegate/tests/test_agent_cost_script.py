"""Tests for where the agent-cost report lives and how the skill reaches it.
Run: python3 -m unittest discover -s plugins/delegate/tests"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SKILL = os.path.join(PLUGIN, "skills", "agent-cost")
SCRIPT = os.path.join(SKILL, "scripts", "agent_cost.py")


def tracked_modes():
    """Each file of the plugin with the mode git records for it, read from the index so that a
    checkout on a filesystem without executable bits is judged the same way."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "ls-files", "-s", "--", "."], cwd=PLUGIN, env=env,
                         capture_output=True, encoding="utf-8", check=True)
    return [(line.split()[0], line.split("\t", 1)[1]) for line in out.stdout.splitlines()]


class AgentCostScriptTests(unittest.TestCase):
    def test_the_plugin_has_no_bin_directory(self):
        # A top-level bin/ puts its files on the Bash tool's PATH, and the plugin directory holds
        # every version that has one for a reviewer.
        self.assertFalse(os.path.exists(os.path.join(PLUGIN, "bin")))

    def test_no_file_in_the_plugin_is_executable(self):
        modes = tracked_modes()
        self.assertTrue(modes, "git listed no files for the plugin")
        self.assertEqual([path for mode, path in modes if mode != "100644"], [])

    def test_the_skill_names_the_script_by_its_own_folder(self):
        with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        named = re.findall(r'"\$\{CLAUDE_SKILL_DIR\}/([^"]+)"', skill)
        self.assertEqual(sorted(set(named)), ["scripts/agent_cost.py"])
        self.assertTrue(os.path.isfile(os.path.join(SKILL, *named[0].split("/"))))
        self.assertNotIn("bin/agent-cost", skill)

    def test_it_runs_the_report_with_its_arguments(self):
        projects = tempfile.mkdtemp(prefix="agent-cost-script-")
        self.addCleanup(shutil.rmtree, projects, True)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        os.makedirs(os.path.join(projects, "-proj"))
        with open(os.path.join(projects, "-proj", "s1.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": now, "version": "2.1.272", "message": {
                "id": "m1", "model": "claude-sonnet-5", "content": [],
                "usage": {"input_tokens": 2, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0, "output_tokens": 10}}}) + "\n")
        home = tempfile.mkdtemp(prefix="agent-cost-home-")
        self.addCleanup(shutil.rmtree, home, True)
        # An empty HOME, so the report can only have read the fixture: its one turn is the proof.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["HOME"] = home
        out = subprocess.run([sys.executable, SCRIPT, "--projects", projects, "--sections", "totals"],
                             capture_output=True, encoding="utf-8", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("=== Totals", out.stdout)
        self.assertRegex(out.stdout, r"turns\s+1\s")

    def test_its_usage_line_keeps_the_reports_name(self):
        out = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("usage: agent-cost", out.stdout)


if __name__ == "__main__":
    unittest.main()
