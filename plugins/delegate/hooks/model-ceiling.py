#!/usr/bin/env python3
"""PreToolUse on Agent: run none of this plugin's agents on a model above the session's own.

Each rung pins a model in its agent file (builder and reviewer: opus). The orchestrator conventions ask
the session to pass its own model on any call whose rung is pinned higher, but a prompt rule was
measured not to hold: on one machine over eight days, 386 of 824 subagents ran on a model above their
parent session's, 48% of all input-equivalent spend. So this hook applies it: where the rung's pin
ranks above the session's model and the call names no model, it adds `model` set to the session's.

The session's model is the transcript's latest `message.model` of an assistant entry, or `modelId` of a
`model` attachment: on a session's first turn the message making this call is not yet in the transcript
when the hook runs, but the attachment naming the session's model is. Families rank
haiku < sonnet < opus < fable, by substring of the id. A call that already names a model, a subagent
type that isn't one of this plugin's rungs, an unknown model on either side, or
DELEGATE_MODEL_CEILING=0 in the environment all leave the call alone. Anything unexpected exits
silently: a model cap is never worth breaking an Agent call.
"""
import io
import json
import os
import sys

FAMILIES = ("haiku", "sonnet", "opus", "fable")  # lowest first
TAIL = 1024 * 1024  # enough of the transcript's end to hold its last assistant turn


def family(model):
    """The rank and name of the highest family named in a model id, or None."""
    model = (model or "").lower()
    found = [(rank, name) for rank, name in enumerate(FAMILIES) if name in model]
    return found[-1] if found else None


def session_model(path):
    """The model named last in the transcript's tail, by the session's own assistant entry or attachment."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        if b'"assistant"' not in raw and b'"model"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # the tail's first line may be cut mid-record
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        if entry.get("type") == "assistant":
            model = (entry.get("message") or {}).get("model")
        else:
            attachment = entry.get("attachment") or {}
            model = (attachment.get("identity") or {}).get("modelId") if attachment.get("type") == "model" else None
        if model and model != "<synthetic>":  # a locally made message names no model it ran on
            return model
    return None


def own_name(root):
    with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        return json.load(f)["name"]


def pin(root, rung):
    """The `model:` value in the rung's agent-file frontmatter, or None."""
    if not rung or rung != os.path.basename(rung) or rung.startswith("."):
        return None
    path = os.path.join(root, "agents", f"{rung}.md")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "model":
            return value.strip().strip("\"'")
    return None


def decision(payload, env):
    """The hook's output for this call, or None to leave the call alone."""
    if env.get("DELEGATE_MODEL_CEILING", "").strip() == "0":
        return None
    if payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("model"):
        return None
    root = env.get("CLAUDE_PLUGIN_ROOT")
    plugin, _, rung = str(tool_input.get("subagent_type") or "").partition(":")
    if not root or not rung or plugin != own_name(root):
        return None
    pinned = family(pin(root, rung))
    path = payload.get("transcript_path")
    session = family(session_model(path)) if path and os.path.isfile(path) else None
    if not pinned or not session or pinned[0] <= session[0]:
        return None
    # The hooks reference, PreToolUse decision control, on `updatedInput`: "Replaces the entire input
    # object, so include unchanged fields alongside modified ones." and "Combine with "allow" to
    # auto-approve, or "ask" to show the modified input to the user." Neither is set, so the call keeps
    # whatever permission it would have had: measured on Claude Code 2.1.293, updatedInput alone was
    # applied in both the auto and default permission modes.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": dict(tool_input, model=session[1]),
        }
    }


def main():
    try:
        # Read stdin as UTF-8 explicitly: on native Windows Python, sys.stdin decodes with the
        # locale code page, which can fail json.load on a non-ASCII UTF-8 payload.
        out = decision(json.load(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")), os.environ)
    except Exception:
        return
    if out:
        json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
