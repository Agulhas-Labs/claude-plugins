"""Tests for a subagent's model and description when its launch reached no board.

Run: python3 -m unittest discover -s plugins/logbook/tests

`SubagentStart` carries only the subagent's id and type; its model and description come from the
`PostToolUse` of the `Agent` call that launched it. When that hook runs before the board exists, the
launch is read back from the transcript instead, the way task-list and work calls are. The transcript
is `fixtures/transcript-agents.jsonl`, whose first launch is the one the captured `PostToolUse-Agent`
payload reports, for the subagent the captured `SubagentStart` starts.
"""
import copy
import json
import os
import unittest

from test_board import at, event
from test_hooks import HERE, fixture, transcript_lines
from test_start_path import StartPath

import board  # noqa: E402  (test_board puts the board folder on the path)
import board_hook  # noqa: E402  (test_hooks puts the hooks folder on the path)

AGENTS = os.path.join(HERE, "fixtures", "transcript-agents.jsonl")
BACKGROUND, LAUNCH = "ac91f2c94fb8193d6", "toolu_01LSBd2Zot343tCJEgpVi48s"
FOREGROUND, WAITED = "a5d0c3b1e2f4a6978", "toolu_01Fg7R2wQeVn9kTzYx3bHcUa"
MODEL = "claude-haiku-4-5-20251001"


def fixture_lines():
    """The fixture's lines, parsed: the prompt, then each call and its result."""
    with open(AGENTS, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


PROMPT, LAUNCH_CALL, LAUNCH_RESULT, WAITED_CALL, WAITED_RESULT, FAILED_CALL, FAILED_RESULT = fixture_lines()


def dumped(*lines):
    return [json.dumps(line, separators=(",", ":")) for line in lines]


def launch(**result_fields):
    """The background launch's two lines, with the result line's fields replaced by `result_fields`
    (None removes one)."""
    call, result = copy.deepcopy(LAUNCH_CALL), copy.deepcopy(LAUNCH_RESULT)
    for key, value in result_fields.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return call, result


class AgentInfo(StartPath):

    def write(self, *lines):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in dumped(PROMPT, *lines)))

    def append(self, *raw):
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in raw))

    def row(self, agent_id):
        return next(agent for agent in self.state()["agents"] if agent["id"] == agent_id)

    def infos(self):
        return [e for e in board.events(self.folder) if e["kind"] == "agent-info"]

    def read(self, *lines):
        """The events the reader finds in a transcript of the prompt and exactly these lines."""
        self.write(*lines)
        return board_hook.read_transcript(self.transcript, set())[1]


class Recovered(AgentInfo):

    def test_a_launch_whose_hook_ran_before_the_board_is_on_the_row_the_first_subagent_starts(self):
        self.write(LAUNCH_CALL, LAUNCH_RESULT)
        self.assertIsNone(self.handled(fixture("PostToolUse-Agent")))
        self.assertFalse(board.is_board(self.folder))
        self.assertIsNone(self.handled(fixture("SubagentStart")), "a subagent start prints nothing")
        self.assertTrue(board.is_board(self.folder))
        agent = self.row(BACKGROUND)
        self.assertEqual(
            (agent["type"], agent["model"], agent["description"]), ("general-purpose", MODEL, "Simple reply task"),
        )
        [info] = self.infos()
        self.assertEqual((info["use"], info["early"]), (LAUNCH, True))

    def test_a_launch_the_transcript_gains_after_the_start_is_caught_up_by_a_later_event(self):
        self.write()
        self.handled(fixture("PostToolUse-Agent"))
        self.handled(fixture("SubagentStart"))
        self.assertIsNone(self.row(BACKGROUND)["model"])
        self.write(LAUNCH_CALL, LAUNCH_RESULT)
        self.handled(fixture("Stop"))
        agent = self.row(BACKGROUND)
        self.assertEqual((agent["model"], agent["description"]), (MODEL, "Simple reply task"))

    def test_a_recovered_launch_does_not_stop_a_later_catch_up_at_itself(self):
        # Read back at the start, the launch is `early`: a task-list call made before it, whose result
        # reached the transcript only afterwards, is still one the board never saw.
        create_call, create_result = transcript_lines((1, "alpha"))
        self.write(LAUNCH_CALL, LAUNCH_RESULT)
        self.append(create_call)
        self.handled(fixture("SubagentStart"))
        self.assertEqual(self.row(BACKGROUND)["model"], MODEL)
        self.append(create_result)
        self.handled(fixture("Stop"))
        self.assertEqual([s["subject"] for s in self.state()["steps"]], ["alpha"])


