#!/usr/bin/env python3
"""UserPromptSubmit: stop a message once before it is sent into a large context that will be re-sent cold.

Every turn re-sends the whole context. While the prompt cache is warm that is a cache read, a tenth of
the input price; once the cache lifetime passes it is a cache write again, 1.25x input for the 5-minute
lifetime and 2x for the 1-hour one. The same turn therefore costs 12.5x or 20x more cold than warm, and
the bill lands on the first message after a break. Measured on one machine over a week: 4.8% of
main-session spend came from ten messages sent after more than an hour idle into contexts over 100k.

Three things put a large context in that position, and each is undoable before the message is sent:

  * the cache lifetime has passed since the last turn — nothing to undo, but /clear is cheaper;
  * the model changed since the last turn, and caches are per model, so the whole conversation is
    written again — /model switches back;
  * the effort level changed since the last turn, which rewrites everything but the system prompt —
    /effort switches back.

In each case the message is held back once with what it would cost; sending it again within the
confirmation window lets it through. An expired cache takes precedence over a settings change: when the
cache is cold too, switching the setting back would not save anything.

There is also a way out that costs nothing at all: a message that is the single word `handoff` never
reaches the model. It writes a handoff file from the transcript by script (see handoff.py), so the
conversation can be continued in a fresh session without paying the cold turn that a `/compact` or a
model-written summary would. `CACHE_GUARD_DISABLE=1` switches the whole guard off.

Anything unexpected — bad JSON, no transcript, no usable turn, any exception at all — allows the prompt.
A guard that breaks a session costs more than the turn it saves.
"""
import json
import os
import re
import stat
import sys
from collections import namedtuple
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

TAIL = 512 * 1024  # enough of the transcript's end to hold the last turn; read on every prompt
HOUR = 3600
FIVE_MINUTES = 300
DEFAULT_MIN_TOKENS = 100_000
DEFAULT_CONFIRM_SECONDS = 120
MARKER_MAX_AGE = 86_400  # a marker older than a day belongs to a session that has long since ended
STATE_DIR_NAME = "cache-guard"  # inside the user's own ~/.claude, never a directory anyone else writes
STATE_DIR_MODE = 0o700
HANDOFF_WORD = "handoff"  # a message that is only this word asks for a handoff file, never the model
HANDOFF_COMMANDS = ("/handoff", "/cache-guard:handoff")  # the same thing, but written by the model
UNDER_A_CENT = "under $0.01"
MARKER_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")  # a sanitised session id, and nothing else
# The plugin's other files, which carry a suffix. `announced-` is no longer written — the session-start
# announcement is made to every fresh session inside the freshness window — but it stays in the sweep so
# that the ones an earlier version left behind are cleared out like everything else.
SWEPT_PREFIXES = ("announced-", "condensed-", "pending-")
HANDOFF_PENDING_PREFIX = "pending-"  # one per session that has a background summary still running
# The line handoff.py writes under the title while the background summary is running. It lives here so
# that this hook can tell a finished handoff from an unfinished one without importing handoff.py, whose
# import costs more than the check and which imports this module in turn.
SUMMARY_PENDING_TEXT = "Summary: being written"
SUMMARY_FAILED_PREFIX = "Summary failed"
SUMMARY_ANY_PREFIX = "Summary"
# A desktop notification, emitted through the host's `terminalSequence` field rather than written to
# the terminal here: the host validates it against its own allowlist and wraps it for tmux and screen.
# Only OSC 0, 1, 2, 9, 99 and 777 are permitted, the whole sequence must be under 4096 bytes, and an
# OSC 9 body may not begin with a digit. Most terminals that notify at all take OSC 9; kitty takes 99.
NOTIFY_BELL = "\x07"
NOTIFY_MAX_CHARS = 120
NOTIFY_LEAD = "cache-guard: "  # every body starts here, which is also what keeps it off a digit
FAILURE_MAX_CHARS = 120  # a failed handoff is reported in one line, not with a traceback

