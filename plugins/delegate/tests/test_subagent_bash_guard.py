"""Tests for subagent-bash-guard.py. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOOK = os.path.join(ROOT, "hooks", "subagent-bash-guard.py")
WRAPPER = os.path.join(ROOT, "hooks", "subagent-bash-guard.sh")
spec = importlib.util.spec_from_file_location("bash_guard", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
    NAME = json.load(f)["name"]
ENV = {"CLAUDE_PLUGIN_ROOT": ROOT}
START = datetime(2026, 1, 1, 12, 0, 0, 123000, tzinfo=timezone.utc)


def stamp(seconds):
    """A transcript timestamp `seconds` after START: UTC, milliseconds, trailing Z."""
    return (START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def call_lines(n, command, issued, returned, result="ok", background=False, context=72_000):
    """The transcript lines of one Bash call: the assistant line issuing it and the user line returning it."""
    tool_input = {"command": command, "description": "run"}
    if background:
        tool_input["run_in_background"] = True
    usage = {"input_tokens": 3, "cache_read_input_tokens": context - 1003, "cache_creation_input_tokens": 1000}
    issue = {"type": "assistant", "timestamp": stamp(issued), "message": {
        "id": f"msg_{n}", "content": [{"type": "tool_use", "id": f"toolu_{n}", "name": "Bash", "input": tool_input}],
        "usage": usage}}
    back = {"type": "user", "timestamp": stamp(returned), "message": {
        "content": [{"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": result}]}}
    return [issue, back]


def notification(n, at):
    """The attachment the harness writes when a backgrounded call finishes."""
    prompt = (f"<task-notification>\n<task-id>b{n}</task-id>\n<tool-use-id>toolu_{n}</tool-use-id>\n"
              "<status>completed</status>\n<summary>Background command completed (exit code 0)</summary>\n"
              "</task-notification>")
    return {"type": "attachment", "timestamp": stamp(at), "attachment": {
        "type": "queued_command", "prompt": prompt, "commandMode": "task-notification", "timestamp": stamp(at)}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="bash-guard-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.parent = os.path.join(self.root, "session.jsonl")
        self.agent_file = os.path.join(self.root, "session", "subagents", "agent-a1.jsonl")
        os.makedirs(os.path.dirname(self.agent_file))
        self.checkout = os.path.join(self.root, "repo")
        self.write()

    def write(self, *entries):
        with open(self.agent_file, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def payload(self, command, agent_type=f"{NAME}:builder", agent="a1", background=False, cwd=None):
        tool_input = {"command": command, "description": "run"}
        if background:
            tool_input["run_in_background"] = True
        payload = {"session_id": "s", "transcript_path": self.parent, "cwd": cwd or self.checkout,
                   "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input,
                   "tool_use_id": "toolu_now", "agent_type": agent_type}
        if agent:
            payload["agent_id"] = agent
        return payload

    def decide(self, command, env=ENV, **kw):
        return guard.decision(self.payload(command, **kw), env)

    def reason(self, command, **kw):
        out = self.decide(command, **kw)
        if out is None:
            return None
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]


class KeyTests(unittest.TestCase):
    def same(self, a, b):
        self.assertTrue(guard.keys(a) & guard.keys(b), f"{a!r} should share a key with {b!r}")

    def distinct(self, a, b):
        self.assertFalse(guard.keys(a) & guard.keys(b), f"{a!r} should not share a key with {b!r}")

    def test_directories_assignments_wrappers_and_redirections_do_not_change_a_key(self):
        for other in ("cd /a && swift test", "sift run -- swift test 2>&1 | tail -5", "timeout 600 swift test",
                      "gtimeout 600 swift test", "time swift test", "time -p swift test", "/usr/bin/time -l swift test",
                      "nice -n 5 swift test", "env A=1 swift test", "A=1 B=2 swift test", "command swift test",
                      "caffeinate -i swift test", "caffeinate -t 600 swift test", "nohup swift test",
                      "lockf -k f swift test", "lockf -k -t 60 /tmp/l swift test", "xcrun swift test",
                      "xcrun --sdk macosx swift test", "swift test | xcbeautify", "cd /a; swift test > /tmp/log 2>&1",
                      "timeout -s KILL 600 sift run --quiet -- swift test", "cd /a && time -p swift test"):
            self.assertEqual(guard.keys(other), {"swift test"}, other)

    def test_other_arguments_are_another_key(self):
        self.distinct("swift test --filter A", "swift test")
        self.distinct("swift test --filter A", "swift test --filter B")
        self.distinct("swift build", "swift test")
        self.distinct("xcodebuild -scheme A test", "xcodebuild -scheme B test")
        self.same("swift build && swift test", "swift test")

    def test_the_build_and_test_set_has_keys(self):
        for command in ("swift build", "swift test --parallel", "xcodebuild -list", "xcodebuild -scheme A test",
                        "cargo build", "cargo test", "go build ./...", "go test ./...", "npm test", "npm run build",
                        "pnpm test", "yarn build", "bun run test", "npm run test:unit", "pytest -x",
                        "python3 -m pytest tests", "python -m pytest", "make", "make -C sub test", "gradle build",
                        "./gradlew test", "mvn verify", "dotnet build", "dotnet test"):
            self.assertTrue(guard.keys(command), command)

    def test_every_other_command_has_no_key(self):
        for command in ("git status", "git push", "ls", "sed -n 1,5p f", "cat f | head", "grep -rn swift .",
                        "echo swift test", "python3 - <<'EOF'\nimport json\nprint(1)\nEOF", "python3 -c 'print(1)'",
                        "python3 -m unittest", "python3 tool.py --build", "xcrun simctl list", "pgrep -f swift",
                        "seq 1 10", "break", "i+1", "swift package resolve", "cargo clippy", "go vet ./...",
                        "npm install", "npm run lint", "dotnet restore", "until grep -q DONE log; do sleep 10; done"):
            self.assertEqual(guard.keys(command), set(), command)

    def test_a_here_document_body_and_quoted_text_are_not_commands(self):
        self.assertEqual(guard.keys("cat <<'EOF' > notes.txt\nswift test\nEOF\nls"), set())
        self.assertEqual(guard.keys('echo "run swift test; then npm test"'), set())

    def test_a_loop_runs_the_commands_in_its_body(self):
        self.assertEqual(guard.keys("for f in a b; do swift test --filter $f; done"), {"swift test --filter $f"})


class LongRunTests(Fixture):
    def test_a_build_or_test_that_ran_long_is_refused_with_the_wait_the_context_and_the_choice(self):
        self.write(*call_lines(1, "swift test", 0, 540, context=72_000))
        said = self.reason("cd /a && sift run -- swift test 2>&1 | tail -5")
        self.assertIn(guard.LONG_TAG, said)
        self.assertIn("9 minutes", said)
        self.assertIn("72k", said)
        self.assertIn("re-issue the identical command", said)
        self.assertIn("hand back", said)

    def test_a_narrower_run_after_a_long_one_is_not_refused(self):
        self.write(*call_lines(1, "swift test", 0, 900))
        self.assertIsNone(self.decide("swift test --filter X"))
        self.assertIsNone(self.decide("sift run -- swift test --filter X 2>&1 | tail -5"))

    def test_the_same_run_is_refused_once_and_the_identical_re_issue_runs(self):
        self.write(*call_lines(1, "swift test", 0, 900))
        said = self.reason("swift test")
        self.assertIn(guard.LONG_TAG, said)
        self.write(*call_lines(1, "swift test", 0, 900), *call_lines(2, "swift test", 910, 911, result=said))
        self.assertIsNone(self.decide("swift test"))
        # The same run spelled another way is a new choice, and is asked again.
        self.assertIsNotNone(self.reason("cd /a && swift test"))

    def test_a_command_that_is_not_a_build_or_test_is_never_refused(self):
        self.write(*call_lines(1, "swift build && git push && python3 - <<'EOF'\nprint(1)\nEOF", 0, 900))
        for command in ("git push", "python3 - <<'EOF'\nprint(2)\nEOF", "python3 -c 'print(1)'", "xcrun simctl list"):
            self.assertIsNone(self.decide(command), command)
        self.assertIsNotNone(self.reason("swift build"))

    def test_a_run_within_the_cache_lifetime_is_not_long(self):
        self.write(*call_lines(1, "swift test", 0, 290))
        self.assertIsNone(self.decide("swift test"))

    def test_the_wait_counts_from_the_line_that_issued_the_call(self):
        # Issued at 0, returned at 301: one second past the default lifetime.
        self.write(*call_lines(1, "swift test", 0, 301))
        self.assertIsNotNone(self.reason("swift test"))

    def test_the_cache_lifetime_override_applies_and_a_bad_one_falls_back(self):
        self.write(*call_lines(1, "swift test", 0, 400))
        self.assertIsNone(self.decide("swift test", env=dict(ENV, DELEGATE_SUBAGENT_CACHE_SECONDS="600")))
        for bad in ("0", "-5", "soon"):
            self.assertIsNotNone(self.decide("swift test", env=dict(ENV, DELEGATE_SUBAGENT_CACHE_SECONDS=bad)), bad)

    def test_a_refusal_returned_as_text_blocks_is_found_too(self):
        refused = [{"type": "text", "text": f"{guard.LONG_TAG} An earlier run ..."}]
        self.write(*call_lines(1, "npm test", 0, 600), *call_lines(2, "npm test", 610, 611, result=refused))
        self.assertIsNone(self.decide("npm test"))

    def test_a_looking_command_after_a_long_run_passes(self):
        self.write(*call_lines(1, "cd /a && git status && swift test", 0, 900))
        for quick in ("git status", "ls", "sed -n 1,5p f"):
            self.assertIsNone(self.decide(quick), quick)
        self.assertIsNone(self.decide("swift build"))

    def test_a_long_run_far_back_in_a_large_transcript_still_counts(self):
        filler = {"type": "user", "timestamp": stamp(1000), "message": {"content": "x" * 4000}}
        self.write(*call_lines(1, "swift test", 0, 900), *[filler] * 400, *call_lines(2, "ls", 2000, 2001, context=150_000))
        self.assertGreater(os.path.getsize(self.agent_file), 1024 * 1024 + 300_000)
        said = self.reason("swift test")
        self.assertIn("15 minutes", said)
        self.assertIn("150k", said)

    def test_a_backgrounded_run_counts_until_its_completion_notification(self):
        self.write(*call_lines(1, "swift test", 0, 1, result="Command running in background with ID: b1", background=True),
                   notification(1, 700))
        said = self.reason("swift test", background=True)
        self.assertIn("11 minutes", said)
        # Without the notification it returned at once, so it was not long.
        self.write(*call_lines(1, "swift test", 0, 1, result="Command running in background with ID: b1", background=True))
        self.assertIsNone(self.decide("swift test"))

    def test_the_latest_long_run_of_the_key_gives_the_wait(self):
        self.write(*call_lines(1, "swift test", 0, 600), *call_lines(2, "swift test", 700, 1900, context=90_000))
        said = self.reason("swift test")
        self.assertIn("20 minutes", said)
        self.assertIn("90k", said)

    def test_the_switch_turns_the_check_off(self):
        self.write(*call_lines(1, "swift test", 0, 600))
        self.assertIsNone(self.decide("swift test", env=dict(ENV, DELEGATE_LONG_RUN_GUARD="0")))

    def test_a_main_session_call_is_left_alone(self):
        self.write(*call_lines(1, "swift test", 0, 600))
        self.assertIsNone(self.decide("swift test", agent=None))

    def test_a_missing_transcript_leaves_the_call_alone(self):
        os.remove(self.agent_file)
        self.assertIsNone(self.decide("swift test"))


def git(cwd, *args):
    """Run git in `cwd` with none of the caller's GIT_* variables, no hooks and a fixed identity."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    settings = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]
    out = subprocess.run(["git", *settings, *args], cwd=cwd, env=env, capture_output=True, encoding="utf-8")
    if out.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {out.stderr}")
    return out.stdout.strip()


