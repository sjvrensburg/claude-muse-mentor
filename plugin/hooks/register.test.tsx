import { expect, test } from 'claude-code/testing'


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
