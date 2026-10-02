import type { EngineInterface, Register, Timer, TurnUsage } from 'claude-code'

// A band above the prompt while subagents run (one row each: what it is, what it is doing, how long,
// how many tokens, an estimated cost), and a toast when one ends. Everything it shows comes from the
// session's own events; it reads one file, its own price table, and nothing else.

type Rates = { input: number; output: number; cache_read: number; cache_write: number }
type Status = 'running' | 'finished' | 'stopped'

export type Agent = {
  type: string
  model: string
  effort: string
  description: string
  startedAt: number
  tool: string
  toolsInFlight: number
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  /** Dollars so far at list rates; null once a request ran on a model the table cannot place. */
  cost: number | null
  status: Status
  endedAt: number
  durationMs: number
}

const TICK_MS = 1000
// How long a finished agent's row stays, so the person sees how it ended.
const LINGER_MS = 20000
const DEFAULT_CAP = 6
const DEFAULT_COLLAPSE_ABOVE = 3

// Module state: a reload starts it over, and the engine drops the old module's timers.
const agents = new Map<string, Agent>()
let families: [string, Rates][] | null = null
let cap = DEFAULT_CAP
let minSeconds = 10
let collapseAbove = DEFAULT_COLLAPSE_ABOVE
// The person's own choice from the button; null leaves it to the agent count.
let chosen: 'collapsed' | 'expanded' | null = null
let tick: Timer | null = null
let linger: Timer | null = null

const normalise = (text: string) => text.toLowerCase().replace(/[^a-z0-9]/g, '')

export function ratesFor(model: string, table: readonly [string, Rates][]): Rates | null {
  const text = normalise(model)
  for (const [family, rates] of table) {
    if (text.includes(family)) {
      return rates
    }
  }
  return null
}

/** Dollars at list rates for one request's usage. */
export function estimate(usage: Omit<TurnUsage, 'model'>, rates: Rates): number {
  return (
    (usage.input_tokens * rates.input +
      usage.output_tokens * rates.output +
      usage.cache_read_input_tokens * rates.cache_read +
      usage.cache_creation_input_tokens * rates.cache_write) /
    1_000_000
  )
}

/** `claude-haiku-4-5-20251001` reads as `haiku-4-5`; an alias stays as it is. */
export const modelLabel = (model: string) => model.replace(/^claude-/, '').replace(/-\d{8}$/, '')

