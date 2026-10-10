"""The fixtures are the contract: the state every page reads, and the payloads every hook receives."""
import glob
import json
import os
import re
import unittest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Every key the page may read, by list. A renderer that emits another, or a fixture that drops one,
# has changed the contract and must change the brief with it.
STATE_KEYS = {
    "schema", "session", "title", "titleSource", "started", "updated", "state", "settings",
    "questions", "steps", "commits", "deliverables", "checks", "decisions", "agents",
    "changes", "commands", "commandsTotal", "tokens", "summary",
}
ITEM_KEYS = {
    "settings": {"theme", "accent", "density", "refreshSeconds", "stuckAfterSeconds"},
    "questions": {"id", "text", "default", "affects", "reverse", "hardStop", "status", "answer", "asked", "answered"},
    "steps": {"id", "subject", "status", "updated"},
    "commits": {"hash", "subject", "branch", "time", "step"},
    "deliverables": {"label", "path", "url", "step", "time", "image"},
    "checks": {"id", "proves", "command", "result", "time", "source", "agent"},
    "decisions": {"id", "text", "why", "reverse", "time", "group", "yours", "revised"},
    "agents": {"id", "type", "model", "description", "started", "ended", "outcome", "gates", "tokens"},
    "changes": {"path", "edits", "added", "removed", "created", "first", "last", "agents"},
    "commands": {"command", "description", "result", "exit", "runs", "fails", "time", "ms", "test", "agents",
                 "latestRun"},
}
TIME_KEYS = {"started", "updated", "asked", "answered", "time", "ended", "first", "last", "revised"}
# The words a status may take. The recorder writes no other, and the page has a glyph for each.
WORDS = {
    "state": {"live", "idle", "finished"},
    "step": {"pending", "in_progress", "completed"},
    "check": {"pass", "fail"},
    "agent": {"running", "finished", "failed"},
    "gate": {"pass", "fail", "unknown"},
}


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.state = load("state-live.json")

    def test_top_level_keys_are_the_contract(self):
        self.assertEqual(set(self.state), STATE_KEYS)

    def test_settings_keys(self):
        self.assertEqual(set(self.state["settings"]), ITEM_KEYS["settings"])

    def test_every_list_has_an_item_with_exactly_its_keys(self):
        for name, keys in ITEM_KEYS.items():
            if name == "settings":
                continue
            items = self.state[name]
            self.assertTrue(items, f"{name} is empty, so the reference shows nothing for it")
            for item in items:
                self.assertEqual(set(item), keys, name)

    def test_times_are_utc(self):
        def walk(value, key=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, k)
            elif isinstance(value, list):
                for v in value:
                    walk(v, key)
            elif key in TIME_KEYS and value is not None:
                self.assertRegex(value, UTC, key)
        walk(self.state)

    def test_a_hard_stop_has_no_default(self):
        stops = [q for q in self.state["questions"] if q["hardStop"]]
        self.assertTrue(stops)
        for stop in stops:
            self.assertIsNone(stop["default"])


class Vocabulary(unittest.TestCase):
    def test_every_fixture_uses_only_the_contract_words(self):
        for path in sorted(glob.glob(os.path.join(FIXTURES, "state-*.json"))):
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            self.assertIn(state["state"], WORDS["state"], path)
            for step in state["steps"]:
                self.assertIn(step["status"], WORDS["step"], path)
            for check in state["checks"]:
                self.assertIn(check["result"], WORDS["check"], path)
            for agent in state["agents"]:
                self.assertIn(agent["outcome"], WORDS["agent"], path)
                for gate in agent["gates"]:
                    self.assertIn(gate["result"], WORDS["gate"], path)

    def test_the_page_has_a_glyph_for_every_word_and_no_other(self):
        page = os.path.join(os.path.dirname(FIXTURES), os.pardir, "board", "template.html")
        with open(page, encoding="utf-8") as f:
            text = f.read()
        table = text[text.index("var STATUS = {"):]
        for kind, words in (("board", "state"), ("step", "step"), ("check", "check"), ("agent", "agent")):
            block = table[table.index(kind + ": {"):]
            block = block[:block.index("}")]
            self.assertEqual(set(re.findall(r"^\s*([a-z_]+): \[", block, re.M)), WORDS[words], kind)

    def test_the_recorder_writes_only_contract_outcomes(self):
        recorder = os.path.join(os.path.dirname(FIXTURES), os.pardir, "board", "board.py")
        with open(recorder, encoding="utf-8") as f:
            text = f.read()
        written = set(re.findall(r'"outcome"\]? ?[:=] ?"([a-z]+)"', text)) | set(re.findall(r'else "([a-z]+)"$', "\n".join(l for l in text.splitlines() if '"outcome"' in l), re.M))
        self.assertEqual(written, WORDS["agent"])


class PayloadFixtures(unittest.TestCase):
    def test_every_payload_parses_and_names_its_event(self):
        paths = sorted(glob.glob(os.path.join(FIXTURES, "payloads", "*.json")))
        self.assertGreaterEqual(len(paths), 13)
        for path in paths:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            event = payload["hook_event_name"]
            self.assertTrue(os.path.basename(path).startswith(event), path)
            self.assertEqual(payload["cwd"], "/home/user/project", path)


if __name__ == "__main__":
    unittest.main()
