"""Tests for the cache-expiry guard. Run: python3 -m unittest discover -s plugins/cache-guard/tests"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

HOOKS = os.path.join(os.path.dirname(__file__), "..", "hooks")
sys.path.insert(0, os.path.abspath(HOOKS))

import cache_guard  # noqa: E402

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
LARGE = 310_000
SMALL = 40_000


def assistant(when, size, split=None, sidechain=False, model="claude-opus-4"):
    usage = {
        "input_tokens": 12,
        "cache_read_input_tokens": size - 12,
        "cache_creation_input_tokens": 0,
    }
    if split is not None:
        usage["cache_creation_input_tokens"] = sum(split.values())
        usage["cache_read_input_tokens"] = size - 12 - usage["cache_creation_input_tokens"]
        usage["cache_creation"] = split
    entry = {
        "type": "assistant",
        "timestamp": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "message": {"id": f"msg_{when.timestamp()}", "model": model, "usage": usage},
    }
    if sidechain:
        entry["isSidechain"] = True
    return entry


def split_1h(tokens=90_000):
    return {"ephemeral_1h_input_tokens": tokens, "ephemeral_5m_input_tokens": 0}


def split_5m(tokens=90_000):
    return {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": tokens}


def model_set(name, sidechain=False):
    """What `/model` leaves in the transcript: the command's own output, as a plain string."""
    return local_command(
        f"<local-command-stdout>Set model to `{name}` and saved as your default for new sessions</local-command-stdout>",
        sidechain=sidechain,
    )


def effort_set(level, sidechain=False):
    return local_command(
        f"<local-command-stdout>Set effort level to {level} (saved as your default for new sessions): "
        "Comprehensive analysis</local-command-stdout>",
        sidechain=sidechain,
    )


def local_command(content, sidechain=False):
    entry = {
        "type": "user",
        "timestamp": "2026-01-02T11:59:00.000Z",
        "message": {"role": "user", "content": content},
    }
    if sidechain:
        entry["isSidechain"] = True
    return entry


