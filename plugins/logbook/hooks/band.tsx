import type { Register } from 'claude-code'

import { drawPane } from './pane'
import { clip, openQuestions, parseState, stepCount, stuckItems, verifiedCount } from './view'
import type { BoardState } from './view'

// The band above the prompt, the pane behind its button, and the toast for a new question.
// Everything shown comes from the board's `state.js`, which the Python hooks keep. mod_state.py finds the board
// and its state (and starts the board early); it runs at session start, when a turn completes, and at the first
// changed file or commit of a session with no board. Every other update re-reads `state.js` with $.fs.read, after
// a tool call that can have changed it and on a 30-second tick while a turn runs. Drawing runs no process.

export const PANE = 'logbook'
export const TAG = 'Logbook'
export const GAP = '  '
export const TICK_MS = 30_000
const HELPER = 'board/mod_state.py'
const FILE_TOOLS = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit']
const COMMITS = /\bgit\b[^\n;&|]*\bcommit\b/ // the gate's own test is stricter; a false hit costs one idempotent run
const TOAST = 70

type State = {
  transcriptPath: string
  cwd: string
  sessionId: string
  board: string | null
  state: BoardState | null
  seen: Set<string> | null // the open questions the last read showed; null until a read has set the baseline
  located: boolean
  startTried: boolean
  tick: { cancel: () => void } | null
}

export type Part = { key: string; text: string; color?: string; bold?: boolean }

// What needs eyes, and only that: parts with nothing to say are left out. Files, commands and agents are the pane's.
export function parts(state: BoardState, now: number): Part[] {
  const out: Part[] = []
  const open = openQuestions(state)
  if (open.some(q => q.hardStop)) out.push({ key: 'stop', text: 'Stopped', color: 'red', bold: true })
  const asked = open.filter(q => !q.hardStop).length
  if (asked > 0) out.push({ key: 'questions', text: `${asked} ${asked === 1 ? 'question' : 'questions'}`, color: 'yellow', bold: true })
  const stuck = stuckItems(state, now).length
  if (stuck > 0) out.push({ key: 'stuck', text: `${stuck} stuck`, color: 'red' })
  const steps = stepCount(state)
  if (steps.total > 0) out.push({ key: 'steps', text: `${steps.done}/${steps.total}`, color: steps.done === steps.total ? 'green' : undefined })
  const checks = verifiedCount(state)
  if (checks.pass > 0) out.push({ key: 'pass', text: `${checks.pass} pass`, color: 'green' })
  if (checks.fail > 0) out.push({ key: 'fail', text: `${checks.fail} fail`, color: 'red' })
  return out
}

// The question a toast announces: a hard stop if there is one, else the first new one.
export function toastText(fresh: BoardState['questions']): string {
  const first = fresh.find(q => q.hardStop) ?? fresh[0]
  const rest = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : ''
  return `Logbook: ${first.hardStop ? 'Stopped, ' : ''}${first.id} ${clip(first.text, TOAST)}${rest}`
}

function forget(s: State) {
  s.board = null
  s.state = null
  s.seen = null
  s.located = false
  s.startTried = false
}

// A new state: toast a question that was not open at the last read, then redraw. The first read of a session
// (`quiet`) only sets the baseline, so a resumed session does not toast the questions it already had.
function take($, s: State, state: BoardState | null, quiet: boolean) {
  s.state = state
  if (state) {
    const open = openQuestions(state)
    const before = s.seen ?? (quiet ? new Set(open.map(q => q.id)) : new Set<string>())
    const fresh = open.filter(q => !before.has(q.id))
    s.seen = new Set(open.map(q => q.id))
    if (fresh.length > 0) $.ui.toast(toastText(fresh))
  }
  $.ui.invalidate('ui.render')
}

async function run<T>(work: () => Promise<T>): Promise<T | undefined> {
  try {
    return await work()
  } catch {
    return undefined // a hook is never in the way: the band shows what it last had
  }
}

