"""Tests for the handoff written without the session's model, and the pointer left for /clear.

Run: python3 -m unittest discover -s plugins/cache-guard/tests

Nothing here starts a real `claude` or a real detached process: the two places one would start are
module-level functions, and every test replaces them. Nothing is written outside its own temp
directory: the state directory and the handoff directory are both given in the environment.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
sys.path.insert(0, os.path.abspath(HOOKS))

import cache_guard  # noqa: E402
import handoff  # noqa: E402
import session_start  # noqa: E402

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


def user(text, **extra):
    entry = {"type": "user", "cwd": "/work/project", "gitBranch": "feat/x",
             "message": {"role": "user", "content": text}}
    entry.update(extra)
    return entry


def user_blocks(blocks, **extra):
    entry = {"type": "user", "cwd": "/work/project", "gitBranch": "feat/x",
             "message": {"role": "user", "content": blocks}}
    entry.update(extra)
    return entry


def assistant(blocks, **extra):
    entry = {"type": "assistant", "cwd": "/work/project", "gitBranch": "feat/x",
             "message": {"role": "assistant", "model": "claude-opus-5", "content": blocks}}
    entry.update(extra)
    return entry


def text_block(text):
    return {"type": "text", "text": text}


def tool_use(name, arguments):
    return {"type": "tool_use", "id": "t1", "name": name, "input": arguments}


def tool_result(content):
    return {"type": "tool_result", "tool_use_id": "t1", "content": content}


def compact_summary(text):
    return {"type": "user", "isCompactSummary": True, "cwd": "/work/project",
            "message": {"role": "user", "content": text}}


LONG_REQUEST = "fix the parser: " + "it drops the last field of a long row. " * 60  # past the 2000-char floor

HANDOFF_REPLY = (
    "## Goal\n\nFix the parser.\n\n## Decisions taken\n\nSplit on tabs.\n\n## Current state\n\n"
    "Committed.\n\n## Files and branches\n\n- a.py\n\n## What is left\n\nTests.\n\n## Next step\n\n"
    "Run them.\n\n## Things to be careful of\n\nThe old fixture.\n"
)


class FakeClaude:
    """A `claude` that never starts: it records the stdin it was given and answers what a test chose."""

    def __init__(self, stdout="", returncode=0, timeouts=0, on_reap=None):
        self.stdout = stdout
        self.returncode = returncode
        self.timeouts = timeouts
        self.on_reap = on_reap  # what the call that reaps a killed process does, when it does not return
        self.calls = []
        self.killed = False
        self.pid = 4242

    def communicate(self, text=None, timeout=None):
        self.calls.append((text, timeout))
        if self.calls[:-1] and self.on_reap:
            raise self.on_reap
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return self.stdout, ""

    def kill(self):
        self.killed = True


META = {
    "cwd": "/work/project",
    "branch": "feat/x",
    "requests": ["make the thing"],
    "last_assistant": "done",
    "files_edited": ["/work/project/a.py"],
    "todos": [{"content": "ship it", "status": "pending"}],
}


class HandoffTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-handoff-test-")
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.handoffs = os.path.join(self.tmp.name, "handoffs")
        self.env = {
            "CACHE_GUARD_STATE_DIR": self.state,
            "CACHE_GUARD_HANDOFF_DIR": self.handoffs,
            "PATH": "/nowhere",
        }
        self.launched = []

    def transcript(self, entries, name="transcript.jsonl"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return path

    def condense(self, entries):
        return handoff.condense(self.transcript(entries))

    def patch(self, name, value):
        patcher = mock.patch.object(handoff, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def with_a_summariser(self):
        """`claude` is on the PATH and the detached run is recorded rather than started."""
        self.patch("find_claude", lambda env: "/usr/bin/claude")
        self.patch("launch_detached", lambda argv, env: self.launched.append((argv, env)))

    def written_handoffs(self):
        return sorted(name for name in os.listdir(self.handoffs) if name.endswith(".md"))

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()


class CondenseTests(HandoffTestCase):
    def test_the_conversation_is_rendered_in_order_with_a_line_for_each_kind(self):
        text, _ = self.condense([
            user("fix the parser"),
            assistant([text_block("Looking at it."), tool_use("Read", {"file_path": "/a.py"})]),
            user_blocks([tool_result("line one\nline two")]),
            assistant([text_block("Fixed.")]),
        ])
        self.assertEqual(
            text.strip().split("\n\n"),
            [
                "USER: fix the parser",
                "ASSISTANT: Looking at it.",
                'TOOL Read: {"file_path": "/a.py"}',
                "RESULT: line one line two",
                "ASSISTANT: Fixed.",
            ],
        )

    def test_a_long_tool_result_is_cut_to_three_hundred_characters(self):
        text, _ = self.condense([user_blocks([tool_result("r" * 10_000)])])
        self.assertIn("RESULT: " + "r" * 300 + "\n", text)
        self.assertNotIn("r" * 301, text)

    def test_a_long_user_message_is_cut_to_four_thousand_characters(self):
        text, meta = self.condense([user("u" * 10_000)])
        self.assertIn("USER: " + "u" * 4000 + "\n", text)
        self.assertNotIn("u" * 4001, text)
        self.assertEqual(meta["requests"], ["u" * 1500])

    def test_a_long_assistant_message_is_cut_to_four_thousand_characters(self):
        text, meta = self.condense([assistant([text_block("a" * 10_000)])])
        self.assertIn("ASSISTANT: " + "a" * 4000 + "\n", text)
        self.assertNotIn("a" * 4001, text)
        self.assertEqual(meta["last_assistant"], "a" * 4000)

    def test_a_tool_call_is_one_line_of_two_hundred_characters(self):
        text, _ = self.condense([assistant([tool_use("Bash", {"command": "echo " + "z" * 5_000})])])
        line = [line for line in text.split("\n") if line.startswith("TOOL ")][0]
        self.assertEqual(len(line), len("TOOL Bash: ") + 200)

    def test_a_subagents_turns_are_not_part_of_this_conversation(self):
        text, meta = self.condense([
            user("do the work"),
            user("subagent prompt", isSidechain=True),
            assistant([text_block("subagent answer")], isSidechain=True),
            assistant([text_block("done")]),
        ])
        self.assertNotIn("subagent", text)
        self.assertEqual(meta["requests"], ["do the work"])
        self.assertEqual(meta["last_assistant"], "done")

    def test_command_output_and_command_names_are_noise_rather_than_requests(self):
        text, meta = self.condense([
            user("<local-command-stdout>Set model to `Opus 5`</local-command-stdout>"),
            user("<command-name>/clear</command-name>"),
            user("the real request"),
        ])
        self.assertNotIn("local-command", text)
        self.assertNotIn("command-name", text)
        self.assertEqual(meta["requests"], ["the real request"])

    def test_the_rendering_restarts_at_the_last_compaction_with_its_summary_in_full(self):
        summary = "Everything that happened before. " * 10
        text, meta = self.condense([
            user("the very first request"),
            assistant([text_block("ancient history")]),
            compact_summary("an earlier summary that was itself compacted away"),
            assistant([text_block("more history")]),
            compact_summary(summary),
            user("carry on from here"),
            assistant([text_block("carrying on")]),
        ])
        self.assertIn("## Summary of the conversation before this point", text)
        self.assertIn(summary, text)
        self.assertNotIn("the very first request", text)
        self.assertNotIn("ancient history", text)
        self.assertNotIn("more history", text)
        self.assertNotIn("an earlier summary", text)
        self.assertEqual(meta["requests"], ["carry on from here"])

    def test_an_unparsable_line_and_a_blank_line_are_skipped(self):
        path = os.path.join(self.tmp.name, "broken.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json at all\n\n")
            f.write(json.dumps(user("still here")) + "\n")
        text, _ = handoff.condense(path)
        self.assertIn("USER: still here", text)


class MetaTests(HandoffTestCase):
    def test_the_facts_a_handoff_is_built_from_are_collected_in_order(self):
        _, meta = self.condense([
            user("first request"),
            assistant([
                text_block("working"),
                tool_use("Edit", {"file_path": "/work/project/a.py", "old_string": "x"}),
                tool_use("Write", {"file_path": "/work/project/b.py"}),
                tool_use("Edit", {"file_path": "/work/project/a.py", "old_string": "y"}),
                tool_use("NotebookEdit", {"notebook_path": "/work/project/c.ipynb"}),
                tool_use("Read", {"file_path": "/work/project/never-edited.py"}),
            ]),
            user_blocks([tool_result("ok")]),
            user("second request"),
            assistant([
                tool_use("TodoWrite", {"todos": [{"content": "old", "status": "completed"}]}),
                tool_use("TodoWrite", {"todos": [
                    {"content": "ship it", "status": "in_progress"},
                    {"content": "then rest", "status": "pending"},
                ]}),
                text_block("stopped here"),
            ]),
        ])
        self.assertEqual(meta["cwd"], "/work/project")
        self.assertEqual(meta["branch"], "feat/x")
        self.assertEqual(meta["requests"], ["first request", "second request"])
        self.assertEqual(meta["last_assistant"], "stopped here")
        self.assertEqual(
            meta["files_edited"],
            ["/work/project/a.py", "/work/project/b.py", "/work/project/c.ipynb"],
        )
        self.assertEqual(
            meta["todos"],
            [{"content": "ship it", "status": "in_progress"}, {"content": "then rest", "status": "pending"}],
        )

    def test_the_branch_last_seen_is_the_one_reported(self):
        _, meta = self.condense([
            user("on main"),
            assistant([text_block("switching")], gitBranch="feat/later"),
        ])
        self.assertEqual(meta["branch"], "feat/later")


class DocumentTests(HandoffTestCase):
    def test_the_extracted_document_has_a_section_for_each_kind_of_fact(self):
        document = handoff.extracted_document(META, NOW)
        self.assertIn("# Handoff — 2026-01-02 12:00 UTC", document)
        self.assertIn("Working directory: /work/project", document)
        self.assertIn("Branch: feat/x", document)
        self.assertIn("## What was asked\n\n1. make the thing", document)
        self.assertIn("## Where it stopped\n\ndone", document)
        self.assertIn("## Files edited\n\n- /work/project/a.py", document)
        self.assertIn("## Open todos\n\n- ship it (pending)", document)
        self.assertIn("extracted from the transcript by a script, without a model", document)

    def test_an_empty_conversation_still_produces_every_section(self):
        empty = {"cwd": "", "branch": "", "requests": [], "last_assistant": "", "files_edited": [],
                 "todos": []}
        document = handoff.extracted_document(empty, NOW)
        for heading in ("## What was asked", "## Where it stopped", "## Files edited", "## Open todos"):
            self.assertIn(heading, document)
        self.assertIn("Working directory: unknown", document)
        self.assertIn("No file was edited.", document)


class WriteHandoffTests(HandoffTestCase):
    def conversation(self):
        """Long enough to be worth summarising: a shorter one is handed off without a model."""
        return self.transcript([
            user(LONG_REQUEST),
            assistant([text_block("Looking."), tool_use("Edit", {"file_path": "/work/project/a.py"})]),
            user_blocks([tool_result("ok")]),
            assistant([text_block("Fixed the parser.")]),
        ])

    def payload(self, path=None):
        return {"session_id": "s-1", "transcript_path": path or self.conversation(), "prompt": "handoff"}

    def fake_condense(self, text, meta=None):
        return mock.patch.object(handoff, "condense", lambda path: (text, dict(meta or META)))

    def test_the_file_is_written_at_once_and_the_summariser_started_detached(self):
        self.with_a_summariser()
        result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(self.written_handoffs(), ["20260102-120000.md"])
        self.assertEqual(result["path"], os.path.join(self.handoffs, "20260102-120000.md"))
        document = self.read(result["path"])
        self.assertIn("Summary: being written by haiku in the background.", document)
        self.assertIn("## What was asked\n\n1. fix the parser", document)
        self.assertIn("## Files edited\n\n- /work/project/a.py", document)

        self.assertEqual(len(self.launched), 1)
        argv, env = self.launched[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], os.path.abspath(handoff.__file__))
        self.assertEqual(argv[2], "--summarise")
        self.assertEqual(argv[4], result["path"])
        self.assertEqual(argv[5], "haiku")
        self.assertEqual(env["CACHE_GUARD_DISABLE"], "1")
        condensed = self.read(argv[3])
        self.assertEqual(os.path.dirname(argv[3]), self.state)
        self.assertIn("USER: fix the parser", condensed)
        self.assertIn("ASSISTANT: Fixed the parser.", condensed)

    def test_the_cheap_model_summarises_up_to_a_hundred_and_fifty_thousand_tokens(self):
        self.with_a_summariser()
        with self.fake_condense("x" * (150_000 * 4)):
            result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(result["est_tokens"], 150_000)
        self.assertEqual(result["summary_model"], "haiku")
        self.assertEqual(result["est_cost"], 0.15)  # 150,000 tokens at $1.00/MTok

    def test_one_token_more_moves_the_summary_to_the_larger_model(self):
        self.with_a_summariser()
        with self.fake_condense("x" * (150_001 * 4)):
            result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(result["est_tokens"], 150_001)
        self.assertEqual(result["summary_model"], "sonnet")
        self.assertAlmostEqual(result["est_cost"], 0.300002)  # at $2.00/MTok

    def test_the_model_can_be_named_and_an_unknown_one_is_not_priced(self):
        self.with_a_summariser()
        env = dict(self.env, CACHE_GUARD_HANDOFF_MODEL="sonnet")
        result = handoff.write_handoff(self.payload(), NOW, env)
        self.assertEqual(result["summary_model"], "sonnet")
        self.assertIsNotNone(result["est_cost"])
        self.assertEqual(self.launched[0][0][5], "sonnet")

        env = dict(self.env, CACHE_GUARD_HANDOFF_MODEL="skunkworks-preview")
        result = handoff.write_handoff(self.payload(), NOW + timedelta(seconds=1), env)
        self.assertEqual(result["summary_model"], "skunkworks-preview")
        self.assertIsNone(result["est_cost"])

    def test_the_summary_can_be_switched_off(self):
        self.with_a_summariser()
        env = dict(self.env, CACHE_GUARD_HANDOFF_SUMMARY="0")
        result = handoff.write_handoff(self.payload(), NOW, env)
        self.assertEqual(self.launched, [])
        self.assertIsNone(result["summary_model"])
        self.assertEqual(result["no_summary_reason"], "switched off")
        self.assertNotIn("Summary:", self.read(result["path"]))
        self.assertIn("## Where it stopped", self.read(result["path"]))
        self.assertEqual(os.listdir(self.state) if os.path.isdir(self.state) else [], [])

    def test_without_claude_on_the_path_the_extracted_handoff_is_the_whole_of_it(self):
        self.patch("find_claude", lambda env: None)
        self.patch("launch_detached", lambda argv, env: self.fail("started a process"))
        result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertIsNone(result["summary_model"])
        self.assertEqual(result["no_summary_reason"], "claude not found on PATH")
        self.assertNotIn("Summary:", self.read(result["path"]))

    def test_the_directory_defaults_to_the_working_directory_of_the_session(self):
        self.with_a_summariser()
        env = {"CACHE_GUARD_STATE_DIR": self.state, "PATH": "/nowhere"}
        payload = dict(self.payload(), cwd=os.path.join(self.tmp.name, "repo"))
        result = handoff.write_handoff(payload, NOW, env)
        self.assertEqual(
            result["path"],
            os.path.join(self.tmp.name, "repo", ".claude", "handoffs", "20260102-120000.md"),
        )

    def test_a_session_too_short_to_be_worth_a_summary_gets_none(self):
        self.with_a_summariser()
        with self.fake_condense("x" * 1999):
            result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(self.launched, [])
        self.assertIsNone(result["summary_model"])
        self.assertIn("too short", result["no_summary_reason"])
        self.assertNotIn("Summary:", self.read(result["path"]))
        self.assertIn("## Where it stopped", self.read(result["path"]))

    def test_one_character_more_is_worth_summarising(self):
        self.with_a_summariser()
        with self.fake_condense("x" * 2000):
            result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(result["summary_model"], "haiku")
        self.assertIsNone(result["no_summary_reason"])

    def test_a_child_that_finishes_first_keeps_its_summary(self):
        """The pending line is on disk before the child starts, so the parent never writes over it."""
        def launcher(argv, env):
            self.launched.append((argv, env))
            with mock.patch.object(handoff, "open_claude", lambda a: FakeClaude(stdout=HANDOFF_REPLY)):
                handoff.summarise(argv[3], argv[4], argv[5])

        self.patch("find_claude", lambda env: "/usr/bin/claude")
        self.patch("launch_detached", launcher)
        result = handoff.write_handoff(self.payload(), NOW, self.env)
        document = self.read(result["path"])
        self.assertIn("Summary written by haiku.", document)
        self.assertNotIn("being written", document)
        self.assertIn("## Extracted from the transcript", document)

    def test_a_launch_that_fails_leaves_no_promise_of_a_summary(self):
        def launcher(argv, env):
            raise OSError("no fork for you")

        self.patch("find_claude", lambda env: "/usr/bin/claude")
        self.patch("launch_detached", launcher)
        result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertEqual(result["no_summary_reason"], "the summariser could not be started")
        self.assertNotIn("Summary:", self.read(result["path"]))
        self.assertEqual(os.listdir(self.state), [])

    def test_a_transcript_too_long_to_summarise_keeps_the_first_request_and_the_end(self):
        self.with_a_summariser()
        body = "USER: first request\n\n" + "m" * (800_001 * 4) + "THE-VERY-END\n"
        meta = dict(META, requests=["first request", "and then this"])
        with self.fake_condense(body, meta):
            result = handoff.write_handoff(self.payload(), NOW, self.env)
        self.assertLessEqual(result["est_tokens"], 800_000)
        condensed = self.read(self.launched[0][0][3])
        self.assertTrue(condensed.startswith("USER: first request"))
        self.assertIn("earlier turns dropped", condensed)
        self.assertTrue(condensed.endswith("THE-VERY-END\n"))
        self.assertEqual(len(condensed), 800_000 * 4)


class HandoffDirectoryTests(HandoffTestCase):
    """The default place for a handoff is inside the user's repository, and it stays out of its commits."""

    def payload(self, root):
        return {"session_id": "s-1", "cwd": root, "prompt": "handoff",
                "transcript_path": self.transcript([user(LONG_REQUEST), assistant([text_block("done")])])}

    def default_env(self):
        return {"CACHE_GUARD_STATE_DIR": self.state, "PATH": "/nowhere"}

    def write_in(self, root, env=None):
        self.with_a_summariser()
        return handoff.write_handoff(self.payload(root), NOW, env or self.default_env())

    def test_the_directory_it_creates_ignores_itself(self):
        root = os.path.join(self.tmp.name, "repo")
        result = self.write_in(root)
        gitignore = os.path.join(os.path.dirname(result["path"]), ".gitignore")
        text = self.read(gitignore)
        self.assertIn("*\n", text)
        self.assertIn("conversation content", text)
        self.assertIn("delete this file", text)

    def test_a_gitignore_already_there_is_left_exactly_as_it_was(self):
        directory = os.path.join(self.tmp.name, "existing")
        os.makedirs(directory)
        gitignore = os.path.join(directory, ".gitignore")
        with open(gitignore, "w", encoding="utf-8") as f:
            f.write("!keep-me.md\n")
        handoff.ensure_gitignore(directory)
        self.assertEqual(self.read(gitignore), "!keep-me.md\n")

    def test_a_directory_that_was_already_there_is_not_furnished_again(self):
        root = os.path.join(self.tmp.name, "repo")
        directory = os.path.join(root, ".claude", "handoffs")
        os.makedirs(directory)
        self.write_in(root)
        self.assertFalse(os.path.exists(os.path.join(directory, ".gitignore")))

    def test_a_directory_the_user_named_is_left_to_them(self):
        result = self.write_in(os.path.join(self.tmp.name, "repo"), env=self.env)
        self.assertEqual(os.path.dirname(result["path"]), self.handoffs)
        self.assertFalse(os.path.exists(os.path.join(self.handoffs, ".gitignore")))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_directory_is_readable_only_by_its_owner(self):
        result = self.write_in(os.path.join(self.tmp.name, "repo"))
        directory = os.path.dirname(result["path"])
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(result["path"]).st_mode & 0o777, 0o600)


