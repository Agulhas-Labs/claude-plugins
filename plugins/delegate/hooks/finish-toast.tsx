import type { Register } from 'claude-code'

type Spawned = { type: string; model: string; description: string }

const seconds = (ms: number) => Math.max(1, Math.round(ms / 1000))

export const register: Register = on => {
  // What each subagent was started as, by its id, so its last turn can name it.
  const spawned = new Map<string, Spawned>()

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)

    if (result.agentId !== undefined) {
      spawned.set(result.agentId, {
        type: e.subagentType,
        model: result.model ?? '',
        description: e.description ?? '',
      })
    }

    return result
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) {
      const info = spawned.get(e.agentId)
      const name = info ? `${info.type} (${info.model})` : 'A subagent'
      const verb = e.isAborted ? 'stopped' : 'finished'
      const what = info?.description ? `: ${info.description}` : ''
      $.ui.toast(`${name} ${verb} in ${seconds(e.durationMs)}s${what}`, { timeoutMs: 8000 })
    }

    return next(e)
  })
}