export const formatTokens = (n: number) =>
  n < 1000 ? `${n}` : n < 1_000_000 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1_000_000).toFixed(2)}M`

export const formatCost = (cost: number | null) =>
  cost === null ? 'cost n/a' : `~$${cost < 10 ? cost.toFixed(3) : cost.toFixed(2)} est.`

export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}

function formatDuration(ms: number): string {
  const total = Math.max(1, Math.round(ms / 1000))
  return total < 60 ? `${total}s` : `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`
}

const tokens = (a: Agent) => a.input + a.output + a.cacheRead + a.cacheWrite

function addUsage(a: Agent, usage: TurnUsage) {
  a.input += usage.input_tokens
  a.output += usage.output_tokens
  a.cacheRead += usage.cache_read_input_tokens
  a.cacheWrite += usage.cache_creation_input_tokens
  const rates = ratesFor(usage.model, families ?? [])
  a.cost = a.cost === null || rates === null ? null : a.cost + estimate(usage, rates)
}

export function rowText(a: Agent, now: number): string {
  const effort = a.effort ? `, ${a.effort}` : ''
  const doing = a.status === 'running' ? a.tool || 'working' : a.status
  const elapsed = a.status === 'running' ? now - a.startedAt : a.durationMs
  return `${a.type} (${modelLabel(a.model)}${effort}) · ${doing} · ${formatClock(elapsed)} · ${formatTokens(tokens(a))} tok · ${formatCost(a.cost)}`
}

export function toastText(a: Agent): string {
  const what = a.description ? `: ${a.description}` : ''
  return `${a.type} (${modelLabel(a.model)}) ${a.status} in ${formatDuration(a.durationMs)} · ${formatTokens(tokens(a))} tok · ${formatCost(a.cost)}${what}`
}

const runningCount = () => [...agents.values()].filter(a => a.status === 'running').length

const isCollapsed = () => (chosen ?? (agents.size > collapseAbove ? 'collapsed' : 'expanded')) === 'collapsed'

/** `delegate:runner` reads as `runner`. */
const shortType = (type: string) => type.slice(type.indexOf(':') + 1)

/** The collapsed band's text, without its button: counts per type, summed tokens and cost. */
export function summaryText(list: readonly Agent[], running: number, capacity: number): string {
  const counts = new Map<string, number>()
  for (const a of list) {
    counts.set(shortType(a.type), (counts.get(shortType(a.type)) ?? 0) + 1)
  }
  const groups = [...counts.entries()]
    .sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]))
    .map(([name, n]) => `${name} x${n}`)
    .join(', ')
  const total = list.reduce((sum, a) => sum + tokens(a), 0)
  const cost = list.reduce<number | null>((sum, a) => (sum === null || a.cost === null ? null : sum + a.cost), 0)
  const done = list.length - running
  const tail = done > 0 ? ` · ${done} finished` : ''
  return `agents ${running}/${capacity} running · ${groups} · ${formatTokens(total)} tok · ${formatCost(cost)}${tail}`
}

async function loadPrices($: EngineInterface) {
  if (families !== null) {
    return
  }
  try {
    const table = JSON.parse(await $.fs.read(`${$.plugin.root}/hooks/prices.json`)) as {
      families: Record<string, Rates>
    }
    families = Object.entries(table.families)
  } catch {
    families = [] // every cost shows as n/a rather than a guess
  }
}

async function readCap($: EngineInterface, configured: number) {
  // The same setting the orchestrator's cap comes from, under the same rule: a positive integer or nothing.
  const raw = (await $.env.get('DELEGATE_MAX_CONCURRENT_AGENTS')) ?? ''
  cap = /^[1-9][0-9]*$/.test(raw) ? Number(raw) : configured
}

function startTicking($: EngineInterface) {
  linger?.cancel()
  linger = null
  tick ??= $.clock.every(TICK_MS, () => $.ui.invalidate('ui.render'))
}

function stopTickingWhenIdle($: EngineInterface) {
  if (runningCount() > 0) {
    return
  }
  tick?.cancel()
  tick = null
  // One redraw once the last row's linger is over takes the band down.
  linger?.cancel()
  linger = $.clock.after(LINGER_MS + 100, () => $.ui.invalidate('ui.render'))
}

function reset() {
  tick?.cancel()
  linger?.cancel()
  tick = null
  linger = null
  agents.clear()
  chosen = null
}

function prune(now: number) {
  for (const [id, a] of agents) {
    if (a.status !== 'running' && now - a.endedAt >= LINGER_MS) {
      agents.delete(id)
    }
  }
}

export const register: Register = (on, options) => {
  const configuredCap = typeof options.cap === 'number' && options.cap > 0 ? options.cap : DEFAULT_CAP
  minSeconds = typeof options.minSeconds === 'number' && options.minSeconds >= 0 ? options.minSeconds : 10
  cap = configuredCap
  collapseAbove = typeof options.collapseAbove === 'number' && options.collapseAbove >= 0 ? options.collapseAbove : DEFAULT_COLLAPSE_ABOVE

  on('agent.spawn', async ($, e, next) => {
    await loadPrices($)
    const result = await next(e)

    if (result.agentId !== undefined) {
      await readCap($, configuredCap)
      agents.set(result.agentId, {
        type: e.subagentType,
        model: result.model ?? '',
        effort: '',
        description: e.description ?? '',
        startedAt: await $.clock.now(),
        tool: '',
        toolsInFlight: 0,
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        cost: 0,
        status: 'running',
        endedAt: 0,
        durationMs: 0,
      })
      startTicking($)
      $.ui.invalidate('ui.render')
    }

    return result
  })

  // Every request of every agent passes here: keep it to a lookup and a few additions.
  on('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    const a = e.agentId === undefined ? undefined : agents.get(e.agentId)

    if (a !== undefined) {
      a.model = e.model
      a.effort = e.effort === undefined ? '' : String(e.effort)
      if (result?.usage) {
        addUsage(a, result.usage)
      }
    }

    return result
  })

  on('tool.call', async ($, e, next) => {
    const a = e.agentId === undefined ? undefined : agents.get(e.agentId)
    if (a === undefined) {
      return next(e)
    }

    // The tool's name only: never its arguments, which can carry the prompt's text.
    a.tool = e.tool
    a.toolsInFlight += 1
    try {
      return await next(e)
    } finally {
      a.toolsInFlight -= 1
      if (a.toolsInFlight === 0) {
        a.tool = ''
      }
    }
  })

  on('turn.complete', async ($, e, next) => {
    const a = e.agentId === undefined ? undefined : agents.get(e.agentId)

    if (a !== undefined && a.status === 'running') {
      a.status = e.isAborted ? 'stopped' : 'finished'
      a.durationMs = e.durationMs
      a.endedAt = await $.clock.now()
      a.tool = ''
      if (e.durationMs >= minSeconds * 1000) {
        $.ui.toast(toastText(a), { timeoutMs: 8000 })
      }
      stopTickingWhenIdle($)
      $.ui.invalidate('ui.render')
    }

    return next(e)
  })

  on('session.end', async ($, e, next) => {
    reset()
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const beneath = await next(e)
    if (agents.size === 0) {
      chosen = null // the choice holds only while there are agents
    }
    if (e.props.hasSurvey || agents.size === 0) {
      return beneath
    }

    prune(await $.clock.now())
    if (agents.size === 0) {
      chosen = null // the choice holds only while there are agents
      return beneath
    }

    const now = await $.clock.now()
    const { Box, Button, Text } = $.ui.resolve(e)
    const toggle = (
      <Button
        key="toggle"
        label={isCollapsed() ? '[Expand]' : '[Collapse]'}
        onPress={() => {
          chosen = isCollapsed() ? 'expanded' : 'collapsed'
          $.ui.invalidate('ui.render')
        }}
      />
    )

    if (isCollapsed()) {
      return (
        <Box flexDirection="column">
          <Box key="agents" flexDirection="row">
            <Text dimColor>{`${summaryText([...agents.values()], runningCount(), cap)}  `}</Text>
            {toggle}
          </Box>
          {beneath}
        </Box>
      )
    }

    const rows = [...agents.entries()].map(([id, a]) => (
      <Box key={`agent-${id}`} flexDirection="row">
        {a.description ? <Text bold>{`${a.description}  `}</Text> : null}
        <Text dimColor>{rowText(a, now)}</Text>
      </Box>
    ))

    return (
      <Box flexDirection="column">
        <Box key="agents" flexDirection="column">
          <Box flexDirection="row">
            <Text dimColor>{`agents ${runningCount()}/${cap} running  `}</Text>
            {toggle}
          </Box>
          {rows}
        </Box>
        {beneath}
      </Box>
    )
  })
}