class DetachTests(HandoffTestCase):
    """A child that is meant to outlive the hook is detached the way each platform detaches one."""

    def popen_kwargs(self, platform):
        self.patch("PLATFORM", platform)
        with mock.patch.object(handoff.subprocess, "Popen") as popen:
            handoff.launch_detached(["claude"], {"PATH": "/nowhere"})
        return popen.call_args[1]

    def test_on_windows_the_child_is_detached_by_creation_flags(self):
        kwargs = self.popen_kwargs("nt")
        self.assertNotIn("start_new_session", kwargs)
        self.assertEqual(
            kwargs["creationflags"],
            handoff.DETACHED_PROCESS | handoff.CREATE_NEW_PROCESS_GROUP,
        )
        self.assertTrue(kwargs["creationflags"] or os.name != "nt")  # the flags exist where they mean something

    def test_on_posix_the_child_is_detached_by_its_own_session(self):
        kwargs = self.popen_kwargs("posix")
        self.assertNotIn("creationflags", kwargs)
        self.assertIs(kwargs["start_new_session"], True)


class SummariseTests(HandoffTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.state)
        os.makedirs(self.handoffs)
        self.prepare()

    def prepare(self):
        """The two files the detached half starts from: the condensed transcript and the handoff."""
        self.condensed_path = os.path.join(self.state, "condensed-abc.txt")
        with open(self.condensed_path, "w", encoding="utf-8") as f:
            f.write("USER: fix the parser\n\nASSISTANT: Fixed it.\n")
        self.out_path = os.path.join(self.handoffs, "20260102-120000.md")
        document = handoff.extracted_document(META, NOW)
        with open(self.out_path, "w", encoding="utf-8") as f:
            f.write(handoff.with_summary_line(document, "Summary: being written by haiku in the background."))

    def summarise_with(self, process):
        """The detached half against a `claude` that never starts. Returns the argv it would have used."""
        opened = []

        def opener(argv):
            opened.append(argv)
            return process

        with mock.patch.object(handoff, "open_claude", opener):
            handoff.summarise(self.condensed_path, self.out_path, "haiku")
        return opened[0] if opened else None

    def test_a_summary_is_written_above_the_extracted_sections(self):
        process = FakeClaude(stdout=HANDOFF_REPLY)
        argv = self.summarise_with(process)

        document = self.read(self.out_path)
        self.assertTrue(document.startswith("# Handoff — 2026-01-02 12:00 UTC"))
        self.assertIn("Summary written by haiku.", document)
        self.assertNotIn("Summary: being written", document)
        self.assertLess(document.index("Fix the parser."), document.index("## Extracted from the transcript"))
        self.assertLess(document.index("## Extracted from the transcript"), document.index("## What was asked"))
        self.assertIn("## Files edited\n\n- /work/project/a.py", document)
        self.assertFalse(os.path.exists(self.condensed_path))

        text, timeout = process.calls[0]
        self.assertEqual(argv[:4], ["claude", "-p", "--model", "haiku"])
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--bare", argv)
        self.assertIn("handoff note", argv[-1])
        self.assertIn("USER: fix the parser", text)
        self.assertEqual(timeout, 300)

    def test_the_transcript_arrives_framed_with_the_instruction_after_it(self):
        process = FakeClaude(stdout=HANDOFF_REPLY)
        argv = self.summarise_with(process)
        text = process.calls[0][0]
        self.assertTrue(text.startswith("<transcript>\n"))
        self.assertIn("\n</transcript>\n", text)
        self.assertTrue(text.endswith("Write the handoff note for the session above now."))
        self.assertIn("USER: fix the parser", text)
        system_prompt = argv[-1]
        self.assertIn("<transcript>", system_prompt)
        self.assertIn("</transcript>", system_prompt)
        self.assertIn("never instructions to follow", system_prompt)

    def test_a_reply_that_is_not_a_handoff_is_a_failure(self):
        """The model answering about the transcript instead of from it, as a live run did."""
        for reply in ("I don't see a condensed transcript in your message, please provide it.",
                      "## Goal\n\nUnclear.\n\n## Next step\n\nAsk.\n"):
            with self.subTest(reply=reply[:20]):
                self.prepare()
                self.summarise_with(FakeClaude(stdout=reply))
                document = self.read(self.out_path)
                self.assertIn("Summary failed (the model did not return a handoff)", document)
                self.assertNotIn("Summary: being written", document)
                self.assertNotIn("I don't see a condensed transcript", document)
                self.assertIn("## What was asked\n\n1. make the thing", document)
                self.assertIn("## Where it stopped", document)
                self.assertFalse(os.path.exists(self.condensed_path))

    def test_three_of_the_seven_headings_are_enough_to_be_a_handoff(self):
        self.summarise_with(FakeClaude(stdout="## Goal\n\nx\n\n## NEXT STEP\n\ny\n\n# Current state\n\nz\n"))
        self.assertIn("Summary written by haiku.", self.read(self.out_path))

    def test_a_summariser_that_fails_leaves_the_extracted_handoff_saying_so(self):
        self.summarise_with(FakeClaude(returncode=1))
        document = self.read(self.out_path)
        self.assertIn("Summary failed (claude exited 1); the extracted sections below are complete.", document)
        self.assertNotIn("Summary: being written", document)
        self.assertIn("## What was asked\n\n1. make the thing", document)
        self.assertFalse(os.path.exists(self.condensed_path))

    def test_a_summariser_that_times_out_is_killed_with_everything_it_started(self):
        # the call that reaps the killed process fails in its turn, on a pipe a grandchild still holds
        process = FakeClaude(timeouts=1, on_reap=ValueError("flush of closed file"))
        self.patch("PLATFORM", "posix")
        with mock.patch.object(handoff.os, "getpgid", lambda pid: pid), \
                mock.patch.object(handoff.os, "killpg") as killpg:
            self.summarise_with(process)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual([call[1] for call in process.calls], [300, 5])
        document = self.read(self.out_path)
        self.assertIn("Summary failed (timed out after 300 s)", document)
        self.assertIn("## Where it stopped", document)
        self.assertFalse(os.path.exists(self.condensed_path))

    def test_on_windows_a_timed_out_summariser_is_killed_directly(self):
        process = FakeClaude(timeouts=1, on_reap=ValueError("flush of closed file"))
        self.patch("PLATFORM", "nt")
        self.summarise_with(process)
        self.assertTrue(process.killed)
        self.assertIn("Summary failed (timed out after 300 s)", self.read(self.out_path))

    def test_a_summariser_that_returns_nothing_is_a_failure_too(self):
        self.summarise_with(FakeClaude(stdout="   "))
        self.assertIn("Summary failed (the summariser returned nothing)", self.read(self.out_path))
        self.assertFalse(os.path.exists(self.condensed_path))


