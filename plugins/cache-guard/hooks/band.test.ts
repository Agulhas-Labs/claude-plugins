import { expect, mock, test } from 'claude-code/testing'
import { LEGEND, PANE, cacheHue, cachedHue, contextHue, costSegments, limitHue, topSegments } from './band.tsx'

const NOW = 1_800_000_000_000
const usage = (input: number, read: number, write: number, output = 100, model = 'claude-opus-5') =>
  ({ input_tokens: input, output_tokens: output, cache_read_input_tokens: read, cache_creation_input_tokens: write, model })

const status = (over: Record<string, unknown> = {}) => ({
  disabled: false, show_cost: true, last_turn_at: NOW, lifetime_s: 3600, context_tokens: 200_000,
  cold_usd: 2, warm_usd: 0.1, agents_usd: null, agents_unpriced: 0, ...over,
})

// The engine beneath the plugin: a session whose usage, status script and handoff script the test sets.
function engine(on, world: { status: Record<string, unknown>; startedAt?: number; handoff?: Record<string, unknown>; below?: boolean }) {
  const seen = { runs: [] as string[][], stdin: [] as string[], toasts: [] as string[], timeouts: [] as unknown[], compacts: 0, files: {} as Record<string, string> }
  on('process.run', (_$, e) => {
    seen.runs.push([...e.argv])
    seen.stdin.push(e.init?.stdin ?? '')
    const out = e.argv.includes('--write') ? world.handoff : world.status
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.usage', () => ({ value: { startedAt: world.startedAt ?? 1, context: { window: 1, percent: 61 }, rateLimits: [{ kind: 'five_hour', percentUsed: 34 }], cost: { usd: 2.51 } } }))
  on('ui.toast', (_$, e) => void (seen.toasts.push(e.text), seen.timeouts.push(e.timeoutMs)))
  on('session.compact', () => { seen.compacts += 1; return { messages: [] } })
  on('fs.read', (_$, e) => ({ value: seen.files[e.path] ?? '' }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('classic.SessionStart', () => ({}))
  on('classic.UserPromptSubmit', () => ({}))
  if (!world.below) on('ui.render', (_$, e) => h(_$.ui.resolve(e).Box, { key: 'engine' }))
  return seen
}

const turn = (u: unknown, agentId?: string) =>
  ({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', agentId, reason: 'answer', usage: u }) as never

async function started($) {
  await $.classic.UserPromptSubmit({ prompt: 'hi', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
}

const mountBand = $ =>
  $.ui.mount({ plugin: 'cache-guard', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false, bodyColumns: 120 } as never })

test('the band wraps the drawing beneath it', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status(), below: true })
  on('ui.render', (_$, e) => {
    const { Text } = _$.ui.resolve(e)
    return h(Text, { key: 'other-band' }, 'another plugin')
  })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect(JSON.stringify(await band.drawn())).toContain('another plugin')
  expect(await band.find({ key: 'cache-guard-compact' })).toBeDefined()
})

type Node = { type: string; props?: Record<string, unknown>; children?: (Node | string)[] }
const texts = (n: Node | string, out: { text: string; props: Record<string, unknown> }[] = []) => {
  if (typeof n === 'string') return out
  if (n.type === 'Text') out.push({ text: (n.children ?? []).join(''), props: n.props ?? {} })
  for (const c of n.children ?? []) texts(c, out)
  return out
}
// the Text drawn for a reading: the one whose text is the value
const textOf = async (band, text: string) => texts((await band.drawn()) as Node).find(t => t.text === text)

test('the band reads in plain words under a Cache-Guard tag', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status({ agents_usd: 0.04 }) })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  await $.turn.complete(turn(usage(500, 0, 500), 'a1')) // a subagent's turn is not the last main turn
  const band = await mountBand($)
  const drawn = JSON.stringify(await band.drawn())
  expect(drawn).toContain('Cache-Guard')
  expect((await textOf(band, 'Cache-Guard'))?.props.bold).toBe(true)
  expect((await textOf(band, '60 min left'))?.props.color).toBe('green')
  expect((await textOf(band, '98% cached'))?.props.color).toBe('green')
  expect((await textOf(band, '61% full'))?.props.color).toBe('yellow')
  expect((await textOf(band, '34% used'))?.props.color).toBe('green')
  expect((await textOf(band, '$2.51 (agents ~$0.04 est.)'))).toBeDefined()
  expect(drawn).toContain('last prompt ')
  expect(drawn).toContain('5-hour limit ')
})

test('colours follow the thresholds, just below and at each boundary', () => {
  const life = 3600_000
  expect(cacheHue(0.5 * life, life)).toBe('green')
  expect(cacheHue(0.5 * life - 1, life)).toBe('yellow')
  expect(cacheHue(0.1 * life, life)).toBe('yellow')
  expect(cacheHue(0.1 * life - 1, life)).toBe('red')
  expect(cacheHue(0, life)).toBe('red')
  expect(cachedHue(80)).toBe('green')
  expect(cachedHue(79)).toBe('yellow')
  expect(cachedHue(40)).toBe('yellow')
  expect(cachedHue(39)).toBe('red')
  expect(contextHue(59)).toBe('green')
  expect(contextHue(60)).toBe('yellow')
  expect(contextHue(79)).toBe('yellow')
  expect(contextHue(80)).toBe('red')
  expect(limitHue(59)).toBe('green')
  expect(limitHue(60)).toBe('yellow')
  expect(limitHue(85)).toBe('yellow')
  expect(limitHue(86)).toBe('red')
})

test('an expired cache reads cache expired in red', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status({ last_turn_at: NOW - 2 * 3600_000 }) })
  await started($)
  await $.classic.SessionStart({ source: 'resume', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  expect((await textOf(band, 'cache expired'))?.props.color).toBe('red')
})

