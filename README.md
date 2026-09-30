# Agulhas Labs plugins for Claude Code

Plugins for Claude Code, the terminal and IDE tool: one that runs your session as the lead of a team of
agents and shows you where your tokens went, one that warns you before the one message that costs twenty
times the rest, and one that shows you what a long task did. They run on your machine through Claude
Code's hooks, agents and commands, so they don't work in claude.ai chat or Cowork.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install delegate@agulhas-labs
/plugin install cache-guard@agulhas-labs
/plugin install logbook@agulhas-labs
```

Start a new session after installing. None of them needs configuring.

| Plugin | The problem it solves | What it does |
| --- | --- | --- |
| [`delegate`](plugins/delegate/README.md) | A few long-running subagents take most of your spend, and you can't see where your tokens go. | Your session leads a roster of subagents on models matched to the job, and re-runs the commands that prove each handoff before it believes the report. Its `agent-cost` report reads the transcripts already on your disk and shows the spend for your sessions and their subagents. |
| [`cache-guard`](plugins/cache-guard/README.md) | A message sent into a big session after its prompt cache expired costs far more than it looks, and nothing tells you. | Holds that message back once and shows what it will cost. Send it again and it goes through. |
| [`logbook`](plugins/logbook/README.md) | A long task ends and you dig through the scroll to find out what happened. | Keeps a live local page of what was built, what was verified, what was decided without you and what's waiting on you. It becomes the report when the session ends. |

## delegate

Your session plans and judges on the model you chose. Haiku runs commands, Sonnet makes changes that are
already spelled out, and Opus writes the code that still has decisions in it, then reviews the result
without having seen how it was made. A hook warns an agent as its context grows: freeze scope at 120k,
hand back at 150k, stop at 200k. At most 4 agents run at once, and you can change that.

Ask "where did my tokens go this week?" and its `agent-cost` report answers from your local transcripts,
down to which projects, sessions and subagents cost the most. Each section says what to do about what it
shows, and `--since`/`--until` let you compare the days before a change with the days after it. Nothing
leaves your machine.

It changes how agents behave in a repository: they commit verified work on a feature branch without
being asked. Read [Before you install](plugins/delegate/README.md#before-you-install) first,
especially if you're turning it on for other people.

[delegate README](plugins/delegate/README.md): the roster, a handoff from start to finish, a sample
`agent-cost` report, and how to tailor the models, tools and conventions.

## cache-guard

Claude Code re-sends the whole conversation on every turn. That's cheap while the prompt cache is warm,
and the cache expires after five minutes or an hour, depending on your plan. Come back after an hour to a
310k-token session, type "ok, carry on", and that one message costs about twenty times what it would have before the
cache expired. Cache Guard stops it once and shows you the cost. It also warns you before a model or effort
change resets the cache, while you can still undo it for free.

If you'd rather start fresh, send the single word `handoff`. A script writes
`.claude/handoffs/<timestamp>.md` from the transcript on disk and a cheap model adds a summary, so your
session's model isn't used. Run `/clear` and the new session is pointed at the file.

[cache-guard README](plugins/cache-guard/README.md): what you see, the settings, and what it never does.

## logbook

Useful when a task runs long or delegates a lot. Its hooks record the files changed, the commands run and
how they ended, and the commits, so the page has something to show without a task list or subagents. The
terminal prints the path to `board.html`; open it and it re-reads its state every 10 seconds, with nothing
fetched from the network. When the session ends, the same file becomes `report.html`, self-contained, the
one to keep or send.

[logbook README](plugins/logbook/README.md): the page's sections, answering a question from the
page, and the settings.

## Installing for a team

The commands at the top install a plugin for you. To offer them to everyone working in a repository,
commit this to the repository's `.claude/settings.json`, keeping the plugins you want:

```json
{
  "extraKnownMarketplaces": {
    "agulhas-labs": {
      "source": { "source": "github", "repo": "Agulhas-Labs/claude-plugins" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": {
    "delegate@agulhas-labs": true,
    "cache-guard@agulhas-labs": true,
    "logbook@agulhas-labs": true
  }
}
```

## Updates

A marketplace you add yourself doesn't update on its own. Turn that on once: `/plugin`, then
Marketplaces, `agulhas-labs`, Enable auto-update. Claude Code then refreshes the marketplace and its
installed plugins when it starts. The `"autoUpdate": true` line above does the same for a repository. To
update by hand, run `/plugin marketplace update agulhas-labs`.

Plugins, agents and hooks load when a session starts, so start a new one after installing or updating.
If your organisation has managed settings, check them first: they can restrict which marketplaces and
models are allowed.

## Requirements

All three use Python 3, standard library only, and do no harm without it: the Python hooks are skipped
and `agent-cost` fails visibly. Their hooks and commands run through a POSIX shell, so on Windows they
need Git Bash. Windows is untested. Each plugin's README has the detail.

## Developing

```text
git config core.hooksPath githooks     # once per clone: the pre-commit gate
scripts/check.sh                        # tests, manifest validation, privacy scan
claude --plugin-dir plugins/<name>      # load a working copy into a fresh session
```

## License, privacy and terms

Apache License 2.0. See [LICENSE](LICENSE). What each plugin reads and sends is in
[PRIVACY.md](PRIVACY.md), and the terms of use are in [TERMS.md](TERMS.md).