class NeverTwice(AgentInfo):

    def setUp(self):
        super().setUp()
        self.write()
        self.handled(fixture("SubagentStart"))

    def test_a_launch_the_hook_recorded_and_the_transcript_holds_is_one_event_the_hooks(self):
        self.handled(fixture("PostToolUse-Agent"))
        self.write(LAUNCH_CALL, LAUNCH_RESULT)
        for _ in range(board_hook.CATCH_UP_LIMIT):
            self.handled(fixture("Stop"))
        [info] = self.infos()
        self.assertEqual(info.get("use"), LAUNCH)
        self.assertNotIn("early", info)

    def test_the_catch_up_stops_at_a_launch_the_hook_recorded(self):
        # A call after one a hook recorded was made while the board existed, and is never read back.
        self.handled(fixture("PostToolUse-Agent"))
        self.write(LAUNCH_CALL, LAUNCH_RESULT)
        self.append(*transcript_lines((1, "alpha")))
        self.handled(fixture("Stop"))
        self.assertEqual(self.state()["steps"], [])


class Refused(AgentInfo):

    def test_the_fixture_yields_the_launch_and_the_waited_call_and_not_the_failed_one(self):
        found = self.read(LAUNCH_CALL, LAUNCH_RESULT, WAITED_CALL, WAITED_RESULT, FAILED_CALL, FAILED_RESULT)
        self.assertEqual(found, [
            ("agent-info", {"id": BACKGROUND, "model": MODEL, "description": "Simple reply task",
                            "use": LAUNCH, "early": True}),
            ("agent-info", {"id": FOREGROUND, "model": MODEL, "description": "Count the files",
                            "use": WAITED, "early": True}),
        ])

    def test_a_result_marked_is_error_yields_nothing_even_with_an_agent_id(self):
        call, result = launch()
        result["message"]["content"][0]["is_error"] = True
        self.assertEqual(self.read(call, result), [])

    def test_a_launch_inside_a_subagent_yields_nothing(self):
        call, result = launch(isSidechain=True)
        call["isSidechain"] = True
        self.assertEqual(self.read(call, result), [])

    def test_a_launch_payload_from_inside_a_subagent_records_nothing(self):
        self.write()
        inside = dict(fixture("PostToolUse-Agent"), agent_id="a0000000000000001", agent_type="general-purpose")
        self.assertIsNone(self.handled(inside))
        self.assertFalse(board.is_board(self.folder))
        self.handled(fixture("SubagentStart"))
        self.assertIsNone(self.handled(inside))
        self.assertEqual(self.infos(), [])


class TaskIsAgent(AgentInfo):

    def test_a_task_call_is_read_as_an_agent_call(self):
        call, result = launch()
        call["message"]["content"][0]["name"] = "Task"
        self.assertEqual(self.read(call, result), self.read(*launch()))
        self.assertEqual(len(self.read(call, result)), 1)


class OddResults(AgentInfo):

    def test_a_result_of_another_shape_raises_nothing_and_yields_nothing(self):
        no_id = {key: value for key, value in LAUNCH_RESULT["toolUseResult"].items() if key != "agentId"}
        for result in ("Launched.", 3, None, no_id, [BACKGROUND]):
            with self.subTest(result=result):
                self.assertEqual(self.read(*launch(toolUseResult=result)), [])


class Derived(unittest.TestCase):
    """What the state makes of an `agent-info` read back, which is applied before everything a hook recorded."""

    def agent(self, *events):
        state = board.derive([event("start", 0, session="s", title="t"), *events], [], board.settings({}))
        [agent] = state["agents"]
        return agent

    def test_the_row_starts_when_the_subagent_started_not_when_its_launch_was_read_back(self):
        agent = self.agent(
            event("agent-start", 3, id=BACKGROUND, type="general-purpose"),
            event("agent-info", 5, id=BACKGROUND, model=MODEL, description="Simple reply task", use=LAUNCH, early=True),
        )
        self.assertEqual(agent["started"], board.utc(at(3)))
        self.assertEqual((agent["type"], agent["model"]), ("general-purpose", MODEL))

    def test_a_launch_read_back_never_overwrites_what_a_hook_recorded(self):
        agent = self.agent(
            event("agent-start", 1, id=BACKGROUND, type="general-purpose"),
            event("agent-info", 2, id=BACKGROUND, model="hooked-model", description="From the hook", use="toolu_a"),
            event("agent-info", 4, id=BACKGROUND, model=MODEL, description="Read back", use="toolu_b", early=True),
        )
        self.assertEqual((agent["model"], agent["description"]), ("hooked-model", "From the hook"))


if __name__ == "__main__":
    unittest.main()
