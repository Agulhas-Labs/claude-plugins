# Hook payloads

Captured from a real headless session on Claude Code 2.1.283 by a plugin that wrote every hook's
stdin to a file, then made generic: the session id, paths and prompt are replaced, nothing else.
The tests feed these to the hooks, so a field the host does not send is never relied on.

`TodoWrite` is not here. That version's sessions carry the task list in `TaskCreate` and
`TaskUpdate`; the older tool was not observed, so its payload in the tests is written from its
documented input, and says so.

`../transcript.jsonl` is the same session's transcript, cut down to the lines a board reads when it
starts late: the first prompt, and each task-list call with its result. A real transcript carries
many other line types between them, which a reader must step over.

The work-call payloads (`PostToolUse-Write`, `-Edit`, `-Bash`, `-Bash-2` and `PostToolUseFailure-Bash`)
came from a second session on the same version, made generic the same way. `../transcript-work.jsonl`
is that session's transcript cut down the same way: the prompt, then each tool call and its result,
with each line's own `timestamp` kept. It holds what the payloads cannot show: a `Read` a reader must
step over, a failed command whose `toolUseResult` is a string, and an `Edit` that failed validation,
which fired no hook at all.

`../transcript-agents.jsonl` is cut down from real transcripts on the same version, every text replaced:
an `Agent` call launched in the background, one that waited for the subagent's report, and one whose
launch failed. The first two carry the subagent's id in `toolUseResult`; the failed one, a string.

`PostToolUse-Bash-subagent` and `PostToolUse-Write-subagent` are the calls a subagent made, captured
the same way. They carry `agent_id` and `agent_type`, written ahead of `hook_event_name`.
