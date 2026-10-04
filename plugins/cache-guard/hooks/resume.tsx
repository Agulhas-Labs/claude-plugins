import type { Register } from 'claude-code'

// What hooks/resume.py found when the session started. Facts only; any field git could not give is null.
type Facts = {
  path?: string
  writtenAt?: string
  summary?: string
  branch?: string | null
  ahead?: number | null
  uncommitted?: number | null
}

const DAY_MS = 86_400_000
const DISMISSED_KEY = 'dismissed'
const DISMISSED_KEPT = 50 // handoff paths remembered; older ones are long past the age limit anyway

const age = (ms: number) => {
  const minutes = Math.floor(ms / 60_000)

  if (minutes < 1) return 'under a minute'
  if (minutes < 60) return `${minutes} min`
  if (minutes < 1440) return `${Math.floor(minutes / 60)} h`

  return `${Math.floor(minutes / 1440)} d`
}

// A handoff that was dismissed or resumed is not offered again in a later session.
async function remember($, path: string) {
  const dismissed = ((await $.store.get(DISMISSED_KEY).catch(() => undefined)) ?? []) as string[]

  if (!dismissed.includes(path)) await $.store.set(DISMISSED_KEY, [...dismissed, path].slice(-DISMISSED_KEPT))
}

export const register: Register = (on, options) => {
  const maxAgeDays = typeof options.resumeMaxAgeDays === 'number' && options.resumeMaxAgeDays > 0 ? options.resumeMaxAgeDays : 7
  let found: Facts | null = null
  let writtenAtMs = 0
  let isHidden = false

  on('session.start', async ($, e, next) => {
    found = null
    isHidden = false
    const root = $.plugin.root
    const run = await $.process.run(['sh', `${root}/hooks/run-python.sh`, `${root}/hooks/resume.py`, e.cwd], { timeoutMs: 15000 })
    let facts: Facts = {}

    try {
      facts = JSON.parse(run.stdout) as Facts
    } catch {
      facts = {}
    }

    const written = facts.writtenAt ? Date.parse(facts.writtenAt) : NaN
    const now = await $.clock.now()

    const dismissed = ((await $.store.get(DISMISSED_KEY).catch(() => undefined)) ?? []) as string[]

    if (run.exitCode === 0 && facts.path && !Number.isNaN(written) && now - written <= maxAgeDays * DAY_MS && !dismissed.includes(facts.path)) {
      found = facts
      writtenAtMs = written
      $.ui.invalidate('ui.render')
    }

    return next(e)
  })

  // Once the user has sent anything, the offer has done its job.
  on('prompt.submit', ($, e, next) => {
    isHidden = true
    $.ui.invalidate('ui.render')

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)

    if (found === null || isHidden || e.props.hasSurvey) {
      return below
    }

    const facts = found
    const { Box, Button, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    const parts = [`Previous session: ${facts.summary || 'handoff'} (${age(now - writtenAtMs)} ago)`]

    if (facts.branch) parts.push(`branch ${facts.branch}`)
    if (typeof facts.ahead === 'number') parts.push(`${facts.ahead} ahead`)
    if (typeof facts.uncommitted === 'number') parts.push(`${facts.uncommitted} uncommitted`)

    return (
      <Box flexDirection="column">
        <Text dimColor>{parts.join(' · ')}</Text>
        <Box>
          <Button
            key="resume"
            label="Resume"
            onPress={async () => {
              await $.prompt.fill({ text: `Read ${facts.path} and continue from it.`, mode: 'replace' })
              isHidden = true
              await remember($, facts.path as string)
              $.ui.invalidate('ui.render')
            }}
          />
          <Button
            key="dismiss"
            label="Dismiss"
            onPress={async () => {
              isHidden = true
              await remember($, facts.path as string)
              $.ui.invalidate('ui.render')
            }}
          />
        </Box>
        {below}
      </Box>
    )
  })
}
