import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Line, Snap } from '../types'

// The daemon holds the one `muse serve` pipe a plugin cannot (spawn has no live stdin); every
// call here is a one-shot of its CLI (bridge/muse-bridge.py in this repo).
import { NAME, bridge, bridgePath } from './bridge'

const UNIT = 'muse-bridge-mentor'

const ROLE =
  'You are the MENTOR; Muse is the implementer. Never edit files yourself for this task: ' +
  'plan, hand Muse a concrete ticket with an acceptance check, then review its work yourself ' +
  '(git diff, Read) and send precise feedback to the SAME Muse session until you approve. ' +
  'Load the mentor:mentoring skill for the full playbook. Muse\'s shell moves any command that runs past 300 s ' +
  'to the background and may end the turn silently: split long builds and test runs into short steps. '

const text = (s: string) => ({ result: s })

// Muse's events as short lines for the model; an approval is the one thing it must act on.
function digest(out: string): string {
  const lines: string[] = []
  for (const raw of out.split('\n').filter(Boolean)) {
    let ev: any
    try { ev = JSON.parse(raw) } catch { lines.push(raw); continue }
    if (ev.type === 'approval')
      lines.push(
        `APPROVAL NEEDED id=${ev.id}: Muse wants to run \`${ev.summary}\` (${ev.kind}${ev.judgeEscalated ? ', its own judge was unsure' : ''}). ` +
          'Allow only what plainly serves the ticket inside the repo; otherwise deny with feedback or ask the operator. ' +
          'Then call muse_decide, then muse_wait. One allow covers every part of a compound command.',
      )
    else if (ev.type === 'message') lines.push(`MUSE SAYS (${ev.phase}): ${ev.text}`)
    else if (ev.type === 'done') {
      lines.push(`${ev.text ? `MUSE SAID: ${ev.text}\n` : ''}TURN ${ev.terminal}${ev.error ? ': ' + JSON.stringify(ev.error) : ''} (${ev.durationMs ?? '?'} ms)`)
      if (ev.backgrounded?.length)
        lines.push(`WARNING: this turn moved ${ev.backgrounded.map((c: string) => `\`${c}\``).join(', ')} to the background, so the turn's ` +
          'end does not mean that work finished or passed. Check its result yourself, and have Muse rerun it in shorter steps.')
    }
    else if (ev.type === 'backgrounded')
      lines.push(`BACKGROUNDED (${ev.initiator ?? '?'}): Muse's shell moved \`${ev.command}\` to the background${ev.initiator === 'timeout' ? ' after 300 s' : ''}.`)
    else if (ev.type === 'background_finished')
      lines.push(`BACKGROUND DONE (${ev.status}): \`${ev.command}\`${ev.output ? `\n${ev.output}` : ''}`)
    else if (ev.type === 'interrupted') lines.push(`(interrupt sent by ${ev.by})`)
    else if (ev.type === 'still_running') lines.push('STILL RUNNING: call muse_wait to keep waiting.')
    else if (ev.type === 'auto_allowed') lines.push(`(auto-allowed: ${ev.command})`)
    else if (ev.type === 'auto_denied') lines.push(`(refused a retry of a command the operator denied: ${ev.command})`)
    else if (ev.type === 'idle') lines.push('Muse is idle; nothing is running.')
    else if (Array.isArray(ev.skills))
      lines.push(ev.skills.length ? ev.skills.map((k: any) => `- ${k.selector}${k.argumentHint ? ` ${k.argumentHint}` : ''}: ${k.description}`).join('\n') : 'Muse has no skills.')
    else if (ev.ok === false) lines.push(`ERROR: ${ev.error}`)
    else if (!['approval_resolved', 'prompt', 'decided'].includes(ev.type)) lines.push(raw) // pane-only events
  }
  return lines.join('\n') || '(no output)'
}

const PANE = 'mentor'
// Change the tag when Snap's fields change: it declines what an earlier version stored.
const snap = atom({ plugin: 'mentor', key: 'snap' } as const, null, { shape: 'snap-2' })
// The id of the risky card whose first Allow press is waiting for its confirming second one.
const armed = atom({ plugin: 'mentor', key: 'armed' } as const, '', { shape: 'armed-1' })

