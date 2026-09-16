#!/usr/bin/env python3
"""PostToolUse: tell a subagent to wrap up once its context passes a budget.

An agent's spend is its context summed over every turn, so it grows with the square of the agent's
length. Measured over 219 subagents: 55% of subagent spend came in turns above 200k.
When a subagent's context crosses 150k, and each further 50k, it is told to finish the item in hand
and hand the rest back, so the orchestrator can give the remainder to a fresh agent.

Only subagent calls carry `agent_id`, so the main session is never told. Anything unexpected exits
silently: a budget nudge is never worth breaking a tool call.
"""
import io
import json
import os
import sys

FIRST = 150_000
STEP = 50_000
TAIL = 1024 * 1024  # enough of the transcript's end to hold its last two assistant turns


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
    """(context size, tool_use ids) of each distinct assistant message in the transcript's tail."""
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
            found[mid] = (size, [])
            order.append(mid)
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found[mid][1].append(block.get("id"))
    return [found[mid] for mid in order]


def level(size):
    return 0 if size < FIRST else 1 + (size - FIRST) // STEP


def advice(payload):
    path = transcript(payload)
    if not path or not os.path.isfile(path):
        return None
    history = turns(path)
    # The turn that issued this call; parallel calls share it, so only its first call speaks.
    index = next((i for i, (_, ids) in enumerate(history) if payload.get("tool_use_id") in ids), None)
    if index is None or history[index][1][0] != payload.get("tool_use_id"):
        return None
    size = history[index][0]
    before = history[index - 1][0] if index > 0 else 0
    if level(size) <= level(before):
        return None
    return (
        f"Context budget: this agent's context is now {size // 1000}k tokens, and every further turn "
        "re-sends all of it. Finish only the item in hand: get it to a verified commit, or, for a "
        "read-only job, write up what you have. Then end, listing every item not yet done so the "
        "orchestrator can hand it to a fresh agent. Don't start another item."
    )


def main():
    try:
        # Read stdin as UTF-8 explicitly: on native Windows Python, sys.stdin decodes with the
        # locale code page, which can fail json.load on a non-ASCII UTF-8 payload.
        message = advice(json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")))
    except Exception:
        return
    if message:
        json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}, sys.stdout)


if __name__ == "__main__":
    main()
