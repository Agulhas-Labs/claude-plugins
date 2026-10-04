import { clip, openQuestions, stepCount, stuckItems, verified } from './view'
import type { BoardState } from './view'

// The pane: what the board holds, in the page's order, with an empty section left out. Plain text. It reads
// the state the band keeps and runs no process.

const TEXT = 160 // a clip for any one line; the pane wraps what is left
const MORE = { commits: 8, decisions: 8, verified: 12, failedCommands: 5 }

const STEP_WORD = { completed: 'done', in_progress: 'now', pending: 'next' }

// `ui` is the drawing surface's element table (`$.ui.resolve(e)`): `$` itself is never passed across a file.
export function drawPane(ui, state: BoardState, now: number, onAnswer: (id: string) => void, onClose: () => void) {
  const { Box, Button, Text } = ui
  const section = (key: string, title: string, rows: unknown[]) => (
    <Box key={`logbook-${key}`} flexDirection="column" marginTop={1}>
      <Text key={`logbook-${key}-title`} bold>{title}</Text>
      {rows}
    </Box>
  )
  const line = (key: string, text: string, props: Record<string, unknown> = {}) => (
    <Text key={key} {...props}>{text}</Text>
  )

  const sections: unknown[] = []

  const questions = openQuestions(state)
  if (questions.length > 0) {
    sections.push(section('needs', `Needs you (${questions.length})`, questions.map(q => (
      <Box key={`logbook-q-${q.id}`} flexDirection="column">
        {line(`logbook-q-${q.id}-text`, `${q.id}${q.hardStop ? ' Stopped' : ''}: ${clip(q.text, TEXT)}`, { bold: true, color: q.hardStop ? 'red' : 'yellow' })}
        {q.default ? line(`logbook-q-${q.id}-default`, `  Default: ${clip(q.default, TEXT)}`) : null}
        {q.affects ? line(`logbook-q-${q.id}-affects`, `  Affects: ${clip(q.affects, TEXT)}`) : null}
        {q.reverse ? line(`logbook-q-${q.id}-reverse`, `  To reverse: ${clip(q.reverse, TEXT)}`) : null}
        <Button key={`logbook-answer-${q.id}`} label="Answer" onPress={() => onAnswer(q.id)} />
      </Box>
    ))))
  }

  const stuck = stuckItems(state, now)
  if (stuck.length > 0) {
    sections.push(section('stuck', `Stuck (${stuck.length})`, stuck.map(item => (
      line(`logbook-stuck-${item.kind}-${item.id}`, `${item.kind} ${clip(item.id === item.text ? '' : item.id, 40)} ${clip(item.text, TEXT)}`.replace(/\s+/g, ' '), { color: 'red' })
    ))))
  }

  const steps = stepCount(state)
  if (steps.total > 0) {
    sections.push(section('steps', `Steps (${steps.done}/${steps.total})`, state.steps.map(step => (
      line(`logbook-step-${step.id}`, `${STEP_WORD[step.status] ?? step.status}: ${clip(step.subject, TEXT)}`, { dimColor: step.status === 'completed' })
    ))))
  }

  const commits = (state.commits ?? []).slice(-MORE.commits)
  const deliverables = state.deliverables ?? []
  if (commits.length + deliverables.length > 0) {
    const earlier = (state.commits ?? []).length - commits.length
    sections.push(section('built', 'Built', [
      earlier > 0 ? line('logbook-built-more', `${earlier} earlier commits on the page`, { dimColor: true }) : null,
      ...commits.map(c => line(`logbook-commit-${c.hash}`, `${c.hash} ${clip(c.subject, TEXT)}`)),
      ...deliverables.map((d, i) => line(`logbook-deliverable-${i}`, `${clip(d.label, TEXT)}${d.path || d.url ? ` (${clip(d.path ?? d.url, TEXT)})` : ''}`)),
    ]))
  }

  const checks = verified(state)
  if (checks.length > 0) {
    const shown = checks.slice(0, MORE.verified)
    sections.push(section('verified', 'Verified', [
      ...shown.map((row, i) => line(`logbook-verified-${i}`, `${row.result || 'ran'}: ${clip(row.text, TEXT)}`, { color: row.result === 'fail' ? 'red' : undefined })),
      checks.length > shown.length ? line('logbook-verified-more', `${checks.length - shown.length} more on the page`, { dimColor: true }) : null,
    ]))
  }

  const decisions = (state.decisions ?? []).slice(-MORE.decisions)
  if (decisions.length > 0) {
    sections.push(section('decisions', 'Decisions', decisions.flatMap(d => [
      line(`logbook-decision-${d.id}`, `${d.id}: ${clip(d.text, TEXT)}`),
      d.why ? line(`logbook-decision-${d.id}-why`, `  Why: ${clip(d.why, TEXT)}`, { dimColor: true }) : null,
    ])))
  }

  const files = (state.changes ?? []).length
  const failedCommands = (state.commands ?? []).filter(c => !c.test && c.result === 'fail')
  const agents = state.agents ?? []
  const failedAgents = agents.filter(a => a.outcome === 'failed')
  const counts: unknown[] = []
  if (files > 0) counts.push(line('logbook-changed', `Changed: ${files} ${files === 1 ? 'file' : 'files'}`))
  if (state.commandsTotal > 0) {
    counts.push(line('logbook-commands', `Commands: ${state.commandsTotal} run${failedCommands.length ? `, ${failedCommands.length} failed` : ''}`))
    failedCommands.slice(0, MORE.failedCommands).forEach((c, i) => counts.push(line(`logbook-command-failed-${i}`, `  failed: ${clip(c.command, TEXT)}`, { color: 'red' })))
  }
  if (agents.length > 0) {
    counts.push(line('logbook-agents', `Agents: ${agents.length}${failedAgents.length ? `, ${failedAgents.length} failed` : ''}`))
    failedAgents.forEach(a => counts.push(line(`logbook-agent-failed-${a.id}`, `  failed: ${clip(a.description ?? a.type ?? a.id, TEXT)}`, { color: 'red' })))
  }
  if (counts.length > 0) sections.push(section('counts', 'Also recorded', counts))

  return (
    <Box flexDirection="column">
      <Box key="logbook-head">
        <Text key="logbook-title" bold>{clip(state.title, TEXT) || 'Logbook'}</Text>
        <Text key="logbook-state" dimColor>{`  ${state.state}   `}</Text>
        <Button key="logbook-close" label="Close" onPress={onClose} />
      </Box>
      {sections.length > 0 ? sections : <Text key="logbook-empty" dimColor>Nothing recorded yet.</Text>}
    </Box>
  )
}
