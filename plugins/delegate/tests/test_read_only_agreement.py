"""The subagent hook and agent-cost must call the same commands read-only."""
import importlib.util
import inspect
import os
import unittest

HERE = os.path.dirname(__file__)


def load(name, *path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", *path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load("budget_agreement", "hooks", "subagent-context-budget.py")
cost = load("agent_cost_agreement", "skills", "agent-cost", "scripts", "agent_cost.py")

COMMANDS = (
    "swift test --filter Foo 2>&1 | tail -20", "npm test | tail", "make 2>&1 | grep error",
    'git commit -am "fix grep usage"', "git push origin x | tail", "git merge feat/x | tail",
    'python3 -c "print(1)" | head', "ls", "find . -name x", "wc -l f",
    "git merge-base --is-ancestor a b", 'grep -E "foo|rm" f',
    "git checkout feat/x && git log -1", "git branch feat/new", "git reset --hard && git status",
    "git stash && git diff", "rm -rf .build && grep -r x .",
    "grep -rn x .", "sed -n 1,20p f", "git log --oneline -5", "git diff main", "git branch",
    "git branch -a", "git branch --list", "git branch -vv", "git branch --show-current",
    "cat f | head", "cd /x && git status", "sed -i s/a/b/ f", "cat x > y", "git fetch origin",
    "git worktree add ../w", "git branch -D old", "git rm f", "git status 2>&1 | head",
)


class AgreementTests(unittest.TestCase):
    def test_the_writes_pattern_is_the_same(self):
        self.assertEqual(hook.WRITES.pattern, cost.WRITES.pattern)

    def test_the_copied_classifier_is_verbatim(self):
        # A drift outside the sampled commands would pass the verdict comparison, so pin the copy itself.
        for name in ("is_wait_loop", "pipeline_stages", "bash_class", "stage_class", "turn_shape_category"):
            self.assertEqual(inspect.getsource(getattr(hook, name)), inspect.getsource(getattr(cost, name)), name)
        for name in ("PURE_FILTER", "WAIT_LOOP", "SLEEPS"):
            self.assertEqual(getattr(hook, name).pattern, getattr(cost, name).pattern, name)

    def test_the_read_only_sets_are_the_same(self):
        self.assertEqual(hook.READ_ONLY_CATEGORIES, cost.READ_ONLY_CATEGORIES)

    def test_both_call_the_same_commands_read_only(self):
        self.assertGreaterEqual(len(COMMANDS), 25)
        for command in COMMANDS:
            tool_input = {"command": command}
            theirs = cost.turn_shape_category(cost.classify("Bash", tool_input), tool_input) in cost.READ_ONLY_CATEGORIES
            self.assertEqual(hook.read_only("Bash", tool_input), theirs, command)

    def test_both_call_the_looking_tools_read_only(self):
        for name, tool_input in (("Read", {}), ("Read", {"limit": 5}), ("Grep", {}), ("Glob", {}), ("Edit", {})):
            theirs = cost.turn_shape_category(cost.classify(name, tool_input), tool_input) in cost.READ_ONLY_CATEGORIES
            self.assertEqual(hook.read_only(name, tool_input), theirs, name)


if __name__ == "__main__":
    unittest.main()
