import { expect, mock, test } from 'claude-code/testing'
import { parseState, stuckItems, verifiedCount } from './view.ts'

const NOW = Date.parse('2026-01-05T09:50:00Z')
const SESSION = 's1'
const BOARD = '/w/.logbook/s1'

const board = (over: Record<string, unknown> = {}) => ({
  title: 'CSV export', state: 'live', updated: '2026-01-05T09:49:00Z', settings: { stuckAfterSeconds: 600 },
  questions: [], steps: [], commits: [], deliverables: [], checks: [], decisions: [], agents: [], changes: [],
  commands: [], commandsTotal: 0, ...over,
})
const question = (id: string, over: Record<string, unknown> = {}) => ({
  id, text: `Should the export include archived reports ${id}?`, default: 'No', affects: 'the query', reverse: 'drop the filter',
  hardStop: false, status: 'open', ...over,
})
const file = (state: unknown) => `window.BOARD = ${JSON.stringify(state)};\n`

type Seen = { runs: string[][]; toasts: string[]; opens: unknown[]; closes: unknown[]; fills: unknown[]; files: Record<string, string>; classic: string[] }
type World = {
  found: Record<string, unknown>; opened?: boolean; panes?: { id: string }[]; below?: boolean; failEdit?: boolean
  hold?: Promise<void> // the first helper run waits for it, so a test can land the answer late
  id?: string // the engine's session id ($.session.id()); a /clear changes it
  bySession?: Record<string, Record<string, unknown>> // what the helper finds for a given --session, over `found`
}

