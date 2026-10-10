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

When the work is done, before your last report:

1. `revise` any decision written in shorthand (see below), so each one reads on its own.
2. `summary "TEXT" --facts "F"`: TEXT is what exists now, in a few plain sentences, and whether
   anything on the page needs an answer to keep going. F is the facts line: branch and commit, test
   counts as the runner printed them, and any issues you filed, with their links. Claim nothing you
   didn't do.
3. Then close:

```text
python3 "${CLAUDE_SKILL_DIR}/../../board/board.py" close
```

This writes `report.html`, so give the user its path. A closed board records nothing more. If you have
an Artifact tool, also publish `report.html` as a private artifact, with every file under the board's
`images/` folder passed as a supporting file at the same relative path, and give the user the link: it
opens on their phone, and the local file does not.

## While a board is running

The hooks record the files you change and the commands you run, with how each command ended. Record a
check only for what a command's exit status does not show.

For a question whose answer changes the work, record it with your default and keep going on the
default, unless it is a hard stop. Record decisions worth a look, checks and deliverables as you make
them. A line from the user like `Q3: …` is an answer: apply it and record it with `answer Q3 "TEXT"`.

### Decisions are rulings

The top of the page lists every decision with a keep/reverse tick, and the user reads it later without
your context. Write each one for that reader:

- `TEXT`: the call itself in plain words, as a statement of what the work now does. Leave out internal
  names unless the user knows them.
- `--why`: what it means, with a concrete example and real numbers from the work.
- `--reverse`: what would be different for the user if it were reversed, not which file to edit.
- `--group`: the topic, so related calls sit together. Reuse the same few group names.
- `--yours`: the user made this call in chat, so they can revisit it now they see the context.

A screenshot or rendered image recorded with `deliverable "CAPTION" --path P` is shown on the page.

A line from the user like `Logbook "TITLE" 10 Oct: reverse D4, Q2 | note: …` comes from that page's
Copy answers button. Reverse those decisions before any other work, skipping any already reversed.
If the decision is on this session's board, `revise` it to say what now holds. Otherwise record the
reversal as a decision here.

## Commands

- `question "TEXT" --default "D" --affects "A" --reverse "R"`
- `stop "TEXT"`
- `answer Q3 "TEXT"`
- `decision "TEXT" --why "W" --reverse "R" --group "G" [--yours]`
- `revise D4 ["TEXT"] [--why "W"] [--reverse "R"] [--group "G"] [--yours | --not-yours]`
- `summary "TEXT" --facts "F"`
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
