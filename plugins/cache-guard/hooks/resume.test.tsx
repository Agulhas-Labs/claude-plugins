import { expect, mock, test } from 'claude-code/testing'

const NOW = Date.parse('2026-10-02T12:00:00Z')
const DAY = 86_400_000

const handoffWritten = (daysAgo: number) => ({
  path: '/work/.claude/handoffs/2026-10-01.md',
  writtenAt: new Date(NOW - daysAgo * DAY).toISOString(),
  summary: 'Wire the resume band',
  branch: 'feat/x',
  ahead: 2,
  uncommitted: 3,
})

const props = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: {}, view: {} } as never
const start = { cwd: '/work', surface: 'terminal', isInteractive: true } as never

const world: { stdout?: string } = {}

const setup = (on: any, facts: unknown, stdout = JSON.stringify(facts), withBase = true) => {
  world.stdout = undefined
  const clock = mock.clock(on, { now: NOW })
  const kept = new Map<string, unknown>() // the plugin's store, which outlives a session
  on('store.get', (_$: unknown, e: { key: string }) => ({ value: kept.get(e.key) }))
  on('store.set', (_$: unknown, e: { key: string; value: unknown }) => {
    kept.set(e.key, e.value)
    return { value: undefined }
  })
  if (withBase) {
    on('ui.render', { component: 'AbovePrompt' }, ($b: any, e: any) => {
      const { Box } = $b.ui.resolve(e)
      return <Box key="base" />
    })
  }
  const calls: string[][] = []
  on('session.start', (_$: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('session.end', (_$: unknown, e: { sessionId: string }) => ({ sessionId: e.sessionId }))
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', (_$: unknown, e: { argv: string[] }) => {
    calls.push([...e.argv])
    return { value: { exitCode: 0, stdout: world.stdout ?? stdout, stderr: '' } }
  })
  const fills: { text: string; mode?: string }[] = []
  on('prompt.fill', (_$: unknown, e: { text: string; mode?: string }) => {
    fills.push({ text: e.text, mode: e.mode })
    return { isFilled: true }
  })
  return { calls, fills, clock }
}

const mountBand = ($: any, surface: 'terminal' | 'desktop') =>
  $.ui.mount({ plugin: 'cache-guard', surface, component: 'AbovePrompt', props })

for (const surface of ['terminal', 'desktop'] as const) {
  test(`shows the previous session with its age and git facts (${surface})`, async ($, on) => {
    const { calls } = setup(on, handoffWritten(2))
    await $.session.start(start)
    const ui = await mountBand($, surface)

    const line = await ui.find({ type: 'Text', text: /Previous session/ })
    expect(line?.text).toBe('Previous session: Wire the resume band (2 d ago) · branch feat/x · 2 ahead · 3 uncommitted')
    expect(await ui.find({ key: 'resume' })).toBeDefined()
    expect(await ui.find({ key: 'dismiss' })).toBeDefined()
    expect(calls).toHaveLength(1)
    expect(calls[0]?.[0]).toBe('sh')
    expect(calls[0]?.some(part => part.endsWith('/hooks/resume.py'))).toBe(true)
  })
}

