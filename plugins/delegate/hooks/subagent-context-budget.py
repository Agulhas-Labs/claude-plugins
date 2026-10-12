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

A run of single read-only calls gets a "batch your reads" line. Measured across four projects on one
machine, five days: 85.6% of subagent turns made exactly one tool call (the baseline is 70%), and a
read-only call straight after another one was 27.9% of subagent turns and 23.8% of subagent spend.
The prose rule to request independent calls together did not change that, so the line says it at the
moment it happens: on the third turn in a row that made one read-only call, once, not on a fourth or
fifth. A turn with two or more calls, or a call that may write, ends the run. `DELEGATE_BATCH_NUDGE=0`
turns the line off.

Only subagent calls carry `agent_id`, so the main session is never told. Anything unexpected exits
silently: a budget nudge is never worth breaking a tool call.
"""
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

FREEZE = 120_000
HAND_BACK = 150_000
STOP = 200_000
STEP = 50_000  # beyond STOP, the stop tier repeats at each step as a backstop
TAIL = 1024 * 1024  # enough of the transcript's end to hold its last four assistant turns, which the batch check reads
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
    """(context size, tool_use ids, {tool_use id: timestamp}, {tool_use id: (name, input)}) of each distinct assistant message in the
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
            found[mid] = (size, [], {}, {})
            order.append(mid)
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found[mid][1].append(block.get("id"))
                found[mid][2][block.get("id")] = entry.get("timestamp")
                found[mid][3][block.get("id")] = (block.get("name"), block.get("input"))
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


# Copied verbatim from agent-cost (skills/agent-cost/scripts/agent_cost.py), so a call counts as
# read-only here exactly when it does in its turn-shape metric; keep the two in step with it. The
# agreement test in tests/ fails when they drift. `classify` keeps only the tools this hook meets.
PURE_FILTER = re.compile(r"^(grep|rg|tail|head|sed|awk|sort|uniq|wc|tee)\b")


# A loop that sleeps until a log shows a verdict line. What it costs is the wait on the run it watches,
# whatever it greps for meanwhile. The loop word opens a statement, so prose in a quoted message is not
# one; a `for` loop must `break`, or it is only pacing its own work.
WAIT_LOOP = re.compile(r"(?:^|[;&|\n(]\s*|\b(?:do|then)\s+)(for|while|until)\b([\s\S]*)\bdone\b")
SLEEPS = re.compile(r"\b(sleep|caffeinate -t)\b")


def is_wait_loop(c):
    m = WAIT_LOOP.search(c)
    if not m or not SLEEPS.search(m.group(2)):
        return False
    return m.group(1) != "for" or re.search(r"\bbreak\b", m.group(2)) is not None


def pipeline_stages(c):
    """Split on `|` outside quotes; `||` is not a pipe."""
    stages, start, quote, i = [], 0, None, 0
    while i < len(c):
        ch = c[i]
        if quote:
            if ch == "\\" and quote == '"': i += 1
            elif ch == quote: quote = None
        elif ch in "'\"": quote = ch
        elif ch == "\\": i += 1
        elif ch == "|":
            if c[i + 1:i + 2] == "|": i += 1
            else:
                stages.append(c[start:i]); start = i + 1
        i += 1
    stages.append(c[start:])
    return stages


def bash_class(cmd):
    c = cmd.strip()
    c = re.sub(r"^(cd [^;&\n]+(&&|;|\n)\s*)+", "", c)
    if is_wait_loop(c): return "bash: wait loop (polling a run)"
    for stage in pipeline_stages(c):
        stage = stage.strip()
        if stage and not PURE_FILTER.match(stage):
            found = stage_class(stage)
            if found != "bash: other": return found
            break
    return stage_class(c)


