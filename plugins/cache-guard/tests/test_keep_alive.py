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


def persistent_monitor(when, task="m1"):
    return tool_result(when, {"taskId": task, "timeoutMs": 0, "persistent": True})


def notification(task, status="completed"):
    status_line = f"<status>{status}</status>\n" if status else ""
    return (f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
            f"{status_line}<summary>finished</summary>\n</task-notification>")


def task(task_id="b1", kind="shell", status="running", **extra):
    """One item of the Stop hook's `background_tasks`, as Claude Code lists it."""
    return {"id": task_id, "type": kind, "status": status, "description": f"{kind} {task_id}", **extra}


def shell(task_id="b1", status="running"):
    return task(task_id, "shell", status, command="make all")


def last_text(entries):
    """The text Claude Code sends as `last_assistant_message` for a transcript, or None when it has none."""
    main = [e for e in entries if e.get("type") == "assistant" and not e.get("isSidechain")]
    if not main:
        return None
    wanted = main[-1]["message"]["id"]
    texts = [block["text"] for e in main if e["message"]["id"] == wanted
             for block in e["message"]["content"] if block.get("type") == "text"]
    return "\n".join(texts).strip() or None


def queued(when, text):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(when), "content": text}


def attached(when, text):
    return {"type": "attachment", "timestamp": stamp(when),
            "attachment": {"type": "queued_command", "prompt": text, "commandMode": "task-notification"}}


def user_text(when, text):
    return {"type": "user", "timestamp": stamp(when), "version": VERSION,
            "message": {"role": "user", "content": text}}


def ping_records(when, shape="idle"):
    """One wake-up as Claude Code stores it: the idle shape, or the one delivered in the middle of a turn."""
    text = ("<task-notification>\n<summary>cache-guard: kept the prompt cache warm</summary>\n"
            "</task-notification>\n<system-reminder>\nA cache-guard hook woke this session: "
            + keep_alive.wake_message(["shell"]) + "\n</system-reminder>")
    second = {"type": "queue-operation", "operation": "dequeue" if shape == "idle" else "remove",
              "timestamp": stamp(when)}
    third = user_text(when, text) if shape == "idle" else attached(when, text)
    return [queued(when, text), second, third]


def prompt_record(when, text="carry on"):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(when), "content": text}


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


FROM_ENTRIES = object()  # run_hook: send the text of the given transcript's last message, as Claude Code does
OMITTED = object()  # run_hook: leave `background_tasks` out of the hook's input


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
        self.record(SESSION)
        self.clock = FakeClock(T0 + timedelta(seconds=20))
        self.alive = True

    def record(self, session):
        os.makedirs(self.state, exist_ok=True)
        with open(self.record_path(), "w", encoding="utf-8") as f:
            json.dump({"session_id": session}, f)

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

    def payload(self, entries=None, session=SESSION, tasks=None, last_message=FROM_ENTRIES):
        """The Stop hook's input: one running command unless `tasks` says otherwise, and the text of
        the last message of `entries` when they are given (a test that writes the transcript itself and
        adds the turn later sends no text unless it passes `last_message`)."""
        payload = {"session_id": session, "transcript_path": self.path, "hook_event_name": "Stop"}
        if tasks is not OMITTED:
            payload["background_tasks"] = [shell()] if tasks is None else tasks
        if last_message is FROM_ENTRIES:
            last_message = last_text(entries) if entries is not None else None
        if last_message is not None:
            payload["last_assistant_message"] = last_message
        return payload

    def run_hook(self, entries=None, env=None, session=SESSION, tasks=None, last_message=FROM_ENTRIES):
        if entries is not None:
            self.write(entries)
        payload = self.payload(entries, session, tasks, last_message)
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

    def assertWokeAt(self, out, seconds_after_t0):
        self.assertIsNotNone(out)
        self.assertAlmostEqual((self.clock.now - T0).total_seconds(), seconds_after_t0, delta=1)


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
        self.record("someone-else")
        self.assertIsNone(self.run_hook(self.busy()))

    def test_process_record_without_a_start_time_is_enough(self):
        with open(self.record_path(), "w", encoding="utf-8") as f:
            json.dump({"session_id": SESSION}, f)
        self.assertIsNotNone(self.run_hook(self.busy()))

    def test_matching_process_record_is_touched(self):
        os.utime(self.record_path(), (1, 1))
        self.run_hook(self.busy())
        self.assertGreater(os.path.getmtime(self.record_path()), time.time() - 600)