def tool_result_quoting(content):
    """The same text read back by a tool: list content, so it is a quotation and not a setting."""
    return {
        "type": "user",
        "timestamp": "2026-01-02T11:59:00.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": content}],
        },
    }


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-guard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.markers = os.path.join(self.tmp.name, "state")

    def transcript(self, entries, name="transcript.jsonl"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return path

    def decide(self, entries=None, now=NOW, env=None, path=None, prompt="carry on", session="s-1"):
        if path is None:
            path = self.transcript(entries or [])
        payload = {"session_id": session, "transcript_path": path, "prompt": prompt}
        return cache_guard.decide(payload, now, env or {}, self.markers)

    def marker_count(self):
        return len(os.listdir(self.markers)) if os.path.isdir(self.markers) else 0


class WarmAndColdTests(GuardTestCase):
    def test_a_warm_large_context_is_allowed(self):
        entries = [assistant(NOW - timedelta(minutes=20), LARGE, split_1h())]
        self.assertIsNone(self.decide(entries))
        self.assertEqual(self.marker_count(), 0)

    def test_a_cold_large_context_is_blocked_with_the_cost_of_sending_it(self):
        entries = [assistant(NOW - timedelta(hours=1, minutes=24), LARGE, split_1h())]
        reason = self.decide(entries)
        self.assertIsNotNone(reason)
        self.assertIn("idle 1h 24m", reason)
        self.assertIn("310k tokens", reason)
        self.assertIn("20x", reason)
        self.assertIn("cache lasts 1h", reason)
        self.assertIn("CACHE_GUARD_MIN_TOKENS=100000", reason)

    def test_a_five_minute_split_gives_a_five_minute_lifetime_and_a_smaller_multiplier(self):
        entries = [assistant(NOW - timedelta(minutes=10), LARGE, split_5m())]
        reason = self.decide(entries)
        self.assertIsNotNone(reason)
        self.assertIn("12.5x", reason)
        self.assertIn("cache lasts 5m", reason)
        self.assertIn("idle 10m", reason)

    def test_a_turn_with_no_split_in_the_tail_is_treated_as_the_short_lifetime(self):
        entries = [assistant(NOW - timedelta(minutes=10), LARGE)]
        reason = self.decide(entries)
        self.assertIn("cache lasts 5m", reason)

    def test_a_cold_context_under_the_threshold_is_allowed(self):
        entries = [assistant(NOW - timedelta(hours=3), SMALL, split_1h())]
        self.assertIsNone(self.decide(entries))
        self.assertEqual(self.marker_count(), 0)

    def test_a_million_token_context_reads_in_millions(self):
        entries = [assistant(NOW - timedelta(hours=2), 1_240_000, split_1h())]
        self.assertIn("1.2M tokens", self.decide(entries))


class ConfirmationTests(GuardTestCase):
    def setUp(self):
        super().setUp()
        self.entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]

    def test_sending_the_message_again_inside_the_window_lets_it_through_once(self):
        self.assertIsNotNone(self.decide(self.entries))
        self.assertEqual(self.marker_count(), 1)
        self.assertIsNone(self.decide(self.entries, now=NOW + timedelta(seconds=30)))
        self.assertEqual(self.marker_count(), 0)
        # The confirmation is spent: a third message into the same cold context is held back again.
        self.assertIsNotNone(self.decide(self.entries, now=NOW + timedelta(seconds=40)))

    def test_sending_it_again_after_the_window_is_held_back_again(self):
        self.assertIsNotNone(self.decide(self.entries))
        self.assertIsNotNone(self.decide(self.entries, now=NOW + timedelta(seconds=300)))

    def test_the_confirmation_window_is_configurable(self):
        self.assertIsNotNone(self.decide(self.entries, env={"CACHE_GUARD_CONFIRM_SECONDS": "600"}))
        again = self.decide(
            self.entries, now=NOW + timedelta(seconds=300), env={"CACHE_GUARD_CONFIRM_SECONDS": "600"}
        )
        self.assertIsNone(again)

    def test_a_warm_turn_clears_a_marker_left_by_an_earlier_cold_one(self):
        self.assertIsNotNone(self.decide(self.entries))
        warm = [assistant(NOW - timedelta(minutes=1), LARGE, split_1h())]
        self.assertIsNone(self.decide(warm))
        self.assertEqual(self.marker_count(), 0)

    def test_markers_older_than_a_day_are_swept(self):
        self.assertIsNotNone(self.decide(self.entries))
        stale = os.path.join(self.markers, "long-gone-session")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("")
        old = (NOW - timedelta(days=3)).timestamp()
        os.utime(stale, (old, old))
        self.decide(self.entries, now=NOW + timedelta(hours=1))
        self.assertEqual(os.listdir(self.markers), ["s-1"])

    def test_a_session_id_with_no_usable_characters_fails_open(self):
        self.assertIsNone(self.decide(self.entries, session="///"))
        self.assertEqual(self.marker_count(), 0)

    def test_a_session_id_is_sanitised_before_it_becomes_a_path(self):
        self.assertIsNotNone(self.decide(self.entries, session="../../escape"))
        self.assertEqual(os.listdir(self.markers), ["escape"])


