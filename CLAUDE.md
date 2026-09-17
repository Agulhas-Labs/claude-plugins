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
  session starts. Load the working copy with `claude --plugin-dir plugins/<name>`. An installed plugin
  of the same name shadows it: measured on Claude Code 2.1.275, the working copy's hooks never
  registered and the proof runs passed for the wrong reason. Where the plugin is installed, load a
  scratch copy under another name and delete it afterwards.
- **A change to a plugin's `dependencies` is proven by a real install and a real update:** `claude plugin
  install` and `claude plugin update` from the marketplace, then `claude plugin list`. `validate --strict`
  passes either way. Measured on Claude Code 2.1.274: a fresh install brings the dependency, an update
  does not, and the dependent plugin stays off until it is installed by hand.
- **Commit on a branch; never merge to `main` unprompted.** Bump a plugin's `version` in both its
  `plugin.json` and the marketplace entry when its behaviour changes.

## Layout
- `.claude-plugin/marketplace.json` — the marketplace: one entry per plugin.
- `plugins/orchestration/` — the roster (`agents/`), the conventions it injects (`context/`), its hooks
  (`hooks/`), and tests (`tests/`).
- `plugins/agent-cost/` — the `report` skill (`skills/report/`): `agentcost.py`, which reads local
  transcripts and reports where tokens went, and its tests beside it.
- `plugins/cache-guard/` — a `UserPromptSubmit` hook (`hooks/`) that holds a message back when the
  context's prompt cache has expired, the handoff it offers instead (written by script, summarised by a
  detached headless run), a `SessionStart` hook that points the next session at it, and tests (`tests/`).
- `scripts/check.sh` — the repository gate. `githooks/` — the pre-commit hook that runs it.
