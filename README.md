# Orchestration for Claude Code

Your Claude Code session becomes a lead with a team. It plans and judges, on Opus, or on Fable if you have it. Haiku runs the commands, Sonnet makes the
changes that are already spelled out, and Opus writes the code that still has decisions in it, then
reviews the result without having seen how it was made. Every handoff ends in commands that prove the
work, and the lead runs them again itself before it believes the report.

It comes with `agent-cost`, which reads the transcripts already on your disk and shows where your
tokens went. Run it before you change how you work. It alters nothing, and it tells you whether you
have the problem the rest of this solves: a few long-running agents taking most of your spend. It is
worth the install even if you ignore everything else.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install orchestration@agulhas-labs
```

Start a new session and carry on working as you were. There is nothing to configure.

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
long contexts full of file reads run on cheaper models. [Models](#models) has more. Around the roster sit four rules, injected into the
session by hooks:

- A handoff ends in gates: one command per outcome, with the output to expect. A fix counts only if its
  test fails with the fix taken out.
- One job per agent. When a subagent's context passes 150k tokens, a hook tells it to finish what it is
  holding and hand the rest back, and a fresh agent picks it up.
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

Ask "where did my tokens go this week?" and `agent-cost` answers from your local transcripts. Nothing
leaves your machine. It reports totals by model and by agent type, how concentrated the spend is, how
many turns made a single tool call, what every agent pays before it does any work, and what is sitting
in the contexts you keep re-sending. Part of that last table, from two days on the machine this was
built on:

```text
=== What fills the context ===
  category                                         calls  resident     %    re-sent     %
  (base) system prompt + tools + first prompt          0      3.0M  23.3     148.3M  23.6
  assistant: tool-call inputs (edits, commands)     4973      2.2M  17.7      96.6M  15.3
  Read (ranged)                                      398      1.2M   9.6      84.9M  13.5
  bash: cat/sed/head window                          519      954k   7.5      56.6M   9.0
  Read (whole file)                                  276      1.1M   8.5      42.6M   6.8
  bash: grep                                         725      583k   4.6      27.4M   4.4
```

`resident` is how much of each kind of content sat in those contexts. `re-sent` is what it cost to
carry, because every turn sends the whole context again. Read the first row that way: 3M tokens of
system prompt and tool definitions, paid for as 148M, about fifty times over, before any work was done.

Each section of the report says what to do about what it shows, and `--since`/`--until` let you compare
the days before a change with the days after it.

## Why it is built this way

We ran `agent-cost` over one heavy week of real agent work, and the rules above are what the numbers
asked for.

An agent re-sends its whole context on every turn, so its cost grows with the square of its length. The
longest 10% of subagents were 45% of all subagent spend, and 55% of spend across 219 subagents came in
turns above 200k tokens. One 340-turn job, split into three agents, would have cost less than half as
much. So an agent gets one job, and a hook watches each subagent's context: past 150k tokens it is told
to finish the item in hand and leave the rest for a fresh agent.

Resuming an agent pays for its past again. One builder, resumed across six review rounds, sent 112M
input tokens, and 80% of them were old conversation. So each review round gets a fresh agent, and
reviews stop after two.

70% of subagent turns made a single tool call. Those turns were two thirds of subagent spend, each one
re-sending everything to do one small thing. So agents are told to ask for independent calls together.

Subagents also run on your main session's model unless told otherwise, and most of what they do is
already specified by the time they get it. That is the roster.

Spend is weighted by price, with cache reads at a tenth of fresh input, so these are shares of cost and
not raw token counts. They come from one week on one machine. Your mix will differ, which is why the
measuring tool ships with the rules.

## Before you install

Agents commit verified work on a feature branch without being asked, and if your session is on the
default branch they branch off it first. The commit is how the work travels: each change-making agent
runs in its own git worktree, which is removed afterwards, and the checks compare the fix with the
branch it started from. They never push to or merge into your default branch themselves. If you would
rather approve every commit, say so in the project's `CLAUDE.md`, which overrides this, or turn the
plugin off for that repository; see [Conventions](#conventions-what-they-change-and-how-to-override-them).

## Installing it for a team

The two commands at the top install it for you. To offer it to everyone working in a repository, commit
this to the repository's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "agulhas-labs": {
      "source": { "source": "github", "repo": "Agulhas-Labs/claude-plugins" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "orchestration@agulhas-labs": true }
}
```

## Updates

A marketplace you add yourself does not update on its own. Turn that on once: `/plugin`, then
Marketplaces, `agulhas-labs`, Enable auto-update. Claude Code then refreshes the marketplace and its
installed plugins when it starts. The `"autoUpdate": true` line above does the same for a repository.
To update by hand, run `/plugin marketplace update agulhas-labs`.

Plugins, agents and hooks load when a session starts, so start a new one after installing or updating.
An organisation's managed settings can restrict which marketplaces and models are allowed; check them
first.

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
its prompt, not a limit the tool list enforces. The deny list was written against current
Claude Code builds. If it names a tool your version doesn't have, that name is ignored.

Run `agent-cost --tools` to see what each agent type on your machine actually called, then edit the
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

## Developing

```text
git config core.hooksPath githooks     # once per clone: the pre-commit gate
scripts/check.sh                        # tests, manifest validation, privacy scan
claude --plugin-dir plugins/orchestration
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
