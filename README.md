# Agulhas Labs plugins for Claude Code

Two plugins about the same thing: what your tokens are spent on, and how to stop paying for the part
that buys nothing. Both came out of measuring real sessions, and the measuring tool ships with them.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install orchestration@agulhas-labs
/plugin install cache-guard@agulhas-labs
```

Install either or both, then start a new session. Neither needs configuring.

| Plugin | What it does | When it earns its place |
| --- | --- | --- |
| [`orchestration`](plugins/orchestration/README.md) | Your session leads a team: cheap models do the specified work, an expensive one designs and reviews, and nothing is done until a command proves it. Includes `agent-cost`. | You delegate to subagents, or want to know where your tokens went. |
| [`cache-guard`](plugins/cache-guard/README.md) | Holds a message back, once, when it is about to go into a large context whose prompt cache has expired, and tells you what it will cost. | You leave long sessions open and come back to them. |

## orchestration

Your Claude Code session becomes a lead with a team. It plans and judges on the model you chose. Haiku
runs the commands, Sonnet makes the changes that are already spelled out, and Opus writes the code that
still has decisions in it, then reviews the result without having seen how it was made. Every handoff
ends in commands that prove the work, and the lead runs them again itself before it believes the report.

An agent re-sends its whole context on every turn, so its cost grows with the square of its length. In
one measured week the longest 10% of subagents were 45% of all subagent spend. So each agent gets one
job, a hook tells a subagent to hand back once its context passes 150k tokens, reviews stop after two
rounds, and at most 4 agents run at once (a setting).

It comes with the `agent-cost` skill, which reads the transcripts already on your disk and shows where
your tokens went: by model and agent type, how concentrated the spend is, what fills the contexts you
keep re-sending, and what every agent pays before it does any work. It alters nothing and nothing
leaves your machine. Run it first; it tells you whether you have the problem the rest solves.

[Read more](plugins/orchestration/README.md): the roster, a handoff from start to finish, the numbers
behind each rule, and how to tailor the models, tools and conventions.

## cache-guard

Claude Code re-sends the whole conversation on every turn, which is cheap while the prompt cache is
warm. The cache expires after five minutes or an hour, depending on your plan. Come back from lunch to a
310k-token session, type "ok, carry on", and that one message costs about twenty times what it did ten
minutes before the cache expired. Nothing in the interface tells you.

The guard holds that message back once and shows the cost. Send it again and it goes through. It also
warns when a model or effort change is about to reset the cache, while that can still be undone for free.

The way out it offers is a handoff: send the single word `handoff` and a script writes
`.claude/handoffs/<timestamp>.md` from the transcript on disk, then a cheap model adds a summary in the
background, without using your session's model. `/clear`, and the new session is pointed at the file.
While the cache is warm, the `/cache-guard:handoff` skill has the session's own model write the same
file.

[Read more](plugins/cache-guard/README.md): what you see, how the handoff is written, the settings, and
what the guard never does.

## How they fit together

`agent-cost` is the instrument for both. Its Concentration and Turn shape sections show whether a few
long agents are taking most of your spend, which is what `orchestration` fixes. Its Cold cache section
shows how much goes on messages sent into an expired cache, which is what `cache-guard` stops. Measure
first, and install what your own numbers ask for.

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
    "orchestration@agulhas-labs": true,
    "cache-guard@agulhas-labs": true
  }
}
```

`orchestration` changes how agents behave in a repository: they commit verified work on a feature
branch without being asked. Read [Before you install](plugins/orchestration/README.md#before-you-install)
before turning it on for other people.

## Updates

A marketplace you add yourself does not update on its own. Turn that on once: `/plugin`, then
Marketplaces, `agulhas-labs`, Enable auto-update. Claude Code then refreshes the marketplace and its
installed plugins when it starts. The `"autoUpdate": true` line above does the same for a repository.
To update by hand, run `/plugin marketplace update agulhas-labs`.

Plugins, agents and hooks load when a session starts, so start a new one after installing or updating.
An organisation's managed settings can restrict which marketplaces and models are allowed; check them
first.

## Requirements

Both plugins use Python 3, standard library only, and do nothing harmful without it: their Python hooks
are skipped, and `agent-cost` fails visibly. `orchestration`'s conventions need only a POSIX shell. On
Windows, hooks need Git Bash; Windows is untested. Each plugin's README has the detail.

## Developing

```text
git config core.hooksPath githooks     # once per clone: the pre-commit gate
scripts/check.sh                        # tests, manifest validation, privacy scan
claude --plugin-dir plugins/<name>      # load a working copy into a fresh session
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
