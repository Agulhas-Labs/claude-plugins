"""Tests for inject-context.sh and the hook wiring. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
HOOK = os.path.join(ROOT, "hooks", "inject-context.sh")
CONTEXT = os.path.join(ROOT, "context")
SH = "/bin/sh"
LIMIT = 10_000  # Claude Code's cap on one hook's additionalContext / a SessionStart cat's stdout
RENDER = os.path.join(ROOT, "hooks", "render-context.sh")
# The plugin directory's validator blocks a hook command that names more than one path or computes one:
# each command is `sh` and one literal script under ${CLAUDE_PLUGIN_ROOT}/hooks/, nothing else.
ONE_SCRIPT = re.compile(r'sh "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[a-z-]+\.sh"')
ASCII_LOCALE = dict(os.environ, LC_ALL="C", LANG="C")


def run(*args, env=None):
    return subprocess.run([SH, HOOK, *args], input="{}", capture_output=True, encoding="utf-8", env=env)


def rendered(path, max_agents=None):
    env = dict(os.environ)
    env.pop("DELEGATE_MAX_CONCURRENT_AGENTS", None)
    if max_agents is not None:
        env["DELEGATE_MAX_CONCURRENT_AGENTS"] = max_agents
    out = subprocess.run([SH, RENDER, path], input="{}", capture_output=True, encoding="utf-8", env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout


def injected(path, env=None):
    out = run(path, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]


def load_hooks():
    with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
        return json.load(f)["hooks"]


def hook_commands(event):
    entries = load_hooks()[event]
    return [hook["command"] for entry in entries for hook in entry["hooks"]]


def all_hook_commands():
    return [command for event in load_hooks() for command in hook_commands(event)]


def run_hook(command, env=None):
    """Run a hooks.json command the way Claude Code does: through sh, with CLAUDE_PLUGIN_ROOT set."""
    env = dict(os.environ if env is None else env, CLAUDE_PLUGIN_ROOT=os.path.abspath(ROOT))
    env.pop("DELEGATE_MAX_CONCURRENT_AGENTS", None)
    out = subprocess.run([SH, "-c", command], input="{}", capture_output=True, encoding="utf-8", env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class HookWiringTests(unittest.TestCase):
    def test_every_hook_command_names_one_literal_script(self):
        for command in all_hook_commands():
            self.assertRegex(command, "^" + ONE_SCRIPT.pattern + "$")
            script = command.split("/hooks/", 1)[1].rstrip('"')
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "hooks", script)), command)

    def test_session_start_prints_the_engineering_conventions_and_renders_the_orchestrator_ones(self):
        commands = hook_commands("SessionStart")
        self.assertEqual(len(commands), 2)
        engineering, orchestrator = (run_hook(c) for c in commands)
        self.assertEqual(engineering, read(os.path.join(CONTEXT, "engineering.md")))
        self.assertIn("# Orchestrator conventions", orchestrator)
        self.assertIn("At most 4 agents at a time", orchestrator)
        self.assertNotIn("{{", orchestrator)

    def test_max_agents_setting_defaults_to_4_and_is_read_from_the_environment(self):
        path = os.path.join(CONTEXT, "orchestrator.md")
        self.assertIn("At most 4 agents at a time", rendered(path))
        self.assertIn("At most 8 agents at a time", rendered(path, "8"))
        self.assertIn("At most 12 agents at a time", rendered(path, "12"))
        for bad in ("", "abc", "0", "-3", "2.5", "08", " 3"):
            self.assertIn("At most 4 agents at a time", rendered(path, bad), repr(bad))

    def test_render_of_a_missing_file_prints_nothing_and_exits_zero(self):
        out = subprocess.run([SH, RENDER, os.path.join(CONTEXT, "absent.md")], input="{}", capture_output=True, encoding="utf-8")
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_subagent_start_injects_engineering_conventions_only_without_python(self):
        commands = hook_commands("SubagentStart")
        self.assertEqual(len(commands), 1)
        payload = json.loads(run_hook(commands[0]))["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "SubagentStart")
        self.assertEqual(payload["additionalContext"], read(os.path.join(CONTEXT, "engineering.md")).strip())
        self.assertNotIn("# Orchestrator conventions", payload["additionalContext"])

    def test_only_the_subagent_hooks_need_python(self):
        for command in hook_commands("SessionStart") + hook_commands("SubagentStart"):
            script = read(os.path.join(ROOT, "hooks", command.split("/hooks/", 1)[1].rstrip('"')))
            self.assertNotIn("run-python", script, command)
        for command in hook_commands("PostToolUse") + hook_commands("SubagentStop"):
            script = read(os.path.join(ROOT, "hooks", command.split("/hooks/", 1)[1].rstrip('"')))
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh"', script, command)

    def test_every_injected_file_or_payload_fits_under_the_hook_context_cap(self):
        for command in hook_commands("SessionStart"):
            self.assertLess(len(run_hook(command)), LIMIT, command)
        for command in hook_commands("SubagentStart"):
            self.assertLess(len(json.loads(run_hook(command))["hookSpecificOutput"]["additionalContext"]), LIMIT)


class JsonEscapingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="inject-context-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, text, name="conventions.md"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_round_trips_quotes_backslashes_tabs_returns_and_non_ascii(self):
        text = ('# Rules\n\nsay "hi" \\ back\\slash\ttab %s &and é × {"k": "v\\n"}\r\n'
                "100% $HOME `code` 'single'\n\nlast line, no newline")
        payload = injected(self.write(text))
        self.assertEqual(payload["additionalContext"], text)

    def test_drops_the_control_characters_json_forbids(self):
        payload = injected(self.write("a\x00b\x0cc\x1bd\n"))
        self.assertEqual(payload["additionalContext"], "abcd")

    def test_works_under_an_ascii_locale(self):
        real = os.path.join(CONTEXT, "engineering.md")
        self.assertEqual(injected(real, env=ASCII_LOCALE)["additionalContext"], read(real).strip())
        text = "é × — “quoted”\n"
        self.assertEqual(injected(self.write(text), env=ASCII_LOCALE)["additionalContext"], text.strip())

    def test_needs_nothing_on_the_path_but_sh_sed_awk_and_tr(self):
        bindir = os.path.join(self.dir, "bin")
        os.mkdir(bindir)
        for tool in ("sed", "awk", "tr"):
            os.symlink(shutil.which(tool), os.path.join(bindir, tool))
        real = os.path.join(CONTEXT, "engineering.md")
        payload = injected(real, env={"PATH": bindir})
        self.assertEqual(payload["additionalContext"], read(real).strip())

    def test_missing_empty_or_unnamed_file_prints_nothing_and_exits_zero(self):
        for args in ((os.path.join(self.dir, "absent.md"),), (self.write("", "empty.md"),), (self.write("\n\n", "blank.md"),), ()):
            out = run(*args)
            self.assertEqual(out.returncode, 0, args)
            self.assertEqual(out.stdout, "", args)


if __name__ == "__main__":
    unittest.main()