// The engine beneath the plugin: the session's id and folder, a process that answers the helper, a file system,
// and a record of the UI calls.
function engine(on, world: World) {
  const seen: Seen = { runs: [], toasts: [], opens: [], closes: [], fills: [], files: {}, classic: [] }
  on('session.id', () => ({ value: world.id ?? SESSION }))
  on('session.cwd', () => ({ value: '/w' }))
  on('process.run', async (_$, e) => {
    const hold = world.hold
    world.hold = undefined
    await hold
    seen.runs.push([...e.argv])
    const session = e.argv[e.argv.indexOf('--session') + 1]
    const found = world.bySession?.[session] ?? world.found
    return { value: { exitCode: 0, stdout: JSON.stringify(e.argv.includes('--open') ? { ...found, opened: world.opened ?? true } : found), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('fs.read', (_$, e) => ({ value: seen.files[e.path] ?? '' }))
  on('ui.toast', (_$, e) => (seen.toasts.push(e.text), { value: {} }))
  on('ui.open', (_$, e) => (seen.opens.push(e), world.panes?.push({ id: e.id }), { value: { isPlaced: true } }))
  on('ui.close', (_$, e) => (seen.closes.push(e), { value: {} }))
  on('ui.panes', () => ({ value: world.panes ?? [] }))
  on('prompt.fill', (_$, e) => (seen.fills.push(e), { isFilled: true }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('classic.SessionStart', () => (seen.classic.push('SessionStart'), {}))
  on('classic.UserPromptSubmit', () => (seen.classic.push('UserPromptSubmit'), {}))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  on('prompt.submit', (_$, e) => ({ text: e.text }))
  on('tool.call', (_$, e) => (world.failEdit && e.tool === 'Edit' ? { isError: true, result: 'no', text: 'no' } : { result: {}, text: '' }))
  if (!world.below) on('ui.render', (_$, e) => h(_$.ui.resolve(e).Box, { key: 'engine' }))
  return seen
}

const start = async $ =>
  $.classic.SessionStart({ source: 'startup', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: SESSION } as never)
const prompt = async $ =>
  $.classic.UserPromptSubmit({ prompt: 'hi', transcript_path: '/t/s.jsonl', cwd: '/w', session_id: SESSION } as never)
// The same session as a host that skips a plugin's classic.* hooks sees it: none of these raises one.
const begin = async $ => $.session.start({ cwd: '/w', surface: 'terminal' } as never)
const submit = async $ => $.prompt.submit({ text: 'hi', wait: false, origin: { kind: 'composer' } } as never)
const end = async ($, reason: string, sessionId = SESSION) => $.session.end({ reason, sessionId, resume: { id: sessionId } } as never)
const turn = ($, agentId?: string) =>
  $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', agentId, reason: 'answer', usage: undefined } as never)
const tool = ($, input: Record<string, unknown>) => $.tool.call({ ...input } as never)
const mountBand = $ =>
  $.ui.mount({ plugin: 'logbook', surface: 'terminal', component: 'AbovePrompt', props: { hasSurvey: false, bodyColumns: 120 } as never })

type Node = { type: string; props?: Record<string, unknown>; children?: (Node | string)[] }
const rowText = (n: Node | string): string =>
  typeof n === 'string' ? n : n.type === 'Button' ? String(n.props?.label ?? '') : (n.children ?? []).map(rowText).join('')
const textOf = async (view, text: string) => {
  const find = (n: Node | string): Node | undefined =>
    typeof n === 'string' ? undefined : n.type === 'Text' && rowText(n) === text ? n : (n.children ?? []).map(find).find(Boolean)
  return find((await view.drawn()) as Node)
}

const live = (state: unknown) => ({ board: BOARD, state })

test('the band wraps a band beneath it', async ($, on) => {
  mock.clock(on, { now: NOW })
  engine(on, { found: live(board()), below: true })
  on('ui.render', (_$, e) => h(_$.ui.resolve(e).Text, { key: 'other-band' }, 'another plugin'))
  await start($)
  const band = await mountBand($)
  expect(JSON.stringify(await band.drawn())).toContain('another plugin')
  expect(await band.find({ key: 'logbook-open' })).toBeDefined()
})

test('the band is one terse row: Stopped, questions, stuck, steps, checks, then the button', async ($, on) => {
  mock.clock(on, { now: NOW })
  const state = board({
    questions: [question('Q1', { hardStop: true, default: null }), question('Q2'), question('Q3', { status: 'answered' })],
    steps: [1, 2, 3, 4, 5].map(n => ({ id: String(n), subject: `s${n}`, status: n <= 3 ? 'completed' : 'pending' })),
    checks: [...Array(6)].map((_, i) => ({ id: `C${i}`, proves: 'x', command: 'c', result: i < 5 ? 'pass' : 'fail' })),
    commands: [{ command: 'make test', result: 'pass', time: '2026-01-05T09:40:00Z', test: true, fails: 0 }],
    changes: [{ path: 'a' }], commandsTotal: 9, agents: [{ id: 'a', outcome: 'failed', description: 'x', type: null }],
  })
  engine(on, { found: live(state) })
  await start($)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Logbook  Stopped · 1 question · 2 stuck · 3/5 · 6 pass · 1 fail   Logbook')
  expect((await textOf(band, 'Logbook'))?.props.color).toBe('white')
  expect((await textOf(band, 'Stopped'))?.props.color).toBe('red')
  expect((await textOf(band, '1 question'))?.props.color).toBe('yellow')
  expect((await textOf(band, '6 pass'))?.props.color).toBe('green')
  expect(rowText(await band.drawn())).not.toMatch(/file|command|agent/i)
})

test('parts with nothing to say are left out, and a finished board draws nothing', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { found: live(board()) as Record<string, unknown> }
  engine(on, world)
  await start($)
  expect(rowText(await (await mountBand($)).drawn())).toBe('Logbook  Logbook')
  world.found = live(board({ state: 'finished' }))
  await turn($)
  expect(JSON.stringify(await (await mountBand($)).drawn())).not.toContain('Logbook')
})

test('drawing runs no process', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board({ questions: [question('Q1')] })) })
  seen.files[`${BOARD}/state.js`] = file(board())
  await start($)
  await prompt($)
  const runs = seen.runs.length
  await mountBand($)
  await clock.advance(5 * 60_000) // ten ticks redraw
  expect(seen.runs.length).toBe(runs)
})

test('the button opens the board in the browser, and opens no pane', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board()) })
  await start($)
  await prompt($)
  await turn($)
  await mountBand($)
  const before = seen.runs.length
  await $.ui.press({ plugin: 'logbook', key: 'logbook-open' })
  const run = seen.runs.at(-1)!
  expect(seen.runs.length).toBe(before + 1)
  expect(run.at(-1)).toBe('--open')
  expect(run.at(-6)).toEndWith('/board/mod_state.py')
  expect(run.slice(-5, -1)).toEqual(['--project', '/w', '--session', SESSION])
  expect(seen.opens).toEqual([])
  expect(seen.toasts).toEqual([])
})

test('a browser that will not open says where the page is', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board()), opened: false })
  await start($)
  await prompt($)
  await mountBand($)
  await $.ui.press({ plugin: 'logbook', key: 'logbook-open' })
  expect(seen.toasts).toEqual([`Could not open a browser: ${BOARD}/board.html`])
})

