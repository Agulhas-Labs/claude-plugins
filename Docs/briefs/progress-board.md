# Brief: progress-board

A plugin that keeps a live local page for a long Claude Code task: what it is doing, what it has built
and verified, what it decided without you, and what is waiting on you. When the task ends, the same
page is the report.

Status: released as the `logbook` plugin; this brief keeps the name it was written with,
`progress-board`. Sections 3 to 7 were amended as the build and its two review rounds went, and section
11 records what was settled and how. Amended again after first use: a session that uses neither the task
list nor subagents got no board and nothing on it, so a board now starts from the work itself and
records it (§3, §4, §6).

## 1. The problem

A long task either stops to ask you things or guesses and keeps going. When it stops, it sits idle
until you come back. When it guesses, the guesses are buried somewhere in the scroll. At the end you
get a wall of text, and you have to dig through it to find three things:

- **What was built:** the commits, the files, the deliverables.
- **What was proved:** which checks ran and whether they passed.
- **What was decided for you:** each question the task met, the default it took, and how to reverse it.

People who have asked for an end-of-task page of open questions found it valuable. It is more
valuable if it also shows what was built, and if you can watch it while the task runs, not only at
the end.

## 2. What it is not

The obvious version is a subagent that designs and rewrites a dashboard after every step. That costs
far more than the page is worth. Every subagent pays a fixed start before it does anything. In one
measurement that was about 55k tokens, so a twenty-step task would spend over a million tokens
repainting HTML. It also lets the layout drift from one task to the next, which makes the page harder
to scan.

So:

- **No model draws the page.** A script renders it from a state file, and design happens once, in a
  template.
- **No model records what a hook can see.** Task-list changes, subagents starting and stopping,
  commits and the clock are all recorded without a model.
- **The page never writes back.** You answer questions in the chat. The page helps by copying the
  answer for you.
- **It is not hosted.** It is a local file that opens with a double-click. Publishing it somewhere is
  a later option (§9), not the design.

## 3. Who writes what

| Source | Records | Model tokens |
| --- | --- | --- |
| `SessionStart` hook | Prunes old boards (§7). After a compaction or a resume, tells the model again about a board that is still running (§6) | None, unless a board is running |
| `PostToolUse` hook, matched to the task-list tools and the agent tool | Steps and their statuses. An agent's model and description, which `SubagentStart` does not carry | None |
| `PostToolUse` and `PostToolUseFailure` hooks, matched to the tools that change a file and to the shell tool | Each file changed, with the lines added and removed. Each command run, with its description, how long it took and how it ended | None |
| `UserPromptSubmit` hook | Marks an idle board live again | None |
| `SubagentStart` / `SubagentStop` hooks | Agents running: type, model, start time, duration, outcome. Gate lines from a report written in the `orchestration` shape | None |
| Render-time `git log` | Commits on any branch since the board started, including worktree branches, and the commits the session made before that, by the hash its own `git commit` printed | None |
| `Stop` / `SessionEnd` hooks | Re-render. At session end, freeze the page as the final report | None |
| The `board` script, called by the model | Questions with their defaults, decisions worth reviewing, deliverables, hard stops, the task title | One Bash call per entry, batched with other calls in the same turn |

Everything the model writes is something only the model knows. Everything else is recorded for free.

None of the recorded sources depends on another. A session with no task list has no Steps section
and loses nothing else: what it changed, ran and committed is on the page all the same.

## 4. The page

It uses one fixed template. A section with nothing in it is hidden. The order puts what needs you
first.

1. **Header:** the task title (the first prompt until the model sets one), the start time, the time of
   the last update, and the state: live, idle or finished. Times are recorded in UTC. The page shows
   them in your local time and computes ages ("4 min ago") from your browser's clock, so a page that
   has stopped updating shows its age honestly.