class RunningWorkTests(KeepAliveTestCase):
    """What is running is what Claude Code lists in the hook's input as `background_tasks`."""

    def test_running_command_is_running_work(self):
        self.assertIn("(1 command)", self.run_hook(self.busy(), tasks=[shell()]))

    def test_pending_task_is_running_work(self):
        self.assertIn("(1 agent)", self.run_hook(self.busy(), tasks=[task("a1", "subagent", "pending")]))

    def test_finished_task_is_not_running_work(self):
        for status in ("completed", "failed", "killed"):
            with self.subTest(status=status):
                self.assertIsNone(self.run_hook(self.busy(), tasks=[shell(status=status)]))
                self.assertEqual(self.clock.sleeps, 0)

    def test_nothing_in_flight_is_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy(), tasks=[]))
        self.assertEqual(self.clock.sleeps, 0)

    def test_claude_codes_own_housekeeping_is_not_running_work(self):
        tasks = [task("d1", "dream"), task("s1", "auto-mode scan"), task("i1", "memory import")]
        self.assertIsNone(self.run_hook(self.busy(), tasks=tasks))
        self.assertEqual(self.clock.sleeps, 0)

    def test_missing_task_list_is_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy(), tasks=OMITTED))
        self.assertEqual(self.clock.sleeps, 0)

    def test_task_list_that_is_not_a_list_is_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy(), tasks={"b1": shell()}))

    def test_persistent_monitor_is_not_running_work(self):
        entries = self.busy(persistent_monitor(T0 - timedelta(minutes=5), "m1"))
        self.assertIsNone(self.run_hook(entries, tasks=[task("m1", "monitor")]))

    def test_persistent_monitor_is_left_out_of_the_count(self):
        entries = self.busy(persistent_monitor(T0 - timedelta(minutes=5), "m1"))
        out = self.run_hook(entries, tasks=[task("m1", "monitor"), task("m2", "monitor"), shell()])
        self.assertIn("(1 command, 1 monitor)", out)

    def test_monitor_that_is_not_persistent_is_running_work(self):
        self.assertIn("(1 monitor)", self.run_hook(self.busy(), tasks=[task("m2", "monitor")]))

    def test_work_is_named_by_type(self):
        tasks = [shell("b1"), task("a1", "subagent"), task("a2", "subagent"), task("w1", "workflow"),
                 task("m1", "monitor"), task("t1", "MCP task"), task("t2", "teammate"), task("d1", "dream")]
        self.assertIn("(1 command, 2 agents, 1 workflow, 1 monitor, 2 tasks)", self.run_hook(self.busy(), tasks=tasks))

    def test_work_that_ended_in_the_transcript_still_counts_while_listed(self):
        # The list is the authority: a notification written before the turn ended says nothing more.
        entries = self.busy(queued(T0 - timedelta(minutes=2), notification("b-other")))
        self.assertIn("(1 command)", self.run_hook(entries))


class WakeTimeTests(KeepAliveTestCase):
    def test_wakes_at_54_minutes_on_the_hour_cache(self):
        self.assertWokeAt(self.run_hook(self.busy()), 54 * 60)

    def test_wakes_at_270_seconds_on_the_five_minute_cache(self):
        entries = self.busy(last=assistant(T0, msg_id="msg_last", split=split_5m()))
        self.assertWokeAt(self.run_hook(entries), 270)

    def test_refresh_is_the_earliest_record_of_the_last_message(self):
        entries = self.busy(assistant(T0, msg_id="msg_last"),
                            last=assistant(T0 + timedelta(minutes=5), msg_id="msg_last"))
        self.clock.now = T0 + timedelta(minutes=5, seconds=20)
        self.assertWokeAt(self.run_hook(entries), 54 * 60)

    def test_long_final_message_on_the_five_minute_cache_wakes_at_once(self):
        # Ported from review: its first record is older than the whole wait by the time the hook starts.
        env = {**self.env, "CACHE_GUARD_LIFETIME_SECONDS": "300"}
        self.clock = FakeClock(T0 + timedelta(seconds=275))
        self.assertWokeAt(self.run_hook(self.busy(), env=env), 275)
        self.assertEqual(self.clock.sleeps, 0)


