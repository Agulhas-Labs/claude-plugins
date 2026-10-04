# Delegate: Agents Sized to the Job

Your Claude Code session leads a team of agents, each on the cheapest model that can do its job, and no
work counts as done until a command proves it. Haiku runs commands, Sonnet makes changes that are already
spelled out, and Opus writes the code that still needs decisions. A separate reviewer sees only the
spec and the diff. No agent runs on a model above your session's, so a Sonnet session stays on Sonnet and
below. A built-in report shows where your tokens went.

A plugin for Claude Code, the terminal and IDE tool. It does not work in claude.ai chat or Cowork.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install delegate@agulhas-labs
```

Start a new session and work as you normally would. There's nothing to configure. Read
[Before you install](#before-you-install) first, though: agents commit to feature branches on their own.

## What changes in your session

Say you ask for a fix. Your session writes a handoff for `builder`, and the handoff ends in gates: one
command per outcome, with the output to expect.

| Gate | Check | Expect |
| --- | --- | --- |
| G1 an empty cart returns a validation error | `pytest tests/test_cart.py -k empty` | `passed` |
| G2 that test pins the fix | `git diff main -- src/cart > <scratch>/fix.patch && git apply -R <scratch>/fix.patch && pytest tests/test_cart.py -k empty; git apply <scratch>/fix.patch` | `1 failed` |
| G3 nothing else broke | `pytest` | `passed` |
| G4 lint is clean | `ruff check .` | `All checks passed` |

In the handoff itself each gate is one line: `G<n> <outcome> — CHECK: <command> — EXPECT: <token>`.

G2 is the one people skip. It takes the fix out, runs the new test and expects it to fail, because a
test that passes either way proves nothing.

The report that comes back is short:

```text
Committed on fix/empty-cart. G1 passed, G2 1 failed with the fix set aside (restored), G3 passed, G4 clean.
Decision: reused the existing ValidationError rather than adding a new one.
```

Your session then runs G1 to G4 itself before it believes any of it. That costs seconds and one turn,
where a second agent to check the first would cost a whole context.

Around that, a few rules hold:

- Each agent gets one job, and hands the rest back as its context grows.
- A subagent that tries to stop with a background command still running is held back once and told to
  wait for it or stop it.
- A change gets two review rounds at most, and each round goes to fresh agents, never a resumed one.
  Minor findings go in an issue.
- At most 4 agents run at once. That's a [setting](#running-more-or-fewer-agents-at-once), and it
  doesn't limit how many a session uses in total.

## See what it saves you

The plugin includes [`agent-cost`](#measure-it-agent-cost), which reads the transcripts already on your
disk and shows where your tokens went, by model and agent type. Use `--since` and `--until` to compare
the days before you installed with the days after. It changes nothing, so you can run it first to see
whether you have the problem this plugin solves: a few long-running agents taking most of your spend.

## How it works

Hooks inject two sets of conventions. The engineering rules reach your session and every subagent at
start: verify before calling anything done, stay in scope, commit verified work on a feature branch,
delete what you create, and stop only processes you started. The orchestrator rules reach only your
session and govern how it delegates: which agent gets which job, how a handoff ends, the concurrency cap,
and a separate git worktree for each agent that makes changes.

Two more hooks watch subagents. One warns an agent as its context grows, in three tiers:

- at 120k, freeze scope and start nothing new;
- at 150k, finish the item in hand and hand the rest back;
- at 200k, stop where it is and report, repeated every further 50k.

The other holds back, once, a subagent that tries to stop with a background run still going, and names
the run so the agent can wait for its verdict or stop it.

A small mod shows each running subagent above the prompt, and a toast when one ends (see
[Watching agents run](#watching-agents-run)).

The rules come from running `agent-cost` over one heavy week of real agent work. An agent re-sends its
whole context on every turn, so its cost grows with the square of its length. Measured that week:

- The longest 10% of subagents were 45% of all subagent spend. Hence one job per agent and the budget tiers.
- One builder resumed across six review rounds sent 112M input tokens, 80% of them old conversation.
  Hence a fresh builder per round, and two rounds at most.
- 70% of subagent turns made a single tool call, and those turns were two thirds of subagent spend.
  Agents are told to ask for independent calls together.
- Before the background-run hook, about 20 main-session turns in one day were agents waking it with
  nothing to report.

Those are shares of cost, weighted by price with cache reads at a tenth of fresh input, from one machine.
Your numbers will differ; `agent-cost` shows you yours.

## The roster

| Agent | Model | Its work |
| --- | --- | --- |
| your session | yours (Sonnet, Opus or Fable) | Plans, picks the agent for each job, writes the handoff, re-runs the gates. |
| `runner` | Haiku | A scripted list of commands whose output is the answer. It edits nothing. |
| `mechanic` | Sonnet | Bulky, fully specified changes: sweeps, renames, fixtures, running suites. |
| `builder-lite` | Sonnet | A small round of fixes a review has already spelled out. |
| `builder` | Opus, or your session's model if lower | Implementation inside an agreed plan, where decisions remain. |
| `reviewer` | Opus, or your session's model if lower | Sees the spec and the diff, never the reasoning that produced them. |

The agents are namespaced (`delegate:runner` and so on). The plugin never changes your session's
model, only what the subagents run on, and it never runs one above your session's model. Your session
passes its own model as a per-call override when a pin is higher, so on Sonnet the `builder` and
`reviewer` are Sonnet agents (still at high effort, which a call cannot change) and `builder-lite` or
`mechanic` take whatever fits them. Start on Opus for the full ladder.

## The model you start on is the ceiling

No agent runs on a model above your session's. Start on Opus and the whole ladder is available. Start
on Sonnet and your session passes `model: "sonnet"` on any call whose agent is pinned higher, so
`builder` and `reviewer` run on Sonnet (at their high effort, which a call cannot change) and
`builder-lite` or `mechanic` take whatever fits them. Start on Haiku and everything runs on Haiku. The
reviewer always runs: it is the only reader that sees just the spec and the diff.

On Sonnet, when a job needs stronger judgement, run `/advisor` and choose `opus` or `fable`. Your session
then asks that model for advice on its whole conversation, without spawning an agent. The advisor has
seen the plan, so it does not replace the reviewer.

## Running more or fewer agents at once

Set `DELEGATE_MAX_CONCURRENT_AGENTS` (default 4) in `~/.claude/settings.json` for yourself, or in a
repository's `.claude/settings.json`:

```json
{ "env": { "DELEGATE_MAX_CONCURRENT_AGENTS": "8" } }
```

Someone on one project at a time may want 2, and someone juggling several may want 8. Anything that
isn't a positive integer means the default.

## Watching agents run

While a subagent runs, a band above the prompt shows a count and one row per agent:

```
agents 2/6 running
run the suite  delegate:runner (haiku-4-5, low) · Bash · 0:42 · 52.0k tokens · ~$0.018 est.
```

The row is the agent's task description, then its type, model and effort, the tool it is in (its name
only, never its arguments; `working` between tools), the time so far, its tokens (input, output, cache
reads and cache writes together) and an estimated cost. A finished row stays about 20 seconds. When an
agent ends, a toast says the same, with `finished` or `stopped`: `delegate:runner (haiku-4-5) finished
in 14s · 52.0k tokens · ~$0.018 est.: run the suite`.

- **Colour:** a bold `Delegate` tag says where the band comes from. The running count is green at or
  under the cap and red over it; a stopped agent reads red; an agent's time turns yellow after five
  minutes (a display choice, not a measurement); a finished row is dim.
- **Collapsed:** with more running or recent agents than `collapseAbove` (default 3; 0 always), the band
  is one line, `agents 8/6 running · runner x5, builder x2, reviewer x1 · 112.0k tokens · ~$0.31 est.
  · 2 finished  [Expand]`: counts per agent type, and tokens and cost summed over every agent shown.
  `[Expand]` and `[Collapse]` switch it; your choice holds until no agent is left, then the count decides
  again. The choice is kept in memory only.
- **The cost is an estimate:** each request's tokens priced at the published list rate of the model
  that answered (`hooks/prices.json`), five-minute cache writes assumed. Effort shows as a label and is
  not a price factor: it changes how many tokens an agent uses, which the count already holds. There is
  no projected final cost. A model the table cannot place shows its tokens and `cost n/a`.
- **The count's cap** is `DELEGATE_MAX_CONCURRENT_AGENTS` when it is set, otherwise the `cap` option
  (default 6). Agents waiting for a free slot are not shown: a mod cannot see them.
- **Toasts** come only for runs of at least `minSeconds` (default 10; 0 for every run). `cap`,
  `minSeconds` and `collapseAbove` are the plugin's options.
- **What it reads and runs:** the session's own events (agent starts, model requests, tool calls, turn
  ends), its own price table and that one environment variable. It writes no files, starts no
  processes and makes no network calls, but like any mod it runs inside Claude Code with your
  permissions.
- **Where it shows:** Claude Code 2.1.287 or later, in the terminal and the Desktop app. Elsewhere, such
  as the VS Code chat panel or `claude -p`, it runs and draws nothing.

## Tailoring

Don't edit the installed copy, because an update replaces it. Copy an agent file into
`~/.claude/agents/` and edit that. It shows up under its bare name (`mechanic`) beside the plugin's
`delegate:mechanic`, so tell your session to use yours.

### Models

The roster needs Haiku, Sonnet and Opus, and the ladder was measured on those only. Each pin
is the `model:` line in its agent file. If you have Fable, it pays off in your session, which plans and
judges. Our recommendation, not a measurement: the one rung worth moving to `fable` is `reviewer`, whose
context is short and whose misses are expensive. Keep `builder` off Fable, since its long contexts
multiply the higher price.

### Tools

Every tool definition an agent carries is re-sent each turn, so the roster trims them. `runner` has a
short `tools:` allowlist. The others get everything except the built-ins no coding job uses, listed in
`disallowedTools`, and keep all your MCP servers, which load on demand. `reviewer` is also denied `Write`, `Edit`
and `NotebookEdit`, but it keeps `Bash`, so part of its read-only rule lives only in its prompt.

Ask for the `agent-cost` report with `--tools` to see what each agent type actually called. To add a tool, take it out
of `disallowedTools` (or add it to `runner`'s `tools:`); to drop one, add it there. MCP tools are named
`mcp__<server>__<tool>`. A name your installation doesn't have, server or built-in, is ignored.

### Conventions

A project's `CLAUDE.md` overrides the conventions, and a handoff or an agent's own
prompt overrides both. To turn the plugin off for one repository, commit this to its
`.claude/settings.json`:

```json
{ "enabledPlugins": { "delegate@agulhas-labs": false } }
```

## Before you install

Agents commit verified work on a feature branch without asking, and if your session is on the default
branch they branch off it first. Each agent that makes changes works in its own git worktree, which is
removed once its work is merged, and the commit is how that work gets back to you. Agents never push to
or merge into your default branch on their own. If you'd rather approve every commit, say so in the
project's `CLAUDE.md`, or turn the plugin off for that repository (see [Tailoring](#tailoring)).

Agents also report conventions they learn as candidates, and your session may record one as a test,
lint rule, skill or line in the project's guidelines.

## Measure it: agent-cost

See where your Claude Code tokens go: which projects and sessions cost the most, and how much your
subagents spend next to your own sessions. It reads the transcripts
Claude Code already keeps on your disk. It changes nothing, and nothing leaves your machine.

Ask "where did my tokens go this week?", or run `/delegate:agent-cost`. You get
back something like this (the figures here are made up for illustration):

```text
=== Totals ===
  main       contexts     14  turns    1210  raw input   142.6M  input-eq    16.3M  output     1.1M
  subagent   contexts     60  turns    5320  raw input   388.4M  input-eq    47.9M  output     2.4M

