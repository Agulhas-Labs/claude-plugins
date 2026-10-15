"""Tests for subagent-bash-guard.py. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

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

    def payload(self, command, agent_type=f"{NAME}:builder", agent="a1", background=False):
        tool_input = {"command": command, "description": "run"}
        if background:
            tool_input["run_in_background"] = True
        payload = {"session_id": "s", "transcript_path": self.parent, "cwd": self.checkout,
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


class FamilyTests(unittest.TestCase):
    def same(self, a, b):
        self.assertTrue(guard.families(a) & guard.families(b), f"{a!r} should match {b!r}")

    def distinct(self, a, b):
        self.assertFalse(guard.families(a) & guard.families(b), f"{a!r} should not match {b!r}")

    def test_wrappers_directories_and_filters_do_not_change_a_family(self):
        for other in ("cd /a && swift test", "sift run -- swift test --filter X", "timeout 600 swift test",
                      "time swift test", "nice -n 5 swift test", "env A=1 swift test", "A=1 B=2 swift test",
                      "caffeinate -i swift test", "caffeinate -t 600 swift test", "nohup swift test",
                      "swift test 2>&1 | tail -30", "cd /a; swift test > /tmp/log 2>&1",
                      "timeout -s KILL 600 sift run --quiet -- swift test --parallel"):
            self.same("swift test", other)

    def test_xcodebuild_that_tests_is_one_family_whatever_its_scheme_or_other_actions(self):
        self.same("xcodebuild -scheme A test", "xcodebuild -scheme B build test")
        self.same("xcodebuild -scheme A -destination 'platform=iOS Simulator,name=X' test | xcbeautify",
                  "xcodebuild test-without-building -scheme A")
        self.distinct("xcodebuild -scheme A test", "xcodebuild -list")

    def test_script_runners_and_test_runners_normalise(self):
        self.same("npm test", "npm run test")
        self.same("python3 -m pytest x", "pytest y")
        self.same("make -C sub test", "make test")
        self.distinct("npm test", "npm run build")
        self.distinct("python3 -m pytest x", "python3 -m unittest")

    def test_looking_commands_never_match_a_long_swift_test(self):
        for quick in ("git status", "ls", "sed -n 1,5p f", "cd /a && git log --oneline -3", "cat f | head",
                      "grep -rn swift .", "echo swift test"):
            self.distinct(quick, "swift test")
            self.distinct(quick, "cd /a && git status && swift test")

    def test_swift_build_and_swift_test_are_distinct(self):
        self.distinct("swift build", "swift test")
        self.same("swift build && swift test", "swift test")

    def test_a_here_document_body_and_quoted_text_are_not_commands(self):
        self.assertEqual(guard.families("cat <<'EOF' > notes.txt\nswift test\nEOF\nls"), set())
        self.assertEqual(guard.families('echo "run swift test; then npm test"'), set())

    def test_a_loop_runs_the_commands_in_its_body(self):
        self.same("for f in a b; do swift test --filter $f; done", "swift test")
        self.assertEqual(guard.families("until grep -q DONE log; do sleep 10; done"), set())


class LongRunTests(Fixture):
    def test_a_command_of_a_family_that_ran_long_is_refused_with_the_wait_the_context_and_the_choice(self):
        self.write(*call_lines(1, "swift test", 0, 540, context=72_000))
        said = self.reason("cd /a && sift run -- swift test --filter X")
        self.assertIn(guard.LONG_TAG, said)
        self.assertIn("9 minutes", said)
        self.assertIn("72k", said)
        self.assertIn("re-issue the identical command", said)
        self.assertIn("hand back", said)

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

    def test_the_identical_re_issue_after_a_refusal_runs(self):
        refused = f"PreToolUse:Bash hook error: {guard.LONG_TAG} An earlier run ..."
        self.write(*call_lines(1, "swift test", 0, 600), *call_lines(2, "swift test --filter A", 610, 611, result=refused))
        self.assertIsNone(self.decide("swift test --filter A"))
        # Another command of the family is a new choice, and is asked again.
        self.assertIsNotNone(self.reason("swift test --filter B"))

    def test_a_refusal_returned_as_text_blocks_is_found_too(self):
        refused = [{"type": "text", "text": f"{guard.LONG_TAG} An earlier run ..."}]
        self.write(*call_lines(1, "npm test", 0, 600), *call_lines(2, "npm run test", 610, 611, result=refused))
        self.assertIsNone(self.decide("npm run test"))

    def test_a_looking_command_after_a_long_run_passes(self):
        self.write(*call_lines(1, "cd /a && git status && swift test", 0, 900))
        for quick in ("git status", "ls", "sed -n 1,5p f"):
            self.assertIsNone(self.decide(quick), quick)
        self.assertIsNone(self.decide("swift build"))

    def test_a_backgrounded_run_counts_until_its_completion_notification(self):
        self.write(*call_lines(1, "swift test", 0, 1, result="Command running in background with ID: b1", background=True),
                   notification(1, 700))
        said = self.reason("swift test", background=True)
        self.assertIn("11 minutes", said)
        # Without the notification it returned at once, so it was not long.
        self.write(*call_lines(1, "swift test", 0, 1, result="Command running in background with ID: b1", background=True))
        self.assertIsNone(self.decide("swift test"))

    def test_the_latest_long_run_of_the_family_gives_the_wait(self):
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


class ReviewerTests(Fixture):
    REVIEWER = f"{NAME}:reviewer"

    def refused(self, command):
        said = self.reason(command, agent_type=self.REVIEWER)
        return said is not None and guard.REVIEWER_TAG in said

    def test_git_commands_that_move_a_checkout_are_refused_anywhere_in_a_line(self):
        for command in ("git checkout main", "git switch -c x", "git reset --hard HEAD~1", "git restore f",
                        "git stash", "echo hi; git stash pop", "git commit -m x", "git merge b", "git rebase main",
                        "git cherry-pick abc", "git revert abc", "git am p", "git pull", "git clean -fd",
                        "git rm f", "git mv a b", "git add .", "git apply p.diff", "git tag v1",
                        "git update-ref refs/heads/x HEAD", "git symbolic-ref HEAD refs/heads/x", "git push",
                        "git branch new-name", "git branch -D old", "git branch -f main HEAD", "git branch -m a b",
                        "git worktree remove ../other", "git worktree prune", "git worktree move a b",
                        "git -C /elsewhere checkout main", "git -c core.x=y --no-pager reset --hard",
                        "git --git-dir=/r/.git --work-tree=/r checkout x", "ls && git log && git checkout -",
                        "x=$(git stash create && git stash)", "cd /a && git checkout main 2>&1 | tail -3",
                        "env GIT_TRACE=1 git checkout main", "timeout 60 git pull"):
            self.assertTrue(self.refused(command), command)

    def test_reads_fetch_and_worktree_add_are_allowed(self):
        for command in ("git log --oneline -5", "git diff main...HEAD", "git show HEAD:f", "git status",
                        "git rev-parse HEAD", "git merge-base a b", "git ls-files", "git grep x", "git blame f",
                        "git branch", "git branch -a --contains abc", "git branch --list 'feat/*'",
                        "git merge-tree --write-tree a b", "git fetch origin", "git apply --check p.diff",
                        "git apply --stat p.diff", "git stash list", "git tag", "git tag -l 'v*'",
                        "git symbolic-ref HEAD", "git worktree list", "git -C /r log",
                        "git worktree add /s/wt HEAD --detach", "cat <<'EOF' > n.txt\ngit reset --hard\nEOF",
                        "grep -rn 'git checkout' docs"):
            self.assertFalse(self.refused(command), command)

    def test_a_worktree_the_reviewer_added_may_be_changed_and_removed(self):
        scratch = os.path.join(self.root, "scratch")
        self.write(*call_lines(1, f"git worktree add {scratch}/wt abc123 --detach", 0, 2))
        self.assertFalse(self.refused(f"cd {scratch}/wt && git checkout def456"))
        self.assertFalse(self.refused(f"git -C {scratch}/wt reset --hard"))
        self.assertFalse(self.refused(f"cd {scratch}/wt/sub && git clean -fd"))
        self.assertFalse(self.refused(f"git worktree remove {scratch}/wt"))
        # The checkout the call runs in is still guarded, and so is a sibling with a shared prefix.
        self.assertTrue(self.refused("git checkout def456"))
        self.assertTrue(self.refused(f"cd {scratch}/wt2 && git checkout def456"))
        self.assertTrue(self.refused(f"git worktree remove {scratch}/other"))

    def test_a_relative_worktree_path_resolves_against_the_directory_git_ran_in(self):
        self.write(*call_lines(1, "git -C sub worktree add -b tmp ../wt main", 0, 2))
        self.assertFalse(self.refused(f"cd {self.checkout}/wt && git commit -m x"))
        self.assertTrue(self.refused(f"cd {self.checkout}/sub && git commit -m x"))

    def test_a_worktree_added_earlier_in_the_same_line_counts(self):
        wt = os.path.join(self.root, "scratch", "wt")
        self.assertFalse(self.refused(f"git worktree add {wt} main && cd {wt} && git checkout -b probe"))

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
        out = self.run_wrapper(self.payload("swift test --filter X"))
        self.assertEqual(out.returncode, 0, out.stderr)
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn(guard.LONG_TAG, decision["permissionDecisionReason"])
        out = self.run_wrapper(self.payload("swift test --filter X", agent=None))
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
