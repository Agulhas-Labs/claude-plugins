---
name: report
description: >-
  Report where Claude Code token spend goes, for main sessions and subagents separately — totals,
  per-day trend, the costliest projects and sessions, concentration, turn shape, cold cache, what fills
  the context, and the fixed start every context pays. Use when asked where tokens/usage went, why
  Claude Code usage or cost is high, which session or project cost the most, how much a subagent or
  agent type is spending, what a session or subagent starts with (instructions, deferred tools, skill listing), or to compare spend before/after a change.
---

# agent-cost

Run `agentcost.py`, beside this file in the skill's directory, rather than analysing transcripts by hand or writing a
new script — it already parses the (undocumented, versioned) transcript format correctly: turn
deduplication by `message.id` (a later duplicate line carries the *more complete* `output_tokens`, not
the first), the window-vs-mtime prefilter, and the pricing formula below.

```
python3 agentcost.py [--since X] [--until X] [--projects DIR] [--transcript PATH] [--top N] [--tools]
                     [--sections NAMES]
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
- `--sections NAMES` prints only those sections, comma-separated, each named by any prefix of it:
  `totals, per-day, main-sessions, concentration, turn-shape, cold-cache, what-fills-the-context,
  fixed-start, largest-contexts, tools-called`. An unknown name fails before any transcript is read
  and lists the ones that exist.
- **Ask for the sections the question needs.** The full report is about 200 lines and every line of it
  lands in the context. "Why was yesterday expensive?" is `--sections totals,per-day,main-sessions`,
  not Fixed start; "did that change help?" is the one or two sections that measure it, run over each
  window; "what should this agent's `tools:` be?" is `--sections tools-called`. Run the whole report
  when the question is open ("where did my tokens go?"), and a selection for every follow-up after it —
  re-reading a section you already have costs as much as reading it the first time.

## Reading the report

The section names `--sections` takes are the headings below, lowercased and hyphenated, plus
`tools-called` for the `--tools` section.

Every section shows main sessions and subagents separately, and leaves out a kind the window does not
hold: someone who has never started a subagent gets no subagent rows. Read the rows for the kind that
holds the spend first, and give advice for that kind. Delegation advice means nothing to a person whose
spend is all in their own sessions.

- **Totals** (`/ by model`) — the top-line number, main and subagent each on its own row.
- **Per day** — trend, with each day's main and subagent spend. A rising top-10% share or rising median
  turns over days means contexts are running longer, not that more work is happening. A day with under
  ten contexts shows `-` for the top-10% share: there is no decile to take.
- **Main sessions** — spend per project, then the largest sessions with their turns and peak context.
  **Action**: a session with hundreds of turns and a peak in the hundreds of thousands is the person's
  own largest lever. End it at a natural break and start the next piece of work fresh (`/clear`, from
  a short note of where things stand), or `/compact`.
- **Concentration** — spend is roughly quadratic in turns (a turn resends its whole context), so a
  high top-10%-of-contexts share, or a low share from contexts under 50 turns, means a few long-running
  contexts dominate the bill. Under ten contexts it reports the largest context's share instead.
  **Action**, main: shorter sessions, as above. **Action**, subagent: hand off one job per agent,
  start a fresh agent for each review round, never resume a long-running one to "just finish one more
  thing."
- **Turn shape** — the % of turns carrying exactly one tool call, and their share of spend. High values
  here mean calls that could have been requested together were made one at a time, each paying the full
  context over again. **Action**: batch independent tool calls into one response. In a main session the
  person can ask for it ("look these up together"); for subagents it belongs in the handoff or the
  agent definition.
- **Cold cache** — the turns that arrived after the prompt cache had expired (a gap of 5 minutes or
  more, and under half the previous context read back from cache), their share of spend, and the
  "avoidable" part: what writing that context in again cost above a warm cache read. The `cache writes`
  line shows the lifetime the writes were actually bought at, per kind — where the records carry the
  split, and prints `no lifetime recorded` where they do not — and it sets how long an idle context
  stays warm. The main-only gap x size table is the case for a guard that warns before a prompt is
  sent into a cold, large context. **Action**: a large main-session context left idle past the cache
  lifetime is cheaper to `/clear` and restart from a short handoff than to continue; `/compact` pays
  for the cold context once, and every turn after it is small. A model or effort change mid-session
  resets the cache the same way, with no gap, so this section does not count it. The section also
  shows what subagent cold turns were waiting on; when most follow one Bash call, the fix is fewer
  long waits inside a large context (run the long suite once, at the end; a narrow test between
  edits), and no prompt guard can catch them.
- **What fills the context** — a category table showing
  which kind of content is resident in a typical context and how much of it gets re-sent turn after
  turn. With both kinds present, `main %` and `sub %` give each category's share of that kind's own
  re-sent total, so each column adds to 100% and the two can be compared row by row. `bash: wait loop
  (polling a run)` is a command that slept until a log showed a verdict: its cost is the wait, which
  the Cold cache section prices. **Action**: if `Read (whole file)` or a re-read-heavy category dominates, read files by range
  and don't re-read what's already in context; if a `bash: cat/sed/head window` or grep category is
  high, prefer the project's structural index/search tool over ad hoc text search.
- **Fixed start** — the context every turn pays before any work happens, by model and agent type, then
  broken into instructions files, deferred MCP tool counts, skill listing, hook context, and the first
  prompt. **Action**: the per-file instruction sizes say which rule or `CLAUDE.md` to trim (a file
  loaded into every subagent is re-sent on every one of its turns); the per-server deferred-tool counts
  say which MCP servers/connectors to disconnect for coding sessions; the skill listing likewise if it's
  large relative to what a session actually uses. All of it applies to a main session as much as to a
  subagent: the difference is that a subagent pays it again for every agent started.
- **Largest contexts** — the worst individual offenders, for a closer look with `--transcript`.

## Caveats (the report states these too)

- Input-equivalent (`input-eq`) prices every token against the uncached input rate (cache read x0.1, or x0.025 on
  Claude Fable 5.1, whose published cache-read price is a fortieth of its input price;
  cache write 5-minute x1.25, cache write 1-hour x2, uncached x1) — it is **a price comparison, not a
  token count**. Output tokens are reported separately and never folded into it.
- Figures marked `≈` are `chars/4` estimates, not exact token counts.
- The transcript format is undocumented and has changed before; the report prints every harness
  `version` it read so a surprising number can be checked against the version that produced it.