test('a new open question toasts once; the first read only sets the baseline', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board({ questions: [question('Q1')] })) })
  await start($)
  expect(seen.toasts).toEqual([])
  await prompt($)
  seen.files[`${BOARD}/state.js`] = file(board({ questions: [question('Q1'), question('Q2', { hardStop: true, text: 'x'.repeat(200) })] }))
  await tool($, { tool: 'Bash', command: 'python3 board.py stop "x"' })
  expect(seen.toasts.length).toBe(1)
  expect(seen.toasts[0]).toStartWith('Logbook: Stopped, Q2 xxx')
  expect(seen.toasts[0].length).toBeLessThan(100)
  await tool($, { tool: 'Bash', command: 'ls' })
  await turn($)
  expect(seen.toasts.length).toBe(1)
})

test('state is re-read from state.js after a tool call, and on the 30 s tick while a turn runs', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board()) })
  await start($)
  await prompt($)
  const band = await mountBand($)
  seen.files[`${BOARD}/state.js`] = file(board({ steps: [{ id: '1', subject: 'a', status: 'completed' }] }))
  await tool($, { tool: 'Bash', command: 'ls' })
  expect(rowText(await band.drawn())).toContain('1/1')
  seen.files[`${BOARD}/state.js`] = file(board({ steps: [{ id: '1', subject: 'a', status: 'completed' }, { id: '2', subject: 'b', status: 'pending' }] }))
  await clock.advance(30_000)
  await clock.advance(1)
  expect(rowText(await band.drawn())).toContain('1/2')
  await turn($)
  seen.files[`${BOARD}/state.js`] = file(board({ steps: [{ id: '1', subject: 'a', status: 'completed' }, { id: '2', subject: 'b', status: 'completed' }, { id: '3', subject: 'c', status: 'pending' }] }))
  await clock.advance(60_000)
  expect(rowText(await band.drawn())).not.toContain('/3') // the tick stopped with the turn
})

test('the first changed file of a session with no board starts one, once, from the transcript', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { found: {} as Record<string, unknown> }
  const seen = engine(on, world)
  await start($)
  await prompt($)
  await tool($, { tool: 'Read', file_path: '/w/a' })
  await tool($, { tool: 'Bash', command: 'ls' })
  expect(seen.runs.filter(r => r.includes('--start'))).toEqual([])
  world.found = live(board())
  await tool($, { tool: 'Edit', file_path: '/w/a', old_string: 'a', new_string: 'b' })
  const started = seen.runs.filter(r => r.includes('--start'))
  expect(started.length).toBe(1)
  expect(started[0].slice(-7)).toEqual(['--project', '/w', '--session', SESSION, '--start', '--transcript', '/t/s.jsonl'].slice(-7))
  expect(rowText(await (await mountBand($)).drawn())).toBe('Logbook  Logbook')
  await tool($, { tool: 'Write', file_path: '/w/b', content: '' })
  expect(seen.runs.filter(r => r.includes('--start')).length).toBe(1)
})

test('a git commit also starts the board, and a failed edit does not', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { found: {} as Record<string, unknown>, failEdit: true }
  const seen = engine(on, world)
  await start($)
  await prompt($)
  world.found = live(board())
  await tool($, { tool: 'Edit', file_path: '/w/a', old_string: 'a', new_string: 'b' })
  expect(seen.runs.filter(r => r.includes('--start')).length).toBe(0)
  await tool($, { tool: 'Bash', command: 'git commit -m x' })
  expect(seen.runs.filter(r => r.includes('--start')).length).toBe(1)
})

test('with no classic hook at all, the band finds the running logbook from the engine, and its tick starts on a prompt', async ($, on) => {
  const clock = mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board()) })
  await submit($)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Logbook  Logbook')
  expect(seen.runs[0].slice(-4)).toEqual(['--project', '/w', '--session', SESSION])
  seen.files[`${BOARD}/state.js`] = file(board({ steps: [{ id: '1', subject: 'a', status: 'completed' }] }))
  await clock.advance(30_000)
  await clock.advance(1)
  expect(rowText(await band.drawn())).toContain('1/1')
  expect(seen.classic).toEqual([])
})

test('with no classic hook at all, a resumed session shows its board at session start', async ($, on) => {
  mock.clock(on, { now: NOW })
  const seen = engine(on, { found: live(board()) })
  await begin($)
  expect(rowText(await (await mountBand($)).drawn())).toBe('Logbook  Logbook')
  expect(seen.classic).toEqual([])
})

