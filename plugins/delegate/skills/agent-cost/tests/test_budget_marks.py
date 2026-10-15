"""Tests for the Budget marks section in scripts/agent_cost.py: how many agents passed each mark, the turns
after it (split by a commit already made), the work left in their reports, and what a follow-up maker's
run-up cost against the larger context it replaced.
Run: python3 -m unittest discover -s plugins/delegate/skills/agent-cost/tests -p 'test_budget_marks.py'"""
import re
import unittest
from datetime import timedelta

from test_agentcost import (BASE, FixtureRoot, ac, assistant, section_body, tool_use_block, ts_str, usage,
                            user_text)
from test_agentcost import handback

WINDOW = (BASE - timedelta(hours=1), BASE + timedelta(hours=1))
COMMIT = {"command": "git commit -m done"}
EDIT = {"file_path": "/x.py", "old_string": "a", "new_string": "b"}


def agent(fx, name, agent_type, secs, prompt, turns, report=None):
    """A subagent starting `secs` after BASE. `turns` is one (context size, tool name, tool input or None) per
    turn, a second apart; the last turn carries `report` as its hand-back when one is given."""
    entries = [user_text(ts_str(BASE + timedelta(seconds=secs)), prompt)]
    for i, (size, tool, inp) in enumerate(turns):
        at = ts_str(BASE + timedelta(seconds=secs + i))
        content = [tool_use_block(f"{name}-{i}", tool, inp)] if tool else []
        if report is not None and i == len(turns) - 1:
            content.append(handback(f"{name}-hb", report))
        entries.append(assistant(f"{name}-m{i}", at, usage(input_tokens=size, output_tokens=1), content))
    fx.subagent(agent=name, agent_type=agent_type, entries=entries)


def rows(fx):
    """The section's rows keyed by (rung, mark), each the list of cells that follow."""
    loaded = ac.load_all(fx.root, None, *WINDOW)
    body = section_body(ac.build_report(loaded, 12, sections=["budget-marks"]), "Budget marks")
    found = {}
    for line in body.splitlines():
        m = re.match(r"\s+(\S+)\s+(freeze|hand-back|stop) \d+k\s+(.*)$", line)
        if m:
            found[(m.group(1), m.group(2))] = re.split(r"\s{2,}|\s(?=\$)", m.group(3).strip())
    return found


class BudgetMarksTests(unittest.TestCase):
    def test_turns_after_the_mark_split_by_a_commit_made_before_it(self):
        fx = FixtureRoot(self)
        # crosses 150k at turn 2, after committing at turn 1; two turns follow
        agent(fx, "c", "builder", 0, "x", [(50_000, "Bash", COMMIT), (60_000, None, None), (160_000, None, None),
                                           (161_000, None, None), (162_000, None, None)])
        # crosses at turn 1 with no commit yet; three turns follow
        agent(fx, "n", "builder", 10, "x", [(50_000, None, None), (160_000, None, None), (161_000, None, None),
                                            (162_000, None, None), (163_000, None, None)])
        row = rows(fx)[("builder", "hand-back")]
        self.assertEqual(row[0], "2")                     # past
        self.assertEqual(row[1:3], ["2 (1)", "3 (1)"])    # committed, not
        agent_c = ac.load_all(fx.root, None, *WINDOW)
        self.assertEqual({l.ctx.agent_id: l.ctx.first_commit_turn for l in agent_c if l.ctx.kind == "subagent"},
                         {"c": 0, "n": None})

    def test_a_remaining_list_is_work_left_but_the_suite_owed_to_a_runner_is_not(self):
        fx = FixtureRoot(self)
        big = [(160_000, None, None)]
        agent(fx, "a", "builder", 0, "x", big, report="Done: one.\nRemaining: the parser change.")
        agent(fx, "b", "builder", 5, "x", big, report="Done: all.\nRemaining: the full suite is owed to a runner.")
        agent(fx, "c", "builder", 10, "x", big, report="Done: all.\nRemaining: none.")
        row = rows(fx)[("builder", "hand-back")]
        self.assertEqual(row[0], "3")
        self.assertEqual(row[3], "1")                      # left work

    def test_a_follow_up_naming_the_branch_links_and_its_run_up_is_the_list_dollars_before_its_first_edit(self):
        fx = FixtureRoot(self)
        agent(fx, "old", "builder", 0, "Build `feat/thing`.", [(100_000, None, None), (160_000, None, None)],
              report="Remaining: the docs.")
        # runs 20s later: 10k, 10k, then edits at 12k, and two more turns
        agent(fx, "new", "builder", 20, "Finish `feat/thing`.",
              [(10_000, None, None), (10_000, None, None), (12_000, "Edit", EDIT), (12_000, None, None),
               (12_000, None, None)])
        row = rows(fx)[("builder", "hand-back")]
        loaded = ac.load_all(fx.root, None, *WINDOW)
        new = next(l for l in loaded if l.ctx.agent_id == "new")
        old = next(l for l in loaded if l.ctx.agent_id == "old")
        self.assertEqual(new.ctx.first_edit_turn, 2)
        runup = ac.list_dollars(20_000, 0, "claude-sonnet-5")
        lift = 160_000 - 12_000
        w = ac.cache_read_weight("claude-sonnet-5")
        extra = ac.list_dollars(lift * w * 2, 0, "claude-sonnet-5")
        self.assertEqual(ac.follow_up_costs(old, loaded), (runup, extra))
        self.assertEqual(row[3:5], ["1", "1"])             # left work, linked
        self.assertEqual(row[5:7], [ac.fmt_usd(runup), ac.fmt_usd(extra)])
        self.assertEqual(row[7], str(int(extra > runup)))  # cheaper agrees with the two sums

    def test_a_follow_up_that_started_before_the_hand_back_ended_or_names_another_branch_is_not_linked(self):
        fx = FixtureRoot(self)
        agent(fx, "old", "builder", 0, "Build `feat/thing`.", [(100_000, None, None), (160_000, None, None)],
              report="Remaining: the docs.")
        agent(fx, "other", "builder", 50, "Build `feat/other`.", [(10_000, "Edit", EDIT)])
        row = rows(fx)[("builder", "hand-back")]
        self.assertEqual(row[3:5], ["1", "0"])

    def test_a_scout_at_80k_is_on_the_scouts_hand_back_line_and_a_builder_is_not(self):
        fx = FixtureRoot(self)
        agent(fx, "s", "scout", 0, "x", [(85_000, None, None)])
        agent(fx, "b", "builder", 10, "x", [(85_000, None, None)])
        found = rows(fx)
        self.assertEqual(found[("scout", "hand-back")][0], "1")
        self.assertEqual(found[("scout", "freeze")][0], "1")
        self.assertNotIn(("scout", "stop"), found)
        self.assertNotIn(("builder", "freeze"), found)
        self.assertNotIn(("builder", "hand-back"), found)

    def test_the_section_is_named_budget(self):
        self.assertEqual(ac.resolve_sections("budget"), ["budget-marks"])
        self.assertIn("budget-marks", ac.DEFAULT_SECTIONS)



class GitCommitTests(unittest.TestCase):
    def test_a_commit_counts_and_commit_tree_does_not(self):
        for command in ("git commit -m x", "git -C repo commit -qm x", "cd a && git commit --amend"):
            self.assertIsNotNone(ac.GIT_COMMIT.search(command), command)
        for command in ("git commit-tree HEAD^{tree} -p a -p b", "git commit-graph write", "git log --oneline"):
            self.assertIsNone(ac.GIT_COMMIT.search(command), command)


if __name__ == "__main__":
    unittest.main()
