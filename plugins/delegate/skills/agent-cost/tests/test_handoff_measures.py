"""Tests for the maker measures that read a command or a handoff file: edits made through Bash, a handoff kept
in a file, and the gates line of the rule scorecard. Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests"""
import importlib.machinery
import importlib.util
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
                    "echo hi | tee out.txt", "cd /tmp/w && make\ncd /repo\necho x > f",
                    "cd /tmp/w && if true; then cd /repo; fi; echo x > f", "cd /tmp/w; for d in a; do cd /repo; done; tee f", "tee -a notes.md < x", "echo x > out.txt", "echo x >> out.txt",
                    "python3 - <<'EOF'\nopen('f.py', 'w').write('x')\nEOF",
                    "python3 -c \"open('f', 'a').write('x')\"",
                    "python3 - <<'EOF'\nfrom pathlib import Path\nPath('f').write_text('x')\nEOF",
                    "python -u - <<EOF\nPath('f').write_bytes(b'x')\nEOF",
                    "cat <<'EOF' > Sources/X.swift\nx\nEOF", "echo x\npatch -p1 < f.patch",
                    "/usr/bin/python3 -c \"open('f', 'w').write('x')\"",
                    "env python3 -c \"open('f', 'w').write('x')\"",
                    ".venv/bin/python -c \"open('f', 'w').write('x')\"",
                    "python3 -c \"open(os.path.join(a, b), 'w').write('x')\"",
                    "perl -pie 's/a/b/' f", "echo x > $S/out", "S=/repo; echo 1 > $S/out",
                    "cd /tmp/w && cd /repo && cat > probe", "echo x | tee out.txt"]:
            with self.subTest(cmd=cmd):
                self.assertTrue(ac.edits_files(cmd), cmd)

    def test_commands_that_do_not_edit(self):
        for cmd in ["sed -n 1,5p f.py", "cat f.py", "grep -n foo f.py", "git diff", "git apply --check fix.patch",
                    "git apply --stat fix.patch", "python3 - <<'EOF'\nprint(open('f').read())\nEOF",
                    "python3 - <<'EOF'\nprint(open('f', 'r').read())\nif 1 > 0: pass\nEOF",
                    "make test 2>&1", "make test > /dev/null", "echo x > /tmp/out", "echo x > /private/tmp/out",
                    "echo x > /var/folders/ab/out", "echo x > $TMPDIR/out", "echo x | tee /tmp/out", "ls &> /dev/null",
                    "echo 'a > b'", "",
                    "perl -MList::Util=sum -e 'print 1'", "sed -n '/ -i/p' f", "(( i > 1 ))",
                    "echo $(( a > b ))", "tee \"$TMPDIR/x\" < f", "tee '/tmp/x' < f",
                    "python3 -c \"print(open('a').read().replace('x', 'y'))\"",
                    "python3 -c \"d=open('a').read(); f(d, 'x')\"",
                    "S=/tmp/x; echo 1 > $S/out", "S=\"$TMPDIR/y\" && cat > \"$S/p\"",
                    "S=/tmp/x; echo 1 > ${S}/out", "cd /tmp/w && cat > probe",
                    "echo x > ~/Library/Caches/a", "echo x > $HOME/Library/Caches/a",
                    "echo x > ${HOME}/Library/Caches/a"]:
            with self.subTest(cmd=cmd):
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

    def test_urls_and_file_scheme_name_no_file(self):
        ctx = self.context("See https://github.com/a/b/README.md and file:///x/y.md for more")
        self.assertEqual(ctx.handoff_missing, 0)

    def test_missing_files_do_not_spend_the_five_file_allowance(self):
        gone = " ".join(f"{self.dir}/gone{i}.md" for i in range(5))
        live = self.write("live.md", "G1 tests pass - CHECK: run them")
        ctx = self.context(f"Read {gone} then {live}")
        self.assertIn("G1 tests pass", ctx.handoff)
        self.assertEqual(ctx.handoff_missing, 5)

    def test_read_handoff_equals_the_hook(self):
        here = os.path.dirname(os.path.abspath(__file__))
        hook_path = os.path.join(here, "..", "..", "..", "hooks", "handoff-gates.py")
        loader = importlib.machinery.SourceFileLoader("handoff_gates_hook", hook_path)
        spec = importlib.util.spec_from_loader("handoff_gates_hook", loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)
        a = self.write("a.md", "alpha body")
        b = self.write("b.txt", "beta body")
        big = self.write("big.md", "x" * (256 * 1024 + 1))
        live = [self.write(f"l{i}.md", f"live{i}") for i in range(7)]
        home = tempfile.mkdtemp(prefix="handoff-home-", dir=os.path.expanduser("~"))
        self.addCleanup(shutil.rmtree, home)
        with open(os.path.join(home, "h.md"), "w") as f:
            f.write("home body")
        tilde = "~/" + os.path.basename(home) + "/h.md"
        gone = [f"{self.dir}/gone{i}.md" for i in range(6)]
        prompts = [
            "no files at all",
            f"{a} {a} {b} {a}",
            f"Read {tilde} please",
            f"{big} then {a}",
            " ".join(gone) + f" {a}",
            " ".join(live),
            f"{gone[0]} {big} {gone[1]} {a} {a} " + " ".join(live),
            f"https://github.com/a/b/README.md and {a}",
            f"file:///x/y.md and {a}",
            f"({a}) `{b}` \"{a}\"",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(ac.read_handoff(prompt)[0], hook.handoff(prompt))

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


class BuildLogRedirectTests(unittest.TestCase):
    def test_a_build_log_or_build_directory_redirect_is_not_an_edit(self):
        for command in ("swift build > .build/agent-build1.log 2>&1",
                        "make 2>&1 > build.log",
                        "echo exit $? >> .build/run.txt",
                        "cd /repo && swift test > /repo/.build/out.txt"):
            self.assertFalse(ac.edits_files(command), command)
        self.assertTrue(ac.edits_files("cat > Sources/A.swift <<'EOF'\nx\nEOF"))


if __name__ == "__main__":
    unittest.main()
