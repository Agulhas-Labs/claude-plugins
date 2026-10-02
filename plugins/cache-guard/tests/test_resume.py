"""resume.py: the JSON the resume band is drawn from, on a fixture handoff and a throwaway repository."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))

import resume  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "resume.py")


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class ResumeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-resume-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.repo = os.path.join(self.root, "repo")
        os.mkdir(self.repo)

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.repo, env=clean_env(), check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

    def make_repo(self):
        self.git("init", "-q", "-b", "main")
        # The repository's own directory must be inside the temp directory before anything is written.
        git_dir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=self.repo, env=clean_env(),
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(os.path.realpath(git_dir).startswith(self.root + os.sep))
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "one")

    def write_handoff(self, name, text):
        directory = os.path.join(self.repo, ".claude", "handoffs")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def run_script(self):
        env = clean_env()
        env.pop("CACHE_GUARD_HANDOFF_DIR", None)
        done = subprocess.run([sys.executable, SCRIPT, self.repo], env=env, capture_output=True, text=True, check=True)
        return json.loads(done.stdout)

    def test_prints_the_handoff_and_the_git_facts(self):
        self.make_repo()
        path = self.write_handoff(
            "2026-10-01.md",
            "# Handoff — 2026-10-01 10:00 UTC\n\nSummary: Wire the resume band into the plugin\n\n"
            "## What was asked\n\n1. First thing\n",
        )
        self.git("checkout", "-q", "-b", "feat/x")
        with open(os.path.join(self.repo, "b.txt"), "w") as f:
            f.write("b\n")
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("more\n")

        facts = self.run_script()

        self.assertEqual(os.path.realpath(facts["path"]), os.path.realpath(path))
        self.assertEqual(facts["summary"], "Wire the resume band into the plugin")
        self.assertEqual(facts["branch"], "feat/x")
        self.assertIsNone(facts["ahead"])  # no upstream: unknown, not zero
        self.assertEqual(facts["uncommitted"], 3)  # b.txt, a.txt, and the handoffs directory
        self.assertRegex(facts["writtenAt"], r"^\d{4}-\d\d-\d\dT.*\+00:00$")

    def test_counts_commits_ahead_of_the_upstream(self):
        self.make_repo()
        self.write_handoff("h.md", "# Handoff\n")
        remote = os.path.join(self.root, "remote.git")
        self.git("init", "-q", "--bare", remote, cwd=self.root)
        self.git("remote", "add", "origin", remote)
        self.git("push", "-q", "-u", "origin", "main")
        for n in ("two", "three"):
            with open(os.path.join(self.repo, f"{n}.txt"), "w") as f:
                f.write(n)
            self.git("add", f"{n}.txt")
            self.git("commit", "-q", "-m", n)

        self.assertEqual(self.run_script()["ahead"], 2)

    def test_pending_summary_falls_back_to_the_first_request(self):
        self.make_repo()
        self.write_handoff(
            "h.md",
            "# Handoff\n\nSummary: being written\n\n## What was asked\n\n1. Fix the login redirect\n2. Second\n",
        )

        self.assertEqual(self.run_script()["summary"], "Fix the login redirect")

    def test_prints_an_empty_object_without_a_handoff(self):
        self.make_repo()

        self.assertEqual(self.run_script(), {})

    def test_outside_a_repository_the_git_facts_are_null(self):
        self.write_handoff("h.md", "# Handoff\n\nSummary: Something\n")

        facts = self.run_script()

        self.assertEqual(facts["summary"], "Something")
        self.assertEqual((facts["branch"], facts["ahead"], facts["uncommitted"]), (None, None, None))

    def test_summary_is_clipped_to_one_line(self):
        self.assertLessEqual(len(resume.summary_line("# H\n\nSummary: " + "word " * 100)), resume.SUMMARY_CAP)


if __name__ == "__main__":
    unittest.main()
