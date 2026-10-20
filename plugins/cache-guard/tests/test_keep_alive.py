"""Tests for the keep-alive Stop hook. Run: python3 -m unittest discover -s plugins/cache-guard/tests

Nothing here waits: the hook's clock, sleep and liveness check are replaced by a fake clock that moves
only when the hook sleeps, and that can change the transcript or the process record at a given time.
Nothing is written outside each test's own temp directory.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
sys.path.insert(0, os.path.abspath(HOOKS))

import cache_guard  # noqa: E402
import keep_alive  # noqa: E402

T0 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)  # the last turn's request
SINCE = T0 - timedelta(hours=30)  # when this process's session started
LARGE = 310_000
SMALL = 40_000
VERSION = "2.1.304"
PID = 4242
SESSION = "s-1"


def stamp(when):
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def split_1h(tokens=90_000):
    return {"ephemeral_1h_input_tokens": tokens, "ephemeral_5m_input_tokens": 0}


def split_5m(tokens=90_000):
    return {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": tokens}


def assistant(when, size=LARGE, split=None, msg_id=None, content=None, sidechain=False,
              model="claude-opus-4-5", version=VERSION):
    split = split_1h() if split is None else split
    usage = {
        "input_tokens": 12,
        "cache_creation_input_tokens": sum(split.values()),
        "cache_read_input_tokens": size - 12 - sum(split.values()),
        "cache_creation": split,
    }
    entry = {
        "type": "assistant",
        "timestamp": stamp(when),
        "message": {"id": msg_id or f"msg_{when.timestamp()}", "model": model, "usage": usage,
                    "content": content or [{"type": "text", "text": "ok"}]},
    }
    if version:
        entry["version"] = version
    if sidechain:
        entry["isSidechain"] = True
    return entry


def tool_result(when, result, sidechain=False):
    entry = {
        "type": "user",
        "timestamp": stamp(when),
        "version": VERSION,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
        "toolUseResult": result,
    }
    if sidechain:
        entry["isSidechain"] = True
    return entry


def background_command(when, task="b1", sidechain=False):
    return tool_result(when, {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": task},
                       sidechain=sidechain)


def timed_out_command(when, task="b2"):
    return tool_result(when, {"stdout": "", "backgroundTaskId": task, "timedOutAfterMs": 120000})


def background_agent(when, agent="a1"):
    return tool_result(when, {"isAsync": True, "status": "async_launched", "agentId": agent})


def foreground_agent(when, agent="a9"):
    return tool_result(when, {"status": "completed", "agentId": agent, "content": []})


def monitor(when, task="m1"):
    return tool_result(when, {"taskId": task, "timeoutMs": 600000, "persistent": False})


def task_stopped(when, task="b1"):
    return tool_result(when, {"message": f"Successfully stopped task: {task} (sleep 999)", "task_id": task,
                              "task_type": "local_bash"})


def send_message(when, to):
    block = {"type": "tool_use", "id": "toolu_send", "name": "SendMessage",
             "input": {"to": to, "summary": "more", "message": "one more thing"}}
    return assistant(when, msg_id="msg_send", content=[block])


def notification(task, status="completed"):
    status_line = f"<status>{status}</status>\n" if status else ""
    return (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
            f"{status_line}<summary>finished</summary>\n</task-notification>")


def monitor_event(task="m1"):
    return (f"<task-notification>\n<task-id>{task}</task-id>\n<summary>Monitor event: \"x\"</summary>\n"
            "<event>line</event>\n</task-notification>")


def queued(when, text):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(when), "content": text}


def attached(when, text):
    return {"type": "attachment", "timestamp": stamp(when),
            "attachment": {"type": "queued_command", "prompt": text, "commandMode": "task-notification"}}


def user_text(when, text):
    return {"type": "user", "timestamp": stamp(when), "version": VERSION,
            "message": {"role": "user", "content": text}}


def ping_record(when):
    return user_text(when, "<system-reminder>A cache-guard hook woke this session: "
                           + keep_alive.wake_message({"b1": "command"}) + "</system-reminder>")


def compact_boundary(when):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(when), "version": VERSION,
            "content": "Conversation compacted"}


class FakeClock:
    """A clock that moves only when the hook sleeps, running each scheduled change once it is due."""

    def __init__(self, now):
        self.now = now
        self.scheduled = []
        self.sleeps = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += timedelta(seconds=seconds)
        for item in list(self.scheduled):
            at, change = item
            if self.now >= at:
                self.scheduled.remove(item)
                change()

    def at(self, when, change):
        self.scheduled.append((when, change))


class KeepAliveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-keep-alive-test-")
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.path = os.path.join(self.tmp.name, "transcript.jsonl")
        self.env = {
            "CACHE_GUARD_STATE_DIR": self.state,
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_PID": str(PID),
        }
        self.record(SESSION, SINCE)
        self.clock = FakeClock(T0 + timedelta(seconds=20))
        self.alive = True

    def record(self, session, since):
        os.makedirs(self.state, exist_ok=True)
        with open(self.record_path(), "w", encoding="utf-8") as f:
            json.dump({"session_id": session, "since": since.isoformat()}, f)

    def record_path(self):
        return os.path.join(self.state, f"process-{PID}")

    def write(self, entries):
        with open(self.path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def append(self, *entries):
        with open(self.path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def run_hook(self, entries=None, env=None, session=SESSION):
        if entries is not None:
            self.write(entries)
        payload = {"session_id": session, "transcript_path": self.path, "hook_event_name": "Stop"}
        return keep_alive.run(payload, env or self.env, clock=self.clock, sleep=self.clock.sleep,
                              alive=lambda pid: self.alive)

    def busy(self, *extra, last=None):
        """A turn that started a background command, and the turn that ended at T0 with it running."""
        return [
            assistant(T0 - timedelta(minutes=10), msg_id="msg_start"),
            background_command(T0 - timedelta(minutes=9)),
            *extra,
            last or assistant(T0, msg_id="msg_last"),
        ]


class GateTests(KeepAliveTestCase):
    def test_busy_session_is_woken(self):
        self.assertIn(keep_alive.KEEP_ALIVE_MARK, self.run_hook(self.busy()))

    def test_disabled_guard_does_nothing(self):
        self.assertIsNone(self.run_hook(self.busy(), env={**self.env, "CACHE_GUARD_DISABLE": "1"}))
        self.assertEqual(self.clock.sleeps, 0)

    def test_keep_alive_switched_off_does_nothing(self):
        self.assertIsNone(self.run_hook(self.busy(), env={**self.env, "CACHE_GUARD_KEEP_ALIVE": "0"}))
        self.assertEqual(self.clock.sleeps, 0)

    def test_headless_entrypoint_exits_at_once(self):
        self.assertIsNone(self.run_hook(self.busy(), env={**self.env, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertEqual(self.clock.sleeps, 0)

    def test_missing_entrypoint_exits_at_once(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_CODE_ENTRYPOINT"}
        self.assertIsNone(self.run_hook(self.busy(), env=env))

    def test_missing_pid_exits_at_once(self):
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PID"}
        self.assertIsNone(self.run_hook(self.busy(), env=env))

    def test_pid_that_is_not_a_positive_integer_exits_at_once(self):
        for pid in ("0", "-3", "abc", ""):
            with self.subTest(pid=pid):
                self.assertIsNone(self.run_hook(self.busy(), env={**self.env, "CLAUDE_PID": pid}))

    def test_missing_transcript_exits_at_once(self):
        self.assertIsNone(self.run_hook())

    def test_transcript_older_than_the_first_version_with_async_rewake_exits_at_once(self):
        entries = self.busy(last=assistant(T0, msg_id="msg_last", version="2.1.303"))
        self.assertIsNone(self.run_hook(entries))
        self.assertEqual(self.clock.sleeps, 0)

    def test_transcript_without_a_version_exits_at_once(self):
        entries = [
            {k: v for k, v in entry.items() if k != "version"} for entry in self.busy()
        ]
        self.assertIsNone(self.run_hook(entries))

    def test_no_process_record_exits_at_once(self):
        os.remove(self.record_path())
        self.assertIsNone(self.run_hook(self.busy()))

    def test_process_record_naming_another_session_exits_at_once(self):
        self.record("someone-else", SINCE)
        self.assertIsNone(self.run_hook(self.busy()))

    def test_matching_process_record_is_touched(self):
        os.utime(self.record_path(), (1, 1))
        self.run_hook(self.busy())
        self.assertGreater(os.path.getmtime(self.record_path()), time.time() - 600)


class RunningWorkTests(KeepAliveTestCase):
    def test_background_command_is_running_work(self):
        self.assertIn("(1 command)", self.run_hook(self.busy()))

    def test_command_moved_to_the_background_by_its_timeout_is_running_work(self):
        entries = [timed_out_command(T0 - timedelta(minutes=5)), assistant(T0, msg_id="msg_last")]
        self.assertIn("(1 command)", self.run_hook(entries))

    def test_background_agent_is_running_work(self):
        entries = [background_agent(T0 - timedelta(minutes=5)), assistant(T0, msg_id="msg_last")]
        self.assertIn("(1 agent)", self.run_hook(entries))

    def test_monitor_is_running_work(self):
        entries = [monitor(T0 - timedelta(minutes=5)), assistant(T0, msg_id="msg_last")]
        self.assertIn("(1 monitor)", self.run_hook(entries))

    def test_counts_are_given_by_kind(self):
        entries = self.busy(background_agent(T0 - timedelta(minutes=8), "a1"),
                            background_agent(T0 - timedelta(minutes=7), "a2"))
        self.assertIn("(1 command, 2 agents)", self.run_hook(entries))

    def test_queued_notification_finishes_the_work(self):
        entries = self.busy(queued(T0 - timedelta(minutes=2), notification("b1")))
        self.assertIsNone(self.run_hook(entries))

    def test_queued_command_attachment_finishes_the_work(self):
        entries = self.busy(attached(T0 - timedelta(minutes=2), notification("b1", "failed")))
        self.assertIsNone(self.run_hook(entries))

    def test_user_message_notification_finishes_the_work(self):
        entries = self.busy(user_text(T0 - timedelta(minutes=2), notification("b1", "killed")))
        self.assertIsNone(self.run_hook(entries))

    def test_task_stop_finishes_the_work(self):
        entries = self.busy(task_stopped(T0 - timedelta(minutes=2), "b1"))
        self.assertIsNone(self.run_hook(entries))

    def test_monitor_event_without_a_status_does_not_finish_it(self):
        entries = [monitor(T0 - timedelta(minutes=5)), queued(T0 - timedelta(minutes=2), monitor_event()),
                   assistant(T0, msg_id="msg_last")]
        self.assertIn("(1 monitor)", self.run_hook(entries))

    def test_notification_for_other_work_leaves_this_running(self):
        entries = self.busy(queued(T0 - timedelta(minutes=2), notification("b-other")))
        self.assertIn("(1 command)", self.run_hook(entries))

    def test_send_message_sets_a_finished_agent_running_again(self):
        entries = [
            background_agent(T0 - timedelta(minutes=20), "a1"),
            queued(T0 - timedelta(minutes=15), notification("a1")),
            send_message(T0 - timedelta(minutes=5), "a1"),
            assistant(T0, msg_id="msg_last"),
        ]
        self.assertIn("(1 agent)", self.run_hook(entries))

    def test_send_message_resumes_a_foreground_agent_in_the_background(self):
        entries = [foreground_agent(T0 - timedelta(minutes=20), "a9"),
                   send_message(T0 - timedelta(minutes=5), "a9"), assistant(T0, msg_id="msg_last")]
        self.assertIn("(1 agent)", self.run_hook(entries))

    def test_send_message_to_an_unknown_id_starts_nothing(self):
        entries = [send_message(T0 - timedelta(minutes=5), "nobody"), assistant(T0, msg_id="msg_last")]
        self.assertIsNone(self.run_hook(entries))

    def test_sidechain_background_work_is_ignored(self):
        entries = [background_command(T0 - timedelta(minutes=5), sidechain=True), assistant(T0, msg_id="msg_last")]
        self.assertIsNone(self.run_hook(entries))

    def test_work_started_before_the_process_record_is_ignored(self):
        self.record(SESSION, T0 - timedelta(minutes=5))  # resumed after the command was started
        self.assertIsNone(self.run_hook(self.busy()))


class WakeTimeTests(KeepAliveTestCase):
    def test_wakes_at_54_minutes_on_the_hour_cache(self):
        self.assertIsNotNone(self.run_hook(self.busy()))
        self.assertAlmostEqual((self.clock.now - T0).total_seconds(), 54 * 60, delta=1)

    def test_wakes_at_270_seconds_on_the_five_minute_cache(self):
        entries = self.busy(last=assistant(T0, msg_id="msg_last", split=split_5m()))
        self.assertIsNotNone(self.run_hook(entries))
        self.assertAlmostEqual((self.clock.now - T0).total_seconds(), 270, delta=1)

    def test_refresh_is_the_earliest_record_of_the_last_message(self):
        entries = self.busy(assistant(T0, msg_id="msg_last"),
                            last=assistant(T0 + timedelta(minutes=5), msg_id="msg_last"))
        self.clock.now = T0 + timedelta(minutes=5, seconds=20)
        self.assertIsNotNone(self.run_hook(entries))
        self.assertAlmostEqual((self.clock.now - T0).total_seconds(), 54 * 60, delta=1)

    def test_this_hooks_own_turn_written_late_is_adopted(self):
        self.write(self.busy(last=assistant(T0 - timedelta(minutes=5), msg_id="msg_before")))
        self.clock.at(T0 + timedelta(minutes=1), lambda: self.append(assistant(T0, msg_id="msg_last")))
        self.assertIsNotNone(self.run_hook())
        self.assertAlmostEqual((self.clock.now - T0).total_seconds(), 54 * 60, delta=1)


class WaitEndsTests(KeepAliveTestCase):
    def test_newer_turn_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20),
                      lambda: self.append(assistant(T0 + timedelta(minutes=19), msg_id="msg_newer")))
        self.assertIsNone(self.run_hook())
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_work_finishing_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20),
                      lambda: self.append(queued(T0 + timedelta(minutes=20), notification("b1"))))
        self.assertIsNone(self.run_hook())
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_process_record_for_another_session_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: self.record("after-clear", T0 + timedelta(minutes=20)))
        self.assertIsNone(self.run_hook())
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_process_record_gone_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: os.remove(self.record_path()))
        self.assertIsNone(self.run_hook())

    def test_process_gone_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: setattr(self, "alive", False))
        self.assertIsNone(self.run_hook())
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_liveness_is_never_checked_with_a_signal_off_posix(self):
        with mock.patch.object(keep_alive.os, "name", "nt"), mock.patch.object(keep_alive.os, "kill") as kill:
            self.assertTrue(keep_alive.process_alive(PID))
        kill.assert_not_called()

    def test_compaction_after_the_last_turn_means_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy() + [compact_boundary(T0 + timedelta(seconds=10))]))

    def test_small_context_means_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy(last=assistant(T0, size=SMALL, msg_id="msg_last"))))


class CapTests(KeepAliveTestCase):
    def with_pings(self, count, split=None, model="claude-opus-4-5"):
        entries = [assistant(T0 - timedelta(hours=25), msg_id="msg_start", split=split, model=model),
                   background_command(T0 - timedelta(hours=25) + timedelta(minutes=1))]
        for i in range(count):
            when = T0 - timedelta(hours=24) + timedelta(minutes=54 * i)
            entries.append(ping_record(when))
            entries.append(assistant(when + timedelta(seconds=5), msg_id=f"msg_ping{i}", split=split, model=model))
        entries.append(assistant(T0, msg_id="msg_last", split=split, model=model))
        return entries

    def test_nineteenth_wake_up_is_allowed_on_the_hour_unpriced(self):
        self.assertIsNotNone(self.run_hook(self.with_pings(18, model="claude-mystery")))

    def test_twentieth_wake_up_is_refused_on_the_hour_unpriced(self):
        self.assertIsNone(self.run_hook(self.with_pings(19, model="claude-mystery")))

    def test_twelfth_wake_up_is_allowed_on_five_minutes(self):
        self.assertIsNotNone(self.run_hook(self.with_pings(11, split=split_5m(), model="claude-mystery")))

    def test_thirteenth_wake_up_is_refused_on_five_minutes(self):
        self.assertIsNone(self.run_hook(self.with_pings(12, split=split_5m(), model="claude-mystery")))

    def test_list_price_ratio_allows_more_where_reads_are_cheaper(self):
        # Opus 5.5 lists its cache read at $0.20 against $4.00 input: 8.00 / 0.20 = 40 on the hour.
        self.assertIsNotNone(self.run_hook(self.with_pings(19, model="claude-opus-5-5")))

    def test_input_price_override_sets_the_ratio(self):
        env = {**self.env, "CACHE_GUARD_INPUT_PRICE": "4"}  # read at a tenth: 8.00 / 0.40 = 20
        self.assertIsNotNone(self.run_hook(self.with_pings(18, model="claude-opus-5-5"), env=env))
        self.assertIsNone(self.run_hook(self.with_pings(19, model="claude-opus-5-5"), env=env))

    def test_typed_prompt_resets_the_count(self):
        entries = self.with_pings(19, model="claude-mystery")
        entries.insert(-1, user_text(T0 - timedelta(minutes=1), "how is it going?"))
        self.assertIsNotNone(self.run_hook(entries))

    def test_assistant_quoting_the_mark_is_not_a_wake_up(self):
        quoted = assistant(T0 - timedelta(minutes=1), msg_id="msg_q",
                           content=[{"type": "text", "text": keep_alive.KEEP_ALIVE_MARK}])
        entries = self.with_pings(18, model="claude-mystery")
        entries.insert(-1, quoted)
        self.assertIsNotNone(self.run_hook(entries))


class OutputTests(KeepAliveTestCase):
    def main(self, entries):
        self.write(entries)
        payload = {"session_id": SESSION, "transcript_path": self.path}
        real_run = keep_alive.run
        out, err = io.StringIO(), io.StringIO()

        def fake_run(request, env):
            return real_run(request, env, clock=self.clock, sleep=self.clock.sleep, alive=lambda pid: True)

        with mock.patch.object(keep_alive, "run", fake_run), \
                mock.patch.object(keep_alive.sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(keep_alive.os, "environ", self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = keep_alive.main()
        return status, out.getvalue(), err.getvalue()

    def test_wake_up_exits_2_with_the_mark_on_stdout_and_nothing_on_stderr(self):
        status, out, err = self.main(self.busy())
        self.assertEqual(status, 2)
        self.assertTrue(out.startswith(keep_alive.KEEP_ALIVE_MARK))
        self.assertEqual(out.count("\n"), 1)
        self.assertIn("make no tool calls", out)
        self.assertEqual(err, "")

    def test_nothing_to_do_exits_0_with_no_output(self):
        status, out, err = self.main(self.busy(task_stopped(T0 - timedelta(minutes=1))))
        self.assertEqual((status, out, err), (0, "", ""))

    def test_headless_script_exits_0_at_once_with_no_output(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CACHE_GUARD_", "CLAUDE_"))}
        env.update({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_PID": str(os.getpid()),
                    "CACHE_GUARD_STATE_DIR": self.state})
        script = os.path.join(os.path.abspath(HOOKS), "keep_alive.py")
        started = time.monotonic()
        done = subprocess.run([sys.executable, script], input='{"session_id":"x"}', capture_output=True,
                              text=True, env=env, timeout=30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))


class RegistrationTests(unittest.TestCase):
    def test_stop_hook_is_registered_async_with_room_to_wait_an_hour(self):
        with open(os.path.join(HOOKS, "hooks.json"), encoding="utf-8") as f:
            stop = json.load(f)["hooks"]["Stop"]
        hook = stop[0]["hooks"][0]
        self.assertIs(hook["asyncRewake"], True)
        self.assertGreaterEqual(hook["timeout"], 3600)
        self.assertIn("keep_alive.py", hook["command"])

    def test_process_record_name_is_swept_once_stale(self):
        self.assertTrue(cache_guard.swept(f"process-{PID}"))
        self.assertTrue(cache_guard.swept("process-tmp-abc123.tmp"))


if __name__ == "__main__":
    unittest.main()