=== Turn shape ===
  main       turns carrying exactly one tool call:  61.2% of turns,  58.7% of input-eq spend
  subagent   turns carrying exactly one tool call:  84.5% of turns,  82.9% of input-eq spend
```

Here the subagents hold most of the spend, and most of it goes on turns that made a single tool call,
each one sending the whole context again to do one small thing. `raw input` is every input token
before cache weighting (fresh, cache reads and cache writes added together), and `input-eq` is spend
priced against the uncached input rate; [How the numbers work](#how-the-numbers-work) explains it.

### What's in the report

Your own sessions and the subagents they started are shown separately in every section. If you've
never started a subagent, there are no subagent rows and the report is about your sessions alone.
Besides totals by model, a per-day trend and a list of the largest contexts, you get these sections,
and each one says what to do about what it shows:

| Section | What it tells you | What helps |
| --- | --- | --- |
| Main sessions | Which projects and which of your own sessions cost the most, with each one's turns and peak context. | Ending a long session at a natural break: `/clear` and start the next piece of work from a short note, or `/compact`. |
| Concentration, Turn shape | Whether a few long-running contexts, or turns that make one tool call each, are taking most of your spend. | In your own sessions: shorter sessions, and asking for independent lookups together. For subagents, the rest of this plugin: one job per agent, a context budget, calls requested together. |
| Cold cache | How much went on messages sent into a large context after its prompt cache expired, and whether your sessions get the five-minute or the one-hour cache lifetime. | [`cache-guard`](../cache-guard/README.md), a sibling plugin, which warns you with the cost before that message is sent. |
| What fills the context | Which kind of content you keep re-sending, with your sessions' share and subagents' share side by side. | Reading files by range, and a code index in place of `cat` and `grep`. |
| Fixed start | What every context pays before any work: instruction files, MCP tool listings, the skill listing. | Trimming the instruction files every session loads (and every subagent loads again), and disconnecting servers a coding session never calls. |
| Tools called (`--tools`) | The tools each agent type actually called. | Editing an agent definition's `tools:` or `disallowedTools:` line. |

### Asking for what you need

The session runs the report for you, and you ask in plain words. To compare before and
after a change, ask for two windows that bracket it. The whole report is about 200 lines, so ask for all
of it once and then for the one section a follow-up question is about.

The report takes these options, and you can name one when you ask:

```text
agent-cost [--since X] [--until X] [--projects DIR] [--transcript PATH] [--top N] [--tools]
           [--sections NAMES]
