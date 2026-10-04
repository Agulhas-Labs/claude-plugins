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
  scratch copy under another name and delete it afterwards. Where the change is to what a hook
  *shows* the user, hook stdout reaches only the model — use the `prove-hook-output` skill, whose
  `prove-render.sh` drives a real PTY and reports what the terminal actually received.
- **A change to a plugin's `dependencies` is proven by a real install and a real update:** `claude plugin
  install` and `claude plugin update` from the marketplace, then `claude plugin list`. `validate --strict`
  passes either way. Measured on Claude Code 2.1.274: a fresh install brings the dependency, an update
  does not, and the dependent plugin stays off until it is installed by hand.
- **A test that runs git clears git's environment first.** The pre-commit hook runs every suite, and a
  git hook exports `GIT_DIR` and `GIT_INDEX_FILE`. Observed once: a suite that made throwaway
  repositories configured, branched and committed to this repository instead of its own, and left it
  marked bare. Drop every `GIT_*` variable from the call, and assert that the new repository's git
  directory is inside the test's temporary directory before writing to it.
- **A plugin names one hooks module.** `hooks.json` `modules` takes a single entry: `claude plugin validate`
  refuses a second (observed on Claude Code 2.1.287). Several mods in one plugin register through one
  entry file that calls each one's `register`.
- **Commit on a branch; never merge to `main` unprompted.** Bump a plugin's `version` in both its
  `plugin.json` and the marketplace entry when its behaviour changes.

## Layout
- `.claude-plugin/marketplace.json` — the marketplace: one entry per plugin.
- `plugins/delegate/` — the orchestration plugin: the roster (`agents/`), the conventions it injects
  (`context/`), its hooks (`hooks/`), the `agent-cost` skill (`skills/agent-cost/`, with the report's
  tests), the report itself (`skills/agent-cost/scripts/agent_cost.py`, a Python script that reads local
  transcripts and reports where tokens went), the agent band and finish toast (`hooks/agent-band.tsx`, a
  mod, with its price table `hooks/prices.json` and tests `hooks/agent-band.test.ts`), and tests (`tests/`). It has no `bin/` directory and no
  file with an executable bit: every script is run as `sh <path>` or `python3 <path>`, and a test pins it.
- `plugins/cache-guard/` — a `UserPromptSubmit` hook (`hooks/`) that holds a message back when the
  context's prompt cache has expired, the handoff it offers instead (written by script, summarised by a
  detached headless run), a `SessionStart` hook that points the next session at it, the band above the
  prompt (`hooks/band.tsx`, reading `hooks/status.py`) and the resume offer (`hooks/resume.tsx`, reading
  `hooks/resume.py`) — mods registered through `hooks/register.tsx`, with their tests beside them
  (`hooks/*.test.ts*`) — and tests (`tests/`).
- `plugins/logbook/` — the recorder (`board/board.py`), its hooks (`hooks/`) that record files changed,
  commands run, the task list, subagents and turn/session boundaries without spending model tokens, the
  `/logbook` skill (`skills/logbook/`), the band above the prompt, its button that opens the page, and the new-question toast (`hooks/band.tsx`,
  `hooks/view.ts`, a mod registered through `hooks/register.tsx`, reading `board/mod_state.py`, with its
  tests `hooks/band.test.ts`), and tests (`tests/`).
- `scripts/check.sh` — the repository gate. `githooks/` — the pre-commit hook that runs it.
