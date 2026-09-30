---
name: prove-hook-output
description: >-
  Prove that a hook's output actually reaches the person at the terminal, not just the model's
  context. Use when changing what a hook prints or returns — systemMessage, additionalContext,
  terminalSequence, a block reason — or when a hook "fires correctly" and the user still reports
  seeing nothing.
---

# prove-hook-output

A hook firing is not a hook being seen. These go to different readers, and a change that looks right
in the hook's own tests can still leave the person in front of the terminal staring at a blank prompt:

| What the hook returns | Who receives it |
| --- | --- |
| stdout (plain text) | the model's context |
| `hookSpecificOutput.additionalContext` | the model's context |
| `systemMessage` | the person, rendered in the terminal |
| `terminalSequence` | the terminal itself, as an escape sequence |
| `decision: "block"` + `reason` | the person, and the prompt is held back |

So "the hook ran and returned the right JSON" answers a different question from "the user saw it".
Prove the second one, in a real terminal, before saying a visibility change works.

## The three levels of proof

Use the cheapest one that answers the question you actually have.

1. **Unit tests** on the hook's own functions. Fast, and they pin the content. They cannot tell you
   whether the host accepts the field or shows it to anyone.
2. **Headless** (`claude -p --output-format stream-json --verbose`). Proves the hook is registered,
   runs, and that its JSON was parsed and accepted — look for a `hook_response` event with
   `"exit_code": 0` and `"outcome": "success"`. It does **not** prove rendering: headless has no UI,
   and `systemMessage` appears only inside the `hook_response` blob, never as its own event.
3. **A real PTY.** The only thing that proves a person would see it. This is what the rest of this
   skill is for.

## Before anything: an installed plugin shadows your working copy

If a plugin of the same name is installed, `--plugin-dir` loads yours but the installed one wins —
silently. Hooks from the working copy never register and the proof passes for the wrong reason.

Always copy the plugin to a scratch directory, rename it in `.claude-plugin/plugin.json`, and load
that. `scripts/prove-render.sh` does this for you and deletes the copy afterwards.

## The PTY proof

```sh
.claude/skills/prove-hook-output/scripts/prove-render.sh \
  --plugin plugins/<name> \
  --grep '<a distinctive phrase from the message>' \
  --env SOME_PLUGIN_VAR=/tmp/somewhere
```

It copies the plugin under a scratch name, starts Claude Code under `expect` in a PTY, waits, quits
with Ctrl-C, and then reports three things from the captured session:

- whether the phrase appears in the ANSI-stripped output — the message was **rendered**;
- every OSC escape sequence the terminal received — a `terminalSequence` was **emitted**;
- any `rejected by the allowlist` line — a `terminalSequence` was **refused**.

Read what it prints. A missing phrase with a clean exit means the hook ran and the person saw
nothing, which is exactly the bug this skill exists to catch.

### Three things that will waste your time

These cost three failed attempts the first time round. All three are avoided by the script, but you
need to know them when you drive `expect` yourself.

- **A fresh directory triggers the trust dialog**, and the session stops there forever. Don't run
  from a temp directory. Run from a directory already trusted, and point the plugin's *output* 
  somewhere else with `--env`. Answering the dialog with arrow keys works, but it is fragile and
  there is no reason to.
- **The TUI writes cursor-moves between words**, so an `expect` pattern of more than one word never
  matches — `"trust this folder"` matches nothing while `"trust"` matches. Don't pattern-match the
  screen at all: sleep, quit, and grep the captured log afterwards.
- **`timeout(1)` is not on every machine.** Use the harness's own timeout, or `expect`'s.

### Reading the log by hand

Strip the ANSI to see what a person saw:

```sh
LC_ALL=C sed -e 's/\x1b\[[0-9;?]*[a-zA-Z]//g' -e 's/\r/\n/g' session.log
```

A rendered `systemMessage` from a SessionStart hook looks like this, prefixed by the host:

```
⎿  SessionStart:startup says: <your message>
```

Find escape sequences in the **raw** bytes, not the stripped text — stripping removes the evidence:

```sh
python3 -c "import re,sys;print(re.findall(rb'\x1b\][0-9]+;[^\x07]*\x07', open(sys.argv[1],'rb').read()))" session.log
```

## If the question is about the hook contract, read the binary

The documentation site paginates and truncates, and it will not reliably tell you whether a given
field applies to a given event. The installed CLI is a single executable that carries its own schema
text and its own error strings, and it answers in one command:

```sh
CLI=$(command -v claude); CLI=$(readlink -f "$CLI" 2>/dev/null || echo "$CLI")
LC_ALL=C strings -a "$CLI" | grep -o '.\{200\}systemMessage.\{200\}' | head
```

Keep the context window small (a few hundred characters) — a wide one produces megabytes of output
and some `grep` builds refuse it outright as too complex.

That is how the table at the top of this file was established, and how these constraints on
`terminalSequence` were, which the host enforces and silently drops anything that breaks:

- only OSC `0`, `1`, `2`, `9`, `99` and `777`, terminated by BEL or ST;
- the whole sequence under 4096 bytes;
- an OSC `9` body may not begin with a digit unless it is the `9;4` progress form;
- control characters are stripped from the payload;
- the host wraps the sequence for tmux and screen itself, so never do that in a hook.

## When it is proven

Say what was rendered, quoting it, and say which level of proof it came from. "The hook returns the
right JSON" is level 2 and should be described as that, not as evidence that anyone saw anything.
