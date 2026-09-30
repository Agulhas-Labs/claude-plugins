"""Tests for the files changed and the commands run: what a work call records, what is read back from
the transcript when a board starts late, what the state makes of it, and the pruning of the gate's
count files.

Run: python3 -m unittest discover -s plugins/logbook/tests

The hook is called in-process (`board_hook.handle`) on a board that already exists in a temporary
project, with payloads varied from the captured ones in `fixtures/payloads`. Nothing here runs git: the
project is not a repository, and the recorder's own git calls drop every variable that points git
elsewhere.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
HOOKS = os.path.join(PLUGIN, "hooks")
PAYLOADS = os.path.join(HERE, "fixtures", "payloads")
TRANSCRIPT = os.path.join(HERE, "fixtures", "transcript-work.jsonl")
sys.path.insert(0, os.path.join(PLUGIN, "board"))
sys.path.insert(0, HOOKS)

import board  # noqa: E402
import board_hook  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
WRITE_ID = "toolu_0126FdNcqWooHZQ5Dqoih9AR"
EDIT_ID = "toolu_01M5TTZmedaenzAzbjxXysAU"
ECHO_ID = "toolu_01RkezLaDT3Ne8Ty9dLFzC7a"
FAIL_ID = "toolu_01SVXuadM7P2bGL9EsQAxwiK"
TESTS_ID = "toolu_01F2ZcBYsjBqJk11WfeVieZA"
REFUSED_ID = "toolu_014ojecXSbNxB7soT1jhBGTo"
WORK = ("change", "command")


def fixture(name):
    with open(os.path.join(PAYLOADS, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def bash(command, use="toolu_bash", **response):
    payload = fixture("PostToolUse-Bash")
    payload["tool_input"] = {"command": command, "description": "Run it"}
    payload["tool_response"].update(response)
    payload["tool_use_id"] = use
    return payload


def failure(error, command="false", use="toolu_failure"):
    payload = fixture("PostToolUseFailure-Bash")
    payload["tool_input"] = {"command": command, "description": "Run it"}
    payload["error"] = error
    payload["tool_use_id"] = use
    return payload


class OpenBoard(unittest.TestCase):
    """A temporary project whose session already has a board, and a transcript beside it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.project = os.path.join(self.tmp, "project")
        os.mkdir(self.project)
        self.transcript = os.path.join(self.tmp, "transcript.jsonl")
        self.write_transcript(1)
        self.env = {"CLAUDE_PROJECT_DIR": self.project, "CLAUDE_PLUGIN_ROOT": PLUGIN}
        self.folder = board.start(self.project, SESSION, NOW, "Recording work", self.env)
        self.assertEqual(self.folder, os.path.join(self.project, ".logbook", SESSION))

    def write_transcript(self, count=None):
        """The captured transcript's first `count` lines (all of them when None) at the test's path."""
        with open(TRANSCRIPT, encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.writelines(lines if count is None else lines[:count])

    def hook(self, payload):
        payload = copy.deepcopy(payload)
        payload["transcript_path"] = self.transcript
        return board_hook.handle(payload, self.env, NOW)

    def work(self):
        return [event for event in board.events(self.folder) if event["kind"] in WORK]

    def only(self, payload):
        """The one work event a payload records on the board."""
        self.hook(payload)
        found = self.work()
        self.assertEqual(len(found), 1, found)
        return found[0]


class FileChanges(OpenBoard):

    def test_a_write_that_creates_a_file_counts_its_lines_as_added(self):
        event = self.only(fixture("PostToolUse-Write"))
        self.assertEqual(
            {key: event.get(key) for key in ("path", "tool", "created", "added", "removed", "use")},
            {"path": "/home/user/project/hello.txt", "tool": "Write", "created": True, "added": 2, "removed": 0,
             "use": WRITE_ID},
        )
        self.assertNotIn("agent", event)
        self.assertNotIn("early", event)

    def test_a_write_that_creates_a_file_without_content_in_its_response_counts_the_input(self):
        payload = fixture("PostToolUse-Write")
        del payload["tool_response"]["content"]
        payload["tool_input"]["content"] = "one\ntwo\nthree"
        event = self.only(payload)
        self.assertEqual((event["created"], event["added"], event["removed"]), (True, 3, 0))

    def test_a_write_that_overwrites_a_file_counts_its_patch_and_is_not_created(self):
        payload = fixture("PostToolUse-Write")
        payload["tool_response"].update(type="update", originalFile="alpha\n", structuredPatch=[
            {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2, "lines": ["-alpha", "+alpha2", "+beta"]},
        ])
        event = self.only(payload)
        self.assertEqual((event["created"], event["added"], event["removed"]), (False, 2, 1))

    def test_an_edit_counts_the_lines_its_patch_adds_and_removes(self):
        event = self.only(fixture("PostToolUse-Edit"))
        self.assertEqual(
            (event["path"], event["tool"], event["created"], event["added"], event["removed"], event["use"]),
            ("/home/user/project/notes.txt", "Edit", False, 1, 1, EDIT_ID),
        )

    def test_a_multi_edit_sums_every_hunk_of_its_patch(self):
        payload = fixture("PostToolUse-Edit")
        payload["tool_name"] = "MultiEdit"
        payload["tool_input"] = {"file_path": "/home/user/project/notes.txt", "edits": [
            {"old_string": "one", "new_string": "uno"}, {"old_string": "three", "new_string": "tres\nquatro"},
        ]}
        payload["tool_response"]["structuredPatch"] = [
            {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-one", "+uno"]},
            {"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 2, "lines": [" two", "-three", "+tres", "+quatro"]},
        ]
        event = self.only(payload)
        self.assertEqual((event["tool"], event["added"], event["removed"]), ("MultiEdit", 3, 2))

    def test_a_notebook_edit_records_its_notebook_path_and_no_counts(self):
        payload = fixture("PostToolUse-Edit")
        payload["tool_name"] = "NotebookEdit"
        payload["tool_input"] = {"notebook_path": "/home/user/project/analysis.ipynb", "cell_id": "c1",
                                 "new_source": "print(1)"}
        payload["tool_response"] = {"new_source": "print(1)", "cell_type": "code", "edit_mode": "replace"}
        event = self.only(payload)
        self.assertEqual((event["path"], event["tool"], event["created"]),
                         ("/home/user/project/analysis.ipynb", "NotebookEdit", False))
        self.assertNotIn("added", event)
        self.assertNotIn("removed", event)

    def test_a_failed_write_or_edit_records_nothing(self):
        for tool in ("Write", "Edit"):
            payload = fixture("PostToolUse-" + tool)
            payload["hook_event_name"] = "PostToolUseFailure"
            payload.pop("tool_response")
            payload["error"] = "String to replace not found in file."
            self.hook(payload)
        self.assertEqual(self.work(), [])

    def test_the_same_payload_handled_twice_is_recorded_once(self):
        # A plugin loaded twice runs every hook twice; the call is on the board once all the same.
        self.hook(fixture("PostToolUse-Edit"))
        self.hook(fixture("PostToolUse-Edit"))
        self.assertEqual([event["use"] for event in self.work()], [EDIT_ID])

    def test_a_call_read_back_from_the_transcript_is_not_recorded_again_by_its_own_hook(self):
        # The transcript can hold a call's result before that call's hook has run.
        board.append(self.folder, "change", NOW, path="/home/user/project/notes.txt", tool="Edit",
                     added=1, removed=1, use=EDIT_ID, early=True)
        self.hook(fixture("PostToolUse-Edit"))
        self.assertEqual([event["use"] for event in self.work()], [EDIT_ID])
        state = board.derive(board.events(self.folder), [], board.settings(self.env))
        self.assertEqual([(row["edits"], row["added"], row["removed"]) for row in state["changes"]], [(1, 1, 1)])


class Commands(OpenBoard):

    def test_a_command_that_succeeds_is_a_pass_with_its_description_and_duration(self):
        event = self.only(fixture("PostToolUse-Bash"))
        self.assertEqual(
            {key: event.get(key) for key in ("command", "description", "result", "ms", "use")},
            {"command": "echo ok && true", "description": "Run echo ok && true", "result": "pass", "ms": 42,
             "use": ECHO_ID},
        )
        self.assertNotIn("exit", event)

    def test_a_failed_command_is_a_fail_with_the_exit_code_its_error_names(self):
        event = self.only(fixture("PostToolUseFailure-Bash"))
        self.assertEqual((event["result"], event["exit"], event["ms"], event["use"]), ("fail", 3, 10, FAIL_ID))

    def test_a_failed_command_whose_error_names_no_exit_code_is_a_fail_without_one(self):
        event = self.only(failure("Command timed out after 2m 0.0s"))
        self.assertEqual(event["result"], "fail")
        self.assertNotIn("exit", event)

    def test_an_exit_code_later_in_the_error_is_not_read_as_the_commands(self):
        event = self.only(failure("bad input\nExit code 3"))
        self.assertNotIn("exit", event)

    def test_a_command_left_running_in_the_background_is_background(self):
        payload = bash("sleep 60")
        payload["tool_input"]["run_in_background"] = True
        payload["tool_response"] = {"backgroundTaskId": "b1", "interrupted": False}
        self.assertEqual(self.only(payload)["result"], "background")

    def test_an_interrupted_command_is_a_fail(self):
        self.assertEqual(self.only(bash("sleep 60", interrupted=True))["result"], "fail")

    def test_a_long_command_and_description_are_cut_at_the_cap(self):
        payload = bash("echo " + "x" * (board.TEXT_CAP + 50))
        payload["tool_input"]["description"] = "d" * (board.TEXT_CAP + 1)
        event = self.only(payload)
        self.assertEqual(event["command"], ("echo " + "x" * board.TEXT_CAP)[:board.TEXT_CAP])
        self.assertEqual(len(event["description"]), board.TEXT_CAP)

    def test_a_command_that_runs_this_plugins_own_board_script_is_not_recorded(self):
        script = os.path.join(PLUGIN, "board", "board.py")
        self.hook(bash(f'python3 "{script}" check "the suite passes" --board "{self.folder}"', use="toolu_own"))
        self.assertEqual(self.work(), [])

    def test_a_command_that_only_mentions_another_board_script_is_recorded(self):
        self.hook(bash("python3 /srv/other/board/board.py --help", use="toolu_other"))
        self.hook(bash("grep -n def board.py", use="toolu_grep"))
        self.assertEqual([event["use"] for event in self.work()], ["toolu_other", "toolu_grep"])


class Subagents(OpenBoard):

    def test_a_work_call_made_inside_a_subagent_is_recorded_with_its_agent(self):
        payload = fixture("PostToolUse-Edit")
        payload["agent_id"] = "agent-7"
        self.assertIsNone(self.hook(payload))
        event = self.work()[0]
        self.assertEqual((event["agent"], event["use"], event["added"]), ("agent-7", EDIT_ID, 1))

        command = fixture("PostToolUse-Bash")
        command["agent_id"] = "agent-7"
        self.hook(command)
        self.assertEqual([(e["kind"], e["agent"]) for e in self.work()], [("change", "agent-7"), ("command", "agent-7")])

    def test_a_task_list_call_made_inside_a_subagent_is_still_dropped(self):
        payload = fixture("PostToolUse-TaskCreate")
        payload["tool_response"] = {"task": {"id": "9", "subject": "inside"}}
        payload["agent_id"] = "agent-7"
        self.assertIsNone(self.hook(payload))
        self.assertEqual([e for e in board.events(self.folder) if e["kind"] == "step"], [])

    def test_every_work_event_carries_its_calls_tool_use_id(self):
        for name in ("PostToolUse-Write", "PostToolUse-Edit", "PostToolUse-Bash", "PostToolUse-Bash-2",
                     "PostToolUseFailure-Bash"):
            self.hook(fixture(name))
        self.assertEqual([event["use"] for event in self.work()], [WRITE_ID, EDIT_ID, ECHO_ID, TESTS_ID, FAIL_ID])


class Transcript(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def read(self, skip=(), stop=()):
        return board_hook.read_transcript(TRANSCRIPT, set(skip), set(stop), {"CLAUDE_PLUGIN_ROOT": PLUGIN})

    def test_the_changes_and_commands_are_read_back_early_with_the_time_of_their_result(self):
        prompt, found = self.read()
        self.assertTrue(prompt.startswith("Add hello.txt"))
        self.assertEqual([(kind, fields["use"]) for kind, fields in found], [
            ("change", WRITE_ID), ("change", EDIT_ID), ("command", ECHO_ID), ("command", FAIL_ID),
            ("command", TESTS_ID),
        ])
        self.assertTrue(all(fields["early"] is True for _, fields in found))
        write, edit, echo, failed, tests = (fields for _, fields in found)
        self.assertEqual((write["created"], write["added"], write["removed"], write["at"]),
                         (True, 2, 0, "2026-09-28T14:20:02Z"))
        self.assertEqual((edit["added"], edit["removed"], edit["at"]), (1, 1, "2026-09-28T14:20:06Z"))
        self.assertEqual((echo["result"], echo["at"]), ("pass", "2026-09-28T14:20:08Z"))
        self.assertEqual(tests["command"], "python3 -m unittest discover -s . 2>&1 | tail -3")

    def test_a_failed_command_is_read_once_as_a_fail_with_its_exit_code(self):
        _, found = self.read()
        failed = [fields for kind, fields in found if fields["use"] == FAIL_ID]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["result"], failed[0]["exit"]), ("fail", 3))

    def test_an_edit_the_tool_refused_is_not_read_at_all(self):
        _, found = self.read()
        self.assertNotIn(REFUSED_ID, [fields["use"] for _, fields in found])

    def test_the_calls_in_skip_are_left_out(self):
        _, found = self.read(skip={WRITE_ID, FAIL_ID})
        self.assertEqual([fields["use"] for _, fields in found], [EDIT_ID, ECHO_ID, TESTS_ID])

    def test_nothing_after_a_stop_id_is_read(self):
        _, found = self.read(stop={FAIL_ID})
        self.assertEqual([fields["use"] for _, fields in found], [WRITE_ID, EDIT_ID, ECHO_ID])

    def test_a_transcript_of_an_unexpected_shape_raises_nothing(self):
        def call(use, name, tool_input, **result):
            return [
                {"type": "assistant", "timestamp": "not a time", "message": {"content": [
                    {"type": "tool_use", "id": use, "name": name, "input": tool_input}]}},
                dict({"type": "user", "timestamp": 5, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": use, "content": 7}]}}, **result),
            ]
        lines = [json.dumps({"type": "user", "message": {"content": "go"}})]
        for entry in (
            call("u1", "Bash", {"command": "true"}, toolUseResult="done"),
            call("u2", "Write", {"file_path": "/p/a"}, toolUseResult=12),
            call("u3", "Edit", {"file_path": "/p/b"}),
            call("u4", "Bash", "not an object", toolUseResult=[1, 2]),
            call("u5", "NotebookEdit", None, toolUseResult={"structuredPatch": "x"}),
            call("u6", "MultiEdit", {"file_path": "/p/c"}, toolUseResult={"structuredPatch": [{"lines": 3}]}),
        ):
            lines.extend(json.dumps(line) for line in entry)
        failed = call("u7", "Bash", {"command": "false"}, toolUseResult=None)
        failed[1]["message"]["content"][0]["is_error"] = True
        lines.extend(json.dumps(line) for line in failed)
        lines.append(json.dumps(call("u8", "Edit", {"file_path": "/p/d"})[0]))
        lines.append(json.dumps(call("u8", "Edit", {"file_path": "/p/d"})[1])[:40])
        path = os.path.join(self.tmp, "odd.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        prompt, found = board_hook.read_transcript(path, set(), env={})

        self.assertEqual(prompt, "go")
        by_use = {fields["use"]: (kind, fields) for kind, fields in found}
        self.assertEqual(sorted(by_use), ["u1", "u2", "u3", "u6", "u7"])
        self.assertEqual(by_use["u1"][1]["result"], "pass")
        self.assertEqual(by_use["u7"][1]["result"], "fail")
        self.assertNotIn("exit", by_use["u7"][1])
        self.assertNotIn("added", {k: v for k, v in by_use["u2"][1].items() if v is not None})
        self.assertTrue(all("at" not in fields for _, fields in found))


class CatchUp(OpenBoard):
    """The board started while the transcript held only the prompt; the calls before it arrive late."""

    def uses(self):
        return [(event["use"], event.get("early") is True) for event in self.work()]

    def test_the_early_calls_a_late_transcript_gained_are_recorded_once(self):
        self.hook(fixture("Stop"))
        self.assertEqual(self.work(), [])

        self.write_transcript()
        self.hook(fixture("Stop"))
        self.hook(fixture("Stop"))

        self.assertEqual(self.uses(), [(WRITE_ID, True), (EDIT_ID, True), (ECHO_ID, True), (FAIL_ID, True),
                                       (TESTS_ID, True)])
        self.assertEqual(self.work()[0]["at"], "2026-09-28T14:20:02Z")

    def test_recovery_stops_at_the_first_call_a_hook_recorded(self):
        self.hook(fixture("PostToolUseFailure-Bash"))
        self.write_transcript()
        self.hook(fixture("Stop"))
        self.assertEqual(self.uses(), [(FAIL_ID, False), (WRITE_ID, True), (EDIT_ID, True), (ECHO_ID, True)])

    def test_the_state_lists_the_recovered_calls_before_the_hooks_own(self):
        self.hook(fixture("PostToolUseFailure-Bash"))
        self.write_transcript()
        self.hook(fixture("Stop"))
        state = board.current_state(self.folder, NOW, self.env)
        self.assertEqual([row["path"] for row in state["changes"]],
                         ["/home/user/project/hello.txt", "/home/user/project/notes.txt"])
        self.assertEqual([row["command"] for row in state["commands"]],
                         ["echo ok && true", 'sh -c "echo bad >&2; exit 3"'])
        self.assertEqual(state["commandsTotal"], 2)


def at(minute):
    return f"2026-01-05T09:{minute:02d}:00Z"


class Derived(unittest.TestCase):
    PROJECT = "/home/user/project"

    def derive(self, *events, tests=None, project=PROJECT):
        start = {"t": at(0), "kind": "start", "session": SESSION, "title": "t", "project": project}
        return board.derive([start, *events], [], board.settings({}), tests)

    def change(self, minute, path, **fields):
        return dict({"t": at(minute), "kind": "change", "path": path, "tool": "Edit"}, **fields)

    def command(self, minute, command, result="pass", **fields):
        return dict({"t": at(minute), "kind": "command", "command": command, "result": result}, **fields)

    def test_changes_are_one_row_per_file_with_edits_and_sums(self):
        state = self.derive(
            self.change(1, "/home/user/project/a.py", added=3, removed=1, agent="agent-1"),
            self.change(2, "/home/user/project/b.py", tool="NotebookEdit"),
            self.change(3, "/home/user/project/a.py", added=2, removed=0, agent="agent-2"),
            self.change(4, "/home/user/project/a.py", agent="agent-1"),
        )
        self.assertEqual(state["changes"], [
            {"path": "a.py", "edits": 3, "added": 5, "removed": 1, "created": False, "first": at(1), "last": at(4),
             "agents": ["agent-1", "agent-2"]},
            {"path": "b.py", "edits": 1, "added": None, "removed": None, "created": False, "first": at(2),
             "last": at(2), "agents": []},
        ])

    def test_created_comes_from_a_files_first_change_only(self):
        state = self.derive(
            self.change(1, "/home/user/project/a.py"),
            self.change(2, "/home/user/project/a.py", tool="Write", created=True),
            self.change(3, "/home/user/project/b.py", tool="Write", created=True),
            self.change(4, "/home/user/project/b.py"),
        )
        self.assertEqual([(row["path"], row["edits"], row["created"]) for row in state["changes"]],
                         [("a.py", 2, False), ("b.py", 2, True)])

    def test_a_path_is_relative_only_when_it_is_inside_the_project(self):
        paths = ["/home/user/project/src/a.py", "/etc/hosts", "/home/user/project-other/x", "relative/y",
                 "/home/user/project"]
        state = self.derive(*(self.change(1, path) for path in paths))
        self.assertEqual([row["path"] for row in state["changes"]],
                         ["src/a.py", "/etc/hosts", "/home/user/project-other/x", "relative/y", "/home/user/project"])

    def test_first_and_last_prefer_the_time_read_from_the_transcript(self):
        state = self.derive(
            self.change(5, "/home/user/project/a.py", early=True, at="2026-01-05T08:30:00Z"),
            self.change(6, "/home/user/project/a.py", at="not a time"),
        )
        row = state["changes"][0]
        self.assertEqual((row["first"], row["last"]), ("2026-01-05T08:30:00Z", at(6)))

    def test_commands_are_one_row_per_text_with_the_latest_run(self):
        state = self.derive(
            self.command(1, "make build", "fail", exit=2, ms=10, description="Build it"),
            self.command(2, "ls"),
            self.command(3, "make build", "pass", ms=20, agent="agent-1"),
            self.command(4, "make build", "fail", exit=1, ms=30, description="Build again"),
            self.command(5, "sleep 9", "background"),
            self.command(6, "ls", "skipped"),
        )
        self.assertEqual(state["commands"], [
            {"command": "make build", "description": "Build again", "result": "fail", "exit": 1, "runs": 3,
             "fails": 2, "time": at(4), "ms": 30, "test": False, "agents": ["agent-1"]},
            {"command": "ls", "description": None, "result": "pass", "exit": None, "runs": 1, "fails": 0,
             "time": at(2), "ms": None, "test": False, "agents": []},
            {"command": "sleep 9", "description": None, "result": "background", "exit": None, "runs": 1,
             "fails": 0, "time": at(5), "ms": None, "test": False, "agents": []},
        ])
        self.assertEqual(state["commandsTotal"], 5)

    def test_the_rows_are_capped_at_the_most_recently_run_and_the_total_is_not(self):
        texts = [f"echo {n}" for n in range(board.COMMAND_ROWS + 5)]
        events = [self.command(1, text) for text in texts] + [self.command(2, texts[0])]
        state = self.derive(*events)
        self.assertEqual(len(state["commands"]), board.COMMAND_ROWS)
        self.assertEqual([row["command"] for row in state["commands"]], [texts[0]] + texts[6:])
        self.assertEqual(state["commandsTotal"], board.COMMAND_ROWS + 6)

    def test_every_listed_test_runner_is_a_test(self):
        runners = [
            "pytest -q", "py.test tests", "python -m unittest", "python3 -m unittest discover -s tests",
            "python3.12 -m pytest", "tox -e py312", "swift test", "xcodebuild -scheme App -destination x test",
            "xcodebuild test-without-building", "npm test", "npm run test", "yarn test", "pnpm test", "jest",
            "npx vitest run", "go test ./...", "cargo test", "gradle test", "./gradlew test", "mvn test",
            "dotnet test", "bundle exec rspec", "vendor/bin/phpunit", "ctest --output-on-failure", "make test",
            "make check", "cd pkg && pytest 2>&1 | tail -3",
        ]
        state = self.derive(*(self.command(1, runner) for runner in runners))
        self.assertEqual([row["command"] for row in state["commands"] if not row["test"]], [])

    def test_near_misses_are_not_tests(self):
        near = [
            "echo contest", "git tag latest", "pip install pytest-cov", "cat pytest.ini", "make tests",
            "npm install jest-cli", "cargo testing", "xcodebuild -scheme App build | grep test", "go vet ./...",
            "ls tests/", "swift build",
        ]
        state = self.derive(*(self.command(1, text) for text in near))
        self.assertEqual([row["command"] for row in state["commands"] if row["test"]], [])

    def test_a_runner_named_as_an_argument_is_not_a_test(self):
        named = [
            "pip install pytest", "brew install tox", "which jest", "grep -r pytest .", "echo swift test",
            "git commit -m 'make test pass'", "cat notes | grep 'go test'",
        ]
        state = self.derive(*(self.command(1, text) for text in named))
        self.assertEqual([row["command"] for row in state["commands"] if row["test"]], [])

    def test_a_runner_a_wrapper_is_handed_after_two_dashes_is_a_test(self):
        wrapped = [
            "wrap run -- swift test", "wrap run --proved -- swift test --filter Parser",
            "wrap exec -- xcodebuild -scheme App -destination 'id=1' test", "timer -- pytest -q",
        ]
        state = self.derive(*(self.command(1, text) for text in wrapped))
        self.assertEqual([row["command"] for row in state["commands"] if not row["test"]], [])
        near = ["wrap run --pytest", "wrap run --runner=pytest build", "swift build -- test", "wrap --swift test"]
        state = self.derive(*(self.command(1, text) for text in near))
        self.assertEqual([row["command"] for row in state["commands"] if row["test"]], [])

    def test_what_follows_two_dashes_in_git_echo_and_printf_is_not_a_command(self):
        paths = ["git diff -- pytest", "git log --oneline -- jest", "git rm -- rspec", "echo -- make test",
                 "cd sub && git checkout -- tox", "printf -- 'go test'"]
        state = self.derive(*(self.command(1, text) for text in paths))
        self.assertEqual([row["command"] for row in state["commands"] if row["test"]], [])
        both = self.derive(self.command(1, "git add -- src && pytest"))
        self.assertTrue(both["commands"][0]["test"])

    def test_a_runner_is_a_test_wherever_a_command_can_begin(self):
        begun = [
            "cd api && pytest -q", "make build; make test", "CI=1 npm test", "uv run pytest tests",
            ".venv/bin/pytest", "./gradlew test", "sudo ctest", "(cd web && yarn test)",
            "lint || cargo test", "true\ngo test ./...", "time python3 -m unittest", "bundle exec rspec",
        ]
        state = self.derive(*(self.command(1, text) for text in begun))
        self.assertEqual([row["command"] for row in state["commands"] if not row["test"]], [])

    def test_the_tests_setting_adds_a_pattern(self):
        pattern = board.test_pattern({"LOGBOOK_TESTS": r"^bats\b"})
        state = self.derive(self.command(1, "bats spec"), self.command(2, "echo bats"), tests=pattern)
        self.assertEqual([row["test"] for row in state["commands"]], [True, False])
        self.assertFalse(self.derive(self.command(1, "bats spec"))["commands"][0]["test"])

    def test_an_invalid_tests_setting_means_none_and_raises_nothing(self):
        for value in ("(", "[z-a]", "", "   "):
            self.assertIsNone(board.test_pattern({"LOGBOOK_TESTS": value}))
        env = {"LOGBOOK_TESTS": "("}
        state = self.derive(self.command(1, "pytest"), self.command(2, "("), tests=board.test_pattern(env))
        self.assertEqual([row["test"] for row in state["commands"]], [True, False])

    def test_early_events_are_applied_first_in_log_order(self):
        state = self.derive(
            self.change(3, "/home/user/project/late.py"),
            self.command(4, "make", "fail", exit=2),
            self.change(1, "/home/user/project/first.py", early=True),
            self.change(2, "/home/user/project/second.py", early=True),
            self.command(1, "make", "pass", early=True),
        )
        self.assertEqual([row["path"] for row in state["changes"]], ["first.py", "second.py", "late.py"])
        self.assertEqual((state["commands"][0]["result"], state["commands"][0]["exit"]), ("fail", 2))


class PruneCalls(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.data = os.path.join(self.tmp, "data")
        self.calls = os.path.join(self.data, board.CALLS_DIR)
        os.makedirs(self.calls)
        self.env = {"CLAUDE_PLUGIN_DATA": self.data, "LOGBOOK_RETENTION_DAYS": "7"}
        self.old = (NOW - timedelta(days=8)).timestamp()
        self.fresh = (NOW - timedelta(days=6)).timestamp()

    def counter(self, folder, name, when):
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(b"....")
        os.utime(path, (when, when))
        return path

    def test_an_old_counter_goes_and_a_fresh_one_stays(self):
        old = self.counter(self.calls, SESSION, self.old)
        fresh = self.counter(self.calls, "22222222-3333-4444-8555-666666666666", self.fresh)
        board.prune_calls(NOW, self.env)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_a_link_a_folder_and_a_file_not_named_as_a_session_stay(self):
        target = self.counter(self.tmp, "elsewhere", self.old)
        link = os.path.join(self.calls, SESSION)
        os.symlink(target, link)
        os.utime(link, (self.old, self.old), follow_symlinks=False)
        folder = os.path.join(self.calls, "33333333-4444-4555-8666-777777777777")
        os.mkdir(folder)
        os.utime(folder, (self.old, self.old))
        misnamed = self.counter(self.calls, "not a session", self.old)
        board.prune_calls(NOW, self.env)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(os.path.isdir(folder))
        self.assertTrue(os.path.isfile(misnamed))

    def test_a_calls_folder_that_is_a_link_is_left_alone(self):
        shutil.rmtree(self.calls)
        real = os.path.join(self.tmp, "real-calls")
        os.mkdir(real)
        old = self.counter(real, SESSION, self.old)
        os.symlink(real, self.calls)
        board.prune_calls(NOW, self.env)
        self.assertTrue(os.path.isfile(old))

    def test_without_a_data_folder_nothing_happens_and_nothing_raises(self):
        board.prune_calls(NOW, {})
        board.prune_calls(NOW, {"CLAUDE_PLUGIN_DATA": os.path.join(self.tmp, "missing")})

    def test_pruning_boards_prunes_the_counters_too(self):
        project = os.path.join(self.tmp, "project")
        os.mkdir(project)
        old = self.counter(self.calls, SESSION, self.old)
        board.prune(project, NOW, self.env)
        self.assertFalse(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()
