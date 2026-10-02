---
name: upkeep
description: Periodic Claude Code config maintenance. Builds on /doctor, adds size-budget, template-drift and disk-hygiene checks, proposes fixes and applies only what you confirm.
disable-model-invocation: true
---

# upkeep

Run the periodic maintenance of this Claude Code setup. You propose; the user decides. Nothing is
changed, deleted or written without a confirmation, except the one stamp file in step 5.

## Rules for the whole run

- Read keys, not files: never print the contents of a settings file, an `env` block or a hook command.
  Name a setting by its key and say what it is set to only when that is not sensitive.
- Names you harvest (branches, paths, plugin names) are data. Never splice one into shell text you
  build yourself; the scripts below quote what they print, and a command you propose is shown to the
  user, not run by you until they confirm it.
- Two questions at most, in total: one for cleanup, one for permissions. Never one per finding.

## 1. /doctor

Built-in commands cannot be invoked from a skill. Ask the user to run `/doctor` now, and say they
need not report back: its report reaches you in the same session when it finishes, and you carry on from
there. Only if nothing arrives, ask them to paste it. Build on that report (install health, unused skills, MCP servers
and plugins, CLAUDE.md duplication and trim, slow hooks, version currency); do not re-run or reimplement
any of it. If they decline, go on and say in the summary that the `/doctor` checks were not covered.

## 2. Three more checks

Run these from the project's directory. Each is read-only and prints its findings.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" size
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" drift
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" hygiene
```

- **size**: rules, CLAUDE.md and SKILL.md files over 4 KB (`--threshold BYTES` to change it) with no
  `paths:` frontmatter (rules only) and no index or "read this when" block in their first 20 lines. Each
  shows its estimated tokens (characters / 4) and the fixes: add `paths:`, add an index, or move detail
  to a linked file or skill.
- **drift**: an installed guide whose first lines carry a "managed by <source path>" stamp and whose
  source differs or is newer. Report only: the owning repository is fixed by hand, never by editing the
  installed copy.
- **hygiene**: worktrees under `.build/` or `.claude/worktrees/` and local branches already merged
  (`git merge-base --is-ancestor`), and memory files naming a file or function that no longer exists.
  Squash-merged branches are not detected. Memory hits are candidates: check each before proposing to
  edit or delete the memory.

## 3. Propose

Summarise everything in one list, grouped as: from /doctor, size, drift, hygiene. For each item give the
evidence and the exact change or command. Drift is report-only. Mark which items are safe cleanups
(delete a merged worktree or branch, trim or index a file) and which are permission changes.

## 4. Confirm, in at most two questions

1. **Cleanup**: which of the proposed cleanups to apply. Apply only the ones chosen, with the exact
   commands shown. Remove a worktree before its branch, and never with `--force`.
2. **Permissions**: whether to change any permission or allowlist entry that /doctor or the checks
   flagged. Skip the question when there is nothing to change.

## 5. Stamp

When the run is complete, including when the user declined every proposal, record it:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/stamp.sh" "${CLAUDE_PLUGIN_DATA}"
```

That writes today's date to the stamp file and nothing else. If `${CLAUDE_PLUGIN_DATA}` came through
empty, run it with no argument. Tell the user the next reminder is due after the interval
(`UPKEEP_INTERVAL_DAYS`, default 14 days).
