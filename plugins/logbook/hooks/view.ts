// What the band says about a board's state: plain functions of the state `state.js` holds, with no
// drawing and no process. The rules for what counts as stuck and as verified are the page's own (template.html:
// `stuck()`, `verifiedRows()`), written once more here because the page's script cannot be loaded into a mod.

export type Question = {
  id: string; text: string; default: string | null; affects: string | null; reverse: string | null
  hardStop: boolean; status: 'open' | 'answered'
}
export type Step = { id: string; subject: string; status: 'pending' | 'in_progress' | 'completed' }
export type Check = { id: string; proves: string | null; command: string | null; result: 'pass' | 'fail' }
export type Command = { command: string; description: string | null; result: string; time: string; test: boolean; fails: number }
export type Agent = { id: string; type: string | null; description: string | null; outcome: string }
export type Commit = { hash: string; subject: string; step: string | null }
export type Deliverable = { label: string; path: string | null; url: string | null }
export type Decision = { id: string; text: string; why: string | null; reverse: string | null }

export type BoardState = {
  title: string | null
  state: 'live' | 'idle' | 'finished'
  updated: string | null
  settings?: { stuckAfterSeconds?: number }
  questions: Question[]
  steps: Step[]
  commits: Commit[]
  deliverables: Deliverable[]
  checks: Check[]
  decisions: Decision[]
  agents: Agent[]
  changes: { path: string }[]
  commands: Command[]
  commandsTotal: number
}

const PREFIX = 'window.BOARD = '

// state.js is `window.BOARD = {json};`; anything else is not a state.
export function parseState(raw: string): BoardState | null {
  const text = raw.trim()
  if (!text.startsWith(PREFIX)) return null
  try {
    const state = JSON.parse(text.slice(PREFIX.length).replace(/;$/, ''))
    return state && typeof state === 'object' && !Array.isArray(state) ? (state as BoardState) : null
  } catch {
    return null
  }
}

const list = <T>(items: T[] | undefined): T[] => (Array.isArray(items) ? items : [])

// Hard stops first, because the task is blocked on them; otherwise the order they were asked.
export function openQuestions(state: BoardState): Question[] {
  const open = list(state.questions).filter(q => q.status !== 'answered')
  return [...open.filter(q => q.hardStop), ...open.filter(q => !q.hardStop)]
}

export type Stuck = { kind: 'step' | 'check' | 'test' | 'agent'; id: string; text: string }

// An in-progress step on a live board that has been quiet past the board's own threshold; a failed check; a test
// whose latest run failed with no later pass; a failed agent. Only the page's `Stuck` rules.
export function stuckItems(state: BoardState, now: number): Stuck[] {
  const out: Stuck[] = []
  const limit = (state.settings?.stuckAfterSeconds || 600) * 1000
  if (state.state === 'live' && now - Date.parse(state.updated ?? '') > limit) {
    for (const step of list(state.steps)) {
      if (step.status === 'in_progress') out.push({ kind: 'step', id: step.id, text: step.subject })
    }
  }
  for (const check of list(state.checks)) {
    if (check.result === 'fail') out.push({ kind: 'check', id: check.id, text: check.proves ?? check.command ?? '' })
  }
  const tests = list(state.commands).filter(c => c.test === true)
  for (const test of tests) {
    const passedLater = tests.some(o => o.result === 'pass' && Date.parse(o.time) > Date.parse(test.time))
    if (test.result === 'fail' && !passedLater) out.push({ kind: 'test', id: test.command, text: test.description ?? test.command })
  }
  for (const agent of list(state.agents)) {
    if (agent.outcome === 'failed') out.push({ kind: 'agent', id: agent.id, text: agent.description ?? agent.type ?? agent.id })
  }
  return out
}

export function stepCount(state: BoardState): { done: number; total: number } {
  const steps = list(state.steps)
  return { done: steps.filter(s => s.status === 'completed').length, total: steps.length }
}

// The rows under Verified: every recorded check, then every test run (a command the board knows is a test).
export function verified(state: BoardState): { kind: 'check' | 'test'; text: string; result: string }[] {
  return [
    ...list(state.checks).map(c => ({ kind: 'check' as const, text: c.proves ?? c.command ?? '', result: c.result })),
    ...list(state.commands).filter(c => c.test === true).map(c => ({ kind: 'test' as const, text: c.command, result: c.result })),
  ]
}

// A check that did not fail and a test that exited 0 pass; a test with no known outcome is neither.
export function verifiedCount(state: BoardState): { pass: number; fail: number } {
  let pass = 0
  let fail = 0
  for (const row of verified(state)) {
    if (row.result === 'fail') fail += 1
    else if (row.kind === 'check' || row.result === 'pass') pass += 1
  }
  return { pass, fail }
}

export function clip(text: string | null | undefined, max: number): string {
  const one = (text ?? '').replace(/\s+/g, ' ').trim()
  return one.length <= max ? one : `${one.slice(0, max - 1).trimEnd()}…`
}
