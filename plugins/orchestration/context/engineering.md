# Engineering conventions

Working rules for every agent in a session: the main session and every subagent. The `orchestration`
plugin injects this file when a session or a subagent starts. It is re-sent on every turn of every
agent, so it holds only what every agent needs. The main session's delegation rules are in the
orchestrator conventions, which subagents never see.

Where instructions conflict, the first of these wins: the handoff (in the main session, the user's
instruction), the agent's own prompt, the project's `CLAUDE.md`, then this file. So a subagent whose
prompt says to stop on an ambiguity stops, even where this file says to make the call, and a reviewer
never edits, even in a project whose `CLAUDE.md` says to fix what you find.

## Verification

- "Done" means a check passed. Never report work complete on your own reading of the diff. If the
  project's test command has not run green, the work is not done. Its `CLAUDE.md` usually names it;
  otherwise find and run the repository's documented one, and if there is none, say so in the report.
- Review runs in a fresh context that has seen only the spec and the diff, never the plan or the
  reasoning that produced it. The maker was confident; that is not evidence.
- Never edit, weaken, skip or delete a test to make a suite pass. Updating a test because the specified
  behaviour changed is legitimate work; changing one to get to green is not. If a test is wrong and no
  spec change explains it, say so and stop.
- The same holds for the checks themselves: never bypass a hook (`--no-verify`) or weaken, suppress or
  disable a lint rule to get through. If a hook blocks on existing violations, bring the tree into
  compliance instead.
- A fix for a non-trivial correctness problem ships with a test that fails without it. Show that it
  fails in as few commands as possible: set the fix aside as a patch file in your scratch directory,
  diffed against the branch you started from so it still works once the fix is committed
  (`git diff <base> -- <the fix's paths, not the test's> > <scratch>/x.patch && git apply -R
  <scratch>/x.patch`; `git add -N` new files first), run the test, restore with
  `git apply <scratch>/x.patch`. A test that passes either way pins nothing. Set aside the fix's **own**
  lines: breaking a neighbour, even one in the same file, proves nothing about the fix. In a compiled
  project, read the build line before believing any verdict — a set-aside that fails to compile can
  leave the previous build's products in place and be reported green — and rebuild after the last
  restore, so the next run is not still running the sabotaged products. A run in which zero tests
  executed is not a pass.

## Scope

- Do what the task requires: no features, refactors or abstractions beyond it, and no error handling
  for cases that cannot happen. Validate at system boundaries only.
- Note anything worth fixing outside the task; don't fix it.
- Never invent a secret, an endpoint or a convention. When a requirement is ambiguous and the user is
  present, ask. In an autonomous run, make the most defensible call, keep it easy to reverse, and flag
  it in the report. A missing credential is the one hard stop.

## Working economy

Every tool round trip re-sends the whole context, so an agent's cost grows with turns × context size.
In one measured week, 70% of subagent turns carried a single tool call, and those turns were two thirds
of all subagent spend: each one re-sent the whole context to do one small thing.

- Request independent calls together in one response: the reads, searches and checks a step needs.
- Don't re-read what is already in your context. Read a large file by the range that matters.
- A subagent's prompt cache lasted five minutes where this was measured. Wait longer than that on one
  command and your next turn rewrites your whole context: 30 of 34 such turns in one week followed a
  single Bash call, after a median wait of nine minutes in a 178k context. So run a suite or build
  that takes that long once, when the work is ready for it, and the narrowest test that covers the
  change between edits.
- Run a build or suite in the foreground, with a timeout long enough for it. Background a command only
  when you have other work to do meanwhile, never to wait on it, and never end your turn while one you
  started is still running: a subagent that does wakes the main session with nothing to report. In one
  measured day that was about 20 main-session turns that could do nothing.
- Your context has three budget marks, and a hook tells you as you pass each. They ask for different
  things, so read which one arrived. At 120k, freeze scope: start no new deliverable, and put
  everything into landing what is already open. At 150k, hand back: finish the item in hand, commit it
  once verified, and end with the remaining items listed. At 200k, stop where you are: commit only what
  is already verified and report, even mid-item, listing everything unfinished. Knowing the later marks
  are coming is the point of the earlier ones — don't take on work you cannot land before the next.

## Shared machine

Other sessions and agents run beside you, and they follow the same naming habits.

- Stop only processes you started, by pid. Never `pkill`/`killall` a pattern.
- Delete only what you created, by exact path or identifier: files, temp directories, simulators,
  containers, worktrees. Never an `rm` glob outside your own scratch directory. Delete every disposable
  resource you create before you report, and list any you had to leave.
- Never use the stash to set work aside: every worktree of a repository shares one stash stack. Use the
  patch file above.
- Build scratch goes in a gitignored directory inside your worktree, where it is reused and dies with
  the worktree, never a fresh temp directory per run.

## Reporting

- Before reporting progress, audit each claim against a tool result from this session. Report only what
  you can point to evidence for. If tests fail, say so with the output; if a step was skipped, say so.
- Commit each verified, self-contained unit on a feature branch with a clear message; if you are on the
  default branch, branch off it first. Never merge or push to the default branch without an explicit
  instruction.
- A subagent's report is read whole by the main session and re-sent on each of its later turns. Give it
  four parts: the commits, each gate's result in one line, the decisions that need review, and what
  remains with what you learned about it. No account of what you tried. Where reports were measured
  they ran 1.5k to 3k tokens each; aim for a quarter of that.
- A subagent that learns how something should be done names it in its report as a candidate
  convention; it doesn't write the rule, skill or guideline itself.
