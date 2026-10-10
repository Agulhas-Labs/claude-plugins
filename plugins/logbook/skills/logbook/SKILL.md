---
name: logbook
description: >-
  Use when the user types /logbook (including /logbook next, on, off or status), or asks
  for a logbook, a live status page, to retitle or close the one that is running, or to
  turn on, check or rewrite a next-session brief.
---

# logbook

## Start

```text
python3 "${CLAUDE_SKILL_DIR}/../../board/board.py" start "TITLE"
```

`TITLE` is a few words naming the task. It prints the path of `board.html`; give that path to the user.
If a board is already running for this session it says so; give the user its path.

## Retitle

```text
python3 "${CLAUDE_SKILL_DIR}/../../board/board.py" title "TEXT"
```

## Close

```text
python3 "${CLAUDE_SKILL_DIR}/../../board/board.py" close
```

Writes `report.html`; give the user its path. A closed board records nothing more.

## While a board is running

The hooks record the files you change and the commands you run, with how each command ended. Record a
check only for what a command's exit status does not show.

For a question whose answer changes the work, record it with your default and keep going on the
default, unless it is a hard stop. Record decisions worth a look, checks and deliverables as you make
them. A line from the user like `Q3: …` is an answer: apply it and record it with `answer Q3 "TEXT"`.

## Commands

- `question "TEXT" --default "D" --affects "A" --reverse "R"`
- `stop "TEXT"`
- `answer Q3 "TEXT"`
- `decision "TEXT" --why "W" --reverse "R"`
- `deliverable "LABEL" --path P`
- `check "WHAT IT PROVED" --command "C" --result pass|fail`

## Next-session mode

One standing brief per project for whichever session picks the work up next. Each session that
finishes work overwrites it whole; it is never appended to.

```text
python3 "${CLAUDE_SKILL_DIR}/../../board/board.py" next on|off|status|facts
```

- `/logbook next on [PATH]`: `next on [PATH]` (default `NEXT_SESSION.md`; relative to the folder that
  holds the setting: the repository's top, the main checkout's from a worktree)
- `/logbook next off`: `next off` (the brief is left as it is)
- `/logbook next status`: `next status`

`/logbook next` with no arguments means rewrite the brief now:

1. Run `next status` and `next facts` together. If status does not say the mode is on, tell the
   user what it says and stop.
2. Overwrite the path status prints, whole, from what this session knows. Don't re-read files to pad
   it out.
3. First line: it is rewritten by each session, never appended to. Then these sections, in order,
   each left out when empty: **Where things stand** (branch, commit, date written); **Read first**
   (one batch of files to request together); **Ask the user first** (open questions, carried from
   `next facts`); **The work** (ordered); **Rules that bite** (only those the next session needs);
   **Stop when**.
4. Carry an entry forward only if it still applies. Drop finished work and history. Keep it under
   about 150 lines.
5. Give the user the path.
