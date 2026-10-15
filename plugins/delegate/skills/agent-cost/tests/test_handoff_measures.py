"""Tests for the maker measures that read a command or a handoff file: edits made through Bash, a handoff kept
in a file, and the gates line of the rule scorecard. Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests"""
import os
import shutil
import tempfile
import unittest
from datetime import timedelta

from test_agentcost import (BASE, FixtureRoot, ac, assistant, ts_str, tool_use_block, usage, user_text)


def bash(command):
    return {"command": command}


class EditsFilesTests(unittest.TestCase):
    def test_commands_that_edit_a_file(self):
        for cmd in ["sed -i 's/a/b/' f.py", "sed -i.bak 's/a/b/' f.py", "sed -i '' 's/a/b/' f.py",
                    "perl -pi -e 's/a/b/' f.py", "cd /repo && sed -n 1p x && sed -i 's/a/b/' f",
                    "git apply fix.patch", "cd r && git apply -p0 fix.patch", "patch -p1 < fix.patch",
                    "echo hi | tee out.txt", "tee -a notes.md < x", "echo x > out.txt", "echo x >> out.txt",
                    "python3 - <<'EOF'\nopen('f.py', 'w').write('x')\nEOF",
                    "python3 -c \"open('f', 'a').write('x')\"",
                    "python3 - <<'EOF'\nfrom pathlib import Path\nPath('f').write_text('x')\nEOF",
                    "python -u - <<EOF\nPath('f').write_bytes(b'x')\nEOF"]:
            self.assertTrue(ac.edits_files(cmd), cmd)

    def test_commands_that_do_not_edit(self):
        for cmd in ["sed -n 1,5p f.py", "cat f.py", "grep -n foo f.py", "git diff", "git apply --check fix.patch",
                    "git apply --stat fix.patch", "python3 - <<'EOF'\nprint(open('f').read())\nEOF",
                    "python3 - <<'EOF'\nprint(open('f', 'r').read())\nif 1 > 0: pass\nEOF",
                    "make test 2>&1", "make test > /dev/null", "echo x > /tmp/out", "echo x > /private/tmp/out",
                    "echo x > /var/folders/ab/out", "echo x > $TMPDIR/out", "echo x | tee /tmp/out", "ls &> /dev/null",
                    "echo 'a > b'", ""]:
            self.assertFalse(ac.edits_files(cmd), cmd)

    def test_a_bash_edit_sets_the_first_edit_turn(self):
        fx = FixtureRoot(self)
        t = lambda n: ts_str(BASE + timedelta(seconds=n))
        path = fx.subagent(entries=[
            user_text(t(0), "go"),
            assistant("m1", t(1), usage(), [tool_use_block("t1", "Bash", bash("grep -n x f.py"))]),
            assistant("m2", t(2), usage(), [tool_use_block("t2", "Bash", bash("sed -i 's/a/b/' f.py"))]),
        ])
        ctx = ac.load_context("subagent", path, "p", "s", "a1")
        self.assertEqual(ctx.first_edit_turn, 1)

    def test_reading_commands_leave_the_first_edit_turn_unset(self):
        fx = FixtureRoot(self)
        t = lambda n: ts_str(BASE + timedelta(seconds=n))
        path = fx.subagent(entries=[
            user_text(t(0), "go"),
            assistant("m1", t(1), usage(), [tool_use_block("t1", "Bash", bash("git diff"))]),
        ])
        self.assertIsNone(ac.load_context("subagent", path, "p", "s", "a1").first_edit_turn)


class HandoffFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="handoff-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def context(self, prompt, agent_type="builder-lite"):
        fx = FixtureRoot(self)
        path = fx.subagent(agent_type=agent_type, entries=[
            user_text(ts_str(BASE), prompt),
            assistant("m1", ts_str(BASE + timedelta(seconds=1)), usage()),
        ])
        return ac.load_context("subagent", path, "p", "s", "a1")

    def test_handoff_is_the_prompt_followed_by_the_named_files(self):
        a = self.write("a.md", "G1 tests pass - CHECK: run them")
        b = self.write("b.txt", "second file body")
        ctx = self.context(f"Read {a} and {b}. Then follow them.")
        self.assertTrue(ctx.handoff.startswith(f"Read {a}"))
        self.assertIn("G1 tests pass", ctx.handoff)
        self.assertIn("second file body", ctx.handoff)
        self.assertEqual(ctx.handoff_missing, 0)

    def test_missing_files_are_counted(self):
        a = self.write("a.md", "body")
        ctx = self.context(f"Read {a} and {self.dir}/gone.md and {self.dir}/gone2.txt")
        self.assertEqual(ctx.handoff_missing, 2)
        self.assertIn("body", ctx.handoff)

    def test_only_md_and_txt_and_five_files_are_read(self):
        files = [self.write(f"f{i}.md", f"body{i}") for i in range(6)]
        other = self.write("code.py", "not read")
        ctx = self.context("Read " + " ".join(files) + f" and {other}")
        self.assertIn("body4", ctx.handoff)
        self.assertNotIn("body5", ctx.handoff)
        self.assertNotIn("not read", ctx.handoff)

    def test_a_file_over_256_kb_is_not_read(self):
        big = self.write("big.md", "x" * (256 * 1024 + 1))
        self.assertEqual(len(self.context(f"Read {big}").handoff), len(f"Read {big}"))

    def test_a_relative_path_is_not_a_named_file(self):
        self.assertEqual(self.context("Read docs/plan.md first").handoff_missing, 0)

    def test_main_sessions_get_no_handoff(self):
        fx = FixtureRoot(self)
        path = fx.main_session(entries=[user_text(ts_str(BASE), f"Read {self.dir}/x.md"),
                                        assistant("m1", ts_str(BASE + timedelta(seconds=1)), usage())])
        ctx = ac.load_context("main", path, "p", "s", None)
        self.assertEqual((ctx.handoff, ctx.handoff_missing), ("", 0))

    def test_branches_in_a_named_file_link_the_rungs(self):
        plan = self.write("plan.md", "Work on `fix/thing` please")
        fx = FixtureRoot(self)
        t = lambda n: ts_str(BASE + timedelta(seconds=n))
        fx.subagent(agent="m", agent_type="builder-lite", entries=[
            user_text(t(0), f"Read {plan}"), assistant("m1", t(1), usage())])
        fx.subagent(agent="r", agent_type="reviewer", entries=[
            user_text(t(5), "Review `fix/thing`"), assistant("r1", t(6), usage())])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        links = ac.rung_links(loaded)
        self.assertEqual(len(links), 1)
        self.assertIsNotNone(links[0][2])


class GateScorecardTests(unittest.TestCase):
    def scorecard(self, prompts):
        fx = FixtureRoot(self)
        for i, prompt in enumerate(prompts):
            fx.subagent(agent=f"a{i}", agent_type="builder-lite", entries=[
                user_text(ts_str(BASE), prompt), assistant(f"m{i}", ts_str(BASE + timedelta(seconds=1)), usage())])
        loaded = ac.load_all(fx.root, None, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        out = []
        ac.section_rule_scorecard(out, loaded)
        return next((l for l in out if "maker handoffs carry gates" in l), None)

    def test_counts_makers_with_a_gate_line(self):
        d = tempfile.mkdtemp(prefix="gate-test-")
        self.addCleanup(shutil.rmtree, d)
        plan = os.path.join(d, "plan.md")
        with open(plan, "w") as f:
            f.write("G1 tests pass - CHECK: run them - EXPECT: OK")
        line = self.scorecard([f"Read {plan}", "G2 CHECK is not on one line\nCHECK: x", "no gates here"])
        self.assertIn("maker handoffs with a gate line: 1 of 3;", line)
        self.assertIn("no baseline", line)

    def test_reports_a_file_no_longer_on_disk(self):
        line = self.scorecard(["Read /nonexistent-dir-for-test/plan.md", "G1 ok - CHECK: x"])
        self.assertIn("1 of 2 (1 named a file no longer on disk)", line)


if __name__ == "__main__":
    unittest.main()
