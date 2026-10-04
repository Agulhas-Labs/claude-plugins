import type { Register } from 'claude-code'

// The band above the prompt: what is left of the prompt cache, how big the context is, and what a miss
// would cost.
// Every figure about the cache comes from status.py, which reads the transcript with the guard's own
// functions; it runs when a turn completes or a session starts, never while drawing. Between turns the
// band only counts down from the last reading, on a 30-second tick.

type Status = {
  disabled: boolean
  show_cost: boolean
  last_turn_at: number | null
  lifetime_s: number | null
  context_tokens: number | null
  cold_usd: number | null
  warm_usd: number | null
}

type Watch = { path: string; pending: string; startedAt: number }

// A finished handoff the band tells you how to pick up, until you dismiss it or /clear.
type Saved = { path: string; partial: boolean }

type State = {
  transcriptPath: string
  cwd: string
  sessionId: string
  status: Status | null
  startedAt: number | null
  watch: Watch | null
  saved: Saved | null
  frame: number
  tick: { cancel: () => void } | null
  poll: { cancel: () => void } | null
  spin: { cancel: () => void } | null
}

export const TAG = 'Cache-Guard' // the band's one row starts with the tag, then the gap
export const GAP = '  '
const TICK_MS = 30_000
const POLL_MS = 5_000
const SPIN_MS = 120
export const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const POLL_CAP_MS = 6 * 60_000 // the summariser is killed at five minutes; a minute's margin

export function msLeft(status: Status | null, now: number): number | null {
  if (!status || status.last_turn_at === null || status.lifetime_s === null) return null
  return status.last_turn_at + status.lifetime_s * 1000 - now
}

function dollars(amount: number): string {
  return amount < 0.01 ? '<$0.01' : `$${amount.toFixed(2)}`
}

