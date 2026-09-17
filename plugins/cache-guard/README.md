# cache-guard

Stops you, once, before you send the most expensive message of your day without knowing it.

Claude Code re-sends the whole conversation on every turn. That is cheap while the prompt cache is warm:
cached tokens cost a tenth of the input price, or less. The cache expires after five minutes or an hour,
depending on your plan. Come back from lunch to a 310k-token session, type "ok, carry on", and that one
message is billed as 310k tokens written to the cache again: about $3.10 on Opus at API list prices,
where the same message ten minutes earlier cost $0.16. On Fable 5.1 it is about $6.20 against $0.08.
Nothing in the interface tells you.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install cache-guard@agulhas-labs
```

Start a new session. There is nothing to configure: the guard reads your cache lifetime from the
session's own transcript.

## What you see

When a message is about to go into a large context whose cache has expired, it is held back and you get
this instead:

```text
Prompt cache expired: this session has been idle 1h 24m and its cache lasts 1h. Sending now re-sends
about 310k tokens at the cache-write price, about $3.10 at API list prices instead of $0.16 with a warm
cache, roughly 20x what this turn costs while the cache is warm. Send the message again within 2 minutes
to go ahead. Cheapest: send the single word handoff, which writes a handoff file without using this
session's model, then /clear. /compact pays for this cold context once and every turn after it is small.
```

Send the same message again and it goes through. The guard never stops a small context, a message that
starts with `/`, or the same message twice.

Two other things reset the cache with no idle time at all, and the guard warns about them while they can
still be undone for free:

- **Changing the model.** Caches are per model, so the next message rewrites everything.
- **Changing effort.** In a test session with a 65k-token context, the turn after an effort change read
  18.5k tokens from cache and rewrote 46.9k; the turns before and after it read all 65k. Only the system
  prompt survives.

Each warning names the command that switches back. Setting the same model or effort again is not a
change and does not warn.

## The way out: `handoff`

Once the cache is cold, anything that asks the session's model to summarise costs the cold turn too,
`/compact` included. So the guard offers a route that does not use that model.

Send the single word `handoff`. The hook takes it before it reaches the model and:

1. writes `.claude/handoffs/<timestamp>.md` from the transcript on disk, by script: what you asked, in
   order, where the work stopped, the files edited, the open todos, the branch. This costs nothing.
2. starts a cheap model in the background on a condensed copy of the transcript: your messages and the
   assistant's prose in full, each tool call as one line, tool output cut to a few hundred characters.
   Across twelve long sessions that came to about a tenth of the live context, so a 400k-token session
   is about 40k tokens for Haiku to read, around four cents at list price. When it finishes, the file
   has a proper summary on top: goal, decisions and why, current state, what is left, next step, what to
   be careful of. Sessions too long for Haiku go to Sonnet, and a session of a few lines gets no summary
   because the script-written file already says it all.
3. tells you the path. `/clear`, and the new session is told a fresh handoff exists and where it is.

The background run has no tools, no MCP servers, no plugins and none of your settings, so it cannot act
on anything it reads, and it cannot trigger the guard. Its reply is only accepted if it has the shape of
a handoff. If the `claude` command is not on your `PATH`, the run fails, or it takes more than five
minutes, you keep the script-written file, which says so.

While the cache is warm, `/cache-guard:handoff` asks the session's own model to write the same file.
It knows more than a condensed transcript does, and warm it costs one ordinary turn. Cold, it would cost
the very turn you were warned about, so the guard holds a cold `/handoff` back once and points at the
word without the slash.

A handoff holds your conversation, so the files are readable by you alone, and the first one written
to `.claude/handoffs/` puts a `.gitignore` beside it that keeps them out of the repository. Delete that
file if you want to commit them.

## Is it worth it for you?

On the machine this was built on, one week of `agent-cost` (in the `orchestration` plugin) showed 6%
of main-session spend going on ten messages, every one sent after more than an hour away into a context
over 100k tokens. The guard's trigger is exactly that cell. Run `agent-cost` and read its Cold cache
section for your own number; it also shows whether your sessions get the five-minute or the one-hour
lifetime.

The dollar figures are API list prices. On a subscription they are not a bill, but the same tokens come
out of your usage limit.

## Settings

All optional, set under `env` in `~/.claude/settings.json` or a repository's `.claude/settings.json`.

| Variable | Default | What it does |
| --- | --- | --- |
| `CACHE_GUARD_MIN_TOKENS` | `100000` | The smallest context worth a warning. |
| `CACHE_GUARD_LIFETIME_SECONDS` | detected | Overrides the cache lifetime read from the transcript. |
| `CACHE_GUARD_CONFIRM_SECONDS` | `120` | How long "send it again" stays valid. |
| `CACHE_GUARD_SHOW_COST` | on | `0` leaves the dollar figures out. |
| `CACHE_GUARD_INPUT_PRICE` | list price | Input price in $ per million tokens, when the list price is not yours. |
| `CACHE_GUARD_HANDOFF_DIR` | `.claude/handoffs` | Where handoffs are written. |
| `CACHE_GUARD_HANDOFF_MODEL` | `haiku`, or `sonnet` when long | The model that writes the summary. |
| `CACHE_GUARD_HANDOFF_SUMMARY` | on | `0` writes the script-only handoff and starts nothing. |
| `CACHE_GUARD_HANDOFF_FRESH_MINUTES` | `30` | How recent a handoff must be for a new session to be told about it. |
| `CACHE_GUARD_STATE_DIR` | `~/.claude/cache-guard` | Where the guard keeps its few small marker files. |
| `CACHE_GUARD_DISABLE` | off | `1` switches the guard off. |

## What it needs, and what it never does

Python 3, standard library only; without it the hooks do nothing and nothing else changes. On every
prompt the hook reads the last 512 KB of the session's transcript, which took 0.05 s on a 5 MB
transcript. If anything at all goes wrong it lets the message through: it is built never to be the
reason a prompt fails. Its own state lives in `~/.claude/cache-guard`, created for you alone; if that
path turns out to be a symlink or somebody else's directory, the guard keeps no state rather than
touch it, and it only ever deletes files it wrote.

It reads transcripts that are already on your disk. The only time anything leaves your machine is the
background summary, which goes to the same service your session already talks to, through your own
`claude` command. Turn that off with `CACHE_GUARD_HANDOFF_SUMMARY=0`.

It cannot warn you before the cache expires. Hooks run on events, and being away is not one.
