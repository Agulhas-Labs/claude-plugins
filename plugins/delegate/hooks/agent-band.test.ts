import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

import { estimate } from './agent-band.tsx'

// The haiku row of prices.json. Tests cannot read files, so the plugin's read of its table is answered
// here; tests/test_prices.py pins the file itself.
const PRICES = JSON.stringify({
  measured: 'test',
  families: { haiku: { input: 1, output: 5, cache_read: 0.1, cache_write: 1.25 } },
})
const HAIKU = 'claude-haiku-4-5-20251001'

// One request's usage, measured against the session's own cost figure: $0.018468 at haiku list rates.
const FIXTURE = { input_tokens: 26, output_tokens: 449, cache_read_input_tokens: 41957, cache_creation_input_tokens: 9601 }

type World = { toasts: string[]; periods: () => number; clock: ReturnType<typeof mock.clock>; reads: string[] }

function world(on: On, env: Record<string, string> = {}): World {
  const toasts: string[] = []
  const reads: string[] = []
  let periods = 0
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, env)
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  // The redraw timer's every period asks for a redraw, so redraws count its periods.
  on('ui.invalidate', () => {
    periods += 1
    return { value: undefined }
  })
  on('fs.read', (_$, e) => {
    reads.push(e.path)
    return { value: PRICES }
  })
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  return { toasts, periods: () => periods, clock, reads }
}

function spawns(on: On, model = HAIKU) {
  let n = 0
  on('agent.spawn', () => ({ model, agentId: `a${++n}` }))
}

function steps(on: On, usage = FIXTURE, model = HAIKU) {
  on('turn.step', async function* ($, e) {
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: { ...usage, model } }
  })
}

const step = async ($: Parameters<Parameters<typeof test>[1]>[0], agentId: string, effort = 'low', model = HAIKU) => {
  const stream = $.turn.step({ turnId: 't', index: 0, model, effort, messageCount: 1, agentId } as never)
  for await (const _ of stream) {
    // drain
  }
  return stream.result
}

const finished = (agentId: string | undefined, durationMs: number, isAborted = false) =>
  ({ answer: 'ok', durationMs, isAborted, turnId: 't', agentId, reason: isAborted ? 'aborted' : 'end_turn' }) as never

const band = (hasSurvey = false, surface: 'terminal' | 'desktop' = 'terminal') =>
  ({
    surface,
    component: 'AbovePrompt',
    requestId: 'band',
    props: { hasSurvey, isWorking: true, maxRows: 20, bodyColumns: 120, scroll: { offset: 0, bodyRows: 19 } },
  }) as never

// The text a tree shows, depth first.
function text(node: unknown): string {
  if (typeof node === 'string') return node
  if (node === null || typeof node !== 'object') return ''
  const children = (node as { children?: unknown[] }).children ?? []
  return children.map(text).join('')
}

function keys(node: unknown, found: string[] = []): string[] {
  if (node === null || typeof node !== 'object') return found
  const key = (node as { props?: { key?: unknown } }).props?.key
  if (typeof key === 'string') found.push(key)
  for (const child of (node as { children?: unknown[] }).children ?? []) keys(child, found)
  return found
}

const beneath = () => ({ type: 'Box', props: { key: 'beneath' }, children: [{ type: 'Text', children: ['another band'] }] })

test('a row appears on spawn with type, model, effort and description, beneath the count', async ($, on) => {
  world(on)
  spawns(on)
  steps(on)
  on('ui.render', () => beneath() as never)

  await $.agent.spawn({ prompt: 'secret prompt text', subagentType: 'delegate:runner', description: 'run the suite' })
  await step($, 'a1', 'low')

  for (const surface of ['terminal', 'desktop'] as const) {
    const shown = text(await $.ui.render(band(false, surface)))
    expect(shown).toContain('agents 1/6 running')
    expect(shown).toContain('run the suite')
    expect(shown).toContain('delegate:runner (haiku-4-5, low) · working · 0:00')
    expect(shown).not.toContain('secret prompt text')
  }
})

test('the band wraps what the plugins beneath drew', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner', description: 'run the suite' })
  const tree = await $.ui.render(band())

  expect(keys(tree)).toEqual(expect.arrayContaining(['agents', 'beneath']))
  expect(text(tree)).toContain('another band')
})

test('draws nothing of its own in a survey, or with no agent', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  expect(keys(await $.ui.render(band()))).toEqual(['beneath'])
  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  expect(keys(await $.ui.render(band(true)))).toEqual(['beneath'])
})