def answer(when, msg_id, text):
    return assistant(when, msg_id=msg_id, content=[{"type": "text", "text": text}])


class SettleTests(KeepAliveTestCase):
    """The Stop hook can start before its own turn's last message is in the transcript."""

    def before(self, *after):
        """A turn an hour old, whose wake-up would be long past due, then this turn's prompt."""
        return [answer(T0 - timedelta(minutes=70), "msg_start", "started it"),
                background_command(T0 - timedelta(minutes=69)),
                answer(T0 - timedelta(minutes=60), "msg_before", "earlier answer"),
                *after]

    def test_own_message_appearing_one_poll_later_is_woken_on_once_at_its_time(self):
        self.write(self.before(user_text(T0 - timedelta(seconds=5), "how is it going?")))
        self.clock.at(self.clock.now + timedelta(seconds=keep_alive.POLL_SECONDS),
                      lambda: self.append(answer(T0, "msg_own", "still building")))
        real = keep_alive.wake_message
        with mock.patch.object(keep_alive, "wake_message", side_effect=real) as wake:
            out = self.run_hook(last_message="still building")
        self.assertWokeAt(out, 54 * 60)
        self.assertEqual(wake.call_count, 1)

    def test_answer_that_reads_like_the_one_before_is_still_waited_for(self):
        # A wake-up's answer usually reads like the one before it; the turn's own prompt tells them apart.
        self.write(self.before(*ping_records(T0 - timedelta(seconds=5))))
        self.clock.at(self.clock.now + timedelta(seconds=keep_alive.POLL_SECONDS),
                      lambda: self.append(answer(T0, "msg_own", "earlier answer")))
        self.assertWokeAt(self.run_hook(last_message="earlier answer"), 54 * 60)

    def test_own_message_never_appearing_exits_quietly_after_the_grace(self):
        self.write(self.before(user_text(T0 - timedelta(seconds=5), "how is it going?")))
        self.assertIsNone(self.run_hook(last_message="still building"))
        self.assertGreaterEqual(self.clock.now, T0 + timedelta(seconds=20 + keep_alive.STALE_GRACE_SECONDS))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=5))

    def test_different_text_on_the_last_message_is_not_taken_for_it(self):
        self.write(self.before())
        self.assertIsNone(self.run_hook(last_message="still building"))
        self.assertGreaterEqual(self.clock.now, T0 + timedelta(seconds=20 + keep_alive.STALE_GRACE_SECONDS))

    def test_text_is_compared_trimmed(self):
        entries = self.busy(last=answer(T0, "msg_last", "\n  done for now  \n"))
        self.assertWokeAt(self.run_hook(entries, last_message="done for now\n"), 54 * 60)

    def test_text_of_a_message_written_in_several_records_is_joined(self):
        entries = self.busy(answer(T0, "msg_last", "first part"),
                            last=answer(T0 + timedelta(seconds=2), "msg_last", "second part"))
        self.assertWokeAt(self.run_hook(entries, last_message="first part\nsecond part"), 54 * 60)

    def test_without_text_the_last_turn_record_being_an_answer_settles(self):
        for text in (None, ""):
            with self.subTest(text=text):
                self.clock = FakeClock(T0 + timedelta(seconds=20))
                self.assertWokeAt(self.run_hook(self.busy(), last_message=text), 54 * 60)

    def test_without_text_a_prompt_after_the_last_answer_is_waited_out(self):
        self.write(self.before(user_text(T0 - timedelta(seconds=5), "how is it going?")))
        self.clock.at(self.clock.now + timedelta(seconds=keep_alive.POLL_SECONDS),
                      lambda: self.append(answer(T0, "msg_own", "still building")))
        self.assertWokeAt(self.run_hook(last_message=None), 54 * 60)

    def test_without_text_a_tool_result_after_the_last_answer_is_waited_out(self):
        self.write(self.before(background_command(T0 - timedelta(seconds=5), "b2")))
        self.assertIsNone(self.run_hook(last_message=None))
        self.assertGreaterEqual(self.clock.now, T0 + timedelta(seconds=20 + keep_alive.STALE_GRACE_SECONDS))

    def test_without_text_a_local_command_after_the_answer_does_not_hold_it_up(self):
        entries = self.busy() + [user_text(T0 + timedelta(seconds=10), "<bash-input>ls</bash-input>")]
        self.assertWokeAt(self.run_hook(entries, last_message=None), 54 * 60)

    def test_process_gone_while_settling_ends_it(self):
        self.write(self.before(user_text(T0 - timedelta(seconds=5), "how is it going?")))
        self.clock.at(self.clock.now + timedelta(seconds=1), lambda: setattr(self, "alive", False))
        self.assertIsNone(self.run_hook(last_message="still building"))
        self.assertLess(self.clock.now, T0 + timedelta(seconds=20 + keep_alive.POLL_SECONDS + 1))


