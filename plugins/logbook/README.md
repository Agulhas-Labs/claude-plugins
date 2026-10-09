# Logbook

A plugin for Claude Code: a live page in your browser for a long task, so you can see what's waiting on you and what
Claude has built, checked and decided without asking. When the session ends, the page becomes a
self-contained `report.html`. It does not work in claude.ai chat or Cowork: its hooks run on your
machine, in Claude Code.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install logbook@agulhas-labs
```

Start a new session. There's nothing to configure.

## What you get

When a task runs long enough, or a turn changes a file or makes a commit, a board starts at
`<project>/.logbook/<session-id>/board.html`. On Claude Code 2.1.287 or later the band above the prompt
(see [The band](#the-band)) points at it, and its button opens the page in your browser. On an older host
nothing is printed: run `/logbook:logbook`, or ask Claude, and it gives you the path. Claude is told where the
board is and how to record on it either way.

Open the page in a browser. A board part-way through a task looks something like this:

```text
Logbook                                              Light  Dark  System
Move settings storage to SQLite
● live    Started 14:05 (40 min ago)    Updated 14:45 (9 s ago)

Needs you                                                        1 open
  Q3  Keep the old settings.json after migrating?
      Running on   keep it, read-only
      Affects      the cleanup step
      To reverse   delete settings.json
      [ Copy "Q3:" ]

Steps                                                       3 of 5 done
Built                                          2 commits, 1 deliverable
Changed                                                         6 files
Verified                                                       6 pass
Decisions                                          1 made, 2 answered
Commands                                     20 commands, 1 failed   ▸
Agents                                          0 running, 1 finished
```

| Section | What's in it |
| --- | --- |
| Needs you | Each open question, the default Claude is running on meanwhile, what it affects and how to undo it. Hard stops, such as a missing credential, go to the top. |
| Stuck | A step in progress with no activity for too long, a failed check, or a subagent that failed. |
| Steps | The task list, if the session kept one. |
| Built | Commits made by the session, and any deliverables Claude recorded. |
| Changed | Every file the session changed, newest first, with lines added and removed and how many edits. |
| Verified | Checks Claude recorded, each with what it proved and the command, then every test run and how it ended. |
| Decisions | Calls Claude made on its own, with why and how to reverse them, plus the questions you've answered. |
| Commands | Every other command, one row per distinct command, failures first. 
| Agents | Subagents, with duration, tokens, type, model and id in aligned columns, outcome and any gate lines in their report. |

Built, Changed, Verified, Commands and Agents are tiles under the header, each with its count: pick one and its rows show below, and only that one is drawn on screen (a printed report holds all of them). Needs you, Stuck, Steps and Decisions are sections of their own. Empty sections and tiles are hidden. The header shows total tokens (input, cache and output, cache reads included) for the session and its agents, and, once the task is finished, when it completed and how long it took. The page redraws itself every 10 seconds without losing your scroll
position or what you opened. Times are in your local time, and ages come from your browser's clock,
so a board that has stopped updating looks old rather than current. There's a light, dark and system
switch in the header, and the report has it too.

### "exited 0" is not "tests passed"

The hooks only see how a command ended, so the page says "exited 0" or "failed", never "the tests
passed". A pipeline ends the way its last command does: `run-tests | tail -3` exits 0 whatever the tests
did. When that matters, Claude records a check in its own words, and those come first under Verified.

## The band

On Claude Code 2.1.287 or later a row above the prompt tells you the board exists and what needs you. For
example, `Logbook  Stopped · 1 question · 3/5 · 6 pass · 1 fail   Logbook`. It names only what needs your eyes
and leaves out a part that is zero: open questions (a hard stop reads `Stopped`, in red), stuck or failed items,
steps done of steps planned, and checks passed and failed. Files changed, commands run and agents are only on
the page. The `Logbook` button opens the board's page in your default browser, the same page as before.
A new open question or hard stop also shows as a one-line toast, so it is not missed.

The row is drawn from the board's `state.js`, which the hooks keep; the mod runs `board/mod_state.py` to find
the board, at session start and when a turn completes, and otherwise re-reads `state.js` after a tool call
that can change it and every 30 seconds while a turn runs. Drawing starts no process. On the first file
change or commit of a session that has no board yet, the mod starts it from the transcript, so the row is up
from the first change instead of at the tenth tool call; the hooks' thresholds are unchanged.

The hooks and the browser page work as before everywhere, older hosts included, and the browser page remains
the end-of-session `report.html`.

## Using it

A board appears on its own. Every turn that changed a file or made a commit ends with one. A board can
also start earlier, so you can watch it while a long turn runs: at the session's 10th file change or
shell command (shell commands count whether or not they changed anything), when the task list reaches 5
items, or when the first subagent starts. To start one yourself, run
`/logbook:logbook`. You can also ask Claude to retitle or close the board.

To answer a question, click its copy button, paste `Q3: ` into the chat, type your answer after it and
send. Claude applies the answer and the page shows it as answered on the next refresh. The page itself
never writes anything back.

Each session gets its own folder:

```text
<project>/.logbook/
  index.html              every board, newest first
  <session-id>/
    board.html            the live page
    report.html           written when the session ends; self-contained
