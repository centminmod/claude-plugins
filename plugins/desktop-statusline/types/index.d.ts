export type Limit = { kind: string; percent: number; resetsAt: string | null }

export type Agent = {
  type: string
  description: string
  status: 'pending' | 'running' | 'waiting' | 'idle' | 'completed' | 'failed' | 'killed'
}

export type WorkflowRun = { runId: string; total: number; active: number; done: number; failed: number }

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
  runs: WorkflowRun[]
}

export type TurnStat = {
  at: number
  durationMs: number
  model: string | null
  cacheHit: number | null
  serverTools: Record<string, number>
}

export type Compactions = { since: number; count: number; before: number | null; after: number | null }

declare module 'claude-code' {
  interface PluginState {
    'desktop-statusline': {
      snap: Snapshot | null
      warned: string[]
      lastTurn: TurnStat | null
      compactions: Compactions | null
      // Workflow runs by run id, from `agent.spawn` and each agent's own `turn.complete`
      // (`$.agent.list()` leaves workflow agents out): when last touched, finished counts, and the
      // agents still out, each with its spawn time.
      runTally: Record<string, { at: number; done: number; failed: number; active: Record<string, number> }>
    }
  }
}