# Published API list prices in US dollars per million input tokens, keyed by a run of characters of the
# normalised model id or display name. The order matters and the first match wins, so a generation with
# a price of its own comes before the family it belongs to. A cache read costs a tenth of input except
# where CACHE_READ_PRICES gives the published rate; a cache write costs input times the multiplier for
# the lifetime it buys. List prices change, so CACHE_GUARD_INPUT_PRICE overrides the input price for any
# model when they do, and a model this table cannot place — including a family name with no generation
# in it, whose generations are priced differently — is described without dollar figures rather than with
# a wrong one.
INPUT_PRICES = {
    "fable51": 10.00,
    "fable": 10.00,
    "opus": 5.00,
    "sonnet5": 2.00,
    "sonnet4": 3.00,
    "haiku": 1.00,
}
CACHE_READ_PRICES = {"fable51": 0.25}
# The CLI's bare aliases name a family, and the plugin picks one for its own background summariser. A
# caller pricing a model it chose itself resolves the alias; a warning about a model read from a
# transcript never does, since that name always carries its generation.
CLI_ALIASES = {"sonnet": "sonnet5"}
CACHE_READ_FRACTION = 0.1
WRITE_MULTIPLIER_1H = 2.0
WRITE_MULTIPLIER_5M = 1.25

# `/model` and `/effort` each leave the command's own output in the transcript, as a user entry whose
# message content is this plain string. A tool result that quotes the same text has list content.
MODEL_PREFIX = "<local-command-stdout>Set model to "
EFFORT_PREFIX = "<local-command-stdout>Set effort level to "
SETTINGS_HINT = b"<local-command-stdout>Set "  # cheap prefilter before any line is parsed

Turn = namedtuple("Turn", "position time size split model")
# What the user is told, and the few words of it that fit in a desktop notification.
Notice = namedtuple("Notice", "message headline")


def positive_int(env, name, default):
    """A positive integer from the environment; anything else means the default."""
    try:
        value = int(env[name])
    except (KeyError, TypeError, ValueError):
        return default
    return value if value > 0 else default


def positive_float(env, name):
    """A positive float from the environment, or None when it is absent or is anything else."""
    try:
        value = float(env[name])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_time(raw):
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def read_tail(path):
    """The transcript's last TAIL bytes as lines, without the leading line the cut may have halved."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - TAIL)
        f.seek(start)
        lines = f.read().splitlines()
    return lines[1:] if start and lines else lines


def turns(lines):
    """(time, context size, cache-creation split) of each real main-session assistant turn.

    Sidechain lines are a subagent's turns and say nothing about this session's cache; `<synthetic>`
    lines are not turns at all.
    """
    found = []
    for position, raw in enumerate(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may still be cut mid-record
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        if message.get("model") == "<synthetic>":
            continue
        when = parse_time(entry.get("timestamp"))
        if when is None:
            continue
        usage = message.get("usage") or {}
        size = sum(
            usage.get(k) or 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        found.append(
            Turn(position, when, size, usage.get("cache_creation") or {}, message.get("model") or "")
        )
    return found


def lifetime_of(found):
    """The cache lifetime this session is buying, from the most recent turn that wrote to cache.

    A turn's `cache_creation` splits its write by the lifetime it bought. Without such a turn in the
    tail, assume the shorter one: warning early costs a keystroke, warning late costs the turn.
    """
    for turn in reversed(found):
        if (turn.split.get("ephemeral_1h_input_tokens") or 0) > 0:
            return HOUR
        if (turn.split.get("ephemeral_5m_input_tokens") or 0) > 0:
            return FIVE_MINUTES
    return FIVE_MINUTES


def settings_events(lines):
    """(position, kind, value) for every `/model` or `/effort` confirmation in the tail.

    Only a string message content is the command's own output: a tool result quoting the same text
    carries a list, and a sidechain entry is a subagent's, so neither changes this session's cache.
    """
    events = []
    for position, raw in enumerate(lines):
        if SETTINGS_HINT not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") != "user" or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, str):
            continue
        if content.startswith(MODEL_PREFIX):
            name = re.search(r"`([^`]+)`", content[len(MODEL_PREFIX):])
            if name:  # without the display name there is nothing to compare, or to name in the warning
                events.append((position, "model", name.group(1)))
        elif content.startswith(EFFORT_PREFIX):
            level = re.match(r"([A-Za-z]+)", content[len(EFFORT_PREFIX):])
            if level:
                events.append((position, "effort", level.group(1)))
    return events


def normalise(text):
    """A model name reduced to lowercase alphanumerics, so a display name can be found in an id."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def settings_change(lines, last):
    """(new model display name, new effort, previous effort) for changes made since the last turn.

    Each is None when it did not change. A previous effort of None with a new one means the tail holds
    no earlier level: the setting was set, which may or may not have changed it.
    """
    events = settings_events(lines)

    def latest(kind, since_the_turn):
        for position, event_kind, value in reversed(events):
            if event_kind == kind and (position > last.position) == since_the_turn:
                return value
        return None

    model = latest("model", True)
    if model and normalise(model) in normalise(last.model):
        model = None  # the same model under its display name: nothing was rewritten
    effort = latest("effort", True)
    previous = latest("effort", False)
    if effort and previous and effort.lower() == previous.lower():
        effort = None
    return model, effort, (previous if effort else None)


