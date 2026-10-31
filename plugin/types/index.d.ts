export type Line = { seq: number; t: number; kind: string; text: string }
export type Pending = { id: string; summary: string; kind: string; isJudgeUnsure: boolean; isRisky: boolean }
export type Snap = {
  isUp: boolean
  workspace: string
  isSandboxed: boolean
  isRunning: boolean
  pending: Pending[]
  feed: Line[]
  diff: string
}

declare module 'claude-code' {
  interface PluginState {
    mentor: { snap: Shaped<Snap | null>; armed: Shaped<string> }
  }
}
