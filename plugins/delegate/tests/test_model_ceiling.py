"""Tests for model-ceiling.py. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HOOK = os.path.join(ROOT, "hooks", "model-ceiling.py")
spec = importlib.util.spec_from_file_location("ceiling", HOOK)
ceiling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ceiling)

with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
    NAME = json.load(f)["name"]
ENV = {"CLAUDE_PLUGIN_ROOT": ROOT}
SONNET, OPUS = "claude-sonnet-4-5", "claude-opus-4-5"


def assistant(model, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "message": {"id": "m", "model": model, "content": []}}


class ModelCeilingTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ceiling-test-")
        self.addCleanup(shutil.rmtree, self.root)
        self.transcript = os.path.join(self.root, "session.jsonl")

    def session(self, *models):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            for model in models:
                f.write(json.dumps(assistant(model)) + "\n")

    def call(self, rung="builder", env=ENV, **extra):
        tool_input = dict({"description": "a slice", "prompt": "build it", "subagent_type": f"{NAME}:{rung}"}, **extra)
        payload = {"session_id": "s", "transcript_path": self.transcript, "cwd": self.root,
                   "tool_name": "Agent", "tool_input": tool_input}
        return ceiling.decision(payload, env)

    def test_a_sonnet_session_runs_the_builder_on_sonnet_and_keeps_the_rest_of_the_call(self):
        self.session(OPUS, SONNET)
        out = self.call(run_in_background=True)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"], {
            "description": "a slice", "prompt": "build it", "subagent_type": f"{NAME}:builder",
            "run_in_background": True, "model": "sonnet",
        })

    def test_a_haiku_session_caps_a_sonnet_rung_at_haiku(self):
        self.session("claude-haiku-4-5")
        self.assertEqual(self.call("mechanic")["hookSpecificOutput"]["updatedInput"]["model"], "haiku")

    def test_an_opus_session_leaves_the_builder_alone(self):
        self.session(SONNET, OPUS)
        self.assertIsNone(self.call())

    def test_a_sonnet_session_leaves_the_runner_alone(self):
        self.session(SONNET)
        self.assertIsNone(self.call("runner"))

    def test_a_call_naming_its_own_model_is_left_alone(self):
        self.session(SONNET)
        self.assertIsNone(self.call(model="opus"))

    def test_a_subagent_type_from_elsewhere_is_left_alone(self):
        self.session(SONNET)
        for other in ("general-purpose", "builder", "other-plugin:builder", f"{NAME}:../builder", f"{NAME}:x/../builder", f"{NAME}:absent"):
            payload = {"transcript_path": self.transcript, "tool_name": "Agent",
                       "tool_input": {"prompt": "p", "subagent_type": other}}
            self.assertIsNone(ceiling.decision(payload, ENV), other)

    def test_the_off_switch_leaves_every_call_alone(self):
        self.session(SONNET)
        self.assertIsNone(self.call(env=dict(ENV, DELEGATE_MODEL_CEILING="0")))
        self.assertIsNotNone(self.call(env=dict(ENV, DELEGATE_MODEL_CEILING="1")))

    def test_an_unknown_session_model_is_left_alone(self):
        self.session("some-other-model")
        self.assertIsNone(self.call())
        os.remove(self.transcript)
        self.assertIsNone(self.call())

    def test_the_session_model_skips_synthetic_and_sidechain_entries(self):
        self.session(SONNET, "<synthetic>")
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(assistant(OPUS, sidechain=True)) + "\n")
        self.assertEqual(self.call()["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")

    def test_on_the_first_turn_the_model_attachment_names_the_session_model(self):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "attachment", "attachment": {"type": "model", "identity": {"modelId": SONNET}}}) + "\n")
        self.assertEqual(self.call()["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(assistant(OPUS)) + "\n")  # a later turn on another model wins
        self.assertIsNone(self.call())

    def test_a_malformed_payload_prints_nothing_and_exits_zero(self):
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=ROOT)
        env.pop("DELEGATE_MODEL_CEILING", None)
        for stdin in ("not json", "[]", "{}", json.dumps({"tool_name": "Agent", "tool_input": "x"})):
            out = subprocess.run(["/bin/sh", os.path.join(ROOT, "hooks", "model-ceiling.sh")], input=stdin,
                                 capture_output=True, encoding="utf-8", env=env)
            self.assertEqual((out.returncode, out.stdout), (0, ""), stdin)

    def test_the_wrapper_applies_the_cap_end_to_end(self):
        self.session(SONNET)
        payload = {"transcript_path": self.transcript, "tool_name": "Agent",
                   "tool_input": {"prompt": "p", "subagent_type": f"{NAME}:reviewer"}}
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=ROOT)
        env.pop("DELEGATE_MODEL_CEILING", None)
        out = subprocess.run(["/bin/sh", os.path.join(ROOT, "hooks", "model-ceiling.sh")], input=json.dumps(payload),
                             capture_output=True, encoding="utf-8", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")

    def test_hooks_json_runs_the_wrapper_before_every_agent_call(self):
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            entries = json.load(f)["hooks"]["PreToolUse"]
        commands = [h["command"] for e in entries if e.get("matcher") == "Agent" for h in e["hooks"]]
        self.assertEqual(commands, ['sh "${CLAUDE_PLUGIN_ROOT}/hooks/model-ceiling.sh"'])


if __name__ == "__main__":
    unittest.main()