class ReviewReproTests(KeepAliveTestCase):
    """Ported from review: each failed before the hook waited for its own turn and ignored local commands."""

    def before(self, *after):
        return [assistant(T0 - timedelta(minutes=10), msg_id="msg_start"),
                background_command(T0 - timedelta(minutes=9)),
                assistant(T0 - timedelta(minutes=5), msg_id="msg_P"), *after]

    def test_race_on_prompted_text_only_turn(self):
        # The hook for turn Q starts before Q's answer is on disk; Q's prompt is.
        self.write(self.before(user_text(T0 - timedelta(seconds=5), "quick question")))
        self.clock.at(self.clock.now + timedelta(seconds=keep_alive.POLL_SECONDS),
                      lambda: self.append(assistant(T0, msg_id="msg_Q")))
        self.assertWokeAt(self.run_hook(last_message="ok"), 54 * 60)

    def test_race_on_notification_turn(self):
        started = background_command(T0 - timedelta(minutes=9), task="b2")
        self.write(self.before(started, user_text(T0 - timedelta(seconds=5), notification("b2"))))
        self.clock.at(self.clock.now + timedelta(seconds=keep_alive.POLL_SECONDS),
                      lambda: self.append(assistant(T0, msg_id="msg_Q")))
        self.assertWokeAt(self.run_hook(last_message="ok"), 54 * 60)

    def test_local_command_while_idle(self):
        self.write(self.busy())
        at = T0 + timedelta(minutes=5)
        command = ("<command-name>/tasks</command-name>\n            <command-message>tasks</command-message>\n"
                   "            <command-args></command-args>")
        self.clock.at(at, lambda: self.append(user_text(at, command)))
        self.assertWokeAt(self.run_hook(last_message="ok"), 54 * 60)

    def test_long_final_message_5m(self):
        env = {**self.env, "CACHE_GUARD_LIFETIME_SECONDS": "300"}
        self.clock = FakeClock(T0 + timedelta(seconds=275))
        self.assertIsNotNone(self.run_hook(self.busy(), env=env))


class LocalCommandTests(KeepAliveTestCase):
    """A local command or a meta record writes a user record but starts no turn."""

    SHAPES = (
        "<command-name>/context</command-name>\n<command-message>context</command-message>",
        "<command-message>effort</command-message>",
        "<local-command-stdout>Set effort level to high</local-command-stdout>",
        "<bash-input>git status</bash-input>",
        "<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>",
        "<bash-stderr>oops</bash-stderr>",
    )

    def test_local_command_mid_wait_does_not_end_it(self):
        for text in self.SHAPES:
            with self.subTest(text=text):
                self.clock = FakeClock(T0 + timedelta(seconds=20))
                self.write(self.busy())
                at = T0 + timedelta(minutes=5)
                self.clock.at(at, lambda text=text: self.append(user_text(at, text)))
                self.assertWokeAt(self.run_hook(last_message="ok"), 54 * 60)

    def test_meta_record_mid_wait_does_not_end_it(self):
        self.write(self.busy())
        at = T0 + timedelta(minutes=5)
        meta = {**user_text(at, "Caveat: the messages below were generated by the user"), "isMeta": True}
        self.clock.at(at, lambda: self.append(meta))
        self.assertWokeAt(self.run_hook(last_message="ok"), 54 * 60)

    def test_local_command_does_not_reset_the_wake_up_count(self):
        entries = CapTests.with_pings(self, 19, model="claude-mystery")
        entries.insert(-1, user_text(T0 - timedelta(minutes=1), "<command-name>/tasks</command-name>"))
        self.assertIsNone(self.run_hook(entries))