test('the current tool comes from tool.call, by name only, and clears when it returns', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)
  let shownDuringCall = ''
  on('tool.call', async () => {
    shownDuringCall = text(await $.ui.render(band()))
    return { result: 'done' } as never
  })

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.tool.call({ tool: 'Bash', input: { command: 'echo private-argument' }, agentId: 'a1' } as never)

  expect(shownDuringCall).toContain('· Bash ·')
  expect(shownDuringCall).not.toContain('private-argument')
  expect(text(await $.ui.render(band()))).toContain('· working ·')
})

test('usage accumulates over steps and prices the measured fixture at $0.018468', async ($, on) => {
  const { toasts } = world(on)
  spawns(on)
  steps(on)
  on('ui.render', () => beneath() as never)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner', description: 'run the suite' })
  await step($, 'a1')
  const once = text(await $.ui.render(band()))
  await step($, 'a1')
  await $.turn.complete(finished('a1', 14000))

  // 26 + 449 + 41957 + 9601 = 52033 tokens a request.
  expect(once).toContain('52.0k tok · ~$0.018 est.')
  expect(toasts).toEqual(['delegate:runner (haiku-4-5) finished in 14s · 104.1k tok · ~$0.037 est.: run the suite'])
})

test('the estimate is the list-rate sum, exactly', async () => {
  const cost = estimate(FIXTURE, { input: 1, output: 5, cache_read: 0.1, cache_write: 1.25 })
  expect(Math.abs(cost - 0.018468)).toBeLessThan(0.0000005)
})

test('an unknown model shows tokens and cost n/a', async ($, on) => {
  const { toasts } = world(on)
  spawns(on, 'some-other-model')
  steps(on, FIXTURE, 'some-other-model')

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await step($, 'a1', 'low', 'some-other-model')
  await $.turn.complete(finished('a1', 14000))

  expect(toasts).toEqual(['delegate:runner (some-other-model) finished in 14s · 52.0k tok · cost n/a'])
})

test('a run shorter than minSeconds toasts nothing', async ($, on) => {
  const { toasts } = world(on)
  spawns(on)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.turn.complete(finished('a1', 9000))

  expect(toasts).toEqual([])
})

test('minSeconds 0 toasts every run', { options: { minSeconds: 0 } }, async ($, on) => {
  const { toasts } = world(on)
  spawns(on)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.turn.complete(finished('a1', 400))

  expect(toasts).toHaveLength(1)
})

test('says stopped for an aborted subagent', async ($, on) => {
  const { toasts } = world(on)
  spawns(on)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.turn.complete(finished('a1', 14000, true))

  expect(toasts[0]).toContain('delegate:runner (haiku-4-5) stopped in 14s')
})

test('stays silent for the main loop and for agents it never saw start', async ($, on) => {
  const { toasts } = world(on)

  await $.turn.complete(finished(undefined, 60000))
  await $.turn.complete(finished('engine-fork', 60000))

  expect(toasts).toEqual([])
})

test('the redraw timer stops when no agent is left, and the band goes once the last row lingers out', async ($, on) => {
  const { periods, clock } = world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:builder' })
  await clock.advance(3000)
  expect(periods()).toBeGreaterThanOrEqual(3)
  expect(text(await $.ui.render(band()))).toContain('agents 2/6 running')

  await $.turn.complete(finished('a1', 3000))
  await clock.advance(2000)
  const whileOneRuns = periods()
  expect(whileOneRuns).toBeGreaterThan(3)

  await $.turn.complete(finished('a2', 5000))
  const atLastFinish = periods()
  await clock.advance(5000)
  expect(periods()).toBe(atLastFinish)
  expect(text(await $.ui.render(band()))).toContain('agents 0/6 running')

  await clock.advance(20000)
  expect(keys(await $.ui.render(band()))).toEqual(['beneath'])
})

test('the cap comes from DELEGATE_MAX_CONCURRENT_AGENTS when it is a positive integer', async ($, on) => {
  world(on, { DELEGATE_MAX_CONCURRENT_AGENTS: '3' })
  spawns(on)
  on('ui.render', () => beneath() as never)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })

  expect(text(await $.ui.render(band()))).toContain('agents 1/3 running')
})

test('reads its price table from its own folder, once', async ($, on) => {
  const { reads } = world(on)
  spawns(on)

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })

  expect(reads).toHaveLength(1)
  expect(reads[0]).toMatch(/\/hooks\/prices\.json$/)
})