2. **Needs you:** each open question has a short id (`Q3`), the default the task is running on, what
   that default affects, and how to reverse it. A copy button puts `Q3: <your answer>` on the
   clipboard for pasting into the chat. Hard stops, such as a missing credential, sit at the top in a
   different style, because the task is actually blocked on them.
3. **Stuck:** a step in progress with no event for longer than a threshold, a failed check, a subagent
   that ended in failure. This is computed in the page from the live clock, not recorded. Past the
   same threshold the header says so too, and says only what is known: no event for that long, and
   the task may be busy, waiting on something, or over. The recorder hears of a few events only, so
   a shorter silence is ordinary work.
4. **Steps:** the task list with statuses. Shown only when the session kept one.
5. **Built:** commits (short hash, subject, branch) and deliverables (paths or links), each under the
   step it belongs to where that is known.
6. **Changed:** each file the session changed, most recent first: whether it is new, the lines added
   and removed, how many times it was changed, and when.
7. **Verified:** each check: what it proved, the command, pass or fail, and when it ran. After the
   checks, each command that was a test run, with how it ended. The page says only what is known:
   a command "exited 0" or "failed", because the hook sees the exit status and nothing else, and a
   pipeline's status is its last command's. A failed test run is shown under Stuck until a later
   test run passes.
8. **Decisions:** calls the task made on its own that are worth a look but don't need an answer.
9. **Commands:** every other command, failures first, one row per distinct command with how often it
   ran. It starts collapsed: it is the long tail, and the sections above it are the summary.
10. **Agents:** subagents running now and finished, with duration. Shown only when there were any.

The page has three modes: light, dark, and system, which follows the viewer's setting. A switch in
the header changes the mode. The choice is kept in the browser's own storage, so it survives the
refresh and a reload, and the page never writes it back to the board. Where the browser refuses
storage to a local file, the switch still works until the page is closed. The mode a board opens in
is a plugin setting, `PROGRESS_BOARD_THEME` (`system` by default), beside an accent colour
(`PROGRESS_BOARD_ACCENT`) and a density, comfortable or compact (`PROGRESS_BOARD_DENSITY`). They are
read from the environment the way the `orchestration` plugin reads its own, and anything that is not
a valid value means the default. The final report carries the same switch.

## 5. Files and refresh

A browser won't `fetch()` a local file from a page opened by double-click, and a meta refresh reloads
the whole page and loses your scroll position. So a live board is two files:

- `board.html`: a static shell, written once. It contains the template, the CSS and the rendering
  script.
- `state.js`: sets `window.BOARD = {…}`. The shell re-inserts this script tag every 10 seconds and
  re-renders in place, keeping your scroll position and any section you have collapsed.

Every write goes to a temporary file and is then renamed, so the page never reads a half-written
state. A board is made the same way: built whole in a folder beside its place, with its first
events in it, and renamed into place, so a hook never finds a board that is half made, and of two
hooks that start one, one wins and the other says nothing. When two hooks render at once, each
looks at the log again just before it replaces the state, and one that finds the log has grown
since it read it does not write: it renders again, up to three times, and then leaves the page to
the hook that recorded the newer event, which renders after recording. So an older state is never
the one left on the page. A render also puts its board back in the index when the index does not
list it, as after a start that was killed between publishing the board and writing the index. When the session ends, the renderer writes `report.html`: one self-contained file with the
state inlined and no refresh. That is the file to keep or send.

Nothing on the page loads from the network.

### The state

`state.js` is one assignment, `window.BOARD = {…};`, and the object is the whole contract between the
recorder and the page. `tests/fixtures/state-live.json` is the reference: every key the page reads is
in it, and the renderer's tests hold its output to the same keys.

- `schema`, `session`, `title`, `titleSource` (`prompt` or `model`), `started`, `updated`.
- `state`: `live`, `idle` (the turn ended and the task is waiting for you) or `finished`. How stale
  the page is comes from `updated` and the browser's clock, never from this field.