class WaitEndsTests(KeepAliveTestCase):
    def test_newer_turn_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20),
                      lambda: self.append(assistant(T0 + timedelta(minutes=19), msg_id="msg_newer")))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_prompt_typed_mid_wait_ends_it(self):
        self.write(self.busy())
        at = T0 + timedelta(minutes=20)
        self.clock.at(at, lambda: self.append(prompt_record(at, "and another thing")))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_notification_mid_wait_ends_it(self):
        self.write(self.busy())
        at = T0 + timedelta(minutes=20)
        self.clock.at(at, lambda: self.append(queued(at, notification("b1"))))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_process_record_for_another_session_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: self.record("after-clear"))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_process_record_gone_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: os.remove(self.record_path()))
        self.assertIsNone(self.run_hook(last_message="ok"))

    def test_process_gone_ends_the_wait(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20), lambda: setattr(self, "alive", False))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_liveness_is_never_checked_with_a_signal_off_posix(self):
        with mock.patch.object(keep_alive.os, "name", "nt"), mock.patch.object(keep_alive.os, "kill") as kill:
            self.assertTrue(keep_alive.process_alive(PID))
        kill.assert_not_called()

    def test_compaction_after_the_last_turn_means_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy() + [compact_boundary(T0 + timedelta(seconds=10))]))

    def test_compaction_mid_wait_ends_it(self):
        self.write(self.busy())
        self.clock.at(T0 + timedelta(minutes=20),
                      lambda: self.append(compact_boundary(T0 + timedelta(minutes=20))))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=21))

    def test_prompt_after_the_last_turn_means_no_wake_up(self):
        for record in (user_text(T0 + timedelta(minutes=53, seconds=50), "and another thing"),
                       attached(T0 + timedelta(minutes=53, seconds=50), "and another thing"),
                       prompt_record(T0 + timedelta(minutes=53, seconds=50), "and another thing")):
            with self.subTest(type=record["type"]):
                self.clock = FakeClock(T0 + timedelta(seconds=20))
                self.write(self.busy() + [record])
                self.assertIsNone(self.run_hook(last_message="ok"))

    def test_enqueue_without_content_does_not_stop_the_wake_up(self):
        record = {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(T0 + timedelta(seconds=5))}
        self.assertIsNotNone(self.run_hook(self.busy() + [record], last_message="ok"))

    def test_prompt_arriving_just_before_the_wake_up_ends_the_wait(self):
        self.write(self.busy())
        at = T0 + timedelta(minutes=53, seconds=50)
        self.clock.at(at, lambda: self.append(user_text(at, "hello")))
        self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertLess(self.clock.now, T0 + timedelta(minutes=54))

    def test_prompt_written_between_the_last_poll_and_the_wake_up_is_seen(self):
        self.write(self.busy())
        real = keep_alive.evaluate
        calls = []

        def evaluate(path, env, settled_id, tasks):
            calls.append(1)
            if len(calls) == 2:  # the final check: nothing changed while waiting, until now
                self.append(user_text(T0 + timedelta(minutes=54), "hello"))
            return real(path, env, settled_id, tasks)

        with mock.patch.object(keep_alive, "evaluate", evaluate):
            self.assertIsNone(self.run_hook(last_message="ok"))
        self.assertEqual(len(calls), 2)

    def test_small_context_means_no_wake_up(self):
        self.assertIsNone(self.run_hook(self.busy(last=assistant(T0, size=SMALL, msg_id="msg_last"))))


