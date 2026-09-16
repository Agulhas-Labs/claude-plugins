# Orchestration for Claude Code

A tiered agent roster for Claude Code, conventions that make the cheap tiers safe, a hook that keeps
agents small, and a report of where the tokens go.

By default, subagents run on the same model as your main session, they keep going until their context
is enormous, and the main session believes them when they say they're done. We measured one heavy week
of real agent work to see where the tokens actually went:

- **Long agents cost far more than they look.** Every turn re-sends the whole context, so an agent's
  cost grows with the square of its length. The longest 10% of subagents were 45% of all subagent spend.
  Split into three smaller agents, one 340-turn job would have cost less than half as much.
- **Resuming an agent pays for its past again.** One builder, resumed across six review rounds, sent
  112M input tokens. 80% of them were old conversation, re-sent on every turn.
- **Most turns were one tool call at a time.** 70% of subagent turns made a single call, and they were
  two thirds of all subagent spend, each one re-sending the whole context to do one small thing.

Spend here is weighted by price, with cache reads counted at a tenth of fresh input, so it reflects
cost rather than a raw token count.

This plugin is what we built from those numbers.

- **The right model for each job.** Your main session plans and judges on whatever model you choose.
  `runner` (Haiku) runs the commands you script. `mechanic` and `builder-lite` (Sonnet) make changes
  that are already specified. `builder` (Opus) takes the work that needs real design judgement.
  `reviewer` (Opus) reads the diff with fresh eyes: it sees the spec and the change, never the reasoning
  that talked the maker into it.
- **Nothing is done until a check says so.** Every handoff ends in commands that prove the outcome, and
  a fix only counts if its test fails without it. The main session re-runs those checks itself before it
  believes a report.
- **Agents stay small.** One job per agent. When a subagent's context passes 150k tokens, it's told to
  finish what it's holding and hand the rest back to a fresh one. Across 219 subagents, 55% of subagent
  spend came in turns above 200k.
- **Reviews end.** Two rounds at most. Minor findings go in an issue, not a third round.
- **You can see the bill.** The `agent-cost` skill reads your local transcripts and shows where every
  token went: by agent, by turn, by what filled each context. Nothing leaves your machine.

