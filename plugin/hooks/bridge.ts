// Shared by the tools and the pane (both in register.tsx): one-shot calls of the bridge CLI.
export const NAME = 'mentor'

// Where muse-bridge.py lives: $MUSE_BRIDGE, else ~/.local/bin/muse-bridge.py (see the README).
export async function bridgePath($: any): Promise<string> {
  return (await $.env.get('MUSE_BRIDGE')) ?? `${(await $.env.get('HOME')) ?? ''}/.local/bin/muse-bridge.py`
}

// `-n NAME` goes straight after the subcommand, so a caller can end its options with `--` and pass
// text from Claude or the operator after it without a word of it being read as a flag.
export async function bridge($: any, [op, ...args]: string[], timeoutMs = 30_000): Promise<string> {
  const r = await $.process.run(['python3', await bridgePath($), op, '-n', NAME, ...args], { timeoutMs })
  return r.exitCode === 0 ? r.stdout.trim() : `bridge failed (${r.exitCode}): ${(r.stderr || r.stdout).trim()}`
}