// One bridge event as the pane's line, or null for the ones not worth a row.
function lineOf(ev: any): Line | null {
  const at = { seq: ev.seq, t: ev.t }
  switch (ev.type) {
    case 'prompt': return { ...at, kind: 'mentor', text: ev.text }
    case 'message': return { ...at, kind: ev.phase === 'finalAnswer' ? 'muse' : 'muse-note', text: ev.text }
    case 'auto_allowed': return { ...at, kind: 'auto', text: ev.command }
    case 'auto_denied': return { ...at, kind: 'denied', text: `retry refused (operator denied it): ${ev.command}` }
    case 'decided': return { ...at, kind: ev.allow ? 'allowed' : 'denied', text: `${ev.by}: ${ev.command ?? ''}` }
    case 'done': return { ...at, kind: 'done', text: `${ev.terminal} in ${Math.round((ev.durationMs ?? 0) / 1000)} s${ev.backgrounded?.length ? ` (${ev.backgrounded.length} backgrounded)` : ''}` }
    case 'backgrounded': return { ...at, kind: 'background', text: `moved to background (${ev.initiator ?? '?'}): ${ev.command}` }
    case 'background_finished': return { ...at, kind: 'background', text: `background ${ev.status}: ${ev.command}` }
    case 'interrupted': return { ...at, kind: 'denied', text: `interrupted by ${ev.by}` }
    default: return null
  }
}

const seenApprovals = new Set<string>()

// The daemon answers `status` once Muse has started a session (each of its two setup calls may take
// up to 60 s). Gives up early if the unit has died, so a bad workspace fails in seconds.
async function waitForBridge($: any): Promise<string | null> {
  for (let i = 0; i < 130; i++) {
    await $.clock.sleep(1_000)
    const out = await bridge($, ['status'], 5_000)
    if (out.startsWith('{')) return out
    const unit = await $.process.run(['systemctl', '--user', 'is-active', UNIT], { timeoutMs: 5_000 })
    if (!['active', 'activating'].includes(unit.stdout.trim())) return null
  }
  return null
}

// A newer refresh supersedes an older one still in flight, so a slow poll cannot repaint a card
// the operator has just answered.
let generation = 0
let polling = false
let timer: { cancel: () => void } | null = null
let downTicks = 0

// The 2 s poll runs only while a Muse session exists (or the pane was asked for): it starts from
// session.start when the bridge is already up, /mentor and muse_start, and stops after 15 polls
// (30 s) with no session, so an idle Claude Code session spawns nothing.
function ensurePolling($: any): void {
  if (timer) return
  downTicks = 0
  timer = $.clock.every(2_000, () => {
    if (polling) return // refresh() supersedes: a poll slower than 2 s would otherwise never apply
    polling = true
    void refresh($)
      .then(isUp => {
        downTicks = isUp ? 0 : downTicks + 1
        if (downTicks >= 15) { timer?.cancel(); timer = null }
      })
      .finally(() => { polling = false })
  })
}

async function refresh($: any): Promise<boolean> {
  const mine = ++generation
  try {
    return await refreshOnce($, mine)
  } catch (err) {
    $.ui.log(`mentor pane refresh failed: ${String(err)}`, { to: 'debug' })
    return false
  }
}

async function refreshOnce($: any, mine: number): Promise<boolean> {
  const out = await bridge($, ['snapshot'], 8_000)
  let s: any
  try { s = JSON.parse(out.split('\n')[0]) } catch { s = null }
  if (s?.ok === false) { // the daemon is up but this snapshot failed: keep what the pane shows
    $.ui.log(`mentor snapshot failed: ${s.error}`, { to: 'debug' })
    return true
  }
  if (!s || s.session === undefined) {
    if (mine !== generation) return false
    await update($, snap, () => ({ isUp: false, workspace: '', isSandboxed: false, isRunning: false, pending: [], feed: [], diff: '' }))
    return false
  }
  if (mine !== generation) return true
  const next: Snap = {
    isUp: true,
    workspace: s.workspace,
    isSandboxed: s.sandbox === true,
    isRunning: s.running,
    pending: s.pending.map((p: any) => ({ id: p.id, summary: p.summary, kind: p.kind, isJudgeUnsure: p.judgeEscalated, isRisky: p.risky })),
    feed: s.events.map(lineOf).filter((l: Line | null): l is Line => l !== null).slice(-30),
    diff: s.diff ?? '',
  }
  const open = new Set(next.pending.map(p => p.id + p.summary))
  for (const key of seenApprovals) if (!open.has(key)) seenApprovals.delete(key) // answered: forget it
  for (const p of next.pending)
    if (!seenApprovals.has(p.id + p.summary)) {
      seenApprovals.add(p.id + p.summary)
      $.ui.toast(`Muse needs an answer: ${p.summary.slice(0, 80)}`, { timeoutMs: 6_000 })
    }
  $.ui.status(next.pending.length > 0 ? `muse: ${next.pending.length} approval(s) waiting` : next.isRunning ? 'muse: working' : 'muse: idle')
  await update($, snap, () => next)
  $.ui.invalidate('ui.render')
  return true
}

