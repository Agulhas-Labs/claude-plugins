import type { Register } from 'claude-code'

// The band above the prompt: what is left of the prompt cache, and what this job has used.
// Every figure about the cache comes from status.py, which reads the transcript with the guard's own
// functions; it runs when a turn completes or a session starts, never while drawing. Between turns the
// band only counts down from the last reading, on a 30-second tick.

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  model?: string
}

type Status = {
  disabled: boolean
  show_cost: boolean
  last_turn_at: number | null
  lifetime_s: number | null
  context_tokens: number | null
  cold_usd: number | null
  warm_usd: number | null
  agents_usd: number | null
  agents_unpriced: number
}

type Watch = { path: string; pending: string; startedAt: number }

type State = {
  transcriptPath: string
  cwd: string
  sessionId: string
  status: Status | null
  lastHit: number | null
  startedAt: number | null
  newTokens: number
  agents: Usage[]
  watch: Watch | null
  tick: { cancel: () => void } | null
  poll: { cancel: () => void } | null
}

export const TAG_WIDTH = 13 // the tag's column: 'Cache-Guard' and a two-cell gap
const CELLS = 7
const TICK_MS = 30_000
const POLL_MS = 5_000
const POLL_CAP_MS = 6 * 60_000 // the summariser is killed at five minutes; a minute's margin

export function hitPercent(u: Usage): number | null {
  const whole = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
  return whole > 0 ? Math.round((100 * u.cache_read_input_tokens) / whole) : null
}

export function newTokensOf(u: Usage): number {
  return u.input_tokens + u.output_tokens + u.cache_creation_input_tokens
}

export function msLeft(status: Status | null, now: number): number | null {
  if (!status || status.last_turn_at === null || status.lifetime_s === null) return null
  return status.last_turn_at + status.lifetime_s * 1000 - now
}

function dollars(amount: number): string {
  return amount < 0.01 ? '<$0.01' : `$${amount.toFixed(2)}`
}

function tokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`
  if (count >= 1_000) return `${Math.round(count / 1_000)}k`
  return String(count)
}

export function compactLabel(status: Status | null, left: number | null): string {
  if (left === null || left > 0) return 'Compact'
  const cost = status?.show_cost && status.cold_usd !== null ? ` ~${dollars(status.cold_usd)}` : ''
  return `Compact (cold${cost})`
}

export type Hue = 'green' | 'yellow' | 'red'
export type Usage5 = { context?: { percent?: number }; rateLimits: { kind: string; percentUsed: number }[]; cost?: { usd: number } }

// Colour by meaning. These cut-offs are display choices, not measured values.
export const CACHE_GREEN_FROM = 0.5 // share of the cache lifetime still left
export const CACHE_YELLOW_FROM = 0.1
export const CACHED_GREEN_FROM = 80 // % of the last prompt served from cache
export const CACHED_YELLOW_FROM = 40
export const CONTEXT_GREEN_BELOW = 60 // % of the window in use
export const CONTEXT_RED_FROM = 80
export const LIMIT_GREEN_BELOW = 60 // % of the 5-hour limit used
export const LIMIT_RED_ABOVE = 85

export function cacheHue(left: number, lifetimeMs: number): Hue {
  const share = left / lifetimeMs
  if (left <= 0) return 'red'
  return share >= CACHE_GREEN_FROM ? 'green' : share >= CACHE_YELLOW_FROM ? 'yellow' : 'red'
}

export function cachedHue(percent: number): Hue {
  return percent >= CACHED_GREEN_FROM ? 'green' : percent >= CACHED_YELLOW_FROM ? 'yellow' : 'red'
}

export function contextHue(percent: number): Hue {
  return percent < CONTEXT_GREEN_BELOW ? 'green' : percent < CONTEXT_RED_FROM ? 'yellow' : 'red'
}

export function limitHue(percent: number): Hue {
  return percent < LIMIT_GREEN_BELOW ? 'green' : percent <= LIMIT_RED_ABOVE ? 'yellow' : 'red'
}

// One reading of the band: a dim label around a (possibly coloured) value.
export type Segment = { key: string; before?: string; value: string; after?: string; hue?: Hue }

export function topSegments(s: State, usage: Usage5, now: number): Segment[] {
  const out: Segment[] = []
  const left = msLeft(s.status, now)
  if (left !== null && s.status?.lifetime_s) {
    const lifetimeMs = s.status.lifetime_s * 1000
    if (left > 0) {
      const filled = Math.ceil((CELLS * left) / lifetimeMs)
      out.push({
        key: 'cache', before: 'cache ', value: `${Math.ceil(left / 60_000)} min left`,
        after: ` ${'▪'.repeat(filled)}${'▫'.repeat(CELLS - filled)}`, hue: cacheHue(left, lifetimeMs),
      })
    } else {
      out.push({ key: 'cache', value: 'cache expired', hue: 'red' })
    }
  }
  if (s.lastHit !== null) {
    out.push({ key: 'cached', before: 'last prompt ', value: `${s.lastHit}% cached`, hue: cachedHue(s.lastHit) })
  }
  if (usage.context?.percent !== undefined) {
    out.push({ key: 'context', before: 'context ', value: `${usage.context.percent}% full`, hue: contextHue(usage.context.percent) })
  }
  const fiveHour = usage.rateLimits.find(r => r.kind === 'five_hour')
  if (fiveHour) {
    const used = Math.round(fiveHour.percentUsed)
    out.push({ key: 'limit', before: '5-hour limit ', value: `${used}% used`, hue: limitHue(used) })
  }
  return out
}

export function costSegments(s: State, usage: Usage5): Segment[] {
  const out: Segment[] = []
  if (usage.cost && s.status?.show_cost !== false) {
    const agents = s.status?.agents_usd ? ` (agents ~${dollars(s.status.agents_usd)} est.)` : ''
    out.push({ key: 'session', before: 'session ', value: `${dollars(usage.cost.usd)}${agents}` })
  }
  if (s.newTokens > 0) out.push({ key: 'tokens', value: tokens(s.newTokens), after: ' new tokens' })
  return out
}

// The legend the [?] button shows in a pane, one entry per figure or button the band draws. `key` is the
// band's own segment key (or button name), so a test can check none is missing; the README table repeats it.
export const PANE = 'cache-guard-legend'
export const LEGEND: { key: string; label: string; hue?: Hue | 'cyan'; text: string }[] = [
  { key: 'cache', label: 'cache N min left', hue: 'green', text: 'Minutes before the prompt cache expires. After that the next message re-sends the whole context at full price. The colour turns yellow, then red, as it runs down.' },
  { key: 'cached', label: 'last prompt N% cached', hue: 'green', text: 'The share of the last prompt read from cache. High is good, and cheap; low right after the cache expires is normal.' },
  { key: 'context', label: 'context N% full', hue: 'green', text: 'The share of the model\'s window in use. Compaction gets more pressing as it climbs.' },
  { key: 'limit', label: '5-hour limit N% used', hue: 'green', text: 'The share of the rolling usage limit used.' },
  { key: 'session', label: 'session $', text: 'Cost so far, including subagents. The agents\' share is estimated at list prices.' },
  { key: 'tokens', label: 'new tokens', text: 'Fresh input, output and cache writes. Cache reads are excluded.' },
  { key: 'compact', label: '[ Compact ]', hue: 'cyan', text: 'Summarises the conversation. When the cache is cold it first pays for the whole context, shown as its cold cost.' },
  { key: 'handoff', label: '[ Handoff ]', hue: 'cyan', text: 'Starts the cheap background summary so you can /clear. It is the emphasised choice when the cache is cold.' },
]

// [?] toggles the legend pane.
export async function toggleLegend($) {
  if ((await $.ui.panes()).some(pane => pane.id === PANE)) await $.ui.close({ id: PANE })
  else await $.ui.open({ id: PANE, title: 'Cache-Guard: what the band shows' })
}

export function drawLegend($, e) {
  const { Box, Button, Text } = $.ui.resolve(e)
  return (
    <Box flexDirection="column">
      {LEGEND.map(entry => (
        <Box key={entry.key} flexDirection="column" marginBottom={1}>
          <Text bold color={entry.hue}>{entry.label}</Text>
          <Text>{entry.text}</Text>
        </Box>
      ))}
      <Button key="cache-guard-legend-close" role="dismiss" label="Close" onPress={() => $.ui.close({ id: PANE })} />
    </Box>
  )
}

export async function refresh($, s: State) {
  const { stdout } = await $.process.run(
    ['sh', `${$.plugin.root}/hooks/run-python.sh`, `${$.plugin.root}/hooks/status.py`],
    { stdin: JSON.stringify({ transcript_path: s.transcriptPath, agents: s.agents }), timeoutMs: 10_000 },
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
    s.newTokens = 0
    s.agents = []
    s.lastHit = null
  }
}

export async function startHandoff($, s: State) {
  if (s.watch) {
    $.ui.toast(`Handoff still writing: ${s.watch.path}`)
    return
  }
  if (!s.transcriptPath) {
    $.ui.toast('No transcript yet: nothing to hand off')
    return
  }
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
    $.ui.toast(`Handoff ready - /clear is safe: ${result.path}`, { timeoutMs: 15_000 })
    return
  }
  $.ui.toast('Handoff writing...')
  s.watch = { path: result.path, pending: result.pending_text, startedAt: await $.clock.now() }
  s.poll = $.clock.every(POLL_MS, () => pollHandoff($, s))
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
  $.ui.toast(
    landed
      ? `Handoff ready - /clear is safe: ${watch.path}`
      : `Handoff summary not in after 6 minutes; the script-written file is complete: ${watch.path}`,
    { timeoutMs: 15_000 },
  )
  $.ui.invalidate('ui.render')
}

export async function compactNow($) {
  try {
    await $.session.compact()
  } catch (failure) {
    $.ui.toast(`Compact did not run: ${String(failure)}`)
  }
}

export async function drawBand($, e, next, s: State) {
  const below = await next(e)
  if (e.props.hasSurvey || !s.status || s.status.disabled || s.status.last_turn_at === null) return below
  const now = await $.clock.now()
  const left = msLeft(s.status, now)
  const cold = left !== null && left <= 0
  const { Box, Button, Text } = $.ui.resolve(e)
  const usage = await $.session.usage()
  const part = (seg: Segment) => [
    seg.before ? <Text key={`${seg.key}-label`} dimColor>{seg.before}</Text> : null,
    <Text key={`${seg.key}-value`} color={seg.hue}>{seg.value}</Text>,
    seg.after ? <Text key={`${seg.key}-after`} color={seg.hue} dimColor={seg.hue === undefined}>{seg.after}</Text> : null,
  ]
  const joined = (segs: Segment[]) =>
    segs.flatMap((seg, i) => [i > 0 ? <Text key={`${seg.key}-sep`} dimColor>{' · '}</Text> : null, ...part(seg)])
  const top = topSegments(s, usage, now)
  const bottom = costSegments(s, usage)
  const row = (
    <Box key="cache-guard-band" flexDirection="column">
      <Box>
        <Box key="cache-guard-tag-column" width={TAG_WIDTH} flexShrink={0}>
          <Text key="cache-guard-tag" bold color="white">Cache-Guard</Text>
        </Box>
        {joined(top)}
      </Box>
      <Box>
        <Box key="cache-guard-row2-column" width={TAG_WIDTH} flexShrink={0} />
        {joined(bottom)}
        <Text>{bottom.length > 0 ? '   ' : ''}</Text>
        {cold ? <Text key="cache-guard-cold" color="yellow" bold>{'! '}</Text> : null}
        <Button key="cache-guard-compact" label={compactLabel(s.status, left)} onPress={() => compactNow($)} />
        <Text> </Text>
        <Button
          key="cache-guard-handoff"
          label={s.watch ? 'Handoff writing...' : 'Handoff'}
          variant={cold ? 'primary' : undefined}
          onPress={() => startHandoff($, s)}
        />
        <Text> </Text>
        <Button key="cache-guard-legend" label="?" dimColor onPress={() => toggleLegend($)} />
      </Box>
    </Box>
  )
  return below ? (
    <Box flexDirection="column">
      {row}
      {below}
    </Box>
  ) : row
}

export const register: Register = on => {
  const s: State = {
    transcriptPath: '', cwd: '', sessionId: '', status: null, lastHit: null, startedAt: null,
    newTokens: 0, agents: [], watch: null, tick: null, poll: null,
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
    if (e.usage) {
      s.newTokens += newTokensOf(e.usage)
      if (e.agentId === undefined) s.lastHit = hitPercent(e.usage)
      else s.agents.push(e.usage)
    }
    if (s.transcriptPath) await refresh($, s)
    if (e.agentId === undefined && s.status?.last_turn_at != null) {
      // the transcript may not hold this turn's last entry yet; the cache was used just now either way
      s.status.last_turn_at = Math.max(s.status.last_turn_at, await $.clock.now())
    }
    return result
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => drawLegend($, e))
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => drawBand($, e, next, s))
}
