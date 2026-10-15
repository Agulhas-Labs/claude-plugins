"""Tests for handoff-gates.py. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
spec = importlib.util.spec_from_file_location("gates", os.path.join(ROOT, "hooks", "handoff-gates.py"))
gates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gates)

with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
    NAME = json.load(f)["name"]
ENV = {"CLAUDE_PLUGIN_ROOT": ROOT}
GATED = "Do it.\nG1 it works — CHECK: `make test` — EXPECT: OK\n"
BARE = "Do it, carefully, and tell me when done."


def use(tool_id, subagent_type, prompt, **extra):
    block = {"type": "tool_use", "id": tool_id, "name": "Agent",
             "input": dict({"subagent_type": subagent_type, "prompt": prompt}, **extra)}
    return {"type": "assistant", "message": {"content": [block]}}


def result(tool_id, content):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]}}


class HandoffGatesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gates-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.transcript = os.path.join(self.dir, "session.jsonl")
        self.write([])

    def write(self, entries):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def file(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def call(self, prompt=BARE, rung="builder", env=ENV, **payload_extra):
        payload = dict({"transcript_path": self.transcript, "tool_name": "Agent",
                        "tool_input": {"description": "d", "prompt": prompt, "subagent_type": f"{NAME}:{rung}"}},
                       **payload_extra)
        return gates.decision(payload, env)

    def reason(self, out):
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_a_gateless_handoff_is_denied_with_the_tag_and_the_way_out(self):
        for rung in ("builder", "builder-lite"):
            reason = self.reason(self.call(rung=rung))
            self.assertIn("[delegate: handoff gates]", reason)
            self.assertIn(f"{NAME}:{rung}", reason)
            self.assertIn("CHECK:", reason)
            self.assertIn("re-issue the identical call", reason)

    def test_a_gate_line_leaves_the_call_alone(self):
        self.assertIsNone(self.call(GATED))
        self.assertIsNone(self.call("G12 outcome — CHECK: x — EXPECT: y", rung="builder-lite"))

    def test_a_check_without_a_gate_number_is_not_a_gate_line(self):
        self.reason(self.call("Goal\nCHECK: run it\nG\nG1 but no marker here"))

    def test_a_gate_in_a_named_file_counts(self):
        path = self.file("handoff.md", GATED)
        self.assertIsNone(self.call(f"Read {path} and do it."))
        self.assertIsNone(self.call(f"Handoff: `{path}`."))

    def test_a_gateless_named_file_or_a_missing_one_does_not(self):
        path = self.file("handoff.txt", "just prose")
        self.reason(self.call(f"Read {path}."))
        self.reason(self.call(f"Read {self.dir}/absent.md."))

    def test_a_relative_path_or_other_extension_is_not_read(self):
        self.file("rel.md", GATED)
        other = self.file("handoff.json", GATED)
        self.reason(self.call("Read rel.md"))
        self.reason(self.call(f"Read {other}"))

    def test_a_tilde_path_is_read(self):
        home = os.path.join(self.dir, "home")
        os.mkdir(home)
        with open(os.path.join(home, "h.md"), "w", encoding="utf-8") as f:
            f.write(GATED)
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None else os.environ.pop("HOME"))
        self.assertIsNone(self.call("Read ~/h.md"))

    def test_a_file_over_the_size_cap_is_not_read(self):
        path = self.file("big.md", GATED + "x" * gates.FILE_MAX)
        self.reason(self.call(f"Read {path}"))

    def test_only_the_first_five_files_are_read(self):
        plain = [self.file(f"p{i}.md", "prose") for i in range(5)]
        gated = self.file("g.md", GATED)
        self.reason(self.call("Read " + " ".join(plain + [gated])))
        self.assertIsNone(self.call("Read " + " ".join(plain[:4] + [gated])))

    def test_the_same_call_is_allowed_after_a_refusal_in_the_transcript(self):
        reason = self.reason(self.call())
        self.write([use("t1", f"{NAME}:builder", BARE),
                    result("t1", f"PreToolUse:Agent hook error: {reason}")])
        self.assertIsNone(self.call())

    def test_a_result_given_as_text_blocks_counts(self):
        reason = self.reason(self.call())
        self.write([use("t1", f"{NAME}:builder", BARE), result("t1", [{"type": "text", "text": reason}])])
        self.assertIsNone(self.call())

    def test_the_reissue_may_carry_a_model_the_first_call_lacked(self):
        reason = self.reason(self.call())
        self.write([use("t1", f"{NAME}:builder", BARE), result("t1", reason)])
        payload = {"transcript_path": self.transcript, "tool_name": "Agent",
                   "tool_input": {"prompt": BARE, "subagent_type": f"{NAME}:builder", "model": "sonnet"}}
        self.assertIsNone(gates.decision(payload, ENV))

    def test_a_different_prompt_or_type_or_an_untagged_result_is_still_refused(self):
        reason = self.reason(self.call())
        self.write([use("t1", f"{NAME}:builder", BARE + " more"), result("t1", reason),
                    use("t2", f"{NAME}:builder-lite", BARE), result("t2", reason),
                    use("t3", f"{NAME}:builder", BARE), result("t3", "an ordinary report")])
        self.reason(self.call())

    def test_a_result_before_its_call_or_for_another_id_does_not_count(self):
        reason = self.reason(self.call())
        self.write([result("t1", reason), use("t1", f"{NAME}:builder", BARE), result("other", reason)])
        self.reason(self.call())

    def test_a_cut_first_line_in_the_tail_is_skipped(self):
        reason = self.reason(self.call())
        pad = {"type": "user", "message": {"content": "x" * gates.TAIL}}
        self.write([pad, use("t1", f"{NAME}:builder", BARE), result("t1", reason)])
        self.assertIsNone(self.call())

    def test_a_missing_transcript_still_refuses(self):
        os.remove(self.transcript)
        self.reason(self.call())

    def test_other_calls_are_left_alone(self):
        for other in ("general-purpose", "builder", "other-plugin:builder", f"{NAME}:reviewer",
                      f"{NAME}:runner", f"{NAME}:builder-lite-x"):
            payload = {"transcript_path": self.transcript, "tool_name": "Agent",
                       "tool_input": {"prompt": BARE, "subagent_type": other}}
            self.assertIsNone(gates.decision(payload, ENV), other)
        self.assertIsNone(gates.decision({"tool_name": "Read", "tool_input": {"prompt": BARE}}, ENV))

    def test_a_subagent_call_is_left_alone(self):
        self.assertIsNone(self.call(agent_id="a1"))

    def test_the_switch_turns_it_off(self):
        self.assertIsNone(self.call(env=dict(ENV, DELEGATE_HANDOFF_GATES="0")))
        self.reason(self.call(env=dict(ENV, DELEGATE_HANDOFF_GATES="1")))

    def test_it_is_registered_after_model_ceiling_on_agent(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            entries = json.load(f)["hooks"]["PreToolUse"]
        agent = [e for e in entries if e["matcher"] == "Agent"]
        self.assertEqual(len(agent), 1)
        commands = [h["command"] for h in agent[0]["hooks"]]
        self.assertEqual([c.split("hooks/")[1].rstrip('"') for c in commands],
                         ["model-ceiling.sh", "handoff-gates.sh"])


if __name__ == "__main__":
    unittest.main()