def humanize_duration(seconds):
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"  # a session left overnight reads as days, not as 72h 0m
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def humanize_lifetime(seconds):
    if seconds % HOUR == 0:
        return f"{seconds // HOUR}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def humanize_tokens(count):
    if count < 1000:
        return str(count)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1000)}k"


def humanize_window(seconds):
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"


def money(amount):
    """Dollars to the cent; anything that rounds to nothing is described rather than printed as $0.00."""
    cents = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return UNDER_A_CENT if cents == 0 else f"${cents}"


def about(amount):
    """`about $1.23`, but never `about under $0.01`: the phrase is already the hedge."""
    text = money(amount)
    return text if text == UNDER_A_CENT else f"about {text}"


def family_of(model, cli_alias=True):
    """The price row a model id or a display name falls in, or None when it falls in none of them.

    Normalising both sides is what lets `Sonnet 4.6` and `claude-sonnet-4-6` land on the same row. A
    name that stops at the family matches no row: two live Sonnet generations are priced differently,
    and a warning that quotes one at the other's price is worse than a warning with no price in it.
    `cli_alias=False` is how the warning path asks for exactly that.
    """
    text = normalise(model)
    for family in INPUT_PRICES:
        if family in text:
            return family
    return CLI_ALIASES.get(text) if cli_alias else None


def prices(model, lifetime, env):
    """(cache-write, cache-read) dollars per million tokens, or (None, None) for an unpriced model."""
    override = positive_float(env, "CACHE_GUARD_INPUT_PRICE")
    if override is not None:
        write, read = override, override * CACHE_READ_FRACTION
    else:
        family = family_of(model, cli_alias=False)
        if family is None:
            return None, None
        write = INPUT_PRICES[family]
        read = CACHE_READ_PRICES.get(family, write * CACHE_READ_FRACTION)
    return write * (WRITE_MULTIPLIER_1H if lifetime >= HOUR else WRITE_MULTIPLIER_5M), read


def humanize_multiplier(value):
    return f"{value:.1f}".rstrip("0").rstrip(".") + "x"


def cost_text(size, model, lifetime, env):
    """(the phrase naming what this turn costs, how many times the warm price that is).

    An unpriced model, or CACHE_GUARD_SHOW_COST=0, leaves out the money; the multiplier is the same
    ratio either way, and falls back to the published one when there is no price to divide.
    """
    write, read = prices(model, lifetime, env)
    if write is None:
        return "", "20x" if lifetime >= HOUR else "12.5x"
    multiplier = humanize_multiplier(write / read)
    if str(env.get("CACHE_GUARD_SHOW_COST") or "").strip() == "0":
        return "", multiplier
    cold, warm = about(size * write / 1_000_000), money(size * read / 1_000_000)
    return f", {cold} at API list prices instead of {warm} with a warm cache", multiplier


def cold_reason(idle, lifetime, size, cost, multiplier, confirm_seconds, min_tokens):
    return (
        f"Prompt cache expired: this session has been idle {humanize_duration(idle)} and its cache "
        f"lasts {humanize_lifetime(lifetime)}. Sending now re-sends about {humanize_tokens(size)} "
        f"tokens at the cache-write price{cost}, roughly {multiplier} what this turn costs while the "
        f"cache is warm. Send the message again within {humanize_window(confirm_seconds)} to go ahead. "
        "Cheapest: send the single word handoff, which writes a handoff file without using this "
        "session's model, then /clear. /compact pays for this cold context once and every turn after "
        f"it is small. (cache-guard; threshold CACHE_GUARD_MIN_TOKENS={min_tokens})"
    )