test('[?] opens the legend pane, a second press closes it, and no toast is raised', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status() })
  const opened: unknown[] = []
  const closed: unknown[] = []
  let up = false
  on('ui.open', (_$, e) => { opened.push(e); up = true; return { value: { isPlaced: true } } })
  on('ui.close', (_$, e) => { closed.push(e); up = false; return { value: undefined } })
  on('ui.panes', () => ({ value: up ? [{ id: PANE, title: 't', isShown: true, hasFocus: false, isPlaced: true }] : [] }))
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-legend' })
  expect(opened).toHaveLength(1)
  expect((opened[0] as { id: string; title: string }).id).toBe('cache-guard-legend')
  expect((opened[0] as { title: string }).title).toBe('Cache-Guard: what the band shows')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-legend' })
  expect(closed).toHaveLength(1)
  expect(seen.toasts).toEqual([])
})

test('the legend pane holds every legend entry, and the Close button closes it', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status() })
  const closed: unknown[] = []
  on('ui.close', (_$, e) => { closed.push(e); return { value: undefined } })
  const pane = await $.ui.mount({ plugin: 'cache-guard', surface: 'terminal', component: 'Pane', requestId: PANE, props: {} as never })
  const drawn = JSON.stringify(await pane.drawn())
  for (const entry of LEGEND) {
    expect(drawn).toContain(entry.label)
    expect(drawn).toContain(entry.text)
  }
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-legend-close' })
  expect(closed).toHaveLength(1)
})

test('every figure the band draws has a legend entry', () => {
  const s = {
    status: { ...status(), agents_usd: 0.04 }, lastHit: 90, newTokens: 5, agents: [], transcriptPath: '', cwd: '', sessionId: '',
    startedAt: null, watch: null, tick: null, poll: null,
  } as never
  const usage5 = { context: { percent: 50 }, rateLimits: [{ kind: 'five_hour', percentUsed: 10 }], cost: { usd: 1 } }
  const keys = [...topSegments(s, usage5, NOW), ...costSegments(s, usage5)].map(seg => seg.key)
  expect(keys.length).toBe(6)
  for (const key of [...keys, 'compact', 'handoff']) expect(LEGEND.map(l => l.key)).toContain(key)
})

test('job totals count main and subagents and reset when startedAt changes', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { status: status(), startedAt: 1 }
  const seen = engine(on, world)
  await started($)
  await $.turn.complete(turn(usage(1000, 5000, 2000, 1000)))
  await $.turn.complete(turn(usage(100_000, 0, 0, 0, 'claude-haiku-4-5'), 'a1'))
  const band = await mountBand($)
  expect((await textOf(band, '104k'))).toBeDefined()
  expect(JSON.parse(seen.stdin.at(-1)!).agents).toHaveLength(1)
  world.startedAt = 2 // a /clear
  await $.turn.complete(turn(usage(1000, 0, 0, 0)))
  expect((await textOf(band, '1k'))).toBeDefined()
  expect(JSON.parse(seen.stdin.at(-1)!).agents).toHaveLength(0)
})

test('Compact compacts, and names the cold cost only when the cache is cold', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status({ last_turn_at: NOW - 30 * 60_000 }) })
  await started($)
  await $.classic.SessionStart({ source: 'resume', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  expect((await band.find({ key: 'cache-guard-compact' }))?.props.label).toBe('Compact')
  await clock.advance(31 * 60_000)
  expect((await band.find({ key: 'cache-guard-compact' }))?.props.label).toBe('Compact (cold ~$2.00)')
  expect((await band.find({ key: 'cache-guard-handoff' }))?.props.variant).toBe('primary')
  expect((await textOf(band, '! '))?.props.color).toBe('yellow')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-compact' })
  expect(seen.compacts).toBe(1)
})

test('Handoff starts the script and says when the summary lands', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const pending = 'Summary: being written'
  const seen = engine(on, { status: status(), handoff: { path: '/w/.claude/handoffs/x.md', summary_model: 'haiku', pending_text: pending } })
  seen.files['/w/.claude/handoffs/x.md'] = `# Handoff\n${pending} by haiku.\n`
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  const run = seen.runs.at(-1)!
  expect(run[0]).toBe('sh')
  expect(run.at(-2)).toEndWith('/hooks/handoff.py')
  expect(run.at(-1)).toBe('--write')
  expect(JSON.parse(seen.stdin.at(-1)!)).toEqual({ transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' })
  expect(seen.toasts).toEqual(['Handoff writing...'])
  await clock.advance(10_000)
  expect(seen.toasts).toHaveLength(1)
  seen.files['/w/.claude/handoffs/x.md'] = '# Handoff\nSummary written by haiku.\n'
  await clock.advance(5_000)
  expect(seen.toasts.at(-1)).toBe('Handoff ready - /clear is safe: /w/.claude/handoffs/x.md')
  await clock.advance(60_000)
  expect(seen.toasts).toHaveLength(2) // the poll stopped once it landed
})

test('drawing runs no process', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status() })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const runs = seen.runs.length
  await mountBand($)
  await clock.advance(5 * 60_000) // ten ticks redraw the countdown
  expect(seen.runs.length).toBe(runs)
})
