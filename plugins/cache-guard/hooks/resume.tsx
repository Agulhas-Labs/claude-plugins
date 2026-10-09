import type { Register } from 'claude-code'

// What hooks/resume.py found when the session started or a /clear moved it on. Facts only; any field git could not give is null.
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

// The offer: what was found, when it was written, whether it is hidden, and which evaluation is current.
type Offer = {
  found: Facts | null
  writtenAtMs: number
  isHidden: boolean
  epoch: number // bumped when a /clear or a resume moves the process on: an answer taken before it is stale
  maxAgeDays: number
}

// Runs resume.py for the directory and offers its handoff unless it is too old or was dismissed.
async function evaluate($, o: Offer, cwd: string) {
  const asked = ++o.epoch
  o.found = null
  o.isHidden = false // before the wait, so a prompt sent while the script runs still hides the band
  const root = $.plugin.root
  const run = await $.process.run(['sh', `${root}/hooks/run-python.sh`, `${root}/hooks/resume.py`, cwd], { timeoutMs: 15000 })
  let facts: Facts = {}

  try {
    facts = JSON.parse(run.stdout) as Facts
  } catch {
    facts = {}
  }

  const written = facts.writtenAt ? Date.parse(facts.writtenAt) : NaN
  const now = await $.clock.now()

  const dismissed = ((await $.store.get(DISMISSED_KEY).catch(() => undefined)) ?? []) as string[]

  if (asked !== o.epoch) return

  if (run.exitCode === 0 && facts.path && !Number.isNaN(written) && now - written <= o.maxAgeDays * DAY_MS && !dismissed.includes(facts.path)) {
    o.found = facts
    o.writtenAtMs = written
    $.ui.invalidate('ui.render')
  }
}

export const register: Register = (on, options) => {
  const o: Offer = {
    found: null, writtenAtMs: 0, isHidden: false, epoch: 0,
    maxAgeDays: typeof options.resumeMaxAgeDays === 'number' && options.resumeMaxAgeDays > 0 ? options.resumeMaxAgeDays : 7,
  }

  on('session.start', async ($, e, next) => {
    await evaluate($, o, e.cwd)

    return next(e)
  })

  // A /clear or a resume moves the process to another session with no session.start, so the offer is taken
  // again here. band.tsx hooks session.end for every reason, and the engine refuses a second hook on it
  // without a matcher. The script runs on a timer, outside the short bound every session.end hook shares.
  on('session.end', { reason: ['clear', 'resume'] }, async ($, e, next) => {
    const result = await next(e)
    o.epoch++
    o.found = null
    $.ui.invalidate('ui.render')
    const cwd = await $.session.cwd()
    $.clock.after(0, () => evaluate($, o, cwd).catch(() => {}))

    return result
  })

  // Once the user has sent anything, the offer has done its job.
  on('prompt.submit', ($, e, next) => {
    o.isHidden = true
    $.ui.invalidate('ui.render')

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)

    if (o.found === null || o.isHidden || e.props.hasSurvey) {
      return below
    }

    const facts = o.found
    const { Box, Button, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    const parts = [`Previous session: ${facts.summary || 'handoff'} (${age(now - o.writtenAtMs)} ago)`]

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
              o.isHidden = true
              await remember($, facts.path as string)
              $.ui.invalidate('ui.render')
            }}
          />
          <Button
            key="dismiss"
            label="Dismiss"
            onPress={async () => {
              o.isHidden = true
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