class TranscriptReadingTests(GuardTestCase):
    def test_sidechain_and_synthetic_lines_are_not_the_last_turn(self):
        entries = [
            assistant(NOW - timedelta(hours=2), LARGE, split_1h()),
            assistant(NOW - timedelta(minutes=2), LARGE, split_1h(), sidechain=True),
            assistant(NOW - timedelta(minutes=1), LARGE, model="<synthetic>"),
        ]
        reason = self.decide(entries)
        self.assertIsNotNone(reason)
        self.assertIn("idle 2h 0m", reason)

    def test_a_subagents_five_minute_split_does_not_set_the_sessions_lifetime(self):
        entries = [
            assistant(NOW - timedelta(hours=2), LARGE, split_1h()),
            assistant(NOW - timedelta(hours=1), LARGE, split_5m(), sidechain=True),
        ]
        self.assertIn("cache lasts 1h", self.decide(entries))

    def test_only_the_tail_is_read_and_a_halved_first_line_does_not_raise(self):
        padding = [
            {"type": "user", "timestamp": "2026-01-02T09:00:00.000Z", "message": {"text": "x" * 10_000}}
            for _ in range(80)
        ]
        entries = padding + [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        path = self.transcript(entries)
        self.assertGreater(os.path.getsize(path), cache_guard.TAIL)
        reason = self.decide(path=path)
        self.assertIsNotNone(reason)
        self.assertIn("310k tokens", reason)

    def test_a_turn_beyond_the_tail_is_out_of_reach(self):
        # The old turn is the only one carrying a 1h split; past the tail it cannot be read, so the
        # guard falls back to the short lifetime rather than guessing.
        old = [assistant(NOW - timedelta(hours=5), LARGE, split_1h())]
        padding = [
            {"type": "user", "timestamp": "2026-01-02T09:00:00.000Z", "message": {"text": "x" * 10_000}}
            for _ in range(80)
        ]
        entries = old + padding + [assistant(NOW - timedelta(minutes=30), LARGE)]
        reason = self.decide(entries)
        self.assertIn("cache lasts 5m", reason)
        self.assertIn("12.5x", reason)


class FailOpenTests(GuardTestCase):
    def test_a_slash_command_passes_untouched(self):
        entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        self.assertIsNone(self.decide(entries, prompt="/clear"))
        self.assertEqual(self.marker_count(), 0)

    def test_a_missing_transcript_path_allows(self):
        payload = {"session_id": "s-1", "prompt": "hello"}
        self.assertIsNone(cache_guard.decide(payload, NOW, {}, self.markers))

    def test_a_transcript_that_is_not_there_allows(self):
        self.assertIsNone(self.decide(path=os.path.join(self.tmp.name, "absent.jsonl")))

    def test_an_empty_transcript_allows(self):
        self.assertIsNone(self.decide([]))

    def test_a_transcript_with_no_assistant_turn_allows(self):
        entries = [{"type": "user", "timestamp": "2026-01-02T09:00:00.000Z", "message": {"text": "hi"}}]
        self.assertIsNone(self.decide(entries))

    def test_an_unparsable_line_is_skipped_rather_than_fatal(self):
        path = os.path.join(self.tmp.name, "broken.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"type":"assistant" not json at all\n')
            f.write(json.dumps(assistant(NOW - timedelta(hours=2), LARGE, split_1h())) + "\n")
        self.assertIsNotNone(self.decide(path=path))

    def test_a_turn_with_no_timestamp_is_not_the_last_turn(self):
        entry = assistant(NOW - timedelta(hours=2), LARGE, split_1h())
        undated = assistant(NOW, LARGE, split_1h())
        del undated["timestamp"]
        self.assertIsNotNone(self.decide([entry, undated]))

    def test_garbage_stdin_allows(self):
        self.assertIsNone(cache_guard.decide("not a payload at all", NOW, {}, self.markers))
        self.assertIsNone(cache_guard.decide({}, NOW, {}, self.markers))


class EnvironmentTests(GuardTestCase):
    def test_the_threshold_can_be_lowered(self):
        entries = [assistant(NOW - timedelta(hours=2), SMALL, split_1h())]
        reason = self.decide(entries, env={"CACHE_GUARD_MIN_TOKENS": "10000"})
        self.assertIsNotNone(reason)
        self.assertIn("CACHE_GUARD_MIN_TOKENS=10000", reason)

    def test_the_threshold_can_be_raised(self):
        entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        self.assertIsNone(self.decide(entries, env={"CACHE_GUARD_MIN_TOKENS": "500000"}))

    def test_the_lifetime_can_be_overridden(self):
        entries = [assistant(NOW - timedelta(minutes=45), LARGE, split_1h())]
        self.assertIsNone(self.decide(entries))  # warm under the detected hour
        reason = self.decide(entries, env={"CACHE_GUARD_LIFETIME_SECONDS": "300"})
        self.assertIn("cache lasts 5m", reason)
        self.assertIn("12.5x", reason)

    def test_values_that_are_not_positive_integers_fall_back_to_the_defaults(self):
        entries = [assistant(NOW - timedelta(hours=2), SMALL, split_1h())]
        for bad in ("banana", "", "0", "-5", "1e5"):
            with self.subTest(value=bad):
                env = {"CACHE_GUARD_MIN_TOKENS": bad, "CACHE_GUARD_LIFETIME_SECONDS": bad}
                self.assertIsNone(self.decide(entries, env=env))  # default threshold still 100k
        large = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        reason = self.decide(large, env={"CACHE_GUARD_MIN_TOKENS": "banana"})
        self.assertIn("CACHE_GUARD_MIN_TOKENS=100000", reason)
        self.assertIn("cache lasts 1h", reason)  # detection, not a bad override


class SettingsChangeTests(GuardTestCase):
    """A model or effort change since the last turn rewrites the cache even while it is warm."""

    def warm_turn(self, model="claude-opus-4", size=LARGE, split=None):
        return assistant(NOW - timedelta(minutes=20), size, split or split_1h(), model=model)

    def test_a_model_change_since_the_last_turn_is_held_back_with_its_cost(self):
        entries = [self.warm_turn(model="claude-haiku-4-5"), model_set("Opus 5")]
        reason = self.decide(entries)
        self.assertIsNotNone(reason)
        self.assertIn("You changed the model to Opus 5", reason)
        self.assertIn("310k tokens", reason)
        # 310,000 tokens on Opus at $5.00/MTok input: a 1h cache write is $10.00/MTok, a read $0.50.
        self.assertIn("$3.10", reason)
        self.assertIn("$0.16", reason)
        self.assertIn("Switch back with /model", reason)
        self.assertIn("CACHE_GUARD_MIN_TOKENS=100000", reason)

    def test_a_display_name_that_is_the_model_already_running_is_not_a_change(self):
        for display, running in (
            ("Opus 5", "claude-opus-5"),
            ("Haiku 4.5", "claude-haiku-4-5-20251001"),
        ):
            with self.subTest(display=display):
                entries = [self.warm_turn(model=running), model_set(display)]
                self.assertIsNone(self.decide(entries))
                self.assertEqual(self.marker_count(), 0)

    def test_a_model_set_before_the_last_turn_is_already_paid_for(self):
        entries = [model_set("Opus 5"), self.warm_turn()]
        self.assertIsNone(self.decide(entries))

    def test_the_same_text_quoted_by_a_tool_is_not_a_settings_change(self):
        quoted = "<local-command-stdout>Set model to `Opus 5` and saved as your default</local-command-stdout>"
        entries = [self.warm_turn(), tool_result_quoting(quoted)]
        self.assertIsNone(self.decide(entries))

    def test_a_subagents_settings_line_is_not_this_sessions_change(self):
        entries = [self.warm_turn(), model_set("Opus 5", sidechain=True)]
        self.assertIsNone(self.decide(entries))

    def test_an_effort_change_names_the_level_to_switch_back_to(self):
        entries = [effort_set("low"), self.warm_turn(), effort_set("high")]
        reason = self.decide(entries)
        self.assertIsNotNone(reason)
        self.assertIn("You changed effort to high since the last turn", reason)
        self.assertIn("resets the cache for the whole conversation", reason)
        self.assertIn("Switch back with /effort low", reason)

    def test_setting_effort_to_the_level_it_already_had_is_not_a_change(self):
        entries = [effort_set("high"), self.warm_turn(), effort_set("high")]
        self.assertIsNone(self.decide(entries))

    def test_an_effort_level_with_nothing_to_compare_it_to_is_hedged(self):
        entries = [self.warm_turn(), effort_set("high")]
        reason = self.decide(entries)
        self.assertIn("You set effort to high since the last turn; if that changed it", reason)
        self.assertIn("Switch back with /effort before sending", reason)
        self.assertNotIn("/effort high", reason)

    def test_a_model_and_an_effort_change_arrive_as_one_message(self):
        entries = [effort_set("low"), self.warm_turn(), model_set("Opus 5"), effort_set("high")]
        reason = self.decide(entries)
        self.assertIn("You changed the model to Opus 5 and effort to high", reason)
        self.assertIn("Switch back with /model and /effort low", reason)

    def test_a_settings_change_under_the_threshold_is_allowed(self):
        small = self.warm_turn(size=SMALL)
        self.assertIsNone(self.decide([small, model_set("Opus 5")]))
        self.assertIsNone(self.decide([small, effort_set("high")]))
        self.assertEqual(self.marker_count(), 0)

    def test_sending_the_message_again_goes_ahead_with_the_settings_change(self):
        entries = [self.warm_turn(), model_set("Opus 5")]
        self.assertIsNotNone(self.decide(entries))
        self.assertIsNone(self.decide(entries, now=NOW + timedelta(seconds=30)))
        self.assertEqual(self.marker_count(), 0)

    def test_a_cold_cache_is_the_warning_even_when_the_model_changed_too(self):
        # Switching the model back would not make this turn warm, so only the cold warning is useful.
        entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h()), model_set("Opus 5")]
        reason = self.decide(entries)
        self.assertIn("Prompt cache expired", reason)
        self.assertNotIn("You changed the model", reason)

    def test_the_new_model_prices_the_warning_when_its_name_says_which_it_is(self):
        entries = [self.warm_turn(model="claude-haiku-4-5"), model_set("Fable 5.1")]
        # 310,000 tokens on Fable 5.1 at $10.00/MTok input: $20.00/MTok to write a 1h cache, $0.25 to read.
        reason = self.decide(entries)
        self.assertIn("$6.20", reason)
        self.assertIn("$0.08", reason)

    def test_an_unrecognisable_new_model_is_priced_as_the_one_that_is_running(self):
        entries = [self.warm_turn(model="claude-haiku-4-5"), model_set("Skunkworks Preview")]
        # 310,000 tokens on Haiku at $1.00/MTok input: $2.00/MTok cold, $0.10/MTok warm.
        reason = self.decide(entries)
        self.assertIn("$0.62", reason)
        self.assertIn("$0.03", reason)