```

The folder carries its own ignore file, so it stays out of `git status` and your `.gitignore` is never
touched. If you resume a session, its board opens again and `report.html` is rewritten when it ends. A
board you closed with the close command stays closed unless the session is resumed.

## Next-session mode

A project can keep one standing brief for whichever session picks the work up next. Turn it on with
`/logbook:logbook next on` (the brief is `NEXT_SESSION.md` in the project unless you name another path
inside it), check it with `next status`, and turn it off with `next off`, which leaves the file alone.

While it's on, each session start tells Claude where the brief is, when it was written and how long it
is, never what it says: Claude reads it only when you ask it to pick up or carry on the work. When the
work is done, or you run `/logbook:logbook next`, Claude rewrites the brief whole for the session after:
finished work and history are dropped, open questions from the board are carried forward, and it is never
appended to. The setting lives in `.logbook/`, so it stays out of `git status`; the brief is an ordinary
file you can commit or ignore.

## How it works

Hooks record file changes, commands and how each ended, the task list, subagents starting and stopping,
and the end of each turn and session. They spend no model tokens. Anything changed or run before the
board started is read back from the session's transcript, so the page covers the whole session.

Commits come from git each time the page is drawn. You see the ones made since the board started, and
earlier ones this session made, which it recognises by the hash its own `git commit` printed. An
earlier commit made with `--quiet` printed no hash, so it won't appear. Another session's commits never
appear.

Claude records only what the hooks can't see: questions with their defaults, decisions, deliverables,
checks, hard stops and the title. Each is one short shell call; the `/logbook:logbook`
skill lists the commands.

A command counts as a test run when it runs a common test runner (`pytest`, `swift test`, `npm test`,
`go test`, `cargo test` and the like), including after `cd there &&`, behind `uv run`, or passed to a
wrapper after `--`. Naming a runner as an argument doesn't count, so `pip install pytest` isn't a test
run. The command is matched as text rather than parsed, so a runner inside a quoted string after a `;`
or `|` counts too. Add your own runners with `LOGBOOK_TESTS`: a command containing any of its
items is a test run.

## Settings

All optional. Set them under `env` in `~/.claude/settings.json` or a repository's
`.claude/settings.json`. An invalid value falls back to the default.

| Variable | Default | What it does |
| --- | --- | --- |
| `LOGBOOK_CALLS` | `10` | File changes and shell commands after which a board starts mid-turn. |
| `LOGBOOK_STEPS` | `5` | Task-list items that start a board. |
| `LOGBOOK_TESTS` | none | Comma-separated text, matched literally and case-sensitively (not a regular expression): a command containing any item is a test run, beside the built-in runners. |
| `LOGBOOK_THEME` | `system` | The mode a board opens in (`system`, `light`, `dark`). Your choice in the page overrides it. |
| `LOGBOOK_ACCENT` | none | An accent colour, as `#rgb` or `#rrggbb`. |
| `LOGBOOK_DENSITY` | `comfortable` | Row spacing (`comfortable`, `compact`). |
| `LOGBOOK_STUCK_MINUTES` | `10` | How long without an event before a step in progress is shown as stuck. |
| `LOGBOOK_RETENTION_DAYS` | `14` | Boards older than this are removed when a session starts. |
| `LOGBOOK_ALLOW_ANY_PATH` | none | Set to `1` to record in a project (or keep the plugin's data) outside your home folder and the temporary folder. Without it, such a project gets no board. |

## Cost

Until a board starts, nothing, with two exceptions: on an older host whose task list is `TodoWrite`
rather than `TaskCreate`/`TaskUpdate`, each `TodoWrite` starts Python to count the list, and once a
project has had a board, each session start runs Python to prune old ones.

Once a board is running, Claude gets one short message saying how to record to it, and each entry it
records is one short shell call. Every file change and shell command runs the recorder, in the main
session and in subagents.

Measured with `tests/latency.py` on a laptop at rest: 8 ms at the median for a command with no board
active, 56 ms for a command that's recorded and rendered, and 91 ms for the end of the turn that starts
a board, which happens once a session. With three browsers being installed and run beside it, the same
measurements took two to three times as long.

## Permissions

The recording command is `python3 <plugin folder>/board/board.py …`. If your permission mode asks
before shell commands, Claude Code will ask the first time. Choose the option that stops it asking
again for that command, or the task will wait on you for every entry.

## Requirements and limits

- Python 3 (standard library only) and a POSIX shell. Without Python the hooks do nothing. On Windows
  the hooks need Git Bash, and Windows is untested.
- Boards contain your prompts and commit subjects, so they stay on your machine. Nothing is uploaded or
  hosted, and the page loads nothing from the network.
- The page was tested from a `file://` URL in the WebKit, Gecko and Chromium engines through
  Playwright, and in an installed Chromium-family browser. Safari itself wasn't tested and may differ
  in what it lets a local page store or copy. If a browser won't let a local file store your colour
  mode, the switch still works until you close the page.
- Pruning removes only what the plugin made: boards carrying its marker file, by exact path, and
  half-made folders holding nothing but its own files.
- A `.logbook` that is a symbolic link is never used.
- Not in this version: a board for a subagent's own task list (a subagent is one row under Agents), or
  publishing the report anywhere.