export async function locate($, s: State, opts: { start?: boolean; quiet?: boolean } = {}) {
  if (!s.sessionId || !s.cwd) return
  const args = ['--project', s.cwd, '--session', s.sessionId]
  if (opts.start && s.transcriptPath) args.push('--start', '--transcript', s.transcriptPath)
  const { stdout } = await $.process.run(
    ['sh', `${$.plugin.root}/hooks/run-python.sh`, `${$.plugin.root}/${HELPER}`, ...args],
    { timeoutMs: 20_000 },
  )
  s.located = true
  const found = stdout.trim() ? JSON.parse(stdout) : {}
  s.board = typeof found.board === 'string' ? found.board : null
  take($, s, s.board ? (found.state as BoardState) : null, opts.quiet === true)
}

export async function reread($, s: State) {
  if (!s.board) return
  const state = parseState(String(await $.fs.read(`${s.board}/state.js`).catch(() => '')))
  if (state) take($, s, state, false)
}

async function togglePane($) {
  const isOpen = (await $.ui.panes()).some(pane => pane.id === PANE)
  if (isOpen) await $.ui.close({ id: PANE })
  else await $.ui.open({ id: PANE, title: 'Logbook' })
}

export async function drawBand($, e, next, s: State) {
  const below = await next(e)
  if (e.props.hasSurvey || !s.state || s.state.state === 'finished') return below
  const { Box, Button, Text } = $.ui.resolve(e)
  const shown = parts(s.state, await $.clock.now())
  const row = (
    <Box key="logbook-band">
      <Text key="logbook-tag" bold color="white">{TAG}</Text>
      <Text>{GAP}</Text>
      {shown.flatMap((part, i) => [
        i > 0 ? <Text key={`${part.key}-sep`} dimColor>{' · '}</Text> : null,
        <Text key={`${part.key}-value`} color={part.color} bold={part.bold}>{part.text}</Text>,
      ])}
      <Text>{shown.length > 0 ? '   ' : ''}</Text>
      <Button key="logbook-open" label="Logbook" onPress={() => togglePane($)} />
    </Box>
  )
  return below ? (
    <Box flexDirection="column">
      {row}
      {below}
    </Box>
  ) : row
}

export async function drawPaneFor($, e, s: State) {
  const { Box, Text } = $.ui.resolve(e)
  if (!s.state) return <Box><Text dimColor>No logbook for this session yet.</Text></Box>
  return drawPane(
    $.ui.resolve(e), s.state, await $.clock.now(),
    id => $.prompt.fill({ text: `${id}: `, mode: 'replace' }),
    () => $.ui.close({ id: PANE }),
  )
}

function startTick($, s: State) {
  s.tick ??= $.clock.every(TICK_MS, () => void run(() => reread($, s)))
}

function stopTick(s: State) {
  s.tick?.cancel()
  s.tick = null
}

export const register: Register = on => {
  const s: State = {
    transcriptPath: '', cwd: '', sessionId: '', board: null, state: null, seen: null,
    located: false, startTried: false, tick: null,
  }

  const remember = (e: { transcript_path?: string; cwd?: string; session_id?: string }) => {
    if (e.session_id && e.session_id !== s.sessionId) {
      forget(s) // a /clear or a resume: another session, another board
      s.sessionId = e.session_id
    }
    s.transcriptPath = e.transcript_path || s.transcriptPath
    s.cwd = e.cwd || s.cwd
  }

  on('classic.SessionStart', async ($, e, next) => {
    remember(e)
    const result = await next(e)
    await run(() => locate($, s, { quiet: true }))
    return result
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    remember(e)
    const result = await next(e)
    startTick($, s)
    if (!s.located) await run(() => locate($, s, { quiet: true })) // a mod loaded mid-session has seen no SessionStart
    return result
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      stopTick(s)
      await run(() => locate($, s))
    }
    return result
  })

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    await run(async () => {
      const isMain = e.agentId === undefined
      const worked = result.deny === undefined && result.isError !== true
      const command = e.tool === 'Bash' ? String(e.command ?? '') : ''
      const changed = worked && (FILE_TOOLS.includes(e.tool) || COMMITS.test(command))
      const recorded = command.includes('board.py')
      if (s.board) {
        if (changed || recorded || e.tool === 'Bash') await reread($, s)
      } else if (isMain && changed && !s.startTried && s.transcriptPath) {
        s.startTried = true // once: the Python hooks start the board themselves at their thresholds
        await locate($, s, { start: true })
      } else if (recorded) {
        await locate($, s)
      }
    })
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => drawBand($, e, next, s))
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => drawPaneFor($, e, s))
}
