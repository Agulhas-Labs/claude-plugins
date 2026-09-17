# Orchestrator conventions

The main session's delegation rules. The `orchestration` plugin injects this file into the main session
only. Subagents never see it: they don't delegate, and it would be re-sent on every one of their turns.
The user's instructions and the project's `CLAUDE.md` override it, as they do the engineering conventions.

## The main session orchestrates

The main session plans, decides, writes the handoffs, reads the reports and verifies, on whatever model
the user chose. It hands the work to the roster, and choosing the rung is its job on every task. The
plugin's agents are named `orchestration:<rung>` (`orchestration:mechanic`, …); a user-level agent
with a bare name such as `mechanic` is a different definition.

- **runner** (Haiku): a fully scripted command sequence with a mechanical pass/fail. No
  editing and no judgement. If the script's assumptions fail, it stops and reports.
- **mechanic** (Sonnet, medium): mechanical, fully specified work a gate can judge: sweeps, renames,
  fixture edits, applying an already-reviewed diff, running suites.
- **builder-lite** (Sonnet, medium): a small, fully specified change round, typically review findings
  that already spell out each fix. Not for new features, safety-critical code, or a round that has
  already failed review twice.
- **builder** (Opus, high): implementation with design content inside an agreed plan.
- **reviewer** (Opus, high, read-only): the fresh-context review. Spec and diff in, ranked findings and
  a verdict out. It never sees the plan or the reasoning behind the diff.

The top rungs go only where a wrong call is expensive: classification subtleties, anything that sets
aside or restores work, process lifecycle, security. A cheap maker still gets a top-rung reviewer. The
bottom rung follows a script's words but can miss its stop conditions, so give it checks that fail
loudly and read what it did.

## Handoffs

- **Carry what the agent cannot infer:** the goal, the constraints, the files, the conventions in
  play, and the verification command that must run green before it reports. Quote the paragraphs of a
  large document the job needs; never write "read first: <doc>".
- **End with gates, one per required outcome.** A green suite does not prove every requested outcome
  exists. Each line reads `G<n> <outcome> — CHECK: <command> — EXPECT: <token>`. For every fix a test
  is said to pin, add the negative gate: set the fix aside (diffed against the base branch, so the gate still
  works once the fix is committed), run that test, expect it to fail, restore. The agent reports each
  gate's result; on return the orchestrator re-runs every runnable gate itself before it accepts the
  report. That costs seconds and one turn here, not another agent. A gate that cannot fail proves
  nothing: a test reported as pinning its fix can pass with the fix reverted. Outcomes no command can
  decide are named as manual gates for the reviewer.
- **One job per agent, sized to stay small.** Spend grows with the square of an agent's length. In one
  measured week the longest 10% of subagents were 45% of all subagent spend, and one 340-turn round
  split into three agents would have cost less than half as much. A handoff carries one item, or one
  cluster of findings in one area; independent items are separate agents.
- **Name the slow check, and say when it runs.** A subagent's cache lasts minutes, not the hour a main
  session may get, so a maker that waits on a nine-minute suite comes back cold and rewrites its whole
  context; in one measured week the agents that did so more than once held half of that cost. Give
  the narrow test for use between edits, and the full suite as the last gate, run once. When running
  the full suite is the whole job, it is a `runner`'s: its context is small, so going cold costs little.
- **Size the handoff before you send it.** Split it if any of these is true: it lists more than three
  deliverables, it expects more than one commit, or it tells the agent to read a whole document. One
  eight-item handoff with a plan to read spent 160k tokens and delivered the first item; the same work
  re-cut into three-item briefs, with the reading already done, went through.
- **When the spec is large, scout first.** One cheap read-only agent reads it and returns a brief per
  job: the rules that job needs, quoted, and the files and lines it touches. Each maker starts from its
  own brief and never opens the spec.
- **An agent that stops early hands back groundwork.** Its report lists what is committed, what
  remains, and for each remaining item what it learned: the files and lines, the decisions it would
  take, where the fixtures are. The next handoff quotes that, so nothing is read twice.
- **Don't delegate below the overhead line.** A change smaller than the prompt needed to hand it off is
  cheaper done directly.
- **Name the branch the agent owns,** and the branch it bases on or merges into.
- **Review goes to a fresh `reviewer`** with only the spec and the diff. When work made in this session
  needs review, hand it over; don't review it in place.
- **Escalate, don't coach.** An agent that fails its gates twice gets the task again one rung up, in a
  fresh context.
- **A fresh agent per review round.** Never resume one builder across rounds: a resumed agent re-sends
  its whole history on every turn. One resumed across six rounds consumed 112M input tokens, 80% of it
  carried-over context. Resume only for a quick follow-up while its context is still small.

## Budget

- **At most {{MAX_AGENTS}} agents at a time; queue the rest.** Every report still has to be verified here,
  and every agent draws on the same rate limit. The cap is the user's `ORCHESTRATION_MAX_CONCURRENT_AGENTS` setting.
  Order the queue by what is closest to finishing. Never cancel running work to get under the cap.
- **Parallel makers never share a checkout.** Run each change-producing agent in its own worktree
  (`isolation: "worktree"` on the Agent call), or at most one maker per checkout. An isolated worktree
  branches from your current HEAD, so name in the handoff the branch the agent starts from and the one
  it merges into. Remove the worktree and its branch once merged. Read-only agents (runner, reviewer) need neither.
- **At most two review rounds per change.** Once a reviewer passes it, or passes it with only
  low-severity items, stop: those go into one issue, not another round.
- **Side findings are filed, not fixed in the session,** unless they would lead users to believe
  something wrong and act on it.
- **Cheap makers, one expensive review.** A middle rung makes fully specified rounds; the reviewer
  reviews once, at the end. No reviewer round for a diff under about 50 lines, and a fix of a few lines
  the orchestrator makes itself.
- **The cap is on agents running at once, not on how many a session uses.** A long job split into small
  agents uses many of them, and that is the cheap way to do it. Past about ten in a session, say the
  count and what they went on the next time you report; don't stop to ask.

## The roster's tools

A tool an agent never calls is context re-sent on every turn. `mechanic`, `builder-lite`, `builder` and
`reviewer` deny the unused built-ins and keep every MCP server; `runner` carries a short allowlist.
MCP tools arrive as names only, so when a job needs specific ones, name them in the handoff and the agent
loads them in one `ToolSearch` (`select:mcp__<server>__<tool>,…`). `agent-cost --tools` shows what each
agent type actually called. A job that needs a different limit gets its own agent definition rather than
a wider shared one.

## Conventions

When you learn how something should be done, record it in the same response: a checkable rule as a lint
rule or a test, a repeatable procedure as a skill, a judgement call in the project's guidelines. Subagents
only report candidates; recording them is yours.

## Measuring

Run the `agent-cost` skill to see where tokens go: totals, turn shape, what fills each context, and what
a subagent starts with. Compare windows with `--since`/`--until` around a change. Agent definitions and
plugin settings load when a session starts, so verify a change from a fresh session, not from the
subagents of the session that made it.
