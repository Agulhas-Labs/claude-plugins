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
  expect(once).toContain('52.0k tokens · ~$0.018 est.')
  expect(toasts).toEqual(['delegate:runner (haiku-4-5) finished in 14s · 104.1k tokens · ~$0.037 est.: run the suite'])
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

  expect(toasts).toEqual(['delegate:runner (some-other-model) finished in 14s · 52.0k tokens · cost n/a'])
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

// Collapsed mode. A Button's label is a prop, so labels are read beside the text.
function labels(node: unknown, found: string[] = []): string[] {
  if (node === null || typeof node !== 'object') return found
  const label = (node as { props?: { label?: unknown } }).props?.label
  if (typeof label === 'string') found.push(label)
  for (const child of (node as { children?: unknown[] }).children ?? []) labels(child, found)
  return found
}
const press = ($: Parameters<Parameters<typeof test>[1]>[0], key = 'toggle') =>
  ($.ui as unknown as { press: (a: { plugin: string; key: string }) => Promise<unknown> }).press({ plugin: 'delegate', key })

async function spawnMany(
  $: Parameters<Parameters<typeof test>[1]>[0],
  types: string[],
) {
  for (const subagentType of types) await $.agent.spawn({ prompt: 'x', subagentType, description: `job ${subagentType}` })
}

test('two agents and the default show the rows, with a Collapse button', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:builder'])
  const tree = await $.ui.render(band())

  expect(text(tree)).toContain('job delegate:runner')
  expect(labels(tree)).toEqual(['[Collapse]'])
  expect(keys(tree)).toEqual(expect.arrayContaining(['agent-a1', 'agent-a2', 'toggle', 'beneath']))
})

test('four agents collapse to one line of type counts, summed tokens and cost, still wrapping beneath', async ($, on) => {
  world(on)
  spawns(on)
  steps(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:runner', 'delegate:builder', 'delegate:reviewer'])
  for (const id of ['a1', 'a2', 'a3', 'a4']) await step($, id)
  await $.turn.complete(finished('a4', 3000))
  const tree = await $.ui.render(band())
  const shown = text(tree)

  // 4 x 52033 = 208132 tokens; 4 x $0.018468 = $0.074.
  expect(shown).toContain('agents 3/6 running · runner x2, builder x1, reviewer x1 · 208.1k tokens · ~$0.074 est.')
  expect(shown).toContain('· 1 finished')
  expect(labels(tree)).toEqual(['[Expand]'])
  expect(shown).toContain('another band')
  expect(keys(tree)).not.toContain('agent-a1')
})

test('the button toggles the mode', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:runner', 'delegate:builder', 'delegate:reviewer'])
  expect(keys(await $.ui.render(band()))).not.toContain('agent-a1')
  await press($)
  expect(keys(await $.ui.render(band()))).toContain('agent-a1')
  await press($)
  expect(keys(await $.ui.render(band()))).not.toContain('agent-a1')
})

test('an explicit choice survives 4 to 5 agents and resets after every agent ends', async ($, on) => {
  const { clock } = world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:runner', 'delegate:builder', 'delegate:reviewer'])
  await $.ui.render(band())
  await press($) // expand
  await spawnMany($, ['delegate:runner'])
  expect(keys(await $.ui.render(band()))).toContain('agent-a1')

  for (const id of ['a1', 'a2', 'a3', 'a4', 'a5']) await $.turn.complete(finished(id, 1000))
  await clock.advance(21000)
  expect(keys(await $.ui.render(band()))).toEqual(['beneath'])

  await spawnMany($, ['delegate:runner', 'delegate:runner', 'delegate:builder', 'delegate:reviewer'])
  expect(keys(await $.ui.render(band()))).not.toContain('agent-a6')
  expect(labels(await $.ui.render(band()))).toEqual(['[Expand]'])
})

test('collapseAbove 0 always collapses', { options: { collapseAbove: 0 } }, async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner'])
  const tree = await $.ui.render(band())

  expect(text(tree)).toContain('agents 1/6 running · runner x1')
  expect(keys(tree)).not.toContain('agent-a1')
})

// The props of every Text whose text is exactly `want`, found in a drawn tree.
function textProps(node: unknown, want: string, found: Record<string, unknown>[] = []): Record<string, unknown>[] {
  if (node === null || typeof node !== 'object') return found
  const n = node as { type?: string; props?: Record<string, unknown>; children?: unknown[] }
  if (n.type === 'Text' && text(n) === want) found.push(n.props ?? {})
  for (const child of n.children ?? []) textProps(child, want, found)
  return found
}

test('the band carries a bold Delegate tag in its own colour', async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner'])
  const [tag] = textProps(await $.ui.render(band()), 'Delegate ')

  expect(tag).toMatchObject({ bold: true, color: 'cyan' })
})

test('the running count is green at or under the cap and red over it', { options: { cap: 2 } }, async ($, on) => {
  world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:runner'])
  expect(textProps(await $.ui.render(band()), 'agents 2/2 running')[0]).toMatchObject({ color: 'green' })
  await spawnMany($, ['delegate:runner'])
  expect(textProps(await $.ui.render(band()), 'agents 3/2 running')[0]).toMatchObject({ color: 'red' })
})

test('elapsed time turns yellow after five minutes and a stopped agent reads red', async ($, on) => {
  const { clock } = world(on)
  spawns(on)
  on('ui.render', () => beneath() as never)

  await spawnMany($, ['delegate:runner', 'delegate:builder'])
  await clock.advance(301_000)
  await $.turn.complete(finished('a2', 301_000, true))
  const tree = await $.ui.render(band())

  expect(textProps(tree, '5:01')).toHaveLength(2)
  expect(textProps(tree, '5:01')[0]).toMatchObject({ color: 'yellow' })
  expect(textProps(tree, 'stopped')[0]).toMatchObject({ color: 'red' })
})