test('with no classic hook at all, the first changed file starts the board, and the helper finds the transcript', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world = { found: {} as Record<string, unknown> }
  const seen = engine(on, world)
  await submit($)
  world.found = live(board())
  await tool($, { tool: 'Edit', file_path: '/w/a', old_string: 'a', new_string: 'b' })
  const started = seen.runs.filter(r => r.includes('--start'))
  expect(started.length).toBe(1)
  expect(started[0].slice(-5)).toEqual(['--project', '/w', '--session', SESSION, '--start'])
  expect(rowText(await (await mountBand($)).drawn())).toBe('Logbook  Logbook')
  expect(seen.classic).toEqual([])
})

test('a /clear: the session id changes with no session.start, and the band drops the old session\'s board', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world: World = { id: SESSION, found: {}, bySession: { [SESSION]: live(board()) } }
  const seen = engine(on, world)
  seen.files[`${BOARD}/state.js`] = file(board({ steps: [{ id: '1', subject: 'a', status: 'pending' }] }))
  await submit($)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Logbook  Logbook')
  world.id = 's2'
  await tool($, { tool: 'Bash', command: 'ls' }) // would re-read the old board's state.js
  expect(JSON.stringify(await band.drawn())).not.toContain('Logbook')
  await turn($)
  expect(seen.runs.at(-1)!.slice(-4)).toEqual(['--project', '/w', '--session', 's2'])
  expect(JSON.stringify(await band.drawn())).not.toContain('Logbook')
})

test('a /clear\'s session.end drops the band at once, and the next prompt reads the new session', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world: World = { id: SESSION, found: {}, bySession: { [SESSION]: live(board()), s2: live(board()) } }
  const seen = engine(on, world)
  await submit($)
  const band = await mountBand($)
  expect(rowText(await band.drawn())).toBe('Logbook  Logbook')
  await end($, 'clear')
  world.id = 's2'
  expect(JSON.stringify(await band.drawn())).not.toContain('Logbook')
  await submit($)
  expect(seen.runs.at(-1)!.slice(-4)).toEqual(['--project', '/w', '--session', 's2'])
  expect(rowText(await band.drawn())).toBe('Logbook  Logbook')
})

test('a /clear while a read is in flight: its late answer neither draws the old board nor toasts its questions', async ($, on) => {
  mock.clock(on, { now: NOW })
  const world: World = { id: SESSION, found: {}, bySession: { [SESSION]: live(board({ questions: [question('Q1')] })) } }
  const seen = engine(on, world)
  let release: () => void = () => {}
  world.hold = new Promise<void>(resolve => { release = resolve }) // the turn's read stays out until after the /clear
  const band = await mountBand($)
  const turning = turn($)
  while (world.hold) await new Promise(resolve => setTimeout(resolve, 0)) // until the helper is running
  await end($, 'clear')
  world.id = 's2'
  release()
  await turning
  expect(JSON.stringify(await band.drawn())).not.toContain('Logbook')
  expect(seen.toasts).toEqual([])
})

test('the view rules: stuck, verified and the state file', () => {
  expect(parseState('window.BOARD = {"state":"live"};\n')?.state).toBe('live')
  expect(parseState('nonsense')).toBeNull()
  expect(parseState('window.BOARD = {oops;')).toBeNull()
  const quiet = board({ steps: [{ id: '1', subject: 'a', status: 'in_progress' }], updated: '2026-01-05T09:30:00Z' }) as never
  expect(stuckItems(quiet, NOW).length).toBe(1)
  expect(stuckItems(board({ ...(quiet as object), state: 'idle' }) as never, NOW).length).toBe(0)
  const fail = { command: 't', result: 'fail', time: '2026-01-05T09:00:00Z', test: true, fails: 1 }
  const pass = { ...fail, result: 'pass', time: '2026-01-05T09:10:00Z' }
  expect(stuckItems(board({ commands: [fail] }) as never, NOW).length).toBe(1)
  expect(stuckItems(board({ commands: [fail, { ...pass, command: 't2' }] }) as never, NOW).length).toBe(0)
  expect(verifiedCount(board({ commands: [pass, { ...fail, command: 'u' }], checks: [{ id: 'C1', proves: 'p', command: 'c', result: 'pass' }] }) as never)).toEqual({ pass: 2, fail: 1 })
})