- `settings`: `theme`, `accent`, `density`, `refreshSeconds`, `stuckAfterSeconds`.
- `questions`, `steps`, `commits`, `deliverables`, `checks`, `decisions`, `agents`: lists, empty when
  there is nothing to show. A hard stop is a question with `hardStop` set and no default.
- `changes`: one row per file changed, in order of first change, with its path (relative to the
  project when inside it), the number of edits, the lines added and removed where known, whether the
  session created it, and the times of its first and last change.
- `commands`: one row per distinct command, in order of first run, holding the latest run's result
  (`pass`, `fail` or `background`), exit code, description, time and duration, the number of runs and
  of failures, and whether it is a test run. At most the 200 most recently run are kept;
  `commandsTotal` counts every run.

All times are UTC, written as `2026-01-05T09:00:00Z`.

The template holds one marker line, `<!-- progress-board:state -->`. For `board.html` the renderer
removes it and the page loads `state.js` from beside itself. For `report.html` the renderer puts the
state there inline, with `window.BOARD_FROZEN = true`, and the page neither refreshes nor looks for
`state.js`.

## 6. When a board starts

A rule in the model's context can't reliably tell whether a task has "more than five steps" or will
"take longer than thirty minutes". So the start is decided by hooks, and the model only confirms the
title:

- A turn ends in which the session changed a file or made a commit, or
- the session makes its tenth file change or shell command (a setting, `PROGRESS_BOARD_CALLS`), or
- the task list reaches a threshold number of items (a setting, default 5), or
- the first subagent starts, or
- you ask for one (`/progress-board:progress-board`: a plugin's skill carries the plugin's name).

The first is the rule that decides whether there is a report: every turn that changed something
ends with a board, however little it took, and the terminal shows its path where the turn ends. A
turn that changed nothing, a question answered or a status looked up, creates nothing. The others
decide only how early the page can be watched. Ten is not a measure of anything: it is where a long
turn gets its live page, early enough to be worth opening and late enough that a quick fix is over
before it. Reading, searching and answering are never counted.

At the end of every later turn that recorded anything, the terminal shows the path again. A turn
that recorded nothing says nothing.

Until a board starts, the hooks exit at once, so a short session pays nothing. That exit is a shell
script's, not Python's. Measured on an Apple silicon laptop: starting the shared launcher and an
empty Python script took 83 ms, a bare `python3` 34 ms, and `sh` 8 ms, so a hook that reaches Python
cannot meet the 50 ms in §10. A gate script reads the payload, and starts Python only when the
session has a board or the event is one that starts it. It needs no state of its own to count
towards the threshold: a task's id is its position in the list, so the fifth `TaskCreate` says so
itself, and the earlier steps are read back from the transcript when the board starts.

A file change or a command carries no position, so that count is kept: one byte appended per call to
a file named for the session in the folder the host gives the plugin for its own data
(`CLAUDE_PLUGIN_DATA`), and the count is the file's length. The byte says what the call was: `c` for
one that changed a file or made a commit, `x` for any other. So at the end of a turn the gate knows,
still with the shell alone, whether there is anything to report, and starts Python only if there is. The gate appends and reads it with the
shell alone. It is outside the project, so a short session still creates nothing there; it is
removed when the board starts and when the session ends, and one left by a session that was killed
is removed with the old boards. A host that gives the plugin no data folder has no such count, and
there a board starts in the other three ways only. The calls made before the board started are read
back from the transcript, as the steps are, each with the time it ran.

That read can come too early. Observed in a fresh session: a model created six tasks in one turn, and
when the hook for the fifth ran, the host had not yet written the first four calls to the transcript.
So the read is repeated on the first five events after a board starts. Every step event carries
the id of the call it came from, a call the log does not have is added, and the state applies the
calls recovered this way before the ones a hook recorded itself: they happened before the board
existed, and everything a hook recorded happened after.

