---
name: handoff
description: >-
  Write a handoff file so a fresh session can continue this work without the conversation: goal,
  decisions, state, files, what is left, next step. Use when the user types /handoff or asks for a
  handoff, a session summary to continue from, or wants to /clear without losing their place.
---

# handoff

Write one markdown file that lets a session with none of this conversation carry on from here. The user
will `/clear` afterwards, so anything not in the file is gone.

Use this while the prompt cache is warm. After a long idle gap it costs a full cold turn, because you
have to re-read the whole context to write it; in that case tell the user that sending the single word
`handoff` writes the file without using this session's model, and stop.

## Where

`.claude/handoffs/<UTC YYYYMMDD-HHMMSS>.md` under the working directory, or the directory in
`CACHE_GUARD_HANDOFF_DIR` when it is set. Create the directory if it is missing. One new file per
handoff; never overwrite an earlier one.

## What goes in it

Exactly these sections, in this order. Leave a section out only when it would be empty.

1. **Goal** — what the user is trying to achieve, in their terms, including the reason when they gave one.
2. **Decisions taken** — each decision with the reason it was taken, and anything the user ruled out.
   These are the most expensive things to lose: a fresh session will otherwise reopen them.
3. **Current state** — what is done, and for each item whether it is verified, committed, pushed or
   merged. Name branches, commits, files and commands. Say what was checked and what the check showed.
4. **Files and branches that matter** — paths, with a phrase on why each matters.
5. **What is left** — in the order it should be done.
6. **Next step** — the single next action, concrete enough to start on without asking.
7. **Things to be careful of** — constraints the user stated, traps found along the way, anything that
   failed and why, anything running or left behind (processes, worktrees, temporary files).

## How

- Write from the conversation you have. Do not re-read files or run commands to pad it out; one
  `git status -sb` and `git log --oneline -5` is reasonable when the state of the branch is unclear.
- Be specific: names, paths, commands, numbers, the user's own wording for a requirement. Leave out the
  story of how you got here.
- State only what happened. Where something was not verified, say so.
- Keep it under about 150 lines. A handoff that needs more is describing two jobs; say which one is next.

## When it is written

Tell the user the path, and that they can `/clear` and start the next session with "read <path> and
carry on". If the cache-guard plugin's session-start hook is active, the new session is told about a
handoff written in the last 30 minutes by itself.
