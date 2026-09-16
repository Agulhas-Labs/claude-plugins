# Claude Code Configuration — claude-plugins

A Claude Code plugin marketplace. Everything here is installed on other people's machines, so it is
written for them.

## Always
- **Generic content only.** No personal names, private project or repository names, machine paths, or
  dated quotes. A rule keeps its measured reason with the number and says it was measured; don't add a
  number nothing measured. `scripts/check.sh` scans for private terms against a list kept outside the
  repository, and the pre-commit hook runs it (`git config core.hooksPath githooks` once per clone).
- **Verify before concluding:** `scripts/check.sh` passes — the unit tests, `claude plugin validate
  --strict` on the marketplace and each plugin, and the privacy scan.
- **A behaviour change to a plugin is proven from a fresh session:** plugins, agents and hooks load when a
  session starts. Load the working copy with `claude --plugin-dir plugins/<name>`.
- **Commit on a branch; never merge to `main` unprompted.** Bump a plugin's `version` in both its
  `plugin.json` and the marketplace entry when its behaviour changes.

## Layout
- `.claude-plugin/marketplace.json` — the marketplace: one entry per plugin.
- `plugins/orchestration/` — the roster (`agents/`), the conventions it injects (`context/`), its hooks
  (`hooks/`), the `agent-cost` skill (`skills/`), and tests (`tests/`, plus the skill's own).
- `scripts/check.sh` — the repository gate. `githooks/` — the pre-commit hook that runs it.