export function tokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`
  if (count >= 1_000) return `${Math.round(count / 1_000)}K`
  return String(count)
}

export type Hue = 'green' | 'yellow' | 'red'
export type Usage5 = { context?: { percent?: number }; rateLimits: { kind: string; percentUsed: number }[]; cost?: { usd: number } }

// Colour by meaning. These cut-offs are display choices, not measured values.
export const CACHE_GREEN_FROM = 0.5 // share of the cache lifetime still left
export const CACHE_YELLOW_FROM = 0.1
export const CONTEXT_GREEN_BELOW = 60 // % of the window in use
export const CONTEXT_RED_FROM = 80
export const LIMIT_GREEN_BELOW = 60 // % of the 5-hour limit used
export const LIMIT_RED_ABOVE = 85

export function cacheHue(left: number, lifetimeMs: number): Hue {
  const share = left / lifetimeMs
  if (left <= 0) return 'red'
  return share >= CACHE_GREEN_FROM ? 'green' : share >= CACHE_YELLOW_FROM ? 'yellow' : 'red'
}

export function contextHue(percent: number): Hue {
  return percent < CONTEXT_GREEN_BELOW ? 'green' : percent < CONTEXT_RED_FROM ? 'yellow' : 'red'
}

export function limitHue(percent: number): Hue {
  return percent < LIMIT_GREEN_BELOW ? 'green' : percent <= LIMIT_RED_ABOVE ? 'yellow' : 'red'
}

// One reading of the band: a dim label around a (possibly coloured) value.
export type Segment = { key: string; before?: string; value: string; after?: string; hue?: Hue }

export function bandSegments(s: State, usage: Usage5, now: number): Segment[] {
  const out: Segment[] = []
  const left = msLeft(s.status, now)
  if (left !== null && s.status?.lifetime_s) {
    if (left > 0) {
      const minutes = Math.ceil(left / 60_000)
      out.push({
        key: 'cache', before: 'Cache ', value: `${minutes} ${minutes === 1 ? 'min' : 'mins'} left`,
        hue: cacheHue(left, s.status.lifetime_s * 1000),
      })
    } else {
      out.push({ key: 'cache', value: 'Cache expired', hue: 'red' })
    }
  }
  if (s.status?.context_tokens) {
    const percent = usage.context?.percent
    out.push({
      key: 'tokens', before: 'Tokens ', value: tokens(s.status.context_tokens),
      after: percent === undefined ? undefined : ` (${percent}%)`, hue: percent === undefined ? undefined : contextHue(percent),
    })
  }
  if (s.status?.show_cost !== false && s.status?.cold_usd != null) {
    out.push({ key: 'miss', before: 'Miss cost ', value: dollars(s.status.cold_usd), hue: left !== null && left <= 0 ? 'yellow' : undefined })
  }
  const fiveHour = usage.rateLimits.find(r => r.kind === 'five_hour')
  if (fiveHour) {
    const used = Math.round(fiveHour.percentUsed)
    out.push({ key: 'limit', before: '5h ', value: `${used}%`, hue: limitHue(used) })
  }
  return out
}

export async function refresh($, s: State) {
  const { stdout } = await $.process.run(
    ['sh', `${$.plugin.root}/hooks/run-python.sh`, `${$.plugin.root}/hooks/status.py`],
    { stdin: JSON.stringify({ transcript_path: s.transcriptPath }), timeoutMs: 10_000 },
  )
  s.status = stdout.trim() ? (JSON.parse(stdout) as Status) : null
  const left = msLeft(s.status, await $.clock.now())
  if (left !== null && left > 0 && !s.tick) {
    s.tick = $.clock.every(TICK_MS, () => tick($, s))
  }
  $.ui.invalidate('ui.render')
}

export async function tick($, s: State) {
  const left = msLeft(s.status, await $.clock.now())
  if ((left === null || left <= 0) && s.tick) {
    s.tick.cancel() // cold: nothing changes until the next turn
    s.tick = null
  }
  $.ui.invalidate('ui.render')
}

export async function resetOnClear($, s: State) {
  const { startedAt } = await $.session.usage()
  if (s.startedAt !== startedAt) {
    s.startedAt = startedAt
    s.saved = null
  }
}

function startSpinner($, s: State) {
  s.spin ??= $.clock.every(SPIN_MS, () => {
    s.frame = (s.frame + 1) % SPINNER.length
    $.ui.invalidate('ui.render')
  })
}

function stopSpinner(s: State) {
  s.spin?.cancel()
  s.spin = null
}

export function resumeHint(path: string): string {
  return `To continue: /clear, then press Resume above the prompt, or send: Read ${path} and continue from it.`
}

export async function startHandoff($, s: State) {
  if (s.watch) return // already writing: the button reads "Writing handoff" and the spinner is turning
  if (!s.transcriptPath) {
    $.ui.toast('No transcript yet: nothing to hand off')
    return
  }
  s.saved = null
  const { stdout } = await $.process.run(
    ['sh', `${$.plugin.root}/hooks/run-python.sh`, `${$.plugin.root}/hooks/handoff.py`, '--write'],
    {
      stdin: JSON.stringify({ transcript_path: s.transcriptPath, cwd: s.cwd, session_id: s.sessionId }),
      timeoutMs: 30_000,
    },
  )
  const result = stdout.trim() ? JSON.parse(stdout) : { error: 'Python 3 was not found' }
  if (result.error) {
    $.ui.toast(`Handoff failed: ${result.error}`)
    return
  }
  if (!result.summary_model) {
    s.saved = { path: result.path, partial: false }
    $.ui.invalidate('ui.render')
    return
  }
  s.watch = { path: result.path, pending: result.pending_text, startedAt: await $.clock.now() }
  s.poll = $.clock.every(POLL_MS, () => pollHandoff($, s))
  startSpinner($, s)
  $.ui.invalidate('ui.render')
}

export async function pollHandoff($, s: State) {
  const watch = s.watch
  if (!watch) return
  const text = String(await $.fs.read(watch.path).catch(() => ''))
  const landed = text !== '' && !text.includes(watch.pending)
  const late = (await $.clock.now()) - watch.startedAt > POLL_CAP_MS
  if (!landed && !late) return
  s.poll?.cancel()
  s.poll = null
  s.watch = null
  stopSpinner(s)
  s.saved = { path: watch.path, partial: !landed }
  $.ui.invalidate('ui.render')
}

// What the person sees once a handoff is on disk: that it is safe to /clear, where it is, and how to pick it up.
function drawSaved($, e, s: State, saved: Saved) {
  const { Box, Button, Text } = $.ui.resolve(e)
  return (
    <Box key="cache-guard-saved" flexDirection="column" borderStyle="round" borderColor="green" paddingX={1}>
      <Box>
        <Text bold color="green">{saved.partial ? 'Handoff saved' : 'Handoff ready'}</Text>
        <Text dimColor>{saved.partial ? ' (the summary did not finish; the file is complete without it)' : ' - /clear is safe'}</Text>
      </Box>
      <Text>{saved.path}</Text>
      <Text dimColor>{resumeHint(saved.path)}</Text>
      <Button
        key="cache-guard-saved-dismiss" label="Dismiss" role="dismiss"
        onPress={() => { s.saved = null; $.ui.invalidate('ui.render') }}
      />
    </Box>
  )
}

export async function drawBand($, e, next, s: State) {
  const below = await next(e)
  if (e.props.hasSurvey || !s.status || s.status.disabled || s.status.last_turn_at === null) return below
  const now = await $.clock.now()
  const { Box, Button, Text } = $.ui.resolve(e)
  const usage = await $.session.usage()
  const part = (seg: Segment) => [
    seg.before ? <Text key={`${seg.key}-label`} dimColor>{seg.before}</Text> : null,
    <Text key={`${seg.key}-value`} color={seg.hue}>{seg.value}</Text>,
    seg.after ? <Text key={`${seg.key}-after`} color={seg.hue} dimColor={seg.hue === undefined}>{seg.after}</Text> : null,
  ]
  const segs = bandSegments(s, usage, now)
  const row = (
    <Box key="cache-guard-band">
      <Text key="cache-guard-tag" bold color="white">{TAG}</Text>
      <Text>{GAP}</Text>
      {segs.flatMap((seg, i) => [i > 0 ? <Text key={`${seg.key}-sep`} dimColor>{' · '}</Text> : null, ...part(seg)])}
      <Text>{segs.length > 0 ? '   ' : ''}</Text>
      <Button
        key="cache-guard-handoff"
        label={s.watch ? `Writing handoff ${SPINNER[s.frame]}` : 'Handoff'}
        hotkey="h"
        onPress={() => startHandoff($, s)}
      />
    </Box>
  )
  const above = s.saved ? drawSaved($, e, s, s.saved) : null
  return above || below ? (
    <Box flexDirection="column">
      {above}
      {row}
      {below}
    </Box>
  ) : row
}

export const register: Register = on => {
  const s: State = {
    transcriptPath: '', cwd: '', sessionId: '', status: null, startedAt: null,
    watch: null, saved: null, frame: 0, tick: null, poll: null, spin: null,
  }

  const remember = (e: { transcript_path?: string; cwd?: string; session_id?: string }) => {
    s.transcriptPath = e.transcript_path || s.transcriptPath
    s.cwd = e.cwd || s.cwd
    s.sessionId = e.session_id || s.sessionId
  }

  on('classic.SessionStart', async ($, e, next) => {
    remember(e)
    const result = await next(e)
    await resetOnClear($, s)
    await refresh($, s)
    return result
  })

  on('classic.UserPromptSubmit', ($, e, next) => {
    remember(e)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    await resetOnClear($, s)
    if (s.transcriptPath) await refresh($, s)
    if (e.agentId === undefined && s.status?.last_turn_at != null) {
      // the transcript may not hold this turn's last entry yet; the cache was used just now either way
      s.status.last_turn_at = Math.max(s.status.last_turn_at, await $.clock.now())
    }
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => drawBand($, e, next, s))
}
