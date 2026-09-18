# orchestration

Your Claude Code session becomes a lead with a team. It plans and judges, on Opus, or on Fable if you have it. Haiku runs the commands, Sonnet makes the
changes that are already spelled out, and Opus writes the code that still has decisions in it, then
reviews the result without having seen how it was made. Every handoff ends in commands that prove the
work, and the lead runs them again itself before it believes the report.

Installing it also installs [`agent-cost`](../agent-cost/README.md), which reads the transcripts
already on your disk and shows where your tokens went. It alters nothing, and it tells you whether you
have the problem this plugin solves: a few long-running agents taking most of your spend. To find that
out before you change how you work, install `agent-cost` on its own first.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install orchestration@agulhas-labs
```

Start a new session and carry on working as you were. There is nothing to configure.

Its sibling in this marketplace is [`cache-guard`](../cache-guard/README.md), which warns you before a
message goes into a large context whose prompt cache has expired.

## What you get

| Agent | Model | The work it takes |
| --- | --- | --- |
| your session | Yours: Opus or Fable | Plans, picks the agent for each job, writes the handoff, re-runs its checks. |
| `runner` | Haiku | A scripted list of commands whose output is the answer. It edits nothing. |
| `mechanic` | Sonnet | Bulky, fully specified changes: sweeps, renames, fixtures, running suites. |
| `builder-lite` | Sonnet | A small round of fixes that a review has already spelled out. |
| `builder` | Opus | Implementation inside an agreed plan, where there are still decisions to make. |
| `reviewer` | Opus | The spec and the diff, nothing else. It never sees the reasoning that talked the maker into it. |

The plugin never changes your session's model, and it works the same with Opus or Fable in that seat.
If you do pay for Fable, this is where to spend it: the planning and the judging stay with it, and the
long contexts full of file reads run on cheaper models. [Models](#models) has more. Around the roster sit five rules, injected into the
session by hooks:

- A handoff ends in gates: one command per outcome, with the output to expect. A fix counts only if its
  test fails with the fix taken out.
- One job per agent. A hook warns a subagent in three tiers as its context grows, each asking for
  something different: at 120k freeze scope and start no new deliverable, at 150k finish what it is
  holding and hand the rest back, at 200k stop where it is and report. A fresh agent picks up the rest.
- A subagent does not stop while a command it put in the background is still running. A hook holds it
  back once and names the run: wait for the verdict, or stop the run, then report. Measured before the
  hook: about 20 turns of the main session in one day, each woken by an agent with nothing to report.
- Two review rounds at most. Minor findings go in an issue.
- At most 4 agents run at the same time, however many the session uses in total. The 4 is a setting.

## A handoff, start to finish

You ask for a fix. The main session hands it to `builder` with this at the end:

```text
G1 an empty cart returns a validation error — CHECK: pytest tests/test_cart.py -k empty — EXPECT: passed
G2 that test pins the fix — CHECK: git diff main -- src/cart > <scratch>/fix.patch && git apply -R <scratch>/fix.patch && pytest tests/test_cart.py -k empty; git apply <scratch>/fix.patch — EXPECT: 1 failed
G3 nothing else broke — CHECK: pytest — EXPECT: passed
G4 lint is clean — CHECK: ruff check . — EXPECT: All checks passed
```

G2 is the one people skip. It removes the fix, runs the new test and expects it to fail. A test that
passes either way proves nothing.

The report that comes back is two lines:

```text
Committed on fix/empty-cart. G1 passed, G2 1 failed with the fix set aside (restored), G3 passed, G4 clean.
Decision: reused the existing ValidationError rather than adding a new one.
```

The main session then runs G1 to G4 itself. That costs seconds and one turn, where a second agent to
check the first would cost a whole context.

## See your own bill

[`agent-cost`](../agent-cost/README.md), its own plugin in this marketplace, reads your local transcripts
and shows where your tokens went: by model and agent type, how concentrated the spend is, how many turns
made a single tool call, and what every agent pays before it does any work. `--since`/`--until` let you
compare the days before a change with the days after it, which is how to find out what this plugin
saved you.

## Why it is built this way

We ran `agent-cost` over one heavy week of real agent work, and the rules above are what the numbers
asked for.

An agent re-sends its whole context on every turn, so its cost grows with the square of its length. The
longest 10% of subagents were 45% of all subagent spend, and 55% of spend across 219 subagents came in
turns above 200k tokens. One 340-turn job, split into three agents, would have cost less than half as
much. So an agent gets one job, and a hook watches each subagent's context. One warning repeated at
every step told an agent nothing it did not already know, so there are three, and they differ in kind:
at 120k it freezes scope and is told a hand-back is coming, at 150k it finishes the item in hand and
leaves the rest for a fresh agent, at 200k it stops where it is and reports. The last one repeats every
further 50k as a backstop.

Resuming an agent pays for its past again. One builder, resumed across six review rounds, sent 112M
input tokens, and 80% of them were old conversation. So each review round gets a fresh agent, and
reviews stop after two.

70% of subagent turns made a single tool call. Those turns were two thirds of subagent spend, each one
re-sending everything to do one small thing. So agents are told to ask for independent calls together.

Subagents also run on your main session's model unless told otherwise, and most of what they do is
already specified by the time they get it. That is the roster.

Spend is weighted by price, with cache reads at a tenth of fresh input (a fortieth on Fable 5.1, as
published), so these are shares of cost and
not raw token counts. They come from one week on one machine. Your mix will differ, which is why the
measuring tool ships with the rules.

## Before you install

Agents commit verified work on a feature branch without being asked, and if your session is on the
default branch they branch off it first. The commit is how the work travels: each change-making agent
runs in its own git worktree, which is removed afterwards, and the checks compare the fix with the
branch it started from. They never push to or merge into your default branch themselves. If you would
rather approve every commit, say so in the project's `CLAUDE.md`, which overrides this, or turn the
plugin off for that repository; see [Conventions](#conventions-what-they-change-and-how-to-override-them).

## Requirements

It isn't tied to a language. Agents use whatever code intelligence you have installed, such as an LSP
plugin or a code-index server, and plain search when you have none.

The conventions reach the main session and every subagent through hooks that need nothing beyond a
POSIX shell with `cat`, `sed`, `awk` and `tr`. The context-budget hook and the background-run hook need Python 3
(standard library only), found as `python3` or `python` (also `py` on Windows); without it those two
are silently skipped, so agents are not told when their context passes the budget or held back from
stopping on a live run, and nothing else changes. The budget hook runs after every tool call, including in the main session, where it exits straight away. On Windows,
hooks need Git Bash (Claude Code's usual setup); Windows is untested.

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
its prompt, not a limit the tool list enforces. The deny list was written against current
Claude Code builds. If it names a tool your version doesn't have, that name is ignored.

Ask `agent-cost` for its `--tools` section to see what each agent type on your machine actually called, then edit the
agent files:

- To add a tool, remove it from `disallowedTools`, or add it to `tools:` on `runner`. An MCP server's
  tools are named `mcp__<server>__<tool>`, and naming a server that isn't installed is harmless.
- To drop a tool, add it to `disallowedTools`.
- To keep your edits, copy the agent file into `~/.claude/agents/`. It then appears under its bare name
  (`mechanic`) beside the plugin's `orchestration:mechanic`, so tell the main session to use yours.

## Conventions: what they change, and how to override them

The engineering rules reach every agent in every repository where the plugin is enabled. Beyond
verification and scope, they tell agents three things:

- Commit each verified, self-contained unit on a feature branch without being asked, and never merge or
  push to the default branch without an instruction.
- Report a convention they learn as a candidate. The main session records it as a test or lint rule, a
  skill, or a line in the project's guidelines.
- Delete what they create (temp files, simulators, containers, worktrees) and stop only processes they
  started.

The orchestrator conventions, which only the main session sees, change how it delegates: a cap on
agents running at once, and each change-making agent in its own git worktree, branched from wherever
your session is and removed once its work is merged. Read-only agents need no worktree.

The cap is the one number people differ on, so it is a setting rather than a rule:
`ORCHESTRATION_MAX_CONCURRENT_AGENTS`, default 4. It limits how many agents run at the same time, not
how many a session uses: a long job cut into many small agents is the cheap way to do it. Someone on
one project at a time may want 2, and someone juggling several may want 8. Set it once for yourself in `~/.claude/settings.json`, or for a repository in its `.claude/settings.json`:

```json
{ "env": { "ORCHESTRATION_MAX_CONCURRENT_AGENTS": "8" } }
```

Anything that is not a positive integer means the default. The setting was called
`ORCHESTRATION_MAX_AGENTS` until 0.1.1, and that name still works.

A project's own `CLAUDE.md` overrides the conventions, and a handoff or an agent's own prompt overrides
both. To turn the plugin off for one repository, commit
`{"enabledPlugins": {"orchestration@agulhas-labs": false}}` to its `.claude/settings.json`. Don't edit
the installed copy: an update replaces it.

Installing it for a whole team, and keeping it updated, are covered in the
[marketplace README](../../README.md#installing-for-a-team).