class CapTests(KeepAliveTestCase):
    def with_pings(self, count, split=None, model="claude-opus-4-5", shape="idle"):
        entries = [assistant(T0 - timedelta(hours=25), msg_id="msg_start", split=split, model=model),
                   background_command(T0 - timedelta(hours=25) + timedelta(minutes=1))]
        for i in range(count):
            when = T0 - timedelta(hours=24) + timedelta(minutes=54 * i)
            entries.extend(ping_records(when, shape))
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

    def test_cheaper_list_price_read_gets_no_more_wake_ups_than_the_ceiling(self):
        # Opus 5.5 lists its cache read at $0.20 against $4.00 input: 8.00 / 0.20 = 40, held to 20 here.
        self.assertIsNotNone(self.run_hook(self.with_pings(18, model="claude-opus-5-5")))
        self.clock = FakeClock(T0 + timedelta(seconds=20))
        self.assertIsNone(self.run_hook(self.with_pings(19, model="claude-opus-5-5")))

    def test_three_wake_ups_count_three_in_each_stored_shape(self):
        for shape in ("idle", "mid-turn"):
            with self.subTest(shape=shape):
                entries = self.with_pings(3, shape=shape)
                lines = [json.dumps(entry).encode() for entry in entries]
                self.assertEqual(keep_alive.pings_since_prompt(lines, keep_alive.records(lines)), 3)

    def test_each_stored_shape_hits_the_cap_at_the_same_wake_up(self):
        for shape in ("idle", "mid-turn"):
            with self.subTest(shape=shape):
                self.clock = FakeClock(T0 + timedelta(seconds=20))
                self.assertIsNotNone(self.run_hook(self.with_pings(18, model="claude-mystery", shape=shape)))
                self.clock = FakeClock(T0 + timedelta(seconds=20))
                self.assertIsNone(self.run_hook(self.with_pings(19, model="claude-mystery", shape=shape)))

    def test_enqueued_prompt_resets_the_count(self):
        entries = self.with_pings(19, model="claude-mystery")
        entries.insert(-1, prompt_record(T0 - timedelta(minutes=1)))
        self.assertIsNotNone(self.run_hook(entries))

    def test_input_price_override_sets_the_ratio(self):
        env = {**self.env, "CACHE_GUARD_INPUT_PRICE": "4"}  # read at a tenth: 8.00 / 0.40 = 20
        self.assertIsNotNone(self.run_hook(self.with_pings(18, model="claude-opus-5-5"), env=env))
        self.clock = FakeClock(T0 + timedelta(seconds=20))
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
    def main(self, entries, tasks=None):
        self.write(entries)
        payload = self.payload(entries, tasks=tasks)
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
        self.assertIn("background work is still running (1 command)", out)
        self.assertIn("make no tool calls", out)
        self.assertIn("If you are in the middle of other work, ignore this and carry on.", out)
        self.assertEqual(err, "")

    def test_nothing_to_do_exits_0_with_no_output(self):
        status, out, err = self.main(self.busy(), tasks=[shell(status="completed")])
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


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-sweep-test-")
        self.addCleanup(self.tmp.cleanup)

    def old(self, name):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(path, (1, 1))
        return path

    def test_live_process_record_survives_the_sweep(self):
        live, dead = self.old("process-111"), self.old("process-222")
        with mock.patch.object(cache_guard, "process_alive", lambda pid: pid == 111):
            cache_guard.prune(self.tmp.name, T0)
        self.assertTrue(os.path.exists(live))
        self.assertFalse(os.path.exists(dead))

    def test_leftover_temporary_is_swept_whatever_is_alive(self):
        leftover = self.old("process-tmp-abc123.tmp")
        with mock.patch.object(cache_guard, "process_alive", lambda pid: True):
            cache_guard.prune(self.tmp.name, T0)
        self.assertFalse(os.path.exists(leftover))

    def test_liveness_is_not_asked_off_posix(self):
        record = self.old("process-111")
        with mock.patch.object(cache_guard.os, "name", "nt"), \
                mock.patch.object(cache_guard, "process_alive") as alive:
            cache_guard.prune(self.tmp.name, T0)
        alive.assert_not_called()
        self.assertFalse(os.path.exists(record))


if __name__ == "__main__":
    unittest.main()
