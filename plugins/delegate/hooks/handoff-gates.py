#!/usr/bin/env python3
"""PreToolUse on Agent: refuse, once, a maker handoff that carries no gate line.

The plugin's promise is that work is not accepted until a command proves it, and the orchestrator
conventions say every maker handoff ends in gates: `G<n> <outcome> - CHECK: <command> - EXPECT: <token>`.
Measured on one machine over about three days of use, 60% of `builder` prompts and 42% of
`builder-lite` prompts carried a gate line (some others kept the handoff in a file the prompt names).
A prose rule aimed at the orchestrator moves when it meets feedback at the moment of the call, so this
hook checks it there.

The handoff is the prompt plus the text of each file it names: an absolute or `~/` path ending `.md` or
`.txt` that exists, at most 256 KB, at most five. agent-cost reads handoff files by the same rule. A gate
line matches `\\bG\\d+\\b[^\\n]*CHECK:`. Without one the call is denied with a reason that says how to
write gates and that re-issuing the identical call goes through. That is checked in the session
transcript: an earlier Agent call with the same subagent_type and prompt whose result carries the tag
means the refusal was already given, so the call is allowed (the model-ceiling hook may add `model` on
the re-issue, so only prompt and subagent_type are compared).

It leaves alone every subagent type other than this plugin's `builder` and `builder-lite`, every call
made by a subagent (payload with `agent_id`), and everything when DELEGATE_HANDOFF_GATES=0 is set.
Anything unexpected exits silently: a handoff check is never worth breaking an Agent call.
"""
import io
import json
import os
import re
import sys

TAG = "[delegate: handoff gates]"
MAKERS = ("builder", "builder-lite")
GATE = re.compile(r"\bG\d+\b[^\n]*CHECK:")
# An absolute or ~/ path to a .md or .txt file, not part of a longer word or URL.
PATH = re.compile(r"(?<![\w.:/~-])(?:~/|/)[^\s`'\"()\[\]<>]*?\.(?:md|txt)(?![\w/-])")
FILE_MAX = 256 * 1024
FILES_MAX = 5
TAIL = 2 * 1024 * 1024  # the transcript's end; a refused call and its re-issue are close together


def own_name(root):
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        return json.load(f)["name"]


def handoff(prompt):
    """The prompt plus the text of each handoff file it names."""
    texts = [prompt]
    seen = []
    for match in PATH.findall(prompt):
        path = os.path.expanduser(match)
        if path in seen or not os.path.isfile(path) or os.path.getsize(path) > FILE_MAX:
            continue
        seen.append(path)
        if len(seen) > FILES_MAX:
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
        except OSError:
            continue
    return "\n".join(texts)


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def already_refused(path, subagent_type, prompt):
    """True if an earlier Agent call with this type and prompt got a result carrying the tag."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL))
        lines = f.read().splitlines()
    ids = set()
    for raw in lines:
        if b'"tool_use' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may be cut mid-record
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Agent":
                given = block.get("input") or {}
                if given.get("subagent_type") == subagent_type and given.get("prompt") == prompt:
                    ids.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in ids:
                if TAG in result_text(block.get("content")):
                    return True
    return False


def decision(payload, env):
    """The hook's output for this call, or None to leave the call alone."""
    if env.get("DELEGATE_HANDOFF_GATES", "").strip() == "0":
        return None
    if payload.get("tool_name") != "Agent" or payload.get("agent_id"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    root = env.get("CLAUDE_PLUGIN_ROOT")
    subagent_type = tool_input.get("subagent_type")
    prompt = tool_input.get("prompt")
    if not root or not isinstance(subagent_type, str) or not isinstance(prompt, str):
        return None
    plugin, _, rung = subagent_type.partition(":")
    if rung not in MAKERS or plugin != own_name(root):
        return None
    if GATE.search(handoff(prompt)):
        return None
    path = payload.get("transcript_path")
    if path and os.path.isfile(path) and already_refused(path, subagent_type, prompt):
        return None
    reason = (
        f"{TAG} this handoff to {subagent_type} has no gate line; end a maker handoff with one gate per "
        "required outcome, `G<n> <outcome> \u2014 CHECK: <command> \u2014 EXPECT: <token>`, plus the "
        "negative gate for each fix a test is said to pin; if no command can judge this job, re-issue "
        "the identical call and it goes through."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main():
    try:
        out = decision(json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")), os.environ)
    except Exception:
        return
    if out:
        json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
