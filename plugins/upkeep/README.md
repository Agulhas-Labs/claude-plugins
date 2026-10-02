# Upkeep

Upkeep is a plugin for Claude Code. It does not work in claude.ai chat or Cowork: it runs a hook and a skill on your machine.

Claude Code config collects debris: rules that grew past what they're worth, guides that drifted from
the file they were generated from, worktrees and branches that merged long ago, memory notes that name
files that no longer exist. Nobody remembers to look. Upkeep reminds you, then does the looking.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install upkeep@agulhas-labs
```

Start a new session after installing.

## What you'll see

When your last run is missing or at least 14 days old, a new session gets one line of context:

```text
Config upkeep is due (last run 2026-09-10): run /upkeep when convenient.
```

Otherwise it adds nothing. It is not added to subagents or after a compaction.

`/upkeep` then:

1. Asks you to run `/doctor` (a skill can't invoke a built-in command) and builds on its report: install
   health, unused skills, MCP servers and plugins, CLAUDE.md duplication, slow hooks, versions.
2. Runs three read-only checks `/doctor` doesn't:
   - **Size budget:** every rule, CLAUDE.md and SKILL.md over 4 KB with no `paths:` frontmatter (rules
     only) and no index or "read this when" block in its first 20 lines, with its estimated tokens
     (characters / 4) and the fix: add `paths:`, add an index, or move detail to a linked file or skill.
   - **Template drift:** an installed guide whose first five lines say `managed by <source path>`,
     where the source differs or is newer. Report only; fix the owning repository by hand.
   - **Disk hygiene:** worktrees under `.build/` or `.claude/worktrees/` and local branches already
     merged (`git merge-base --is-ancestor`), and memory notes naming a file or function that is gone.
     Squash-merged branches aren't detected. Deletions are proposed as exact commands, never run
     unasked.
3. Proposes everything in one list and asks at most two questions: cleanup, and permissions.
4. Records the run, including a run where you declined every proposal.

## What it reads and writes

- **The hook** reads one file, `last-upkeep` (one `YYYY-MM-DD` line), and runs one subprocess, `date`.
  It never reads settings or transcripts and never touches the network.
- **`/upkeep`** reads your rules, CLAUDE.md and SKILL.md files, the current project's git metadata and
  its memory notes. It never prints the contents of a settings file, an env block or a hook command.
- **It writes** `last-upkeep`, in the plugin's data directory (`${CLAUDE_PLUGIN_DATA}`), or
  `~/.claude/upkeep/` where that isn't set. Nothing else is written without your confirmation.

## Settings

- `UPKEEP_INTERVAL_DAYS`: days between reminders, default 14. A value that isn't a whole number is
  ignored.

## Silencing it

Disable the plugin (`/plugin`), or set `UPKEEP_INTERVAL_DAYS=0`; the hook then prints nothing. A stamp
dated in the future counts as not due, and a malformed stamp counts as never run.