class CostTests(GuardTestCase):
    def cold(self, model, size=LARGE, split=None, env=None, hours=2, session="s-1"):
        entries = [assistant(NOW - timedelta(hours=hours), size, split or split_1h(), model=model)]
        return self.decide(entries, env=env, session=session)

    def test_an_hour_long_fable_cache_is_priced_and_read_eighty_times_cheaper(self):
        # 200,000 tokens: $20.00/MTok to write a 1h cache, $0.25/MTok to read it on Fable 5.1.
        reason = self.cold("claude-fable-5-1", size=200_000)
        self.assertIn("$4.00", reason)
        self.assertIn("$0.05", reason)
        self.assertIn("80x", reason)
        self.assertIn("at API list prices", reason)

    def test_a_five_minute_opus_cache_is_priced_at_the_smaller_write_multiplier(self):
        # 310,000 tokens: $6.25/MTok to write a 5m cache, $0.50/MTok to read it.
        reason = self.cold("claude-opus-4", split=split_5m(), hours=1)
        self.assertIn("$1.94", reason)
        self.assertIn("$0.16", reason)
        self.assertIn("12.5x", reason)

    def test_a_few_cents_reads_as_under_a_cent_rather_than_zero(self):
        # 30,000 tokens on Haiku: $2.00/MTok cold is $0.06; $0.10/MTok warm is $0.003.
        reason = self.cold("claude-haiku-4-5", size=30_000, env={"CACHE_GUARD_MIN_TOKENS": "10000"})
        self.assertIn("$0.06", reason)
        self.assertIn("under $0.01", reason)

    def test_a_model_with_no_published_price_is_warned_about_without_dollars(self):
        reason = self.cold("some-other-assistant-v2")
        self.assertNotIn("$", reason)
        self.assertIn("20x", reason)

    def test_the_cost_can_be_switched_off_and_the_multiplier_stays(self):
        reason = self.cold("claude-opus-4", env={"CACHE_GUARD_SHOW_COST": "0"})
        self.assertNotIn("$", reason)
        self.assertIn("20x", reason)

    def test_the_input_price_can_be_overridden_when_list_prices_change(self):
        # 310,000 tokens at $3.00/MTok input: $6.00/MTok to write a 1h cache, $0.30/MTok to read it.
        reason = self.cold("claude-opus-4", env={"CACHE_GUARD_INPUT_PRICE": "3"})
        self.assertIn("$1.86", reason)
        self.assertIn("$0.09", reason)

    def test_an_override_that_is_not_a_positive_number_is_ignored(self):
        for index, bad in enumerate(("abc", "", "0", "-2")):
            with self.subTest(value=bad):
                # A fresh session each time: a warning already given is a confirmation, not a warning.
                reason = self.cold(
                    "claude-opus-4", env={"CACHE_GUARD_INPUT_PRICE": bad}, session=f"bad-{index}"
                )
                self.assertIn("$3.10", reason)

    def test_the_cold_warning_says_what_compact_actually_does(self):
        reason = self.cold("claude-opus-4")
        self.assertIn("/compact pays for this cold context once", reason)
        self.assertNotIn("does not help", reason)

    def test_the_cold_warning_offers_the_handoff_that_costs_nothing_first(self):
        reason = self.cold("claude-opus-4")
        self.assertIn("send the single word handoff", reason)
        self.assertIn("without using this session's model", reason)
        self.assertLess(reason.index("handoff"), reason.index("/compact"))


