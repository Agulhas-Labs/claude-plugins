import { expect, test } from 'claude-code/testing'

const finished = (agentId?: string, isAborted = false) =>
  ({ answer: 'ok', durationMs: 14000, isAborted, turnId: 't1', agentId, reason: isAborted ? 'aborted' : 'end_turn' }) as never

test('names a finished subagent by type, model and description', async ($, on) => {
  const toasts: string[] = []
  on('ui.toast', (_$, e) => void toasts.push(e.text))
  on('agent.spawn', () => ({ model: 'haiku', agentId: 'a1' }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner', description: 'run the suite' })
  await $.turn.complete(finished('a1'))

  expect(toasts).toEqual(['delegate:runner (haiku) finished in 14s: run the suite'])
})

test('says stopped for an aborted subagent', async ($, on) => {
  const toasts: string[] = []
  on('ui.toast', (_$, e) => void toasts.push(e.text))
  on('agent.spawn', () => ({ model: 'haiku', agentId: 'a1' }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))

  await $.agent.spawn({ prompt: 'x', subagentType: 'delegate:runner' })
  await $.turn.complete(finished('a1', true))

  expect(toasts[0]).toContain('stopped')
})

test('stays silent for the main loop', async ($, on) => {
  const toasts: string[] = []
  on('ui.toast', (_$, e) => void toasts.push(e.text))
  on('turn.complete', (_$, e) => ({ text: e.answer }))

  await $.turn.complete(finished(undefined))

  expect(toasts).toEqual([])
})