```

- `--since` and `--until` take `today`, `yesterday`, `<N>d`, a date or an ISO datetime. The default is
  the last 7 days.
- `--transcript` reports on one session, with its subagents, or on one subagent file. A transcript
  is a `.jsonl` file; any other name is refused before it is read.
- `--sections` prints only the sections you name, such as `--sections totals,main,cold`. Any prefix of a
  heading works.
- `--tools` adds the Tools called section.
- `--top` sets how long the largest-sessions and largest-contexts lists are.
- `--projects` reads transcripts from another directory instead of Claude Code's own.

### How the numbers work

Every turn sends the whole context again, so what a context holds and what it costs you are different
numbers. Here's part of the What fills the context table, again with made-up figures:

```text
=== What fills the context ===
  category                                         calls  resident     %    re-sent     %
  (base) system prompt + tools + first prompt          0      900k  22.5      43.2M  24.0
  assistant: tool-call inputs (edits, commands)     1240      720k  18.0      29.5M  16.4
  Read (ranged)                                       96      410k  10.3      24.1M  13.4
  bash: cat/sed/head window                          130      300k   7.5      15.8M   8.8
  Read (whole file)                                   70      330k   8.3      11.9M   6.6
  bash: grep                                         180      190k   4.8       7.6M   4.2