// The card goes at the click, not at the next poll; the refresh after the decision confirms it.
async function dropCard($: any, id: string): Promise<void> {
  await update($, snap, (s: Snap | null) => (s ? { ...s, pending: s.pending.filter(x => x.id !== id) } : s))
  $.ui.toast('Answered', { timeoutMs: 800 }) // replaces the question toast, which cannot be dismissed
  $.ui.invalidate('ui.render')
}

// onPress must return at once (the engine repaints when the handler settles): drop the card now,
// then decide and refresh in the background.
function answer($: any, id: string, verdict: 'allow' | 'deny', flags: string[]): void {
  void dropCard($, id)
  void bridge($, ['decide', ...flags, '--', id, verdict]).then(() => refresh($))
}

const COLOR: Record<string, string> = { mentor: 'cyan', allowed: 'green', denied: 'red', background: 'yellow' }

const GLYPH: Record<string, string> = {
  mentor: 'CLAUDE', muse: 'MUSE', 'muse-note': 'muse', auto: 'auto-ok', allowed: 'ALLOW', denied: 'DENY', done: 'done', background: 'BG',
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const spec = (name: string, description: string, properties: Record<string, unknown>, required: string[] = []) =>
      $.tool.register({ name, description, isDeferred: false, inputSchema: { type: 'object', properties, required } })
    await spec('muse_start', ROLE + 'Start (or restart) the Muse session on a repo. Call once before muse_ask.',
      { workspace: { type: 'string', description: 'absolute repo path' },
        auto_allow: { type: 'string', description: 'comma list of read-only/routine commands Muse may run unasked, e.g. "ls,cat,git,python3,pytest"' },
        sandbox: { type: 'boolean', description: 'run Muse under bubblewrap so it can write only the repo and cannot see the home directory (default true). Pass false only when the operator asks for it.' } },
      ['workspace'])
    await spec('muse_ask', 'Send Muse a task or feedback in its ONE ongoing session; returns at the first approval request, when Muse finishes, or after ~9 min. ' +
        'Attach images with `images` (absolute paths, sent inline). Any other file (PDF, text, audio): put its absolute path in the prompt; Muse reads it itself. ' +
        'To have Muse run one of its own skills, name it in `skill` (a selector from muse_skills).',
      { prompt: { type: 'string' }, images: { type: 'array', items: { type: 'string' }, description: 'absolute paths of image files to attach' },
        skill: { type: 'string', description: 'a Muse skill selector from muse_skills, invoked before the prompt' },
        skill_args: { type: 'string', description: 'free-text arguments for the skill' } }, ['prompt'])
    await spec('muse_wait', 'Keep waiting on Muse after a muse_decide or a STILL RUNNING result.', {})
    await spec('muse_decide', "Answer one of Muse's approval requests. Dangerous ones (secrets, sudo, push, outside the repo) are refused here and need the operator.",
      { id: { type: 'string' }, allow: { type: 'boolean' }, feedback: { type: 'string', description: 'on deny: guidance Muse receives' } },
      ['id', 'allow'])
    await spec('muse_steer', 'Add a note to the turn Muse is running now, without waiting for it to finish.', { text: { type: 'string' } }, ['text'])
    await spec('muse_interrupt', 'Stop the turn Muse is running now (it is going the wrong way, or stuck); the session stays. Returns how the turn ended. Then send feedback with muse_ask.', {})
    await spec('muse_skills', "List Muse's own skills (selectors for muse_ask's `skill`).", {})
    await spec('muse_status', 'Is the Muse session up, running, or waiting on approvals?', {})
    await spec('muse_stop', 'End the Muse session and stop the daemon.', {})
    await $.command.register({ name: 'mentor', description: 'Open the mentor pane: Muse session, approvals, steer' })
    void refresh($).then(isUp => { if (isUp) ensurePolling($) }) // a bridge left running by an earlier session
    return next(e)
  })

  on('tool.call', { tool: 'mcp__mentor__muse_start' }, async ($, e) => {
    // the daemon runs under systemd, whose working directory is not Claude's: a relative path would miss
    if (!String(e.workspace).startsWith('/')) return text(`workspace must be an absolute path, not ${JSON.stringify(e.workspace)}`)
    await bridge($, ['stop'], 5_000)
    await $.process.run(['systemctl', '--user', 'stop', UNIT], { timeoutMs: 10_000 }) // a lingering unit blocks the new one's name
    const auto = e.auto_allow ? ['--auto-allow', String(e.auto_allow)] : []
    const sandbox = e.sandbox === false ? [] : ['--sandbox']
    const r = await $.process.run(['systemd-run', '--user', `--unit=${UNIT}`, '--collect', '-q', 'python3', await bridgePath($), 'serve', '-w', String(e.workspace), '-n', NAME, ...auto, ...sandbox], { timeoutMs: 6_000 })
    if (r.exitCode !== 0) return text(`could not start: ${r.stderr.trim()}`)
    const status = await waitForBridge($)
    if (status === null) {
      const log = await $.process.run(['journalctl', '--user', '-u', UNIT, '-n', '15', '--no-pager', '-o', 'cat'], { timeoutMs: 5_000 })
      return text(`the bridge did not come up. Its log:\n${log.stdout.trim() || '(empty)'}`)
    }
    $.ui.status('muse: up')
    ensurePolling($)
    return text(status)
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))

  on('tool.call', { tool: 'mcp__mentor__muse_ask' }, async ($, e) => {
    $.ui.status('muse: working')
    const images = ((e.images as string[] | undefined) ?? []).map(path => `--image=${String(path)}`)
    // one argument each, so a selector or its arguments cannot be read as a flag
    const skill = e.skill ? [`--skill=${String(e.skill)}`, ...(e.skill_args ? [`--skill-args=${String(e.skill_args)}`] : [])] : []
    const out = digest(await bridge($, ['ask', ...images, ...skill, '--', String(e.prompt)], 600_000))
    $.ui.status(out.includes('APPROVAL NEEDED') ? 'muse: needs approval' : out.includes('STILL RUNNING') ? 'muse: working' : 'muse: idle')
    return text(out)
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))

  on('tool.call', { tool: 'mcp__mentor__muse_wait' }, async ($) => {
    const out = digest(await bridge($, ['wait'], 600_000))
    $.ui.status(out.includes('APPROVAL NEEDED') ? 'muse: needs approval' : out.includes('STILL RUNNING') ? 'muse: working' : 'muse: idle')
    return text(out)
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))

  on('tool.call', { tool: 'mcp__mentor__muse_decide' }, async ($, e) => {
    // never --force or --by: a risky command needs the operator, in the pane
    const fb = e.feedback ? [`--feedback=${String(e.feedback)}`] : []
    return text(digest(await bridge($, ['decide', ...fb, '--', String(e.id), e.allow ? 'allow' : 'deny'])))
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))

  on('tool.call', { tool: 'mcp__mentor__muse_steer' }, async ($, e) => text(digest(await bridge($, ['steer', '--', String(e.text)])))).catch(() => text('mentor: the tool failed'))
  on('tool.call', { tool: 'mcp__mentor__muse_interrupt' }, async $ => {
    const sent = await bridge($, ['interrupt'])
    if (!sent.includes('"ok": true')) return text(digest(sent)) // nothing running, or the bridge is down
    // the interrupt is only admitted; the turn is over at its `done` (terminal cancelled)
    const out = digest(await bridge($, ['wait', '--max-wait', '60'], 90_000))
    $.ui.status(out.includes('STILL RUNNING') ? 'muse: working' : 'muse: idle')
    return text(out)
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))
  on('tool.call', { tool: 'mcp__mentor__muse_skills' }, async $ => text(digest(await bridge($, ['skills'])))).catch(() => text('mentor: the tool failed'))
  on('tool.call', { tool: 'mcp__mentor__muse_status' }, async $ => text(await bridge($, ['status']))).catch(() => text('mentor: the tool failed'))
  on('tool.call', { tool: 'mcp__mentor__muse_stop' }, async $ => {
    $.ui.status(undefined)
    return text(await bridge($, ['stop']))
  }).catch(() => text('mentor: the tool failed (see claude --debug)'))

  // ---- the pane

  on('command.run', { command: 'mentor' }, async $ => {
    await refresh($)
    ensurePolling($)
    const r = await $.ui.open({ id: PANE, title: 'Mentor', focus: true, closeOnEscape: true })

    return { text: r.isPlaced ? 'Mentor pane opened.' : 'The mentor pane waits for a wider terminal (about 110 columns).' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Input } = $.ui.resolve(e)
    const s = await read($, snap)
    const armedId = await read($, armed)
    const width = Math.max(20, e.props.bodyColumns)
    const rows = Math.max(8, e.viewport?.rows ?? 30)
    const fit = (t: string) => t.replace(/\s+/g, ' ').slice(0, width - 10)

    if (!s || !s.isUp)
      return (
        <Box flexDirection="column">
          <Text bold>No Muse session.</Text>
          <Text dimColor>Ask Claude to call muse_start on a repo.</Text>
        </Box>
      )

    const room = Math.max(4, rows - 12 - s.pending.length * 3)

    return (
      <Box flexDirection="column">
        {s.pending.length > 0 && <Text bold color="yellow">{s.pending.length} question(s) waiting for you</Text>}
        <Text bold color={s.isRunning ? 'green' : undefined}>{s.isRunning ? 'Muse is working' : 'Muse is idle'}</Text>
        <Text dimColor>{s.workspace.slice(-(width - 2))}</Text>
        {s.isSandboxed
          ? <Text dimColor>sandboxed: Muse can write only this repo; home is hidden</Text>
          : <Text color="yellow">NOT sandboxed: Muse can read everything you can</Text>}

        {s.pending.map((p, i) => (
          <Box key={`p-${p.id}`} flexDirection="column" borderStyle="bold" borderColor={p.isRisky ? 'red' : 'yellow'}>
            <Text bold color={p.isRisky ? 'red' : 'yellow'}>
              {p.isRisky ? '! RISKY: secrets, sudo, push or outside the repo' : '? ANSWER NEEDED'}
              {p.isJudgeUnsure ? ' (its own judge was unsure)' : ''}
            </Text>
            <Text bold>{fit(p.summary)}</Text>
            <Box>
              {!p.isRisky && (
                <Button
                  key={`allow-${p.id}`}
                  hotkey={i === 0 ? 'a' : undefined} // one card per key: with two, `a` must not answer either
                  onPress={() => answer($, p.id, 'allow', ['--force', '--by', 'operator'])}
                >
                  <Text bold color="green">Allow</Text>
                </Button>
              )}
              {p.isRisky && armedId !== p.id && (
                <Button
                  key={`arm-${p.id}`}
                  onPress={() => {
                    void update($, armed, () => p.id)
                    $.ui.invalidate('ui.render')
                  }}
                >
                  <Text bold color="yellow">Allow…</Text>
                </Button>
              )}
              {p.isRisky && armedId === p.id && (
                <Button
                  key={`confirm-${p.id}`}
                  onPress={() => answer($, p.id, 'allow', ['--force', '--by', 'operator'])}
                >
                  <Text bold color="red">Confirm: allow this risky command</Text>
                </Button>
              )}
              <Button
                key={`deny-${p.id}`}
                hotkey={i === 0 ? 'd' : undefined}
                onPress={() => answer($, p.id, 'deny', ['--feedback=The operator denied this.', '--by', 'operator'])}
              >
                <Text bold color="red">Deny</Text>
              </Button>
            </Box>
          </Box>
        ))}

        <Text bold>Activity</Text>
        {s.feed.length === 0 && <Text dimColor>Nothing yet.</Text>}
        {s.feed.slice(-room).map(l => (
          <Text key={`l-${l.seq}`} dimColor={l.kind === 'auto' || l.kind === 'muse-note'} color={COLOR[l.kind]}>
            {GLYPH[l.kind] ?? l.kind}: {fit(l.text)}
          </Text>
        ))}

        {s.diff !== '' && <Text bold>Changes</Text>}
        {s.diff.split('\n').slice(0, 8).map((d, i) => <Text key={`d-${i}`} dimColor>{d.slice(0, width - 2)}</Text>)}

        <Input
          key="steer"
          label="steer> "
          placeholder={s.isRunning ? 'note for the running turn' : 'Muse is idle'}
          submitLabel="send"
          onSubmit={async text => {
            if (text.trim() !== '') await bridge($, ['steer', '--', text])
            await refresh($)
          }}
        />
        <Box>
          <Button key="interrupt" label="Interrupt" hotkey="i" onPress={async () => { await bridge($, ['interrupt', '--by', 'operator']); await refresh($) }} />
          <Button key="stop" label="Stop session" hotkey="s" onPress={async () => { await bridge($, ['stop']); await refresh($) }} />
        </Box>
      </Box>
    )
  })
}