def settings_reason(model, effort, previous, size, cost, confirm_seconds, min_tokens):
    """The warning for a model or an effort change since the last turn; one message names both."""
    tokens = humanize_tokens(size)
    effort_switch = f"/effort {previous}" if previous else "/effort"
    if model and effort:
        lead = (
            f"You changed the model to {model} and effort to {effort} since the last turn. Caches are "
            "per model, and an effort change resets the cache for the whole conversation: this message "
            f"re-sends about {tokens} tokens at the cache-write price{cost}."
        )
        switch = f"Switch back with /model and {effort_switch} before sending and it costs nothing extra."
    elif model:
        lead = (
            f"You changed the model to {model} since the last turn. Caches are per model, so this "
            f"message re-sends about {tokens} tokens at the cache-write price{cost}."
        )
        switch = "Switch back with /model before sending and it costs nothing extra."
    else:
        opening = (
            f"You changed effort to {effort} since the last turn. That resets"
            if previous
            else f"You set effort to {effort} since the last turn; if that changed it, that resets"
        )
        lead = (
            f"{opening} the cache for the whole conversation: up to about {tokens} tokens are re-sent "
            f"at the cache-write price{cost}."
        )
        switch = f"Switch back with {effort_switch} before sending and it costs nothing extra."
    return (
        f"{lead} {switch} Send the message again within {humanize_window(confirm_seconds)} to go "
        f"ahead. (cache-guard; threshold CACHE_GUARD_MIN_TOKENS={min_tokens})"
    )


def handoff_reason(result, env):
    """What the user is told once the handoff file exists: where it is, and what is still coming."""
    tokens = humanize_tokens(result["est_tokens"])
    cost = result.get("est_cost")
    show_cost = str(env.get("CACHE_GUARD_SHOW_COST") or "").strip() != "0"
    model = result.get("summary_model")
    if model:
        priced = f"{about(cost)} at API list prices for " if cost is not None and show_cost else ""
        # The background run is a detached process, not a subagent, so nothing about it appears in the
        # session while it works. Saying where the state is written is what makes it observable.
        middle = (
            f"A summary by {model} is being added in the background (shortly, {priced}~{tokens} "
            "tokens). The file's Summary line says which it is until then, and you will be told here "
            "when it lands."
        )
    else:
        middle = f"No summary was added ({result.get('no_summary_reason')})."
    return (
        f"Handoff written without using this session's model: {result['path']}. {middle} When you are "
        "ready: /clear, and the new session says so and where the file is."
    )


def handoff_command(prompt):
    """True when the prompt invokes the handoff skill, with or without arguments after it.

    `/handoff` is the same file written by the model, which is exactly what the word avoids paying for.
    """
    words = str(prompt).strip().lower().split(None, 1)
    return bool(words) and words[0] in HANDOFF_COMMANDS


def failed_handoff_reason(failure):
    """A handoff that did not happen still blocks: the word itself must never reach the model."""
    text = " ".join(f"{type(failure).__name__}: {failure}".split())
    if len(text) > FAILURE_MAX_CHARS:
        text = text[: FAILURE_MAX_CHARS - 3] + "..."
    return (
        f"The handoff could not be written ({text}). Nothing was sent. Send your message again to "
        "continue without one."
    )


def handoff_block(payload, now, env):
    """Write the handoff and hold the prompt back with where it went. Importing costs, so it is late."""
    hooks = os.path.dirname(os.path.abspath(__file__))
    if hooks not in sys.path:
        sys.path.insert(0, hooks)  # sys.path[0] is the caller's, not this file's, when the hook is run
    try:
        import handoff  # only the word `handoff` pays for this import; every other prompt skips it

        return handoff_reason(handoff.write_handoff(payload, now, env), env)
    except Exception as failure:
        # Not the outer fail-open: allowing here would send the literal word into the cold context.
        return failed_handoff_reason(failure)


def slash_handoff_reason(size, cost, confirm_seconds, min_tokens):
    """The `/handoff` skill is the model writing the file the single word writes for nothing."""
    return (
        f"The prompt cache has expired, so /handoff would have this session's model re-read about "
        f"{humanize_tokens(size)} tokens to write it{cost}. Send the single word handoff (no slash) "
        "instead: it writes the file without using this session's model. Send /handoff again within "
        f"{humanize_window(confirm_seconds)} to use the model anyway. "
        f"(cache-guard; threshold CACHE_GUARD_MIN_TOKENS={min_tokens})"
    )


def is_kitty(env):
    """kitty answers OSC 99 and not OSC 9; everything else that notifies at all answers OSC 9."""
    return "kitty" in str(env.get("TERM") or "").lower() or bool(env.get("KITTY_WINDOW_ID"))