```

`resident` is how much of each kind of content sat in those contexts, and `re-sent` is what it cost to
carry. The first row reads as 900k tokens of system prompt and tool definitions, paid for as 43.2M,
about fifty times over, before any work was done.

Spend is weighted by price against the uncached input rate. A cache read counts a tenth (a fortieth on
Fable 5.1, as published), a five-minute cache write 1.25 and a one-hour cache write 2. The figures are
shares of cost, not raw token counts. Output tokens are reported separately, and figures marked `≈` are
estimates from character counts.

The transcript format is undocumented and has changed before. The report lists every Claude Code
version it read, so you can check a surprising number against the version that produced it.

### What it needs

The report needs Python 3 and uses the standard library only. It is a script in the skill's own folder,
run with `python3`; where that is missing (often on Windows), the skill runs it with `py` or `python` instead.

## Requirements

It isn't tied to a language. Agents use whatever code intelligence you have, such as an LSP plugin or a
code-index server, and plain search otherwise.

The context hooks need a POSIX shell with `cat`, `sed`, `awk` and `tr`. The context-budget and
background-run hooks need Python 3 (standard library only) as `python3` or `python` (also `py` on
Windows). Without it those two are skipped silently and nothing else changes. The context-budget hook runs
after every tool call, in your session too, where it exits at once. On Windows, hooks need
Git Bash, Claude Code's usual setup; Windows is untested.

To install for a whole team and keep it updated, see the
[marketplace README](../../README.md#installing-for-a-team). Its sibling here,
[`cache-guard`](../cache-guard/README.md), warns you before a message goes into a large context whose
prompt cache has expired.
