# Privacy

The plugins in this repository run inside Claude Code on your own machine. Agulhas Labs runs no server
for them and receives nothing from them: no analytics, no telemetry, no accounts.

## delegate

- Reads the Claude Code transcripts already on your disk (`~/.claude/projects`, or
  `$CLAUDE_CONFIG_DIR/projects` when that is set) to build the `agent-cost` report, and prints the
  report in your terminal or session.
- Its hooks read the plugin's own convention files and the running session's transcript, and add text
  to Claude's context.
- Writes no files and makes no network requests. Its agents run through Claude Code like any other
  subagent, on the service your session already uses.

## cache-guard

- Reads the end of the running session's transcript to estimate what the next message will cost.
- Keeps a small state directory in your own `~/.claude`, and writes handoff files to the project's
  `.claude/handoffs/` when you ask for one.
- Sends nothing itself. The optional handoff summary is written by your own `claude` command, so the
  transcript it summarises goes to the same service your session already uses.
  `CACHE_GUARD_HANDOFF_SUMMARY=0` turns that off.

## logbook

- Records the files changed, commands run, commits and subagents of a session into a page inside the
  project (`.logbook/`), which is ignored by git.
- The page holds your prompts and commit subjects, stays on your machine, and loads nothing from the
  network.

## upkeep

- The session-start hook reads one file, `last-upkeep` in the plugin's data directory (or
  `~/.claude/upkeep/`), and nothing else: no settings, no transcripts.
- `/upkeep` reads your rules, CLAUDE.md and SKILL.md files, the project's git metadata and the project's
  memory notes to report on them. It writes only that one file, unless you confirm a cleanup it proposes.
- Makes no network requests and sends nothing.

## Questions

Open an issue at https://github.com/Agulhas-Labs/claude-plugins/issues.
