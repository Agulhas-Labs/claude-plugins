# agent-cost

Shows where your Claude Code tokens went. It reads the transcripts already on your disk, changes
nothing, and nothing leaves your machine.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install agent-cost@agulhas-labs
```

Start a new session and ask "where did my tokens go this week?", or run `/agent-cost:report`.

## What it reports

Your own sessions and any subagents they started, each shown separately: totals by model, the trend per
day, which projects and sessions cost the most, how concentrated the spend is, how many turns made a
single tool call, what went on turns sent into an expired prompt cache, what every context pays before
it does any work, and what is sitting in the contexts you keep re-sending. If you have never started a
subagent, the report has no subagent rows and reads as your sessions' own picture. Part of that last
table, from two days on the machine this was built on:

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

Each section of the report says what to do about what it shows.

## What to do with it

| Section | What it tells you | What helps |
| --- | --- | --- |
| Main sessions | Which projects and which of your own sessions cost the most, with each one's turns and peak context. | Ending a long session at a natural break: `/clear` and start the next piece of work from a short note, or `/compact`. |
| Concentration, Turn shape | Whether a few long-running contexts, or turns that make one tool call each, are taking most of your spend. Shown for your sessions and for subagents separately. | In your own sessions: shorter sessions, and asking for independent lookups together. For subagents, [`orchestration`](../orchestration/README.md): one job per agent, a context budget, calls requested together. |
| Cold cache | How much went on messages sent into a large context after its prompt cache expired, and whether your sessions get the five-minute or the one-hour lifetime. | [`cache-guard`](../cache-guard/README.md): a warning, with the cost, before that message is sent. |
| What fills the context | Which kind of content you keep re-sending, with your sessions' share and subagents' share side by side. | Reading files by range, and a code index in place of `cat` and `grep`. |
| Fixed start | What every context pays before any work: instruction files, MCP tool listings, the skill listing. | Trimming the instruction files every session loads (and every subagent loads again), and disconnecting servers a coding session never calls. |
| Tools called (`--tools`) | The tools each agent type actually called. | Editing an agent definition's `tools:` or `disallowedTools:` line. |

## Options

```text
python3 agentcost.py [--since X] [--until X] [--projects DIR] [--transcript PATH] [--top N] [--tools]
                     [--sections NAMES]
```

The session runs the script for you; these are what you can ask for. `--since` and `--until` take
`today`, `yesterday`, `<N>d`, a date or an ISO datetime, and default to the last 7 days. To compare
before and after a change, ask for two windows that bracket it. `--transcript` reports on one session,
with its subagents, or on one subagent file.

`--sections` prints only the sections you name — `--sections totals,main,cold`, each name any prefix of
a heading. The whole report is about 200 lines, so ask for the whole thing once and then for the one
section a follow-up question is about.

## How to read the numbers

Spend is weighted by price against the uncached input rate: a cache read counts a tenth (a fortieth on
Fable 5.1, as published), a five-minute cache write 1.25, a one-hour cache write 2. So the figures are
shares of cost and not raw token counts. Output tokens are reported separately. Figures marked `≈` are
estimates from character counts.

The transcript format is undocumented and has changed before. The report prints every Claude Code
version it read, so a surprising number can be checked against the version that produced it.

## What it needs

Python 3, standard library only. The session runs `agentcost.py` itself, with `python3`, or `py` or
`python` where that is the Python 3, so without Python it fails visibly. Windows is untested.