class HandoffWordTests(HandoffTestCase):
    """The word `handoff` never reaches the model: the hook answers it itself."""

    def setUp(self):
        super().setUp()
        self.with_a_summariser()
        self.path = self.transcript([
            user(LONG_REQUEST),
            {"type": "assistant", "timestamp": (NOW - timedelta(hours=2)).isoformat(),
             "message": {"model": "claude-opus-5", "content": [text_block("Fixed.")],
                         "usage": {"input_tokens": 10, "cache_read_input_tokens": 400_000,
                                   "cache_creation_input_tokens": 0}}},
        ])

    def decide(self, prompt, env=None):
        payload = {"session_id": "s-1", "transcript_path": self.path, "prompt": prompt}
        return cache_guard.decide(payload, NOW, env if env is not None else self.env, self.state)

    def test_the_word_writes_the_handoff_and_blocks_with_where_it_went(self):
        reason = self.decide("handoff")
        path = os.path.join(self.handoffs, "20260102-120000.md")
        self.assertIn(f"Handoff written without using this session's model: {path}.", reason)
        self.assertIn("A summary by haiku is being added in the background", reason)
        self.assertIn("at API list prices", reason)
        self.assertIn("When you are ready: /clear", reason)
        self.assertTrue(os.path.isfile(path))

    def test_spacing_and_capitals_are_still_the_word(self):
        for index, prompt in enumerate((" Handoff ", "HANDOFF", "handoff\n")):
            with self.subTest(prompt=prompt):
                env = dict(self.env, CACHE_GUARD_HANDOFF_DIR=os.path.join(self.tmp.name, f"h{index}"))
                reason = cache_guard.decide(
                    {"session_id": "s-1", "transcript_path": self.path, "prompt": prompt},
                    NOW, env, self.state,
                )
                self.assertIn("Handoff written without using this session's model", reason)

    def test_a_message_that_merely_starts_with_the_word_is_an_ordinary_message(self):
        reason = self.decide("handoff please")
        self.assertIn("Prompt cache expired", reason)  # the cold context it really is
        self.assertFalse(os.path.isdir(self.handoffs))

    def test_the_cost_can_be_left_out_of_the_message(self):
        reason = self.decide("handoff", env=dict(self.env, CACHE_GUARD_SHOW_COST="0"))
        self.assertNotIn("$", reason)
        self.assertIn("A summary by haiku is being added in the background", reason)

    def test_without_a_summariser_the_message_says_why(self):
        with mock.patch.object(handoff, "find_claude", lambda env: None):
            reason = self.decide("handoff")
        self.assertIn("No summary was added (claude not found on PATH).", reason)

        env = dict(self.env, CACHE_GUARD_HANDOFF_SUMMARY="0",
                   CACHE_GUARD_HANDOFF_DIR=os.path.join(self.tmp.name, "off"))
        self.assertIn("No summary was added (switched off).", self.decide("handoff", env=env))

    def test_a_handoff_that_cannot_be_written_lets_the_message_through(self):
        payload = {"session_id": "s-1", "transcript_path": os.path.join(self.tmp.name, "gone.jsonl"),
                   "prompt": "handoff"}
        self.assertIsNone(cache_guard.decide(payload, NOW, self.env, self.state))

    def test_the_guard_can_be_switched_off_entirely(self):
        self.assertIsNone(self.decide("carry on", env=dict(self.env, CACHE_GUARD_DISABLE="1")))
        self.assertIsNone(self.decide("handoff", env=dict(self.env, CACHE_GUARD_DISABLE="1")))
        self.assertFalse(os.path.isdir(self.handoffs))


