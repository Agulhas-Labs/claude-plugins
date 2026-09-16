---
name: agent-cost
description: >-
  Report where Claude Code token spend goes — totals, per-day trend, concentration, turn shape, what
  fills the context, and the fixed start every context pays. Use when asked where tokens/usage went,
  why Claude Code usage or cost is high, how much a subagent or agent type is spending, what a subagent
  starts with (instructions, deferred tools, skill listing), or to compare spend before/after a change.
---

# agent-cost

Run `agentcost.py`, beside this file in the skill's directory, rather than analysing transcripts by hand or writing a
new script — it already parses the (undocumented, versioned) transcript format correctly: turn
deduplication by `message.id` (a later duplicate line carries the *more complete* `output_tokens`, not
the first), the window-vs-mtime prefilter, and the pricing formula below.

```
python3 agentcost.py [--since X] [--until X] [--projects DIR] [--transcript PATH] [--top N] [--tools]
```

- On Windows run it with `py` or `python`, whichever is the Python 3 on the machine: a bare `python3`
  there is often a Store placeholder that fails.

- `--since`/`--until` accept `today`, `yesterday`, `<N>d`, `YYYY-MM-DD`, or an ISO datetime
  (`2026-09-15T16:06`, local unless it carries `Z`/an offset). Default: last 7 days.
- To compare spend **before/after a change**, run it twice with `--since`/`--until` datetimes that
  bracket each side of the change (e.g. the commit time), not two separate loose windows.
- `--tools` adds a section listing the tools each agent type actually called, with counts. It is the
  evidence for an agent definition's `tools:`/`disallowedTools:` line: a tool a type never calls is
  context re-sent on every one of its turns, and a tool its jobs need but its list drops is a failure.
- `--transcript PATH` reports on one session (its `subagents/` come along) or one subagent file,
  ignoring the window — use it to inspect a single agent's start or shape.

## Reading the report

- **Totals / by model** — the top-line number. Split main vs subagent, because subagent spend is
  usually the larger and more actionable share.
- **Per day** — trend. A rising top-10% share or rising median turns over days means contexts are
  running longer, not that more work is happening.
- **Concentration** — spend is roughly quadratic in turns (a turn resends its whole context), so a
  high top-10%-of-contexts share, or a low share from contexts under 50 turns, means a few long-running
  agents dominate the bill. **Action**: hand off one job per agent, start a fresh agent for each review
  round, never resume a long-running one to "just finish one more thing."
- **Turn shape** — the % of turns carrying exactly one tool call, and their share of spend. High values
  here mean calls that could have been requested together were made one at a time, each paying the full
  context over again. **Action**: batch independent tool calls into one response.
- **What fills the context** — a category table showing
  which kind of content is resident in a typical context and how much of it gets re-sent turn after
  turn. **Action**: if `Read (whole file)` or a re-read-heavy category dominates, read files by range
  and don't re-read what's already in context; if a `bash: cat/sed/head window` or grep category is
  high, prefer the project's structural index/search tool over ad hoc text search.
- **Fixed start** — the context every turn pays before any work happens, by model and agent type, then
  broken into instructions files, deferred MCP tool counts, skill listing, hook context, and the first
  prompt. **Action**: the per-file instruction sizes say which rule or `CLAUDE.md` to trim (a file
  loaded into every subagent is re-sent on every one of its turns); the per-server deferred-tool counts
  say which MCP servers/connectors to disconnect for coding sessions; the skill listing likewise if it's
  large relative to what a session actually uses.
- **Largest contexts** — the worst individual offenders, for a closer look with `--transcript`.

## Caveats (the report states these too)

- Input-equivalent (`input-eq`) prices every token against the uncached input rate (cache read x0.1,
  cache write 5-minute x1.25, cache write 1-hour x2, uncached x1) — it is **a price comparison, not a
  token count**. Output tokens are reported separately and never folded into it.
- Figures marked `≈` are `chars/4` estimates, not exact token counts.
- The transcript format is undocumented and has changed before; the report prints every harness
  `version` it read so a surprising number can be checked against the version that produced it.