class StateDirectoryTests(GuardTestCase):
    """The state directory is the user's own, and the sweep only ever touches this plugin's files."""

    def cold(self):
        return [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]

    def aged(self, path, days=3):
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        old = (NOW - timedelta(days=days)).timestamp()
        os.utime(path, (old, old))
        return path

    def test_a_state_directory_that_is_a_symlink_is_not_used_and_sweeps_nothing(self):
        target = os.path.join(self.tmp.name, "someone-else")
        os.makedirs(target)
        precious = self.aged(os.path.join(target, "precious.key"))
        marker_shaped = self.aged(os.path.join(target, "old-session"))
        os.symlink(target, self.markers)
        self.assertIsNone(self.decide(self.cold()))  # no state to trust: the guard says nothing
        self.assertTrue(os.path.exists(precious))
        self.assertTrue(os.path.exists(marker_shaped))
        self.assertEqual(sorted(os.listdir(target)), ["old-session", "precious.key"])

    def test_a_state_directory_owned_by_someone_else_is_not_used(self):
        if not hasattr(os, "getuid"):
            self.skipTest("no file ownership to compare against on this platform")
        os.makedirs(self.markers)
        stranger = self.aged(os.path.join(self.markers, "old-session"))
        with unittest.mock.patch.object(os, "getuid", lambda: os.stat(self.markers).st_uid + 1):
            self.assertIsNone(self.decide(self.cold()))
        self.assertTrue(os.path.exists(stranger))

    def test_the_state_directory_is_created_private_to_this_user(self):
        self.assertIsNotNone(self.decide(self.cold()))
        self.assertEqual(stat.S_IMODE(os.stat(self.markers).st_mode), 0o700)

    def test_the_sweep_only_removes_this_plugins_own_files_and_never_follows_a_link(self):
        os.makedirs(self.markers)
        elsewhere = self.aged(os.path.join(self.tmp.name, "linked-away"))
        notes = self.aged(os.path.join(self.markers, "notes.md"))
        link = os.path.join(self.markers, "condensed-link")
        os.symlink(elsewhere, link)
        for name in ("long-gone-session", "announced-abc", "condensed-xyz"):
            self.aged(os.path.join(self.markers, name))
        self.assertIsNotNone(self.decide(self.cold()))
        self.assertEqual(sorted(os.listdir(self.markers)), ["condensed-link", "notes.md", "s-1"])
        self.assertTrue(os.path.exists(notes))
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.exists(elsewhere))

    def test_the_default_state_directory_is_under_the_home_directory(self):
        home = os.path.join(self.tmp.name, "home")
        with unittest.mock.patch.dict(os.environ, {"HOME": home}):
            directory = cache_guard.state_dir({})
        self.assertEqual(directory, os.path.join(home, ".claude", "cache-guard"))
        for old in ("cache-guard", "claude-cache-guard"):  # never the shared temporary directory again
            self.assertNotEqual(directory, os.path.join(tempfile.gettempdir(), old))
        self.assertFalse(os.path.exists(directory))  # naming it is not creating it

    def test_the_state_directory_can_still_be_pointed_somewhere_else(self):
        self.assertEqual(cache_guard.state_dir({"CACHE_GUARD_STATE_DIR": self.markers}), self.markers)