**One behaviour to know about before you install.** Agents commit verified work on a feature branch
without being asked, and never push to or merge into your default branch themselves. A project's
`CLAUDE.md` overrides this, or you can turn the plugin off for a repository; see
[Conventions](#conventions-what-they-change-and-how-to-override-them).

Those numbers are from one week on one machine, and your mix will be different. Run `agent-cost` on
your own transcripts and tune from there.

## Install

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install orchestration@agulhas-labs
```

Start a new session afterwards: plugins, agents and hooks load when a session starts.

To offer it to everyone working in a repository, commit this to the repository's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "agulhas-labs": { "source": { "source": "github", "repo": "Agulhas-Labs/claude-plugins" } }
  },
  "enabledPlugins": { "orchestration@agulhas-labs": true }
}
```

An organisation's managed settings can restrict which marketplaces and models are allowed; check them
first.

## What it looks like

You ask for a fix. The main session hands it to `builder`, and the handoff ends in gates, one per
outcome, each with the command that decides it:

```text
G1 an empty cart returns a validation error — CHECK: pytest tests/test_cart.py -k empty — EXPECT: passed
G2 that test pins the fix — CHECK: git diff main -- src/cart > <scratch>/fix.patch && git apply -R <scratch>/fix.patch && pytest tests/test_cart.py -k empty; git apply <scratch>/fix.patch — EXPECT: 1 failed
G3 nothing else broke — CHECK: pytest — EXPECT: passed
G4 lint is clean — CHECK: ruff check . — EXPECT: All checks passed
```

The report that comes back is short:

```text
Committed on fix/empty-cart. G1 passed, G2 1 failed with the fix set aside (restored), G3 passed, G4 clean.
Decision: reused the existing ValidationError rather than adding a new one.
```

Before it believes any of that, the main session runs G1 to G4 again itself. That costs seconds and one
turn of its own, not another agent.

## Requirements

It isn't tied to a language. Agents use whatever code intelligence you have installed, such as an LSP
plugin or a code-index server, and plain search when you have none.

The conventions reach the main session and every subagent through hooks that need nothing beyond a
POSIX shell with `cat`, `sed`, `awk` and `tr`. The context-budget hook needs Python 3 (standard library
only), found as `python3` or `python` (also `py` on Windows); without it that one hook is silently
skipped, so agents are not told when their context passes the budget, and nothing else changes. It
runs after every tool call, including in the main session, where it exits straight away. `agent-cost`
also needs Python 3; the session runs `agentcost.py` itself, with `python3`, or `py` or `python` where
that is the Python 3, so without Python it fails visibly instead. On Windows, hooks need Git Bash (Claude Code's usual
setup); Windows is untested.

## Models

The roster pins Haiku, Sonnet and Opus and needs all three available. The ladder was measured on those
models only. Each pin is one line in its agent file (`model:`), so move a rung if your plan or your
results say so, in your own copy of the file: [Tools](#tools-tailor-them-to-your-machine) shows how to
keep an edit across updates.

If you run your main session on Claude Fable, that is where it pays off: it's the session that plans
and judges. Our recommendation, not a measurement: the one rung worth moving to Fable is `reviewer`. Its
context is short and bounded, and a defect it misses is expensive; the change is its `model:` line, set to `fable`.
Don't put `builder` on Fable: its long contexts multiply a higher price.

## Tools: tailor them to your machine

The plugin's agents are namespaced: `orchestration:runner`, `orchestration:mechanic`, and so on.

Every tool definition an agent carries is re-sent on every turn, so the roster trims them. The makers
(`mechanic`, `builder-lite`, `builder`) and `reviewer` get every tool with the built-ins no coding job
uses denied. They keep every MCP server you have, because which servers matter differs from machine to
machine, and MCP tools already load on demand. `runner` carries a short allowlist instead, because its
job only runs commands or reads. `reviewer` keeps `Bash`, so its read-only rule is an instruction in
its prompt, not a limit the tool list enforces. The deny list names every built-in a coding job never
uses across current Claude Code builds; a name your version lacks is ignored.

Run `agent-cost --tools` to see what each agent type on your machine actually called, then edit the
agent files:

- to add a tool, remove it from `disallowedTools` (or add it to `tools:` on `runner`). An MCP server's
  tools are named `mcp__<server>__<tool>`, and naming a server that isn't installed is harmless;
- to drop one, add it to `disallowedTools`;
- to keep your edits, copy the agent file into `~/.claude/agents/`. It then appears under its bare name
  (`mechanic`) beside the plugin's `orchestration:mechanic`; tell the main session to use yours.

## Conventions: what they change, and how to override them

The engineering rules reach every agent in every repository where the plugin is enabled. Beyond
verification and scope, they tell agents to:

- commit each verified, self-contained unit on a feature branch without being asked, and never merge or
  push to the default branch without an instruction;
- report a convention they learn as a candidate; the main session records it as a test or lint rule, a
  skill, or a line in the project's guidelines;
- delete what they create (temp files, simulators, containers, worktrees) and stop only processes they
  started.

The orchestrator conventions, which only the main session sees, change how it delegates: a cap on
agents running at once, and each change-making agent in its own git worktree, branched from wherever
your session is and removed once its work is merged. Read-only agents need no worktree.

The cap is the one number people differ on, so it is a setting rather than a rule: `ORCHESTRATION_MAX_AGENTS`,
default 4. Someone on one project at a time may want 2; someone juggling several may want 8. Set it once
for yourself in `~/.claude/settings.json`, or for a repository in its `.claude/settings.json`:

```json
{ "env": { "ORCHESTRATION_MAX_AGENTS": "8" } }
```

Anything that is not a positive integer means the default.

A project's own `CLAUDE.md` overrides the conventions, and a handoff or an agent's own prompt overrides
both. To turn the plugin off for one repository, commit
`{"enabledPlugins": {"orchestration@agulhas-labs": false}}` to its `.claude/settings.json`. Don't edit
the installed copy: an update replaces it.

## Developing

```text
git config core.hooksPath githooks     # once per clone: the pre-commit gate
scripts/check.sh                        # tests, manifest validation, privacy scan
claude --plugin-dir plugins/orchestration
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