test('draws nothing when there is no handoff', async ($, on) => {
  setup(on, {})
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('draws nothing when the handoff is older than the age limit', async ($, on) => {
  setup(on, handoffWritten(8))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('the age limit is the resumeMaxAgeDays option', { options: { resumeMaxAgeDays: 10 } }, async ($, on) => {
  setup(on, handoffWritten(8))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeDefined()
})

test('draws nothing when the script prints something that is not JSON', async ($, on) => {
  setup(on, null, 'not json')
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('draws nothing during a survey', async ($, on) => {
  setup(on, handoffWritten(1))
  await $.session.start(start)
  const ui = await $.ui.mount({ plugin: 'cache-guard', surface: 'terminal', component: 'AbovePrompt', props: { ...(props as object), hasSurvey: true } as never })

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('Resume fills the prompt with the path as an unsent draft, then hides the band', async ($, on) => {
  const { fills } = setup(on, handoffWritten(1))
  const submits: string[] = []
  on('prompt.submit', (_$: unknown, e: { text: string }) => {
    submits.push(e.text)
    return { text: e.text }
  })
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await ui.press({ key: 'resume' })

  expect(fills).toEqual([{ text: 'Read /work/.claude/handoffs/2026-10-01.md and continue from it.', mode: 'replace' }])
  expect(submits).toEqual([])
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('Dismiss hides the band without touching the prompt', async ($, on) => {
  const { fills } = setup(on, handoffWritten(1))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await ui.press({ key: 'dismiss' })

  expect(fills).toEqual([])
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('hides after the first prompt is submitted', async ($, on) => {
  setup(on, handoffWritten(1))
  on('prompt.submit', (_$: unknown, e: { text: string }) => ({ text: e.text }))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeDefined()

  await $.prompt.submit({ text: 'hello' } as never)

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('wraps next(e): another plugin band survives beneath it', async ($, on) => {
  setup(on, handoffWritten(1), undefined, false)
  on('ui.render', { component: 'AbovePrompt' }, ($2: any, e: any) => {
    const { Text } = $2.ui.resolve(e)
    return <Text>OTHER BAND</Text>
  })
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')

  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /OTHER BAND/ })).toBeDefined()
})

test('Dismiss is remembered: a later session does not offer the same handoff again', async ($, on) => {
  setup(on, handoffWritten(1))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await ui.press({ key: 'dismiss' })

  await $.session.start(start) // the next session, same handoff on disk
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('Resume is remembered too, and a newer handoff is still offered', async ($, on) => {
  setup(on, handoffWritten(1))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await ui.press({ key: 'resume' })

  await $.session.start(start)
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()

  world.stdout = JSON.stringify({ ...handoffWritten(0.5), path: '/work/.claude/handoffs/2026-10-02.md' })
  await $.session.start(start)
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeDefined()
})

// A /clear (or a resume) moves the process to a new session id and the engine fires no session.start for it.
const end = async ($: any, clock: { settle: () => Promise<void> }, reason: string) => {
  await $.session.end({ reason, sessionId: 's1', resume: { id: 's1' } } as never)
  await clock.settle() // the script runs on a timer once the end chain is done
}

const newer = () => ({ ...handoffWritten(0.5), path: '/work/.claude/handoffs/2026-10-02.md', summary: 'Written after the first' })

test('a /clear after a prompt offers the newest handoff again, read in the directory the engine names', async ($, on) => {
  const { calls, clock } = setup(on, handoffWritten(1))
  on('prompt.submit', (_$: unknown, e: { text: string }) => ({ text: e.text }))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await $.prompt.submit({ text: 'hello' } as never)
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()

  world.stdout = JSON.stringify(newer())
  await end($, clock, 'clear')

  const line = await ui.find({ type: 'Text', text: /Previous session/ })
  expect(line?.text).toContain('Previous session: Written after the first')
  expect(calls).toHaveLength(2)
  expect(calls[1]?.at(-1)).toBe('/work')
})

test('a handoff dismissed before a /clear is not offered after it', async ($, on) => {
  const { calls, clock } = setup(on, handoffWritten(1))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await ui.press({ key: 'dismiss' })

  await end($, clock, 'clear')

  expect(calls).toHaveLength(2)
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})

test('a session.end for another reason neither runs the script nor brings the band back', async ($, on) => {
  const { calls, clock } = setup(on, handoffWritten(1))
  on('prompt.submit', (_$: unknown, e: { text: string }) => ({ text: e.text }))
  await $.session.start(start)
  const ui = await mountBand($, 'terminal')
  await $.prompt.submit({ text: 'hello' } as never)

  world.stdout = JSON.stringify(newer())
  await end($, clock, 'prompt_input_exit')

  expect(calls).toHaveLength(1)
  expect(await ui.find({ type: 'Text', text: /Previous session/ })).toBeUndefined()
})
