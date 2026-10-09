#!/usr/bin/env python3
"""PostToolUse and PostToolUseFailure: warn a subagent, in three tiers, as its context passes a budget.

An agent's spend is its context summed over every turn, so it grows with the square of the agent's
length. A single warning repeated verbatim tells an agent nothing it does not already know: measured
over six subagents carrying this hook, each started near 30k and ran on to between 180k and 252k,
because the warning arrived after the agent had already committed to work it then had to finish.

So the tiers differ in kind, and each says what the next one will ask for:

- 120k, scope freeze: start no new deliverable; a hand-back is coming at 150k.
- 150k, hand back: finish the item in hand, get it to a verified commit, end listing what remains.
- 200k, stop: commit what is already verified and report now, even mid-item. Repeats every 50k
  beyond as a backstop; the first two fire once each.

Separately, a call that outlived the prompt cache gets one more line. A subagent's cache lasted five
minutes where this was measured, so an agent that waits longer on one command rewrites its whole
context on its next turn. A command that exits non-zero or times out arrives as PostToolUseFailure,
with the same `tool_use_id`, and gets the line too: observed on Claude Code 2.1.293, that event's
additionalContext reaches the subagent. The wait counts from the turn that issued the call, not from
the call's own start, and the line says so.
Measured on one machine over eight days: 79 of 84 cold subagent turns followed a single Bash call,
after a median wait of nine minutes. Cold turns were 2.6% of subagent spend, and agents that went cold
twice or more held 74% of it, so the first cold wait predicts the next.
So the line says the cache has expired, what each further turn costs, and to land what is verified
and report rather than run another long command. `DELEGATE_SUBAGENT_CACHE_SECONDS` overrides the
300-second lifetime.

Only subagent calls carry `agent_id`, so the main session is never told. Anything unexpected exits
silently: a budget nudge is never worth breaking a tool call.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

FREEZE = 120_000
HAND_BACK = 150_000
STOP = 200_000
STEP = 50_000  # beyond STOP, the stop tier repeats at each step as a backstop
TAIL = 1024 * 1024  # enough of the transcript's end to hold its last two assistant turns
CACHE_SECONDS = 300  # a subagent's prompt cache lifetime; DELEGATE_SUBAGENT_CACHE_SECONDS overrides


def transcript(payload):
    """The subagent's own transcript: the payload may name the parent's, so derive it from `agent_id`."""
    path, agent = payload.get("transcript_path") or "", payload.get("agent_id")
    if not agent or not path:
        return None
    if os.path.basename(path).startswith("agent-"):
        return path
    session = os.path.splitext(path)[0]
    return os.path.join(session, "subagents", f"agent-{agent}.jsonl")


def turns(path):
    """(context size, tool_use ids, {tool_use id: timestamp}) of each distinct assistant message in the
    transcript's tail. A message split across lines takes each call's timestamp from the line that
    carries it."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL))
        lines = f.read().splitlines()
    order, found = [], {}
    for raw in lines:
        if b'"assistant"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may be cut mid-record
        message = entry.get("message") or {}
        mid = message.get("id")
        if entry.get("type") != "assistant" or not mid:
            continue
        if mid not in found:
            usage = message.get("usage") or {}
            size = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            found[mid] = (size, [], {})
            order.append(mid)
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found[mid][1].append(block.get("id"))
                found[mid][2][block.get("id")] = entry.get("timestamp")
    return [found[mid] for mid in order]


def level(size):
    """0 below the first tier, then 1 freeze, 2 hand back, 3+ stop (one level per STEP beyond)."""
    if size < FREEZE:
        return 0
    if size < HAND_BACK:
        return 1
    if size < STOP:
        return 2
    return 3 + (size - STOP) // STEP


def nudge(size):
    """The tier's own words. They differ in kind, and the first two name what the next tier will ask."""
    opening = (
        f"Context budget: this agent's context is now {size // 1000}k tokens, and every further turn "
        "re-sends all of it. "
    )
    tier = level(size)
    if tier == 1:
        return opening + (
            "Scope freeze: start no new deliverable. Everything from here goes toward landing what is "
            "already open — finishing it, verifying it, committing it. At 150k you will be asked to hand "
            "back whatever is still unfinished, so take on nothing you cannot land before then."
        )
    if tier == 2:
        return opening + (
            "Finish only the item in hand: get it to a verified commit, or, for a read-only job, write "
            "up what you have. Then end, listing every item not yet done so the orchestrator can hand it "
            "to a fresh agent. Don't start another item."
        )
    return opening + (
        "Stop here, even mid-item. Commit only what is already verified — start no further run to "
        "verify the rest — and report now. List everything unfinished, with what you learned about "
        "each, so the orchestrator can hand it to a fresh agent."
    )


def cache_seconds(env):
    """The prompt cache's lifetime: the override if it is a positive integer, else the default."""
    try:
        seconds = int(env.get("DELEGATE_SUBAGENT_CACHE_SECONDS", ""))
    except ValueError:
        return CACHE_SECONDS
    return seconds if seconds > 0 else CACHE_SECONDS


def waited(stamp, now):
    """Seconds since an ISO transcript timestamp, or None if it is missing or unreadable."""
    try:
        # Python before 3.11 reads no trailing "Z", which is how transcripts write UTC.
        issued = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if issued.tzinfo is None:  # a stamp with no zone is UTC, as transcripts write it, not local time
        issued = issued.replace(tzinfo=timezone.utc)
    return now - issued.timestamp()


def cold(seconds, size):
    """The line for a call that outlived the prompt cache."""
    minutes = max(1, int(seconds) // 60)
    return (
        f"Cache expired: this call returned {minutes} minute{'' if minutes == 1 else 's'} after the turn that "
        "issued it, longer than the prompt cache lasts, so each further turn here rewrites about "
        f"{size // 1000}k tokens of context. Land what is verified and report, rather than run another "
        "long command here."
    )


def advice(payload, env=None, now=None):
    path = transcript(payload)
    if not path or not os.path.isfile(path):
        return None
    history = turns(path)
    call = payload.get("tool_use_id")
    index = next((i for i, (_, ids, _) in enumerate(history) if call in ids), None)
    if index is None:
        return None
    size, ids, stamps = history[index]
    before = history[index - 1][0] if index > 0 else 0
    said = []
    # The turn that issued this call; parallel calls share it, so only its first call speaks a tier.
    if ids[0] == call and level(size) > level(before):
        said.append(nudge(size))
    # Any call can outlive the cache: the slow one of several parallel calls is not always the first.
    elapsed = waited(stamps.get(call), time.time() if now is None else now)
    if elapsed is not None and elapsed > cache_seconds(env or {}):
        said.append(cold(elapsed, size))
    return "\n\n".join(said) or None


def main():
    try:
        # Read stdin as UTF-8 explicitly: on native Windows Python, sys.stdin decodes with the
        # locale code page, which can fail json.load on a non-ASCII UTF-8 payload.
        payload = json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
        message = advice(payload, os.environ)
    except Exception:
        return
    if message:
        # The output names the event it answers: a failed call's reply is not a PostToolUse one.
        event = "PostToolUseFailure" if payload.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
        json.dump({"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}, sys.stdout)


if __name__ == "__main__":
    main()
