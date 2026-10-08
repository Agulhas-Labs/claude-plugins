# Cache Guard

Cache Guard is a plugin for Claude Code. It does not work in claude.ai chat or Cowork: it watches Claude Code's prompt cache from hooks on your machine.

Claude Code re-sends your whole conversation on every turn. While the prompt cache is warm that's
cheap, because cached tokens cost a tenth of the input price or less. The cache expires after five
minutes or an hour, depending on your plan, and then the next message pays to write the whole context
again. Come back from lunch to a 310k-token session, type "ok, carry on", and that one message costs
about $3.10 on Opus at API list prices. Sent before the hour was up, it would have cost $0.16. Nothing
in the interface tells you.

Cache Guard holds that message back once and shows you the cost. Send it again and it goes through, or
type `handoff` to carry on in a fresh session for next to nothing.

```text
/plugin marketplace add Agulhas-Labs/claude-plugins
/plugin install cache-guard@agulhas-labs
```

Start a new session. There's nothing to configure: the guard reads your cache lifetime from the
session's own transcript.

## What you'll see

```text
Prompt cache expired, so this message was held back. Cheapest: send the single word `handoff`, which
writes a handoff file without using this session's model, then /clear. To go ahead instead, send the
message again within 2 minutes: this session has been idle 1h 24m and its cache lasts 1h, so sending
re-sends about 310k tokens at the cache-write price, about $3.10 at API list prices instead of $0.16 with
a warm cache, roughly 20x what this turn costs while the cache is warm. /compact pays for this cold
context once and every turn after it is small. (cache-guard; threshold CACHE_GUARD_MIN_TOKENS=100000)
```

The guard never stops a small context, the same message twice, or a message that starts with `/`
(except a cold `/handoff`, below).

Changing the model or the effort level also resets the cache, with no idle time at all, so the guard
warns about those too. Caches are per model, so a model change rewrites everything. An effort change
keeps only the system prompt: in a test session with a 65k-token context, the turn after one read 18.5k
tokens from cache and rewrote 46.9k, while the turns before and after it read all 65k. The warning names
the command that switches back, and switching back before you send costs nothing. Setting the same
model or effort again isn't a change and doesn't warn.

## The cheap way out: `handoff`

Once the cache is cold, anything that asks the session's model to summarise pays for the cold turn,
`/compact` included. Send the single word `handoff` instead. The hook takes it before it reaches the
model and:

1. Writes `.claude/handoffs/<timestamp>.md` from the transcript on disk, by script: what you asked, in
   order, where the work stopped, the files edited, the open todos, the branch. This costs nothing.
2. Starts a cheap model in the background to add a summary on top: goal, decisions and why, current
   state, what's left, next step, what to be careful of.
3. Tells you where the file is, and to wait two minutes before you `/clear` or start anything else. The
   runs measured took 35 to 54 seconds, a short session included.

Then `/clear`. The new session tells you, in the terminal, that a fresh handoff is waiting, where it is,
and whether its summary is in yet. When the summary lands you're told again, with a desktop notification
if your terminal supports them. Hooks only run when you do something, so that news arrives with your next
prompt or your next session rather than the moment the summary is written. Until then, the file's
`Summary:` line says whether it's still being written.

If the summary fails, the notice says so and how to retry: send `handoff` again, which never reaches the
model. If you've already cleared, resume the session that wrote it (`claude --resume`) and send it
there. The script-written file is complete and readable either way.

While the cache is warm, `/cache-guard:handoff` has the session's own model write the file instead. It
knows more than a condensed transcript does, and costs one ordinary turn. Cold, it would cost the very
turn you were warned about, so the guard holds it back once and points you at the plain word.

## How the handoff works

The background model reads a condensed transcript: your messages and the assistant's prose in full,
each tool call as one line, tool output cut to a few hundred characters. Across twelve long sessions that
came to about a tenth of the live context, so a 400k-token session is about 40k tokens for Haiku, around
four cents at list price. Sessions too long for Haiku go to Sonnet; a session of a few lines gets no
summary, since the script-written file already says it all.

That run has no tools, no MCP servers, no plugins and none of your settings, so it can't act on anything
it reads and can't trigger the guard. Its reply is only accepted if it has the shape of a handoff. If the
`claude` command isn't on your `PATH`, the run fails, or it takes more than five minutes, you keep the
script-written file, and it says so.

Every session started in that directory within the freshness window is told about a new handoff, and
no session outside it is. Handoff files hold your conversation, so they're readable by you alone, and the
first one puts a `.gitignore` in `.claude/handoffs/` that keeps them out of the repository. Delete it if
you want to commit them.

## The resume offer

When a session starts in a directory whose newest handoff was written within the last 7 days, a two-line
band above the prompt offers to pick the work up:

```
Previous session: <the handoff's summary line> (2 d ago) · branch feat/x · 2 ahead · 3 uncommitted
[Resume] [Dismiss]
```

Every figure is computed when the session starts (or a `/clear` starts a new one), not remembered: the
age is from the handoff file's modification time, and the branch, the commits ahead of its upstream and
the count of uncommitted files come from `git` in the session's working directory. A figure git can't
give (no upstream, not a repository) is left out. The band says nothing about whether the work is
finished or any test passed.

**Resume** puts `Read <path> and continue from it.` in the prompt box as a draft. Nothing is sent and
nothing is spent until you press Enter. **Dismiss** and **Resume** hide the band and are remembered: that
handoff is not offered again in a later session (a newer handoff still is). Sending any prompt hides it
for the rest of the session. A `/clear` or a resume in the same window starts a new session, so the offer
is evaluated again then: the handoff you just wrote is offered, unless you dismissed or resumed it. The
band is not drawn when there is no handoff, when the newest is older than the limit, or while a survey is
up.