No rule is added to a session when it starts. The first design added one, of about 150 tokens, to
every session, including the ones that never start a board. Instead the hook that starts a board
tells the model so, with the path and the commands, and `SessionStart` says it again after a
compaction or a resume, when the first telling has gone from the context. A session without a board
is told nothing. What the model is told:

> For a question whose answer changes the work, record it with your default and keep going on the
> default, unless it is a hard stop. Record decisions worth a look, checks and deliverables as you
> make them. A line from the user like `Q3: …` is an answer: apply it and record it.

The commands are shell calls, so a permission mode that asks before shell commands asks before the
first one. The plugin does not approve its own commands: that is the user's decision to make, once,
at the prompt.

This matches the engineering conventions the `orchestration` plugin already injects: in an autonomous
run, make the most defensible call, keep it easy to reverse, and flag it. The board is where the
flags go.

## 7. Where boards live

A board lives at `<project>/.progress-board/<session-id>/`. That folder holds its own `.gitignore`
containing `*`, so the plugin never edits the repository's ignore file. There is one folder per
session, because parallel sessions in the same checkout are common. `index.html` at the top lists the
boards, newest first.

A boards folder that is a symbolic link is refused, and nothing is written: a repository can
commit one, and it would point the plugin at files that are not its own.

A session's end closes its board and writes the report. Resuming that session reopens the board,
and the report is written again when the session next ends.

The path is shown once, when the board starts, so you can open it. `SessionStart` removes boards
older than a retention setting (default 14 days). It removes only folders that carry the plugin's own
marker file, by exact path. The one exception is a folder a start was building when it was killed,
before it wrote the marker: that one is removed once it is an hour old, and only if it holds nothing
but the plugin's own files. Boards contain prompts and commit subjects, so they stay on the machine.

## 8. Layout in this repository

```
plugins/progress-board/
  .claude-plugin/plugin.json
  hooks/hooks.json            SessionStart, PostToolUse (task-list, agent, file-changing and shell tools),
                              PostToolUseFailure (file-changing and shell tools), SubagentStart/Stop,
                              UserPromptSubmit, Stop, SessionEnd
  hooks/gate.sh               the shell fast path in front of every hook
  hooks/board_hook.py         payloads in, events out
  hooks/run-python.sh         the same launcher the other plugins use
  board/board.py              event log in, state.js / report.html out; also the model's CLI
  board/template.html         the shell: layout, CSS, render script
  skills/progress-board/      /progress-board: start, retitle, close
  tests/                      renderer and hook tests on fixture payloads and event logs; and three
                              scripts a person runs: latency.py, browser_proof.mjs, engines_proof.mjs
  README.md
```

It uses only the Python standard library and has no build step. `scripts/check.sh` gains the new test
directory, and the marketplace gains an entry.

## 9. Later, not now

- Publish `report.html` somewhere shareable at the end, where the host offers a place for it.
- Seed a `cache-guard` handoff from the board state, since the board already holds most of what a
  handoff needs.
- A digest across sessions: every board in a project over a week.

## 10. Acceptance

1. **Cost.** The hooks spend no model tokens. Over a real 15-step task with subagents, the extra model
   spend, measured with `agent-cost` against a comparable session without the plugin, is the rule
   plus the `board` calls, and nothing more.
2. **Latency.** Every hook exits within 50 ms when no board is active, and within 200 ms when it
   renders. Measured.
3. **A cold page is honest.** Kill a session mid-task. The open page shows the age of its last update,
   and flags the in-progress step as stuck once the threshold passes.
4. **File, not server.** `board.html` refreshes from `file://` in Safari, Chrome and Firefox without
   losing scroll position. `report.html` opens anywhere and makes no network request. Proven in the
   three engines (WebKit, Gecko, Chromium) by `tests/engines_proof.mjs` and in a Chromium-family
   browser as installed by `tests/browser_proof.mjs`. Safari as installed accepts a driver only once
   its owner allows remote automation, so it was not driven.
