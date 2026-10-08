import { expect, mock, test } from 'claude-code/testing'
import { SPINNER, bandSegments, cacheHue, contextHue, resumeHint, tokens } from './band.tsx'

const NOW = 1_800_000_000_000
const usage = (input: number, read: number, write: number, output = 100, model = 'claude-opus-5') =>
  ({ input_tokens: input, output_tokens: output, cache_read_input_tokens: read, cache_creation_input_tokens: write, model })

const status = (over: Record<string, unknown> = {}) => ({
  disabled: false, show_cost: true, last_turn_at: NOW, lifetime_s: 3600, context_tokens: 109_000,
  min_tokens: 100_000, cold_usd: 3.34, warm_usd: 0.1, ...over,
})

// The engine beneath the plugin: a session whose usage, status script and handoff script the test sets.
function engine(on, world: { status: Record<string, unknown>; startedAt?: number; sessionId?: string; handoff?: Record<string, unknown>; below?: boolean; hold?: Promise<void>; entered?: () => void }) {
  const seen = { runs: [] as string[][], stdin: [] as string[], toasts: [] as string[], timeouts: [] as unknown[], files: {} as Record<string, string> }
  on('process.run', async (_$, e) => {
    world.entered?.()
    if (world.hold) await world.hold // a test holds the script's answer back to land it later
    seen.runs.push([...e.argv])
    seen.stdin.push(e.init?.stdin ?? '')
    const out = e.argv.includes('--write') ? world.handoff : world.status
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.usage', () => ({ value: { startedAt: world.startedAt ?? 1, context: { window: 1, percent: 61 } } }))
  on('session.id', () => ({ value: world.sessionId ?? 's' }))
  on('session.cwd', () => ({ value: '/w' }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  on('ui.toast', (_$, e) => (seen.toasts.push(e.text), seen.timeouts.push(e.timeoutMs), { value: {} }))
  on('fs.read', (_$, e) => ({ value: seen.files[e.path] ?? '' }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('tool.call', () => ({ result: {}, text: '' }))
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
  expect(await band.find({ key: 'cache-guard-handoff' })).toBeDefined()
})

type Node = { type: string; props?: Record<string, unknown>; children?: (Node | string)[] }
const texts = (n: Node | string, out: { text: string; props: Record<string, unknown> }[] = []) => {
  if (typeof n === 'string') return out
  if (n.type === 'Text') out.push({ text: (n.children ?? []).join(''), props: n.props ?? {} })
  for (const c of n.children ?? []) texts(c, out)
  return out
}
// the row as the terminal shows it: every Text and Button label, in order
const rowText = (n: Node | string): string =>
  typeof n === 'string' ? n
    : n.type === 'Button' ? String(n.props?.label ?? '')
    : (n.children ?? []).map(rowText).join('')
// the Text drawn for a reading: the one whose text is the value
const textOf = async (band, text: string) => texts((await band.drawn()) as Node).find(t => t.text === text)

test('the band is one row of plain labels under a Cache-Guard tag, Handoff last', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status() })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  const drawn = JSON.stringify(await band.drawn())
  expect((await textOf(band, 'Cache-Guard'))?.props.bold).toBe(true)
  expect((await textOf(band, 'Cache-Guard'))?.props.color).toBe('white')
  expect((await textOf(band, '60 mins left'))?.props.color).toBe('green')
  expect((await textOf(band, '109K'))).toBeDefined()
  expect((await textOf(band, ' (61%)'))?.props.color).toBe('yellow')
  expect((await textOf(band, '$3.34'))).toBeDefined()
  expect(rowText(await band.drawn())).toBe('Cache-Guard  Cache 60 mins left · Tokens 109K (61%) · Miss cost $3.34   Handoff')
  expect(drawn).not.toContain('Compact')
  expect(drawn).not.toMatch(/[▪▫█░▓▒]/)
})

test('one minute left is singular, and the miss cost is left out when cost display is off', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status({ last_turn_at: NOW - 3600_000 + 30_000, show_cost: false }) })
  await started($)
  await $.classic.SessionStart({ source: 'resume', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Cache-Guard  Cache 1 min left · Tokens 109K (61%)   Handoff')
})

test('colours follow the thresholds, just below and at each boundary', () => {
  const life = 3600_000
  expect(cacheHue(0.5 * life, life)).toBe('green')
  expect(cacheHue(0.5 * life - 1, life)).toBe('yellow')
  expect(cacheHue(0.1 * life, life)).toBe('yellow')
  expect(cacheHue(0.1 * life - 1, life)).toBe('red')
  expect(cacheHue(0, life)).toBe('red')
  expect(contextHue(59)).toBe('green')
  expect(contextHue(60)).toBe('yellow')
  expect(contextHue(79)).toBe('yellow')
  expect(contextHue(80)).toBe('red')
})

test('token counts read 109K and 1.2M', () => {
  expect(tokens(950)).toBe('950')
  expect(tokens(109_400)).toBe('109K')
  expect(tokens(1_200_000)).toBe('1.2M')
})

test('an expired cache reads Cache expired in red, with the miss cost the next message pays', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  engine(on, { status: status({ last_turn_at: NOW - 30 * 60_000 }) })
  await started($)
  await $.classic.SessionStart({ source: 'resume', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  await clock.advance(31 * 60_000)
  expect((await textOf(band, 'Cache expired'))?.props.color).toBe('red')
  expect((await textOf(band, '$3.34'))?.props.color).toBe('yellow')
  expect(rowText(await band.drawn())).toBe('Cache-Guard  Cache expired · Tokens 109K (61%) · Miss cost $3.34   Handoff')
})

test('an expired cache under the floor reads not held in yellow, with the floor status.py reported', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  // the floor is whatever status.py read from CACHE_GUARD_MIN_TOKENS, never a number of the band's own
  engine(on, { status: status({ last_turn_at: NOW - 30 * 60_000, context_tokens: 62_000, min_tokens: 75_000 }) })
  await started($)
  await $.classic.SessionStart({ source: 'resume', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  await clock.advance(31 * 60_000)
  expect((await textOf(band, 'Cache expired'))?.props.color).toBe('yellow')
  expect((await textOf(band, ' (under 75K, not held)'))?.props.color).toBe('yellow')
  expect((await textOf(band, '$3.34'))?.props.color).toBe('yellow')
  expect(rowText(await band.drawn())).toBe('Cache-Guard  Cache expired (under 75K, not held) · Tokens 62K (61%) · Miss cost $3.34   Handoff')
})

test('a context at the floor is held, so its expired cache stays red; with no floor reported it stays red too', () => {
  const expired = (over: Record<string, unknown>) =>
    bandSegments({ status: status({ last_turn_at: NOW - 3600_000, ...over }) } as never, {}, NOW).find(seg => seg.key === 'cache')
  expect(expired({ context_tokens: 100_000, min_tokens: 100_000 })).toEqual({ key: 'cache', value: 'Cache expired', hue: 'red' })
  expect(expired({ context_tokens: 99_999, min_tokens: 100_000 })?.after).toBe(' (under 100K, not held)')
  expect(expired({ context_tokens: 62_000, min_tokens: null })?.hue).toBe('red')
})

test('there is no Compact button and no [?] legend', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status() })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect(await band.find({ key: 'cache-guard-compact' })).toBeUndefined()
  expect(await band.find({ key: 'cache-guard-legend' })).toBeUndefined()
})