def stage_class(c):
    if re.search(r"\bswift (test|build)\b|xcodebuild", c): return "bash: swift build/test"
    if re.search(r"^(npm|pnpm|yarn|bun) (run )?(test|build)\b", c): return "bash: build/test"
    if re.search(r"^cargo (build|test)\b", c): return "bash: build/test"
    if re.search(r"^go (build|test)\b", c): return "bash: build/test"
    if re.search(r"^(pytest|python3? -m pytest)\b", c): return "bash: build/test"
    if re.search(r"^dotnet (build|test)\b", c): return "bash: build/test"
    if re.search(r"^(\./)?gradlew\b|^(gradle|mvn)\b", c): return "bash: build/test"
    if re.search(r"^make\b", c): return "bash: build/test"
    if re.search(r"swiftlint|swiftformat", c): return "bash: lint/format/gates"
    if re.search(r"^(npx )?(eslint|tsc|ruff|mypy|black|prettier)\b", c): return "bash: lint/format/gates"
    if re.search(r"^cargo clippy\b", c): return "bash: lint/format/gates"
    if re.search(r"^go vet\b", c): return "bash: lint/format/gates"
    if re.search(r"^(npm|pnpm|yarn|bun) (run )?lint\b", c): return "bash: lint/format/gates"
    if re.search(r"\bgit (diff|show)\b", c): return "bash: git diff/show"
    if re.search(r"\bgit (log|status|branch|rev-parse|merge-base|ls-remote|worktree|fetch|cherry)", c): return "bash: git log/status/etc"
    if re.search(r"\bgit (commit|push|rebase|merge|checkout|switch|add|reset|stash|cherry-pick)", c): return "bash: git mutate"
    if re.search(r"\bgh\b", c): return "bash: gh"
    if re.search(r"\b(grep|rg|git grep)\b", c): return "bash: grep"
    if re.search(r"^(cat|sed|head|tail|nl|awk)\b|\|\s*(sed|head|tail)", c): return "bash: cat/sed/head window"
    if re.search(r"python3?|jq\b", c): return "bash: python/jq"
    return "bash: other"


def classify(name, inp):
    if name == "Bash": return bash_class(inp.get("command", ""))
    if name == "Read":
        return "Read (ranged)" if inp.get("offset") or inp.get("limit") else "Read (whole file)"
    if name in ("Grep", "Glob"): return "Grep/Glob tool"
    return name


# Categories that only look: a run of single-call turns made of these could have been one request.
# MCP tools and "bash: other" are left out because they may write, so some reads go uncounted; and a
# read may have needed the one before it, so not every counted turn could have been saved.
READ_ONLY_CATEGORIES = frozenset({
    "Read (ranged)", "Read (whole file)", "Grep/Glob tool", "bash: grep",
    "bash: cat/sed/head window", "bash: git diff/show", "bash: git log/status/etc",
})


# Commands that the categories above file as looking but that change something: an in-place sed, a
# redirect into a file, tee, the git subcommands that move refs, the index or the worktree, `git branch`
# given a name (a listing flag or none stays a read), and a standalone rm.
WRITES = re.compile(r"\bsed\b[^|;&]*\s-i|(?<![0-9&>])>>?(?!&)\s*(?!/dev/null)[^\s&|;]|\btee\b"
                    r"|\bgit (branch -[dDmM]|worktree (add|remove|move|prune)|fetch|tag)\b"
                    r"|\bgit (checkout|switch|reset|stash|commit|merge|rebase|push|pull|cherry-pick|add|rm|mv|restore|clean)(?![\w-])"
                    r"|\bgit branch\s+(?![0-9]*>)[^-\s|;&]|(?:^|[;&(\n])\s*rm\s")


def turn_shape_category(cat, inp):
    """A call's category for the turn shape: a looking category is kept only when the command writes
    nothing, so a sed -i or a heredoc into a file never counts as a read."""
    if cat in READ_ONLY_CATEGORIES and cat.startswith("bash: ") and WRITES.search(inp.get("command", "")):
        return "bash: writes"
    return cat


def read_only(name, tool_input):
    """Whether one call only looks, by agent-cost's rule: its category is a looking one and the command writes nothing."""
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(tool_input.get("command", ""), str):
        return False
    return turn_shape_category(classify(name, tool_input), tool_input) in READ_ONLY_CATEGORIES


def qualifies(turn):
    """A turn qualifies when it made exactly one tool call and that call is read-only."""
    calls = turn[3]
    return len(calls) == 1 and read_only(*next(iter(calls.values())))


def batch(size):
    """The line for the third single read-only turn in a row."""
    return (
        "Batch your reads: your last three turns each made one read-only call, and every turn re-sends all "
        f"{size // 1000}k tokens of context. When you know the next reads, searches or checks you need, "
        "request them together in one response."
    )


def advice(payload, env=None, now=None):
    path = transcript(payload)
    if not path or not os.path.isfile(path):
        return None
    history = turns(path)
    call = payload.get("tool_use_id")
    index = next((i for i, (_, ids, _, _) in enumerate(history) if call in ids), None)
    if index is None:
        return None
    size, ids, stamps, _ = history[index]
    before = history[index - 1][0] if index > 0 else 0
    said = []
    # The turn that issued this call; parallel calls share it, so only its first call speaks a tier.
    if ids[0] == call and level(size) > level(before):
        said.append(nudge(size))
    if (env or {}).get("DELEGATE_BATCH_NUDGE") != "0" and index >= 2 and all(qualifies(history[i]) for i in (index - 2, index - 1, index)) \
            and not (index >= 3 and qualifies(history[index - 3])):
        said.append(batch(size))
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