5. **Answering works end to end.** A question recorded mid-task is answered by pasting its copied line
   into the chat. The model applies it, and the page shows it answered within one refresh.
6. **Starts without being asked.** In a fresh session, the end of a turn that changed something, a
   tenth file change or command, a task-list threshold or a first subagent starts a board, and the
   path is shown. A session with no task list and no subagent gets a board that shows what it
   changed, ran and committed, whether it took three calls or three hundred. A session that changed
   nothing creates no folder.
7. **Hygiene.** No board escapes into `git status`. Pruning removes only marked folders past
   retention, by exact path.
8. **Proven from a fresh session,** loaded as a scratch copy under another name, as `CLAUDE.md`
   requires for hooks. Where a hook shows the user anything (the path at start), use the
   `prove-hook-output` skill.
9. `scripts/check.sh` passes.

## 11. Settled before building

Each of these was an open question. The payloads were read off a real session on Claude Code
2.1.283, by a plugin that wrote every hook's stdin to a file; they are in `tests/fixtures/payloads/`.

- **Task-list tool names and payloads.** `TaskCreate` and `TaskUpdate`. `TaskCreate` receives
  `tool_input.subject` and answers `tool_response.task.id`; `TaskUpdate` receives `tool_input.taskId`
  and `tool_input.status`. The older `TodoWrite` was not observed. It is matched too, since the
  plugin is installed on versions this one was not measured on, and its handling is written from its
  documented input.
- **The `SubagentStop` payload** carries both `last_assistant_message` and `agent_transcript_path`.
  The hook reads gate lines from the message and never opens the transcript. `SubagentStart` carries
  the agent's id and type only; the model and the description come from the agent tool's
  `PostToolUse`, which is why that tool is matched. When that call's hook runs before the board
  exists, the launch is read back from the session's transcript, as the early steps are.
- **File changes and commands.** Read off a second real session on the same version. A `Write` or an
  `Edit` answers with `tool_response.structuredPatch`, whose lines give the counts, and a `Write` that
  made the file answers `type: create`. A command that exits 0 arrives as `PostToolUse` with its
  output and no exit code; one that does not arrives as `PostToolUseFailure`, with no response and
  `error` beginning `Exit code 3`. An `Edit` refused before it ran (its text was not found) fires
  neither hook, which is right: nothing changed.
- **A commit made before a late start.** A board that starts at the tenth call has the first nine on
  it, read from the transcript. A commit made among them is behind the heads the board recorded when
  it started, so comparing branches never finds it, and comparing dates would bring in the commits
  of parallel sessions. The session's own `git commit` printed the hash, so that is what is used: a
  command read back from the transcript that names git and printed git's line for a new commit
  (`[main c286754] Subject`) records the hash, whether the command then passed or failed (a push
  refused after the commit), and so does the call that starts the board. The page
  shows such a commit only while git confirms it is on a local branch, with the subject and time
  git gives, and asks about the latest ten at most.
- **Commits from parallel sessions.** The board shows commits made since it started on the branch it
  started on, on branches created after it started, and on the branch checked out when it renders.
  Other sessions' older branches are left out. A commit the board has shown stays shown, so work
  merged and its branch deleted is still in the report. If git could not be read when the board
  started, no commits are shown rather than the whole history.
- **Where the path is shown.** The hook that starts the board returns the path twice: as
  `systemMessage`, which the terminal shows you, and as `additionalContext`, which gives the model
  the path and the exact command to record with. That it is rendered is proven in a real terminal
  with the `prove-hook-output` skill.
- **A task's steps inside a subagent.** Not in v1: a task-list call that carries an `agent_id` is
  ignored, and the subagent is one row under Agents.
- **How the model's command finds its board.** The command the hook hands the model names the board's
  folder. Without it, the script looks for the session's folder from `CLAUDE_CODE_SESSION_ID`, which
  the shell of a tool call carries.