class ReviewerTests(Fixture):
    """Real repositories: the ones made before the reviewer's transcript begins are others', the ones made
    after are its own."""
    REVIEWER = f"{NAME}:reviewer"

    def setUp(self):
        super().setUp()
        os.makedirs(self.checkout)
        self.init(self.checkout)
        self.sibling = os.path.join(self.root, "sibling")
        git(self.checkout, "worktree", "add", "--detach", self.sibling, "HEAD")
        self.start = time.time() + 0.02
        time.sleep(0.06)  # everything made from here on is younger than the reviewer
        self.write()
        self.scratch = os.path.join(self.root, "scratch")
        os.makedirs(self.scratch)

    def init(self, path):
        git(path, "init", "-q")
        self.assertTrue(git(path, "rev-parse", "--absolute-git-dir").startswith(self.root + os.sep))
        with open(os.path.join(path, "f"), "w", encoding="utf-8") as f:
            f.write("x\n")
        git(path, "add", "f")
        git(path, "commit", "-q", "-m", "first")

    def write(self, *entries):
        if not hasattr(self, "start"):
            return super().write(*entries)
        opening = {"type": "user", "timestamp": datetime.fromtimestamp(self.start, timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"), "message": {"role": "user", "content": "Review"}}
        super().write(opening, *entries)

    def refused(self, command, env=ENV, cwd=None):
        said = self.reason(command, agent_type=self.REVIEWER, env=env, cwd=cwd)
        return said is not None and guard.REVIEWER_TAG in said

    def test_commands_that_move_the_checkout_it_started_in_are_refused_anywhere_in_a_line(self):
        for command in ("git checkout main", "git checkout -q --detach", "git switch -c x", "git reset --hard HEAD~1", "git restore f",
                        "git stash", "echo hi; git stash pop", "git commit -m x", "git merge b", "git rebase main",
                        "git cherry-pick abc", "git revert abc", "git am p", "git pull", "git clean -fd",
                        "git rm f", "git mv a b", "git add .", "git apply p.diff", "git bisect start",
                        "git bisect good", "git read-tree HEAD", "git submodule update --init",
                        "git -c core.x=y --no-pager reset --hard", "ls && git log && git checkout -",
                        f"git --git-dir={self.checkout}/.git --work-tree={self.checkout} checkout x",
                        "x=$(git stash create && git stash)", f"cd {self.scratch} && cd {self.checkout} && git checkout main 2>&1 | tail -3",
                        "env GIT_TRACE=1 git checkout main", "timeout 60 git pull"):
            self.assertTrue(self.refused(command), command)

    def test_a_sibling_worktree_made_before_the_reviewer_is_refused(self):
        for command in (f"cd {self.sibling} && git checkout main", f"git -C {self.sibling} reset --hard",
                        f"cd {self.scratch} && git -C {self.sibling} bisect start", f"git worktree remove {self.sibling}",
                        f"git worktree move {self.sibling} {self.scratch}/moved"):
            self.assertTrue(self.refused(command), command)

    def test_reads_fetch_new_branches_and_worktree_add_are_allowed(self):
        for command in ("git log --oneline -5", "git diff main...HEAD", "git show HEAD:f", "git status",
                        "git rev-parse HEAD", "git merge-base a b", "git ls-files", "git grep x", "git blame f",
                        "git branch", "git branch -a --contains abc", "git branch --list 'feat/*'", "git branch new-name",
                        "git branch new-name abc123", "git merge-tree --write-tree a b", "git fetch origin",
                        "git apply --check p.diff", "git apply --stat p.diff", "git stash list", "git stash show",
                        "git tag", "git tag -l 'v*'", "git symbolic-ref HEAD", "git worktree list", "git bisect log",
                        "git checkout", "git checkout -q 2>/dev/null; git rev-parse HEAD",
                        f"git -C {self.sibling} log", f"git worktree add {self.scratch}/wt HEAD --detach",
                        f"git worktree add -b probe {self.scratch}/wt main", "git notes show HEAD",
                        "cat <<'EOF' > n.txt\ngit reset --hard\nEOF", "grep -rn 'git checkout' docs"):
            self.assertFalse(self.refused(command), command)

    def test_refs_every_worktree_shares_are_refused_from_any_checkout_of_an_older_repository(self):
        own = os.path.join(self.scratch, "own")
        git(self.checkout, "worktree", "add", "--detach", own, "HEAD")
        for command in ("branch -f main HEAD", "branch -D old", "branch -m a b", "branch -c a b", "branch -u origin/x",
                        "branch --set-upstream-to=origin/x", "update-ref refs/heads/x HEAD", "symbolic-ref HEAD refs/heads/x",
                        "tag v1", "tag -d v1", "push", "push origin HEAD:main", "stash", "stash pop",
                        "notes add -m x HEAD", "notes --ref review append -m x", "worktree add -B main ../x"):
            self.assertTrue(self.refused(f"git -C {own} {command}"), command)
        self.assertFalse(self.refused(f"git -C {own} branch new-name"))

    def test_a_repository_the_reviewer_made_itself_is_its_own(self):
        made = os.path.join(self.scratch, "made")
        os.makedirs(made)
        self.init(made)
        for command in ("git add . && git commit -m x", "git reset --hard", "git branch -f main HEAD",
                        "git stash", "git tag v1", "git checkout -b x"):
            self.assertFalse(self.refused(f"cd {made} && {command}"), command)

    def test_a_repository_made_earlier_in_the_same_line_is_its_own(self):
        inner = os.path.join(self.checkout, ".build", "p")
        os.makedirs(inner)  # inside the older checkout, so until the init the older one is its nearest
        self.assertFalse(self.refused(f"P={inner}; mkdir -p $P && cd $P && git init -q && git add . && git commit -qm x"))
        self.assertFalse(self.refused(f"git init -q {inner} && git -C {inner} add . && git -C {inner} branch -f x"))
        self.assertTrue(self.refused(f"cd {inner} && git add ."))
        clone = os.path.join(self.checkout, ".build", "c")
        os.makedirs(clone)
        self.assertFalse(self.refused(f"git clone -q {self.checkout} {clone} && cd {clone} && git reset --hard HEAD~1"))

    def test_its_own_worktree_spelled_through_a_variable_is_its_own(self):
        wt = os.path.join(self.scratch, "rv-p34")
        git(self.checkout, "worktree", "add", "--detach", wt, "HEAD")
        self.assertFalse(self.refused(f"S={self.scratch}; (cd $S/rv-p34 && git checkout -- f)"))
        self.assertFalse(self.refused(f"export S={self.scratch} && git -C ${{S}}/rv-p34 reset --hard"))
        self.assertFalse(self.refused(f"S={self.scratch}; git worktree remove $S/rv-p34"))
        self.assertTrue(self.refused(f"S={self.root}; cd $S/sibling && git checkout main"))
        # Added earlier in the same line, into a directory that is already there.
        later = os.path.join(self.checkout, ".build", "rv")
        os.makedirs(later)
        self.assertFalse(self.refused(f"S={self.checkout}/.build; git worktree add --detach $S/rv HEAD && cd $S/rv && git checkout -b probe"))

    def test_a_failed_add_or_clone_into_a_directory_that_is_not_empty_makes_nothing(self):
        leftover = os.path.join(self.checkout, ".build", "rv")
        os.makedirs(leftover)
        with open(os.path.join(leftover, "log"), "w", encoding="utf-8") as f:
            f.write("x\n")  # git refuses to add or clone into it, and the line goes on in the older checkout
        for sep in ("; ", "\n"):
            for command in (f"git worktree add .build/rv HEAD{sep}cd .build/rv{sep}git checkout -b probe",
                            f"git clone -q {self.checkout} .build/rv{sep}cd .build/rv{sep}git reset --hard HEAD~1",
                            f"git clone -q {self.checkout} {leftover}{sep}git -C {leftover} branch -f main HEAD"):
                self.assertTrue(self.refused(command), command)
        self.assertFalse(self.refused("git worktree add .build/new HEAD && cd .build/new && git checkout -b probe"))

    def test_a_new_repository_does_not_hold_an_older_checkout_nested_inside_it(self):
        for command in (f"git init -q {self.root} && cd {self.checkout} && git checkout -b x",
                        f"git init -q {self.root}; git -C {self.checkout} branch -f main HEAD",
                        f"cd {self.root} && git init -q && cd sibling && git reset --hard"):
            self.assertTrue(self.refused(command), command)

    def test_an_add_or_init_where_a_git_entry_is_already_there_makes_nothing(self):
        for command in ("git worktree add . && git checkout main", "git worktree add . HEAD; git checkout main",
                        "git init -q . && git checkout main", "git init -q; git branch -f main HEAD"):
            self.assertTrue(self.refused(command), command)

    def test_git_dir_and_work_tree_name_the_directory_whatever_the_cwd(self):
        for command in (f"git --git-dir={self.checkout}/.git --work-tree={self.checkout} checkout x",
                        f"git --work-tree={self.checkout} --git-dir={self.checkout}/.git reset --hard",
                        f"git --git-dir={self.checkout}/.git branch -f main HEAD",
                        f"git --work-tree={self.sibling} reset --hard"):
            self.assertTrue(self.refused(command, cwd=self.scratch), command)

    def test_a_nested_worktree_made_after_the_reviewer_started_is_its_own(self):
        nested = os.path.join(self.checkout, ".build", "rv")
        git(self.checkout, "worktree", "add", "--detach", nested, "HEAD")
        for command in (f"cd {nested} && git reset --hard", f"cd {nested}/ && git checkout -- f",
                        f"git -C {nested} bisect start", "cd .build/rv && git clean -fd", f"git worktree remove {nested}"):
            self.assertFalse(self.refused(command), command)

    def test_a_directory_that_cannot_be_told_is_allowed(self):
        for command in ("cd $UNSET/x && git checkout main", "S=$(mktemp -d); cd $S && git checkout main",
                        "git -C $WT reset --hard", f"cd {self.root}/gone && git checkout main",
                        "for d in a b; do (cd $d && git checkout main); done", "cd - && git checkout main"):
            self.assertFalse(self.refused(command), command)

    def test_a_cd_inside_a_subshell_or_substitution_ends_with_it_and_popd_returns(self):
        s, c = self.scratch, self.checkout
        for command in (f"(cd {s} && ls); git checkout main", f"x=$(cd {s}; pwd); git checkout main",
                        f"y=`cd {s}`; git reset --hard", f"pushd {s} && popd && git checkout main",
                        f"(cd {s}; (cd /); ls) && git stash", f"x=$(cd {s}; y=`cd /; pwd`; pwd); git checkout main"):
            with self.subTest(command=command):
                self.assertTrue(self.refused(command))
        for command in (f"{{ cd {s}; }}; git checkout main", f"(cd {s} && git init -q n && cd n && git checkout -b x)",
                        f"pushd {s} && git checkout main", f"cd {s} && pushd {c} && popd && git checkout main",
                        f"(cd {c}; cd {s}; git checkout main)", f"popd; cd {s} && popd && git checkout main",
                        f"pushd {s} && pushd +1 && git checkout main",
                        # D is unknown once its group closes, so the cd target cannot be told: allowed, as before.
                        f"(D={s}); cd $D 2>/dev/null; git checkout main"):
            with self.subTest(command=command):
                self.assertFalse(self.refused(command))

    def test_after_a_cd_that_may_fail_a_broken_chain_is_judged_from_the_directory_before_it_too(self):
        s, typo = self.scratch, "/nonexistent-typo"
        branch = git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD")
        for command in (f"git worktree add {s}/rv {branch}; cd {s}/rv; git checkout -b probe",
                        f"git worktree add {s}/rv {branch}\ncd {s}/rv\ngit checkout -b probe",
                        f"cd {typo}; git checkout main", f"cd {typo} || true; git reset --hard",
                        f"cd {typo} & git checkout main", f"(cd {typo}; git checkout main)",
                        f"cd {typo} && ls; git stash", f"cd {typo} && (ls); git checkout main",
                        f"pushd {typo}; git checkout main", f"cd {typo} &&\nls\ngit checkout main"):
            with self.subTest(command=command):
                self.assertTrue(self.refused(command))
        for command in (f"git worktree add {s}/wt HEAD --detach && cd {s}/wt && git checkout -b x",
                        f"git worktree add {s}/wt HEAD --detach &&\ncd {s}/wt &&\ngit checkout -b x",
                        f"mkdir -p {s}/m && cd {s}/m && git init -q && git commit -q --allow-empty -m x",
                        f"cd {typo} && git checkout main", f"cd {typo} &>/dev/null && git checkout main",
                        f"cd {typo} && ls | cat && git checkout main", f"cd {typo} && (ls; git checkout main)",
                        f"cd {s}; git checkout main", f"(cd {typo}; ls); cd {s}; git checkout main",
                        f"cd {typo}; cd {s}; git checkout main",
                        # Either scratch (the first cd succeeded) or the checkout's parent (it failed): no checkout.
                        f"cd {s}/missing; cd ..; git checkout main"):
            with self.subTest(command=command):
                self.assertFalse(self.refused(command))

    def test_list_forms_of_tag_and_stash_with_options_are_reads(self):
        for command in ("git tag -n5", "git tag -n 5", "git tag -n5 'v*'", "git stash -q list", "git stash --quiet show"):
            with self.subTest(command=command):
                self.assertFalse(self.refused(command))
        for command in ("git stash -q", "git stash -q pop", "git stash -m list"):
            with self.subTest(command=command):
                self.assertTrue(self.refused(command))

    def test_home_and_tilde_expand(self):
        env = dict(ENV, HOME=self.root)
        self.assertTrue(self.refused("cd ~/sibling && git checkout main", env=env))
        self.assertTrue(self.refused("git -C $HOME/sibling reset --hard", env=env))

    def test_pruning_an_older_worktree_is_refused_and_pruning_its_own_is_not(self):
        own = os.path.join(self.scratch, "own")
        git(self.checkout, "worktree", "add", "--detach", own, "HEAD")
        shutil.rmtree(own)
        self.assertFalse(self.refused("git worktree prune"))
        shutil.rmtree(self.sibling)
        self.assertTrue(self.refused("git worktree prune"))

    def test_the_creation_time_decides(self):
        made = os.path.join(self.scratch, "made")
        os.makedirs(made)
        self.init(made)
        with mock.patch.object(guard, "created", lambda path: self.start - 60):
            self.assertTrue(self.refused(f"cd {made} && git checkout main"))
        with mock.patch.object(guard, "created", lambda path: self.start + 60):
            self.assertFalse(self.refused("git checkout main"))
            self.assertFalse(self.refused("git push"))

    def test_without_a_birth_time_the_oldest_entry_of_the_git_directory_gives_it(self):
        dot = os.path.join(self.checkout, ".git")
        for entry in os.listdir(dot):
            os.utime(os.path.join(dot, entry), (5000, 5000))
        open(os.path.join(dot, "description"), "a").close()  # git init writes it from its template
        os.utime(os.path.join(dot, "description"), (1000, 1000))
        os.utime(os.path.join(dot, "index"), (9000, 9000))  # rewritten by every index write
        os.utime(dot, (9000, 9000))
        real = os.lstat

        class NoBirth:
            def __init__(self, info):
                self.st_mode, self.st_mtime = info.st_mode, info.st_mtime

        with mock.patch.object(guard.os, "lstat", lambda path: NoBirth(real(path))):
            self.assertEqual(guard.created(dot), 1000)
            worktree_file = os.path.join(self.sibling, ".git")
            self.assertEqual(guard.created(worktree_file), real(worktree_file).st_mtime)

    def test_re_issuing_does_not_lift_the_refusal(self):
        refused = f"PreToolUse:Bash hook error: {guard.REVIEWER_TAG} ..."
        self.write(*call_lines(1, "git checkout main", 0, 1, result=refused))
        self.assertTrue(self.refused("git checkout main"))

    def test_only_this_plugins_reviewer_is_guarded(self):
        for agent_type in (f"{NAME}:builder", "other:reviewer", "reviewer", None):
            self.assertIsNone(self.decide("git checkout main", agent_type=agent_type), agent_type)

    def test_the_switch_turns_the_check_off(self):
        env = dict(ENV, DELEGATE_REVIEWER_GUARD="0")
        self.assertIsNone(self.decide("git checkout main", env=env, agent_type=self.REVIEWER))

    def test_a_reviewer_also_gets_the_long_run_check(self):
        self.write(*call_lines(1, "swift test", 0, 600))
        self.assertIn(guard.LONG_TAG, self.reason("swift test", agent_type=self.REVIEWER))


class WiringTests(Fixture):
    def run_wrapper(self, payload, env=None):
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=ROOT, **(env or {}))
        for key in ("DELEGATE_LONG_RUN_GUARD", "DELEGATE_REVIEWER_GUARD", "DELEGATE_SUBAGENT_CACHE_SECONDS"):
            env.pop(key, None)
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(["/bin/sh", WRAPPER], input=stdin, capture_output=True, encoding="utf-8", env=env)

    def test_the_wrapper_refuses_end_to_end_and_leaves_the_main_session_alone(self):
        self.write(*call_lines(1, "swift test", 0, 600))
        out = self.run_wrapper(self.payload("swift test 2>&1 | tail -3"))
        self.assertEqual(out.returncode, 0, out.stderr)
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(guard.LONG_TAG, decision["permissionDecisionReason"])
        out = self.run_wrapper(self.payload("swift test 2>&1 | tail -3", agent=None))
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_a_malformed_payload_prints_nothing_and_exits_zero(self):
        for stdin in ("not json", "[]", "{}", json.dumps({"agent_id": "a1", "tool_name": "Bash", "tool_input": "x"}),
                      json.dumps({"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": 5}})):
            out = self.run_wrapper(stdin)
            self.assertEqual((out.returncode, out.stdout), (0, ""), stdin)

    def test_hooks_json_runs_the_wrapper_before_every_bash_call(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            entries = json.load(f)["hooks"]["PreToolUse"]
        commands = [h["command"] for e in entries if e.get("matcher") == "Bash" for h in e["hooks"]]
        self.assertEqual(commands, ['sh "${CLAUDE_PLUGIN_ROOT}/hooks/subagent-bash-guard.sh"'])


if __name__ == "__main__":
    unittest.main()
