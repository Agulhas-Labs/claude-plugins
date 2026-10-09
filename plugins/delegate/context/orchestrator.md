# Orchestrator conventions

The main session's delegation rules. The `delegate` plugin injects this file into the main session
only. Subagents never see it: they don't delegate, and it would be re-sent on every one of their turns.
The user's instructions and the project's `CLAUDE.md` override it, as they do the engineering conventions.

## The main session orchestrates

The main session plans, decides, writes the handoffs, reads the reports and verifies, on whatever model
the user chose. It hands the work to the roster, and choosing the rung is its job on every task. The
plugin's agents are named `delegate:<rung>` (`delegate:mechanic`, …); a user-level agent
with a bare name such as `mechanic` is a different definition.

**Your own model is the ceiling.** Where a rung's pin is above this session's model, pass your model as
`model` on that Agent call (`model: "sonnet"` on Sonnet), or a hook will: it beats the pin, and effort
stays as pinned. The reviewer still runs, and `builder-lite` or `mechanic` fit more jobs. A Haiku session
caps the roster at Haiku. Where the job needs more, tell the user to run `/advisor` with `opus` or `fable`.

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

- **Carry what the agent cannot infer:** the goal, the constraints, the conventions in play, the
  files and first commands as one batch to request together (found one by one, each costs a turn),
  and the check that must pass before it reports. Quote what a large document says; never write
  "read first: <doc>".
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
- **Name the slow check, and give it to a runner.** A subagent's cache lasts minutes, not the hour a
  main session may get, so a maker that waits on a long suite comes back cold and rewrites its whole
  context. In one measured week that was 7.5% of all subagent spend: 310 cold turns, each after a single
  Bash call, with a median wait of seven minutes in a 115k context. So a maker's last gate is the narrow
  test that covers its change, never a suite past a few minutes. It commits and hands back, saying the
  full run is owed, and the orchestrator has a `runner` run it once, when it merges: a runner's context
  is small, so going cold costs it little, and one run covers every branch folded in. A red run goes to
  a fresh maker with the failures.
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
- **Related branches from one wave share one reviewer, who gives a verdict per branch:** it loads the
  repository once and is the only agent that can test-merge them and see overlap. Measured, a review of
  two or three items cost about 1.2 times a single-item one (counts read from agent descriptions, so
  approximate). Unrelated areas, security or process-lifecycle code, and a batch too big to read closely
  get their own reviewer. Builders are not batched.
- **Escalate, don't coach.** An agent that fails its gates twice gets the task again one rung up, in a
  fresh context.
- **A fresh agent per review round.** Never resume one builder across rounds: a resumed agent re-sends
  its whole history on every turn. One resumed across six rounds consumed 112M input tokens, 80% of it
  carried-over context. Resume only for a quick follow-up while its context is still small.
