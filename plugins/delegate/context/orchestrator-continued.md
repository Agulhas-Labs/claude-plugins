# Orchestrator conventions, continued

This continues the orchestrator conventions: the rest of the main session's delegation rules, which
subagents never see.

## Budget

- **At most {{MAX_AGENTS}} agents at a time; queue the rest.** Every report still has to be verified here,
  and every agent draws on the same rate limit. The cap is the user's `DELEGATE_MAX_CONCURRENT_AGENTS` setting.
  Order the queue by what is closest to finishing. Never cancel running work to get under the cap.
- **Parallel makers never share a checkout.** Run each change-producing agent in its own worktree
  (`isolation: "worktree"` on the Agent call), or at most one maker per checkout. An isolated worktree
  branches from your current HEAD, so name in the handoff the branch the agent starts from and the one
  it merges into. Remove the worktree and its branch once merged. Read-only agents (runner, reviewer) need neither.
- **At most two review rounds per change.** Once a reviewer passes it, or passes it with only
  low-severity items, stop: those go into one issue, not another round. The count is per change, not
  per batch: a second round covers only the branches that failed. A re-review reads the fixes and the
  findings, not the whole branch again (measured: it cost as much as the first review).
- **Side findings are filed, not fixed in the session,** unless they would lead users to believe
  something wrong and act on it.
- **Cheap makers, one expensive review, at the end.** No reviewer round for a diff under about 50 lines,
  and a fix of a few lines the orchestrator makes itself.
- **A subagent is warned in three tiers as its context grows, and is expected to hand work back.** At
  120k it freezes scope, at 150k it hands back after landing the item in hand, at 200k it stops where
  it is and reports. So a report that lists unfinished items is the hook working, not an agent failing:
  give the remainder to a fresh agent with what that report learned, rather than resuming the old one.
- **The cap is on agents running at once, not on how many a session uses.** A long job split into small
  agents is the cheap way to do it. Past about ten in a session, say the count and what they went on the
  next time you report; don't stop to ask.

## The roster's tools

A tool an agent never calls is context re-sent on every turn. `mechanic`, `builder-lite`, `builder` and
`reviewer` deny the unused built-ins and keep every MCP server; `runner` carries a short allowlist.
MCP tools arrive as names only, so when a job needs specific ones, name them in the handoff and the agent
loads them in one `ToolSearch` (`select:mcp__<server>__<tool>,…`). The `agent-cost` skill's `--tools`
section shows what each agent type called. A job needing a different limit gets its own agent definition.

## Conventions

When you learn how something should be done, record it in the same response: a checkable rule as a lint
rule or a test, a repeatable procedure as a skill, a judgement call in the project's guidelines. Subagents
only report candidates; recording them is yours.

## Measuring

Use the `agent-cost` skill (this plugin's) to see where tokens go: totals, turn shape, what fills each context, and what
a subagent starts with. Compare windows with `--since`/`--until` around a change. Agent definitions and
plugin settings load when a session starts, so verify a change from a fresh session, not from the
subagents of the session that made it.