def notification_body(headline):
    """The headline reduced to what an OSC payload may carry: printable, no separator, and short.

    A semicolon would end the payload for a terminal that reads further fields after it, so it goes.
    """
    kept = "".join(c for c in str(headline) if 32 <= ord(c) < 127 or ord(c) > 159)
    return kept.replace(";", ",").strip()[:NOTIFY_MAX_CHARS]


def notification_sequence(headline, env):
    """The escape sequence for a desktop notification, or None when there is not one to send.

    This is the only moment a notification can be raised: a hook is the one thing here that the host
    will emit on behalf of, and hooks run when the user does something. The background summariser
    finishing is not such a moment, so the notification lands on the next prompt or the next session
    start rather than the instant the summary is written.
    """
    if str(env.get("CACHE_GUARD_NOTIFY") or "").strip() == "0":
        return None
    said = notification_body(headline)
    if not said:
        return None  # nothing printable to say, and a notification of the lead alone is only noise
    body = notification_body(NOTIFY_LEAD + said)
    if body[0].isdigit():
        return None  # an OSC 9 body that opens with a digit is a progress report, and is rejected
    return f"\x1b]99;;{body}{NOTIFY_BELL}" if is_kitty(env) else f"\x1b]9;{body}{NOTIFY_BELL}"


def session_of(payload):
    """The session id reduced to the characters a file name may hold, or "" when there is none."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("session_id") or ""))


def pending_record(marker_dir, session):
    """Where a session records that it started a background summary it has not yet reported on."""
    return os.path.join(marker_dir, HANDOFF_PENDING_PREFIX + session)


def summary_line_of(document):
    """The handoff's `Summary...` line, whichever of the three states it is in, or "" when it has none."""
    for line in document.split("\n"):
        if line.startswith(SUMMARY_ANY_PREFIX):
            return line.strip()
    return ""


def landed_reason(path, document):
    """What the user is told once the background summary is no longer running.

    The headline is the same news in the few words a desktop notification has room for.
    """
    line = summary_line_of(document)
    if line.startswith(SUMMARY_FAILED_PREFIX):
        return Notice(
            f"The handoff's background summary did not finish — {line} The script-written handoff at "
            f"{path} is complete and readable as it is.",
            "handoff summary failed, the handoff itself is complete",
        )
    return Notice(
        f"The handoff's background summary has landed: {path} is complete. /clear when you are ready.",
        "handoff summary ready",
    )


def summary_notice(payload, env, marker_dir):
    """Tell the user, once, that the summary promised by an earlier `handoff` in this session is in.

    A detached background process cannot write to the terminal, so the report has to be made by the
    next hook that runs. Silence while it is still running is deliberate: starting it was announced
    already, and repeating that on every prompt would be the noise this plugin exists to avoid.
    """
    try:
        if str(env.get("CACHE_GUARD_DISABLE") or "").strip() == "1":
            return None
        session = session_of(payload)
        if not session:
            return None
        marker_dir = usable_state_dir(marker_dir)
        if marker_dir is None:
            return None
        record = pending_record(marker_dir, session)
        try:
            with open(record, encoding="utf-8") as f:
                path = f.read().strip()
        except OSError:
            return None  # no handoff of this session's is waiting on a summary
        document = ""
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    document = f.read()
            except OSError:
                document = ""  # the file was moved or deleted: stop watching for it
        if document and SUMMARY_PENDING_TEXT in document:
            return None  # still running
        forget(record)
        return landed_reason(path, document) if document else None
    except Exception:
        return None


def forget(path):
    try:
        os.remove(path)
    except OSError:
        pass


def state_dir(env):
    """Where the plugin keeps its own files. The one place that answers this, for every hook here.

    Under the user's own home, not the shared temporary directory: this directory is swept, and a
    sweep of a path any local user can pre-create is a way to delete someone else's files.
    """
    return env.get("CACHE_GUARD_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", STATE_DIR_NAME
    )


def usable_state_dir(directory):
    """The directory, created private to this user, or None when it is not ours to read or write.

    A symlink, or a directory owned by somebody else, is one another user may have put there first;
    the answer is to keep no state at all rather than to sweep or write inside it.
    """
    try:
        os.makedirs(directory, mode=STATE_DIR_MODE, exist_ok=True)
        if os.path.islink(directory):
            return None
        getuid = getattr(os, "getuid", None)  # Windows has no owner to compare against
        if getuid is not None and os.stat(directory).st_uid != getuid():
            return None
    except OSError:
        return None
    return directory


