import { expect, mock, test } from 'claude-code/testing'


const PANE = {
  plugin: 'mentor',
  component: 'Pane',
  requestId: 'mentor',
  props: { title: 'Mentor', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

const SNAP = {
  isUp: true,
  workspace: '/work/repo',
  isRunning: true,
  pending: [{ id: 'a1', summary: 'rm -rf build', kind: 'shell', isJudgeUnsure: false, isRisky: false }],
  feed: [
    { seq: 1, t: 0, kind: 'mentor', text: 'write fizzbuzz' },
    { seq: 2, t: 1, kind: 'muse', text: 'done, tests pass' },
  ],
  diff: '?? fizzbuzz.py',
}

test('with no session the pane says so', async ($, on) => {
  on('clock.now', () => ({ value: 0 }))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /No Muse session/ }))?.text).toBeTruthy()
  await ui.unmount()
})

test('a pending approval shows its command and Allow/Deny', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'snap-1', value: SNAP }, version: 1 } }))
  on('clock.now', () => ({ value: 0 }))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const seen = {
    cmd: await ui.find({ type: 'Text', text: /rm -rf build/ }),
    allow: await ui.find({ type: 'Button', key: 'allow-a1' }),
    deny: await ui.find({ type: 'Button', key: 'deny-a1' }),
    muse: await ui.find({ type: 'Text', text: /done, tests pass/ }),
  }
  expect(Object.entries(seen).filter(([, v]) => !v).map(([k]) => k)).toEqual([])
  await ui.unmount()
})

test('a risky approval has no one-press Allow: it arms first', async ($, on) => {
  const risky = { ...SNAP, pending: [{ id: 'r1', summary: 'cat ~/.ssh/config', kind: 'shell', isJudgeUnsure: false, isRisky: true }] }
  on('clock.now', () => ({ value: 0 }))
  on('state.get', () => ({ value: { value: { shape: 'snap-1', value: risky }, version: 1 } }))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /RISKY/ })).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'arm-r1' })).toBeTruthy()
  expect(await ui.find({ type: 'Button', key: 'allow-r1' })).toBeFalsy()
  expect(await ui.find({ type: 'Button', key: 'deny-r1' })).toBeTruthy()
  await ui.unmount()
})

test('with two questions only the first card has the a/d hotkeys', async ($, on) => {
  const two = { ...SNAP, pending: [SNAP.pending[0], { ...SNAP.pending[0], id: 'a2', summary: 'touch b.txt' }] }
  on('clock.now', () => ({ value: 0 }))
  on('state.get', () => ({ value: { value: { shape: 'snap-1', value: two }, version: 1 } }))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const first = await ui.find({ type: 'Button', key: 'deny-a1' })
  const second = await ui.find({ type: 'Button', key: 'deny-a2' })
  expect([first?.props.hotkey, second?.props.hotkey]).toEqual(['d', undefined])
  await ui.unmount()
})

test('Deny sends the operator decision with `--` before the id', async ($, on) => {
  // the pane's state in memory, so the press can drop the card
  const held: Record<string, { value: unknown; version: number }> = { snap: { value: { shape: 'snap-1', value: SNAP }, version: 1 } }
  on('state.get', (_: any, e: any) => ({ value: held[e.key] ?? { value: undefined, version: 0 } }))
  on('state.set', (_: any, e: any) => {
    held[e.key] = { value: e.value, version: (held[e.key]?.version ?? 0) + 1 }
    return { value: { isSet: true, version: held[e.key]!.version } }
  })
  const argvs: string[][] = []
  on('process.run', (_: any, e: any) => {
    argvs.push(e.argv)
    return { value: { exitCode: 0, stdout: '{"ok": true}', stderr: '' } }
  })
  on('clock.now', () => ({ value: 0 }))
  mock.env(on, { HOME: '/home/t' })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'deny-a1' })
  await new Promise(r => setTimeout(r, 50))
  expect(argvs[0]).toEqual(['python3', '/home/t/.local/bin/muse-bridge.py', 'decide', '-n', 'mentor',
    '--feedback=The operator denied this.', '--by', 'operator', '--', 'a1', 'deny'])
  await ui.unmount()
})
