"""Tests for the read-only upkeep checks. Run: python3 -m unittest discover -s plugins/upkeep/tests"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
CHECKS = os.path.join(SCRIPTS, "checks.py")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra)
    return env


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "config")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.config, "rules"))
        os.makedirs(os.path.join(self.project, ".claude", "rules"))

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def checks(self, name, *args):
        done = subprocess.run(
            [sys.executable, CHECKS, name, "--cwd", self.project, *args],
            capture_output=True, text=True, env=clean_env(CLAUDE_CONFIG_DIR=self.config, HOME=self.tmp),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def git(self, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.project, capture_output=True, text=True, env=clean_env(
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout


class SizeBudget(Sandbox):
    def test_flags_only_big_files_without_paths_or_an_index(self):
        big = "x" * 5000
        self.write(os.path.join(self.config, "rules", "plain.md"), big)
        self.write(os.path.join(self.config, "rules", "scoped.md"), "---\npaths:\n  - 'src/**'\n---\n" + big)
        self.write(os.path.join(self.config, "rules", "indexed.md"), "# T\nRead this when editing X.\n" + big)
        self.write(os.path.join(self.config, "rules", "small.md"), "tiny")
        self.write(os.path.join(self.project, "CLAUDE.md"), "---\npaths: ['a']\n---\n" + big)  # paths exempts rules only
        out = self.checks("size")
        self.assertIn("plain.md", out)
        self.assertIn("~1250 tokens", out)
        for name in ("scoped.md", "indexed.md", "small.md"):
            self.assertNotIn(name, out)
        self.assertIn("CLAUDE.md", out)
        self.assertNotIn("add `paths:`", out.split("CLAUDE.md")[1])

    def test_threshold_is_an_option(self):
        self.write(os.path.join(self.config, "rules", "mid.md"), "x" * 2000)
        self.assertIn("nothing over", self.checks("size"))
        self.assertIn("mid.md", self.checks("size", "--threshold", "1000"))


class Drift(Sandbox):
    def test_reports_difference_and_missing_source_and_stays_quiet_when_in_sync(self):
        src = os.path.join(self.tmp, "src.md")
        self.write(src, "same\n")
        self.write(os.path.join(self.config, "rules", "ok.md"), f"<!-- managed by {src} -->\nsame\n")
        self.assertIn("no stamped guide differs", self.checks("drift"))
        self.write(src, "changed\n")
        self.assertIn("differs from its source", self.checks("drift"))
        self.write(os.path.join(self.config, "rules", "gone.md"), "<!-- managed by /nowhere/x.md -->\n")
        self.assertIn("source named in its stamp is missing", self.checks("drift"))


class Hygiene(Sandbox):
    def setUp(self):
        super().setUp()
        os.makedirs(self.project, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        top = self.git("rev-parse", "--git-dir").strip()
        self.assertTrue(os.path.realpath(os.path.join(self.project, top)).startswith(self.tmp))
        self.write(os.path.join(self.project, "a.txt"), "a")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "a")

    def test_merged_branch_and_worktree_are_proposed_unmerged_are_not(self):
        wt = os.path.join(self.project, ".build", "done")
        self.git("worktree", "add", "-q", "-b", "done", wt)
        self.git("branch", "old-merged")
        self.git("checkout", "-q", "-b", "wip")
        self.write(os.path.join(self.project, "b.txt"), "b")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "b")
        self.git("checkout", "-q", "main")
        out = self.checks("hygiene")
        self.assertIn(f"worktree {wt} (branch done) is merged into main", out)
        self.assertIn("worktree remove", out)
        self.assertIn("local branch old-merged is merged", out)
        self.assertNotIn("branch wip", out)
        self.assertNotIn("local branch main", out)
        self.assertEqual(self.git("branch", "--list", "old-merged").strip(), "old-merged")  # reported, not deleted

    def test_branch_name_with_shell_metacharacters_is_quoted(self):
        self.git("branch", "x;$HOME`id`")
        out = self.checks("hygiene")
        self.assertIn("'x;$HOME`id`'", out)
        self.assertFalse(os.path.exists(os.path.join(self.project, "id")))

    def test_memory_naming_a_missing_file_is_reported(self):
        slug = self.project.replace("/", "-").replace(".", "-").replace("_", "-")
        self.write(os.path.join(self.config, "projects", slug, "memory", "m.md"), "see `a.txt` and `gone/file.py`\n")
        out = self.checks("hygiene")
        self.assertIn("gone/file.py", out)
        self.assertNotIn("a.txt,", out)
        self.assertNotIn(", a.txt", out)


if __name__ == "__main__":
    unittest.main()
