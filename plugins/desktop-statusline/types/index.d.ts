export type Limit = { kind: string; percent: number; resetsAt: string | null }

export type Agent = { type: string; description: string }

export type Snapshot = {
  at: number
  startedAt: number
  prompts: number
  dir: string
  branch: string | null
  isWorktree: boolean
  ahead: number
  behind: number
  changed: number
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number
  costUsd: number | null
  limits: Limit[]
  agents: Agent[]
}

export type TurnStat = {
  at: number
  durationMs: number
  model: string | null
  cacheHit: number | null
}

export type Compactions = { since: number; count: number; before: number | null; after: number | null }

declare module 'claude-code' {
  interface PluginState {
    'desktop-statusline': {
      snap: Snapshot | null
      warned: string[]
      lastTurn: TurnStat | null
      compactions: Compactions | null
    }
  }
}