const HANDOFF = { path: '/w/.claude/handoffs/x.md', summary_model: 'haiku', pending_text: 'Summary: being written' }

test('Handoff shows a turning spinner while it writes, then a box saying how to resume', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status(), handoff: HANDOFF })
  seen.files[HANDOFF.path] = `# Handoff\n${HANDOFF.pending_text} by haiku.\n`
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect((await band.find({ key: 'cache-guard-handoff' }))?.props.label).toBe('Handoff')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  const run = seen.runs.at(-1)!
  expect(run.at(-2)).toEndWith('/hooks/handoff.py')
  expect(run.at(-1)).toBe('--write')
  expect(JSON.parse(seen.stdin.at(-1)!)).toEqual({ transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' })
  const labelA = (await band.find({ key: 'cache-guard-handoff' }))?.props.label
  expect(labelA).toBe(`Writing handoff ${SPINNER[0]}`)
  await clock.advance(120)
  expect((await band.find({ key: 'cache-guard-handoff' }))?.props.label).toBe(`Writing handoff ${SPINNER[1]}`)
  expect(JSON.stringify(await band.drawn())).not.toContain('Handoff ready')
  expect(seen.toasts).toEqual([])
  seen.files[HANDOFF.path] = '# Handoff\nSummary written by haiku.\n'
  await clock.advance(5_000)
  expect((await band.find({ key: 'cache-guard-handoff' }))?.props.label).toBe('Handoff')
  const drawn = JSON.stringify(await band.drawn())
  expect(drawn).toContain('Handoff ready')
  expect(drawn).toContain(HANDOFF.path)
  expect(drawn).toContain(resumeHint(HANDOFF.path))
  expect(seen.toasts).toEqual([])
  const runs = seen.runs.length
  await clock.advance(60_000)
  expect(seen.runs.length).toBe(runs) // the poll and the spinner stopped
})