def swept(name):
    """True for a file this plugin writes: a session marker, an announcement, a condensed transcript."""
    return bool(MARKER_NAME.match(name)) or name.startswith(SWEPT_PREFIXES)


def prune(marker_dir, now):
    """Drop files left by sessions that ended long ago. Best effort: a swept file is never missed.

    Only this plugin's own names, and only regular files reached without following a link: whatever
    else shares the directory is somebody else's business.
    """
    try:
        names = os.listdir(marker_dir)
    except OSError:
        return
    for name in names:
        if not swept(name):
            continue
        path = os.path.join(marker_dir, name)
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and now.timestamp() - info.st_mtime > MARKER_MAX_AGE:
                os.remove(path)
        except OSError:
            pass


def confirmed(marker, now, confirm_seconds):
    """True when this message is the user sending the held-back one again, in which case it is spent."""
    try:
        age = now.timestamp() - os.path.getmtime(marker)
    except OSError:
        return False
    try:
        os.remove(marker)
    except OSError:
        pass
    return 0 <= age < confirm_seconds  # a marker dated in the future confirms nothing


def decide(payload, now, env, marker_dir):
    """The reason to block this prompt, or None to let it through."""
    try:
        if str(env.get("CACHE_GUARD_DISABLE") or "").strip() == "1":
            return None  # switched off, including inside the plugin's own background summariser
        prompt = payload.get("prompt") or ""
        path = payload.get("transcript_path")
        if prompt.strip().lower() == HANDOFF_WORD:
            if not path or not os.path.isfile(path):
                return None  # nothing to condense, and nothing measured to warn about either
            return handoff_block(payload, now, env)
        skill = handoff_command(prompt)
        if prompt.lstrip().startswith("/") and not skill:
            return None  # a slash command is the escape route, including the /clear we recommend
        session = session_of(payload)
        if not session:
            return None
        marker_dir = usable_state_dir(marker_dir)
        if marker_dir is None:
            return None  # no state we can trust: sweep nothing, write nothing, warn about nothing
        marker = os.path.join(marker_dir, session)
        prune(marker_dir, now)

        if not path:
            return None
        lines = read_tail(path)
        found = turns(lines)
        if not found:
            return None

        last = found[-1]
        size = last.size
        lifetime = positive_int(env, "CACHE_GUARD_LIFETIME_SECONDS", 0) or lifetime_of(found)
        idle = (now - last.time).total_seconds()
        min_tokens = positive_int(env, "CACHE_GUARD_MIN_TOKENS", DEFAULT_MIN_TOKENS)
        cold = idle > lifetime
        # A cold cache is the whole story: switching a setting back would not make this turn warm.
        model, effort, previous = (None, None, None) if cold else settings_change(lines, last)
        if skill:
            # The skill is only the expensive way to get a handoff while the cache is cold; a settings
            # change costs the same whichever way the file is written, so it is not this hook's business.
            model, effort, previous = None, None, None
        if size < min_tokens or not (cold or model or effort):
            try:
                os.remove(marker)  # a warm turn clears whatever an earlier cold one left behind
            except OSError:
                pass
            return None

        confirm_seconds = positive_int(env, "CACHE_GUARD_CONFIRM_SECONDS", DEFAULT_CONFIRM_SECONDS)
        if confirmed(marker, now, confirm_seconds):
            return None
        with open(marker, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(marker, (now.timestamp(), now.timestamp()))  # one clock decides ages, the caller's
        priced = model if (model and family_of(model, cli_alias=False)) else last.model
        cost, multiplier = cost_text(size, priced, lifetime, env)
        if skill:
            return slash_handoff_reason(size, cost, confirm_seconds, min_tokens)
        if model or effort:  # only ever set while the cache is warm: the cold warning comes first
            return settings_reason(model, effort, previous, size, cost, confirm_seconds, min_tokens)
        return cold_reason(idle, lifetime, size, cost, multiplier, confirm_seconds, min_tokens)
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    now, env = datetime.now(timezone.utc), os.environ
    directory = state_dir(env)
    # The notice is read before `decide` may prune the record it lives in, and it never blocks: a
    # finished summary is news, not a reason to hold a message back.
    notice = summary_notice(payload, env, directory)
    reason = decide(payload, now, env, directory)
    output = {}
    if reason:
        output.update({"decision": "block", "reason": reason})
    if notice:
        output["systemMessage"] = notice.message
        sequence = notification_sequence(notice.headline, env)
        if sequence:
            output["terminalSequence"] = sequence
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