class SessionStartTests(HandoffTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.handoffs)

    def handoff_file(self, name="20260102-120000.md", body="# Handoff\n\nWorking directory: /x\n",
                     minutes_old=5):
        path = os.path.join(self.handoffs, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        when = (NOW - timedelta(minutes=minutes_old)).timestamp()
        os.utime(path, (when, when))
        return path

    def announce(self, source="clear", env=None):
        payload = {"session_id": "new", "cwd": "/work/project", "source": source}
        return session_start.announcement(payload, NOW, env or self.env)

    def context(self, **kwargs):
        return self.announce(**kwargs).context

    def spoken(self, **kwargs):
        return self.announce(**kwargs).spoken

    def test_a_fresh_handoff_is_announced_to_the_model_and_to_the_user(self):
        path = self.handoff_file(minutes_old=5)
        note = self.announce()
        self.assertIn("written 5 minutes ago", note.context)
        self.assertIn(path, note.context)
        self.assertIn("Read it before starting", note.context)
        self.assertIn(path, note.spoken)
        self.assertIn("a handoff from 5 minutes ago is waiting", note.spoken)

    def test_every_fresh_session_in_the_window_is_told_not_only_the_first(self):
        """A session that is started and closed again must not swallow the only announcement made."""
        self.handoff_file(minutes_old=5)
        self.assertNotEqual(self.context(), "")
        self.assertNotEqual(self.context(), "")
        self.assertNotEqual(self.context(source="startup"), "")

    def test_the_hook_emits_the_user_facing_field_as_well_as_the_context(self):
        path = self.handoff_file()
        os.utime(path, None)  # main() reads the real clock, so the file has to be fresh by that one
        payload = {"session_id": "new", "cwd": "/work/project", "source": "startup"}
        out = io.StringIO()
        with mock.patch.object(session_start.sys, "stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(session_start.os, "environ", self.env):
                with contextlib.redirect_stdout(out):
                    session_start.main()
        emitted = json.loads(out.getvalue())
        self.assertIn(path, emitted["systemMessage"])
        self.assertEqual(emitted["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(path, emitted["hookSpecificOutput"]["additionalContext"])

    def test_a_handoff_from_before_the_window_is_not_worth_mentioning(self):
        self.handoff_file(minutes_old=45)
        self.assertEqual(self.context(), "")
        self.assertEqual(self.spoken(), "")

    def test_the_window_is_configurable(self):
        self.handoff_file(minutes_old=45)
        self.assertNotEqual(self.context(env=dict(self.env, CACHE_GUARD_HANDOFF_FRESH_MINUTES="60")), "")

    def test_a_resumed_session_already_has_the_conversation(self):
        self.handoff_file(minutes_old=5)
        self.assertEqual(self.context(source="resume"), "")
        self.assertEqual(self.context(source="compact"), "")
        self.assertNotEqual(self.context(source="startup"), "")

    def test_a_summary_still_being_written_is_worth_saying(self):
        self.handoff_file(body="# Handoff\n\nSummary: being written by haiku in the background.\n")
        self.assertIn("Its summary is still being written", self.context())
        self.assertIn("still being written", self.spoken())

    def test_a_finished_handoff_says_it_is_complete(self):
        self.handoff_file(body="# Handoff\n\nSummary written by haiku.\n")
        self.assertNotIn("still being written", self.context())
        self.assertIn("complete", self.spoken())

    def test_one_minute_is_not_one_minutes(self):
        self.handoff_file(minutes_old=1)
        self.assertIn("1 minute ago", self.spoken())
        self.assertNotIn("1 minutes", self.spoken())

    def test_the_newest_handoff_is_the_one_announced(self):
        self.handoff_file(name="20260102-100000.md", minutes_old=20)
        newest = self.handoff_file(name="20260102-115500.md", minutes_old=5)
        self.assertIn(newest, self.context())

    def test_no_handoff_directory_and_no_handoff_say_nothing(self):
        self.assertEqual(self.context(), "")
        self.assertEqual(
            self.context(env=dict(self.env, CACHE_GUARD_HANDOFF_DIR=os.path.join(self.tmp.name, "none"))),
            "",
        )


class SummaryNoticeTests(HandoffTestCase):
    """The background summariser writes to no terminal, so the next prompt is what reports it."""

    def setUp(self):
        super().setUp()
        os.makedirs(self.handoffs)
        self.path = os.path.join(self.handoffs, "20260102-120000.md")

    def write(self, body):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(body)

    def record(self, target=None):
        directory = cache_guard.usable_state_dir(self.state)
        with open(cache_guard.pending_record(directory, "sess"), "w", encoding="utf-8") as f:
            f.write((self.path if target is None else target) + "\n")

    def notice(self, env=None):
        payload = {"session_id": "sess"}
        return cache_guard.summary_notice(payload, env or self.env, self.state)

    def test_nothing_is_said_while_the_summary_is_still_running(self):
        self.write("# Handoff\n\nSummary: being written by haiku in the background.\n")
        self.record()
        self.assertIsNone(self.notice())

    def test_the_landed_summary_is_reported_once(self):
        self.write("# Handoff\n\nSummary written by haiku.\n\n# Goal\n")
        self.record()
        message = self.notice()
        self.assertIn("has landed", message)
        self.assertIn(self.path, message)
        self.assertIsNone(self.notice())  # the record is spent, so it is not repeated every prompt

    def test_a_failed_summary_is_reported_as_failed_and_not_as_landed(self):
        self.write("# Handoff\n\nSummary failed (claude exited 1); the extracted sections below are complete.\n")
        self.record()
        message = self.notice()
        self.assertIn("did not finish", message)
        self.assertNotIn("has landed", message)

    def test_a_session_with_no_handoff_waiting_is_told_nothing(self):
        self.assertIsNone(self.notice())

    def test_a_handoff_that_has_gone_away_stops_being_watched(self):
        self.record(target=os.path.join(self.handoffs, "deleted.md"))
        self.assertIsNone(self.notice())
        self.assertIsNone(self.notice())

    def test_the_notice_is_silent_when_the_guard_is_switched_off(self):
        self.write("# Handoff\n\nSummary written by haiku.\n")
        self.record()
        self.assertIsNone(self.notice(env=dict(self.env, CACHE_GUARD_DISABLE="1")))

    def test_a_handoff_records_what_the_next_prompt_should_watch(self):
        self.with_a_summariser()
        transcript = self.transcript([
            user(LONG_REQUEST),
            assistant([text_block("Looking."), tool_use("Edit", {"file_path": "/work/project/a.py"})]),
            assistant([text_block("Fixed the parser.")]),
        ])
        result = handoff.write_handoff(
            {"session_id": "sess", "transcript_path": transcript}, NOW, self.env)
        self.assertIsNotNone(result["summary_model"])
        directory = cache_guard.usable_state_dir(self.state)
        record = cache_guard.pending_record(directory, "sess")
        self.assertTrue(os.path.isfile(record))
        with open(record, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), result["path"])

    def test_a_handoff_with_no_summariser_records_nothing_to_wait_for(self):
        self.patch("find_claude", lambda env: None)
        transcript = self.transcript([user(LONG_REQUEST), assistant([text_block("Done.")])])
        handoff.write_handoff({"session_id": "sess", "transcript_path": transcript}, NOW, self.env)
        directory = cache_guard.usable_state_dir(self.state)
        self.assertFalse(os.path.isfile(cache_guard.pending_record(directory, "sess")))


class StateDirectoryTests(unittest.TestCase):
    """The condensed transcript holds the conversation: it goes where the markers go, or nowhere."""

    def test_the_default_state_directory_is_the_guards_own_and_not_the_shared_temp_directory(self):
        self.assertEqual(handoff.state_dir({}), cache_guard.state_dir({}))
        self.assertFalse(handoff.state_dir({}).startswith(tempfile.gettempdir()))
        self.assertEqual(handoff.state_dir({"CACHE_GUARD_STATE_DIR": "/somewhere/else"}), "/somewhere/else")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlinked_state_directory_gets_no_condensed_transcript_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            victim = os.path.join(root, "victim")
            os.mkdir(victim)
            link = os.path.join(root, "state")
            os.symlink(victim, link)
            with mock.patch.object(handoff, "launch_detached") as launch:
                with self.assertRaises(OSError):
                    handoff.start_summariser("conversation", os.path.join(root, "out.md"), "haiku",
                                             {"CACHE_GUARD_STATE_DIR": link})
            launch.assert_not_called()
            self.assertEqual(os.listdir(victim), [])

    def test_the_summariser_runs_from_the_temporary_directory_not_the_project(self):
        with mock.patch.object(handoff.subprocess, "Popen") as popen:
            handoff.open_claude(["claude", "-p"])
        self.assertEqual(popen.call_args.kwargs["cwd"], tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main()