It is a mod, so it needs Claude Code 2.1.287 or later and draws in the terminal and the Desktop app only;
elsewhere it does nothing, and the `SessionStart` hook's notice above is unchanged. The limit is the
plugin option `resumeMaxAgeDays` (default `7`).

What it reads and runs, since a mod is code with your full permissions: it reads the newest `.md` file in
the handoffs directory (the one the handoff hook writes to) and the git status of the working directory;
it runs `hooks/resume.py` once when a session starts and again after a `/clear` or a resume, which runs
`git rev-parse`, `git rev-list --count` and `git status --porcelain` locally with a three-second limit
each and no network. It writes one thing: the paths of the handoffs you dismissed or resumed, in the plugin's own store, so they are not offered again.

## Is it worth it for you?

On the machine this was built on, a week of the `agent-cost` report from the `delegate` plugin showed 6%
of main-session spend going on ten messages, each sent after more than an hour away into a context over
100k tokens. Its Cold cache section gives you your own number, and whether your sessions get the
five-minute or the one-hour cache. On a subscription the dollar figures aren't a bill, but the same
tokens come out of your usage limit.

## The band above the prompt

In Claude Code 2.1.287 or later, in the terminal or the desktop app, one row above the prompt shows what is left of the prompt cache and what a miss would cost.

An example (made-up figures), with each figure coloured by what it means:

```text
Cache-Guard  Cache 25 mins left · Tokens 109K (7%) · Miss cost $3.34   [ Handoff ]
```

| Figure | Means | Colour |
| --- | --- | --- |
| `Cache 25 mins left` | Minutes before the prompt cache expires, counted down from the last answer and the lifetime the session is buying. `Cache expired` once it has gone, and `Cache expired (under 100K, not held)` when the context is smaller than `CACHE_GUARD_MIN_TOKENS`: the next message pays the miss, but the guard lets it through. | Green with half the lifetime or more left, yellow down to a tenth, red below that. Expired is red when the next message will be held and yellow when it will not. |
| `Tokens 109K (7%)` | The size of the context the next message re-sends, and the share of the model's window it fills when Claude Code has a reading. | The share is green below 60%, yellow below 80%, red at 80% or more. |
| `Miss cost $3.34` | What the next message costs if the cache has expired: the whole context written back at list price. Left out when `CACHE_GUARD_SHOW_COST=0`. | Yellow once the cache has expired, plain before. |

The colour cut-offs are display choices, not measured values.

`Handoff` (or `h` while the band has the focus) writes the same handoff as the word `handoff` and starts its
background summary. The button reads `Writing handoff` with a turning spinner until the summary is in; then
a box above the band says the handoff is ready and `/clear` is safe, gives the file's path, and says how to
pick it up: `/clear`, then press Resume above the prompt, or send `Read <path> and continue from it.`
Dismiss closes the box, and so does `/clear`. It sends nothing to the session's model.

The band reads the transcript with the same script the guard uses (`hooks/status.py`), when a turn ends
or a session starts, never while drawing. It runs `hooks/handoff.py` when you press `Handoff`, and reads
the handoff file while it waits for the summary. Elsewhere nothing draws and the hooks above still work.

## Settings

All optional, set under `env` in `~/.claude/settings.json` or a repository's `.claude/settings.json`.

| Variable | Default | What it does |
| --- | --- | --- |
| `CACHE_GUARD_MIN_TOKENS` | `100000` | The smallest context worth a warning. |
| `CACHE_GUARD_LIFETIME_SECONDS` | detected | Overrides the cache lifetime read from the transcript. |
| `CACHE_GUARD_CONFIRM_SECONDS` | `120` | How long "send it again" stays valid. |
| `CACHE_GUARD_SHOW_COST` | on | `0` leaves the dollar figures out. |
| `CACHE_GUARD_INPUT_PRICE` | list price | Input price in $ per million tokens, when the list price isn't yours. |
| `CACHE_GUARD_HANDOFF_DIR` | `.claude/handoffs` | Where handoffs are written. |
| `CACHE_GUARD_HANDOFF_MODEL` | `haiku`, or `sonnet` when long | The model that writes the summary. |
| `CACHE_GUARD_HANDOFF_SUMMARY` | on | `0` writes the script-only handoff and starts nothing. |
| `CACHE_GUARD_HANDOFF_FRESH_MINUTES` | `30` | How recent a handoff must be for a new session to be told about it. |
| `resumeMaxAgeDays` (plugin option, not an environment variable) | `7` | How old the newest handoff may be for the resume band to be offered. |
| `CACHE_GUARD_NOTIFY` | on | `0` sends no desktop notifications; the terminal messages are unchanged. |
| `CACHE_GUARD_STATE_DIR` | `cache-guard` in `CLAUDE_CONFIG_DIR`, else `~/.claude/cache-guard` | Where the guard keeps its few small marker files. |
| `CACHE_GUARD_DISABLE` | off | `1` switches the guard off. |

## Requirements and limits

Python 3, standard library only, and a POSIX shell (Git Bash on Windows); without Python the hooks do
nothing and nothing else changes. On every
prompt the hook reads the last 512 KB of the transcript, and if anything goes wrong it lets the message
through. Its state directory is created for you alone; if that path is a symlink or someone else's
directory, the guard keeps no state rather than touch it. It only ever deletes files it wrote.

Nothing leaves your machine except the background summary, which goes to the same service your session
already uses, through your own `claude` command. `CACHE_GUARD_HANDOFF_SUMMARY=0` turns that off.

It can't warn you before the cache expires. Hooks run on events, and being away isn't one.