test('a handoff whose summary never lands is reported as saved without it', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status(), handoff: HANDOFF })
  seen.files[HANDOFF.path] = `# Handoff\n${HANDOFF.pending_text} by haiku.\n`
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  await clock.advance(6 * 60_000 + 10_000)
  const drawn = JSON.stringify(await band.drawn())
  expect(drawn).toContain('Handoff saved')
  expect(drawn).toContain('the summary did not finish')
})

test('Dismiss clears the box, and a /clear clears it too', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const world = { status: status(), startedAt: 1, handoff: { path: HANDOFF.path } }
  engine(on, world)
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' }) // no summariser: saved at once
  expect(JSON.stringify(await band.drawn())).toContain('Handoff ready')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-saved-dismiss' })
  expect(JSON.stringify(await band.drawn())).not.toContain('Handoff ready')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  expect(JSON.stringify(await band.drawn())).toContain('Handoff ready')
  world.startedAt = 2 // a /clear
  await $.turn.complete(turn(usage(10, 980, 10)))
  expect(JSON.stringify(await band.drawn())).not.toContain('Handoff ready')
  await clock.advance(1)
})

test('a failed handoff is a toast, not a box', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status(), handoff: { error: 'disk full' } })
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  expect(seen.toasts).toEqual(['Handoff failed: disk full'])
  expect(JSON.stringify(await band.drawn())).not.toContain('Handoff ready')
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

test('a tool call mid-turn re-reads the transcript, at most every 10 seconds, and starts the countdown', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const world = { status: status({ last_turn_at: null, lifetime_s: null, context_tokens: null, cold_usd: null }) as Record<string, unknown> }
  const seen = engine(on, world)
  await started($)
  await $.classic.SessionStart({ source: 'clear', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' } as never)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).not.toContain('Cache')
  world.status = status({ last_turn_at: NOW, context_tokens: 95_000 })
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(rowText(await band.drawn())).toContain('Tokens 95K')
  expect(rowText(await band.drawn())).toContain('Cache 60 mins left')
  const runs = seen.runs.length
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(seen.runs.length).toBe(runs) // inside the 10 seconds: no second read
  world.status = status({ last_turn_at: NOW, context_tokens: 120_000 })
  await clock.advance(11_000)
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(rowText(await band.drawn())).toContain('Tokens 120K')
  await clock.advance(31 * 60_000)
  expect(rowText(await band.drawn())).toContain('Cache 29 mins left')
  await $.tool.call({ tool: 'Bash', command: 'ls', agentId: 'sub' } as never) // a subagent's call does not read
})

// A host may skip a user plugin's classic.* hooks (Claude Code 2.1.292 does): the band must not need them.
const START = { cwd: '/w', surface: 'terminal', isInteractive: true } as never
const SID = '0b6f2c1e-4a7d-4c3b-9e21-5f8a7d6c4b3a'
const statusStdin = seen => seen.runs.flatMap((run, i) => (run.at(-1).endsWith('/hooks/status.py') ? [JSON.parse(seen.stdin[i])] : []))

test('with no classic hook at all, a turn draws the band from the session the engine names', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { status: status(), sessionId: SID })
  await $.session.start(START)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Cache-Guard  Cache 60 mins left · Tokens 109K (61%) · Miss cost $3.34   Handoff')
  expect(statusStdin(seen).at(-1)).toEqual({ cwd: '/w', session_id: SID })
})