class SlashHandoffTests(GuardTestCase):
    """`/handoff` is the model writing the file the single word writes for nothing."""

    def cold(self):
        return [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]

    def warm(self):
        return [assistant(NOW - timedelta(minutes=20), LARGE, split_1h())]

    def test_the_skill_is_held_back_while_the_cache_is_cold(self):
        for index, prompt in enumerate(("/handoff", "/cache-guard:handoff", "/handoff this session")):
            with self.subTest(prompt=prompt):
                reason = self.decide(self.cold(), prompt=prompt, session=f"skill-{index}")
                self.assertIsNotNone(reason)
                self.assertIn("The prompt cache has expired", reason)
                self.assertIn("310k tokens", reason)
                self.assertIn("no slash", reason)
                self.assertIn("without using this session's model", reason)
                self.assertIn("CACHE_GUARD_MIN_TOKENS=100000", reason)

    def test_sending_the_command_again_uses_the_model_anyway(self):
        self.assertIsNotNone(self.decide(self.cold(), prompt="/handoff"))
        self.assertIsNone(self.decide(self.cold(), prompt="/handoff", now=NOW + timedelta(seconds=30)))

    def test_a_warm_session_writes_its_handoff_however_it_likes(self):
        self.assertIsNone(self.decide(self.warm(), prompt="/handoff"))
        self.assertEqual(self.marker_count(), 0)

    def test_a_small_cold_context_is_not_worth_holding_back(self):
        entries = [assistant(NOW - timedelta(hours=2), SMALL, split_1h())]
        self.assertIsNone(self.decide(entries, prompt="/handoff"))

    def test_every_other_slash_command_still_passes(self):
        for prompt in ("/clear", "/handoffs", "/handoff-later", "/compact"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(self.decide(self.cold(), prompt=prompt))
                self.assertEqual(self.marker_count(), 0)


class HandoffFailureTests(GuardTestCase):
    """A handoff that cannot be written blocks: the word must never reach the model instead."""

    def test_a_failing_handoff_blocks_rather_than_sending_the_word(self):
        import handoff

        def raiser(payload, now, env):
            raise RuntimeError("no room on device")

        entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        with unittest.mock.patch.object(handoff, "write_handoff", raiser):
            reason = self.decide(entries, prompt="handoff")
        self.assertIsNotNone(reason)
        self.assertIn("The handoff could not be written (RuntimeError: no room on device)", reason)
        self.assertIn("Nothing was sent", reason)

    def test_a_long_failure_is_cut_to_one_readable_line(self):
        text = cache_guard.failed_handoff_reason(RuntimeError("a very long tale\n" * 40))
        inside = text[text.index("(") + 1:text.index(")")]
        self.assertLessEqual(len(inside), 120)
        self.assertNotIn("\n", inside)
        self.assertTrue(inside.endswith("..."))


class MarkerAgeTests(GuardTestCase):
    def test_a_marker_dated_in_the_future_confirms_nothing(self):
        entries = [assistant(NOW - timedelta(hours=2), LARGE, split_1h())]
        self.assertIsNotNone(self.decide(entries))
        marker = os.path.join(self.markers, "s-1")
        ahead = (NOW + timedelta(seconds=60)).timestamp()
        os.utime(marker, (ahead, ahead))
        self.assertIsNotNone(self.decide(entries))


class GenerationPriceTests(GuardTestCase):
    """Two generations of a family are priced differently, and a family alone is not priced at all."""

    def cold(self, model, size=LARGE, session="s-1", env=None):
        entries = [assistant(NOW - timedelta(hours=2), size, split_1h(), model=model)]
        return self.decide(entries, session=session, env=env)

    def test_sonnet_four_costs_its_own_input_price_and_not_sonnet_fives(self):
        # 100,000 tokens at $3.00/MTok input: $6.00/MTok to write a 1h cache, $0.30/MTok to read it.
        reason = self.cold("claude-sonnet-4-6", size=100_000)
        self.assertIn("$0.60", reason)
        self.assertIn("$0.03", reason)

    def test_sonnet_five_is_cheaper_than_sonnet_four(self):
        # 100,000 tokens at $2.00/MTok input: $4.00/MTok cold, $0.20/MTok warm.
        reason = self.cold("claude-sonnet-5", size=100_000)
        self.assertIn("$0.40", reason)
        self.assertIn("$0.02", reason)

    def test_a_fable_before_five_one_reads_at_a_tenth_rather_than_a_fortieth(self):
        # 200,000 tokens at $10.00/MTok input: $20.00/MTok cold, $1.00/MTok warm.
        reason = self.cold("claude-fable-5", size=200_000)
        self.assertIn("$4.00", reason)
        self.assertIn("$0.20", reason)
        self.assertIn("20x", reason)

    def test_a_new_model_named_by_its_display_name_is_priced_by_generation(self):
        # 310,000 tokens at $3.00/MTok input: $6.00/MTok cold, $0.30/MTok warm.
        warm = assistant(NOW - timedelta(minutes=20), LARGE, split_1h(), model="claude-haiku-4-5")
        reason = self.decide([warm, model_set("Sonnet 4.6")])
        self.assertIn("$1.86", reason)
        self.assertIn("$0.09", reason)

    def test_a_family_with_no_generation_in_it_is_not_given_a_price(self):
        reason = self.cold("sonnet")
        self.assertNotIn("$", reason)
        self.assertIn("20x", reason)


class WordingTests(GuardTestCase):
    def test_nothing_hedges_a_figure_that_is_already_a_hedge(self):
        entries = [assistant(NOW - timedelta(hours=2), 1_000, split_1h(), model="claude-haiku-4-5")]
        reason = self.decide(entries, env={"CACHE_GUARD_MIN_TOKENS": "100"})
        self.assertIn("under $0.01", reason)
        for hedge in ("roughly under", "about under"):
            self.assertNotIn(hedge, reason)

    def test_the_handoff_message_does_not_hedge_it_either(self):
        result = {"path": "/tmp/h.md", "summary_model": "haiku", "est_tokens": 100, "est_cost": 0.0001}
        message = cache_guard.handoff_reason(result, {})
        self.assertIn("under $0.01", message)
        for hedge in ("roughly under", "about under"):
            self.assertNotIn(hedge, message)

    def test_a_summary_is_promised_shortly_rather_than_by_a_clock_nothing_measured(self):
        result = {"path": "/tmp/h.md", "summary_model": "haiku", "est_tokens": 100, "est_cost": None}
        message = cache_guard.handoff_reason(result, {})
        self.assertIn("shortly", message)
        self.assertNotIn("a minute", message)

    def test_the_landing_is_promised_to_the_session_that_clears_as_well_as_this_one(self):
        """The same message recommends /clear, so a promise made only to this session has no carrier."""
        result = {"path": "/tmp/h.md", "summary_model": "haiku", "est_tokens": 100, "est_cost": None}
        message = cache_guard.handoff_reason(result, {})
        self.assertIn("you will be told when it lands", message)
        self.assertIn("the session you /clear into", message)

    def test_days_idle_read_as_days(self):
        entries = [assistant(NOW - timedelta(hours=72), LARGE, split_1h())]
        self.assertIn("idle 3d 0h", self.decide(entries))
        self.assertEqual(cache_guard.humanize_duration(72 * 3600), "3d 0h")


class EndToEndTests(GuardTestCase):
    """main() through the launcher the hook actually runs, against a real clock."""

    def run_hook(self, payload, state_dir):
        launcher = os.path.join(HOOKS, "run-python.sh")
        script = os.path.join(HOOKS, "cache_guard.py")
        env = dict(os.environ, CACHE_GUARD_STATE_DIR=state_dir)
        return subprocess.run(
            ["/bin/sh", launcher, script],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )

    def payload(self, minutes_idle):
        now = datetime.now(timezone.utc)
        entries = [assistant(now - timedelta(minutes=minutes_idle), LARGE, split_1h())]
        return {
            "session_id": "end-to-end",
            "transcript_path": self.transcript(entries, f"live-{minutes_idle}.jsonl"),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "carry on",
        }

    def test_a_cold_turn_prints_a_block_decision(self):
        state = os.path.join(self.tmp.name, "live-state")
        result = self.run_hook(self.payload(minutes_idle=90), state)
        self.assertEqual(result.returncode, 0)
        decision = json.loads(result.stdout)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("Prompt cache expired", decision["reason"])
        self.assertEqual(os.listdir(state), ["end-to-end"])

    def test_a_warm_turn_prints_nothing(self):
        state = os.path.join(self.tmp.name, "warm-state")
        result = self.run_hook(self.payload(minutes_idle=5), state)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_stdin_that_is_not_json_prints_nothing(self):
        launcher = os.path.join(HOOKS, "run-python.sh")
        script = os.path.join(HOOKS, "cache_guard.py")
        env = dict(os.environ, CACHE_GUARD_STATE_DIR=os.path.join(self.tmp.name, "junk-state"))
        result = subprocess.run(
            ["/bin/sh", launcher, script], input="{{{", capture_output=True, text=True, env=env
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