test('with no classic hook, a resumed session draws the band when it starts, before any turn', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { status: status(), sessionId: SID })
  await $.session.start(START)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toContain('Cache-Guard  Cache 60 mins left')
})

test('a /clear takes the old figures down at once, and the next reading asks for the new session', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { status: status(), sessionId: SID }
  const seen = engine(on, world)
  await $.session.start(START)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toContain('Cache-Guard')
  world.sessionId = 'f1e2d3c4-b5a6-4978-8a6b-5c4d3e2f1a0b'
  world.status = status({ last_turn_at: null, lifetime_s: null, context_tokens: null, cold_usd: null })
  await $.session.end({ reason: 'clear', sessionId: SID, resume: { id: SID } } as never)
  expect(rowText(await band.drawn())).not.toContain('Cache-Guard')
  await $.turn.complete(turn(usage(10, 980, 10)))
  expect(statusStdin(seen).at(-1)).toEqual({ cwd: '/w', session_id: world.sessionId })
})

test('a classic transcript path is not sent once the engine names another session', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { status: status(), sessionId: 's' }
  const seen = engine(on, world)
  await started($)
  await $.turn.complete(turn(usage(10, 980, 10)))
  expect(statusStdin(seen).at(-1)).toEqual({ transcript_path: '/t/s.jsonl', cwd: '/w', session_id: 's' })
  world.sessionId = SID // a /clear the classic hooks never reported
  await $.turn.complete(turn(usage(10, 980, 10)))
  expect(statusStdin(seen).at(-1)).toEqual({ cwd: '/w', session_id: SID })
})

test('with no classic hook, a tool call re-reads the transcript and Handoff sends the session', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { status: status({ last_turn_at: null, lifetime_s: null, context_tokens: null, cold_usd: null }) as Record<string, unknown>, sessionId: SID, handoff: { path: HANDOFF.path } }
  const seen = engine(on, world)
  await $.session.start(START)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).not.toContain('Cache-Guard')
  world.status = status({ context_tokens: 95_000 })
  await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
  expect(rowText(await band.drawn())).toContain('Tokens 95K')
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  expect(JSON.parse(seen.stdin.at(-1)!)).toEqual({ cwd: '/w', session_id: SID })
  expect(JSON.stringify(await band.drawn())).toContain('Handoff ready')
})

test('a reading still in flight when /clear ends the session does not bring the old figures back', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world: { status: Record<string, unknown>; sessionId: string; hold?: Promise<void>; entered?: () => void } = { status: status(), sessionId: SID }
  engine(on, world)
  await $.session.start(START)
  await $.turn.complete(turn(usage(10, 980, 10)))
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toContain('Cache-Guard')
  // the next reading is held back until the test lets it go
  let release = () => {}
  world.hold = new Promise<void>(resolve => { release = resolve })
  world.status = status({ context_tokens: 150_000 })
  const asked = new Promise<void>(resolve => { world.entered = resolve })
  const inFlight = $.turn.complete(turn(usage(10, 980, 10)))
  await asked // the script is running for the old session
  await $.session.end({ reason: 'clear', sessionId: SID, resume: { id: SID } } as never)
  expect(rowText(await band.drawn())).not.toContain('Cache-Guard')
  release()
  await inFlight
  const after = rowText(await band.drawn())
  expect(after).not.toContain('Cache-Guard')
  expect(after).not.toContain('Tokens')
})

test('a handoff with no transcript yet is a notice, and a real error keeps the failed form', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { status: status(), sessionId: SID, handoff: { error: 'no transcript yet: nothing to hand off' } }
  const seen = engine(on, world)
  await $.session.start(START)
  await $.turn.complete(turn(usage(10, 980, 10)))
  await mountBand($)
  await $.ui.press({ plugin: 'cache-guard', key: 'cache-guard-handoff' })
  expect(seen.toasts).toEqual(['No transcript yet: nothing to hand off'])
})
