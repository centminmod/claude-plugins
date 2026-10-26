import { atom, read, update } from 'claude-code'
import type { EngineInterface, ModelUsage, ProcessRunResult, Register } from 'claude-code'

import type { Limit, Snapshot, WorkflowRun } from '../types'

const snapshot = atom({ plugin: 'desktop-statusline', key: 'snap' } as const, null)
const warned = atom({ plugin: 'desktop-statusline', key: 'warned' } as const, [])
const lastTurn = atom({ plugin: 'desktop-statusline', key: 'lastTurn' } as const, null)
const compactions = atom({ plugin: 'desktop-statusline', key: 'compactions' } as const, null)
const workflowRuns = atom({ plugin: 'desktop-statusline', key: 'runTally' } as const, {})
// An agent out this long without an answer stops counting as active: one killed without an
// answer, or a remote one, whose answer never reaches this session.
const AGENT_STALE_MS = 60 * 60_000
const MAX_RUNS = 20
// Answers from subagent loops that matched no tracked run yet: a fast workflow agent can answer
// before its spawn hook has recorded it. Newest last, a few kept.
const early = new Map<string, 'done' | 'failed'>()
const EARLY_MAX = 50

// Server tools (web search, advisor, ...) the API ran inside the main loop's current turn.
let serverTurnId: string | null = null
let serverTools: Record<string, number> = {}

const AGENT_ICON: Record<string, string> = { pending: '⋯', running: '⏳', waiting: '⏸', idle: '💤' }
const ACTIVE = ['pending', 'running', 'waiting']

const REFRESH_MS = 60_000
const WARN_AT = [95, 80]
const LABELS: Record<string, string> = { five_hour: '5-hour', seven_day: 'Weekly' }
const LABEL_CELLS = 8
const PERCENT_CELLS = 5
const DETAIL_GAP = 2
const METER_GAP = 3
const AGENT_ROWS = 3
// Desktop metrics in CSS px, measured from screenshots of the band: a Box `width` cell, one
// column of `bodyColumns`, and an average glyph of the band's proportional font. Bars are SVG
// in px, so the limits row is fitted in px, keeping 5% spare for the estimate.
const CELL_PX = 15
const COL_PX = 12.5
const CHAR_PX = 13
const SPARE = 0.95
const MIN_BAR_PX = 60
const MAX_BAR_PX = 180

const label = (kind: string) => LABELS[kind] ?? kind.replace(/_/g, ' ')
const tone = (percent: number) => (percent >= 95 ? 'error' : percent >= 80 ? 'warning' : undefined)
const tokens = (n: number) =>
  n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`

// Drawn as an image, so it cannot follow the theme: a translucent track reads on light and dark.
const bar = (percent: number, width: number) => {
  const fill = percent >= 95 ? '#e5484d' : percent >= 80 ? '#e0a030' : '#2f7de1'
  const filled = Math.round((Math.min(percent, 100) / 100) * width)
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="8" viewBox="0 0 ${width} 8">` +
    `<rect width="${width}" height="8" rx="4" fill="#808080" fill-opacity="0.3"/>` +
    `<rect width="${filled}" height="8" rx="4" fill="${fill}"/></svg>`
  )
}

const until = (iso: string, now: number) => {
  const minutes = Math.max(0, Math.round((Date.parse(iso) - now) / 60_000))
  if (minutes < 60) return `${minutes}m`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
  return `${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`
}

const elapsed = (ms: number) => {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

const ago = (ms: number) => {
  const minutes = Math.floor(ms / 60_000)
  if (minutes < 1) return '<1m'
  if (minutes < 60) return `${minutes}m`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
  return `${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`
}

const cacheHit = (u: ModelUsage) => {
  const input = u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
  return input === 0 ? null : Math.round((u.cache_read_input_tokens / input) * 100)
}

const isOnDesktop = async ($: EngineInterface) => (await $.session.surfaces()).includes('desktop')

// Each git command is written out in full at its call; this only reads the result.
const output = async (run: Promise<ProcessRunResult>) => {
  try {
    const ran = await run
    return ran.exitCode === 0 ? ran.stdout.trim() : null
  } catch {
    return null
  }
}

const warn = async ($: EngineInterface, limits: Limit[], now: number) => {
  const seen = await read($, warned)
  const fresh: string[] = []

  for (const limit of limits) {
    const threshold = WARN_AT.find(t => limit.percent >= t)
    const key = `${limit.kind}@${threshold}@${limit.resetsAt}`
    if (threshold === undefined || seen.includes(key)) continue

    fresh.push(key)
    const resets = limit.resetsAt ? ` · resets in ${until(limit.resetsAt, now)}` : ''
    const text = `${label(limit.kind)} usage limit at ${limit.percent}%${resets}`
    // A system notification where the person's channel delivers one; the toast otherwise.
    if (!(await $.ui.notify(text, { title: 'Usage limit' })).isSent) $.ui.toast(text, { timeoutMs: 8000 })
  }

  if (fresh.length > 0) await update($, warned, s => [...s, ...fresh].slice(-50))
}

const refresh = async ($: EngineInterface) => {
  if (!(await isOnDesktop($))) return

  const [usage, cwd, now, agents, prompts, tracked] = await Promise.all([
    // A local estimate (no API calls), asked for only its compaction window.
    $.session.usage({ breakdown: 'summary' }),
    $.session.cwd(),
    $.clock.now(),
    $.agent.list(),
    $.session.turns(),
    read($, workflowRuns),
  ])
  // `$.agent.list()` leaves a workflow's agents out, so runs are counted from their own spawn and
  // turn.complete events: one line per run while any agent is active and the run is not stale.
  const runs: WorkflowRun[] = []
  for (const [runId, run] of Object.entries(tracked)) {
    const active = Object.values(run.active).filter(at => now - at <= AGENT_STALE_MS).length
    if (active === 0) continue
    runs.push({ runId, total: active + run.done + run.failed, active, done: run.done, failed: run.failed })
  }
  const [status, dirs] = await Promise.all([
    output($.process.run(['git', 'status', '--porcelain=v2', '--branch'], { cwd, timeoutMs: 5000 })),
    output($.process.run(['git', 'rev-parse', '--git-dir', '--git-common-dir'], { cwd, timeoutMs: 5000 })),
  ])

  const lines = status?.split('\n') ?? []
  const head = lines.find(l => l.startsWith('# branch.head '))?.slice(14)
  const ab = lines.find(l => l.startsWith('# branch.ab '))?.match(/\+(\d+) -(\d+)/)
  const [gitDir, commonDir] = dirs?.split('\n') ?? []
  const limits = usage.rateLimits.map(r => ({
    kind: r.kind,
    percent: r.percentUsed,
    resetsAt: r.resetsAt ?? null,
  }))
  // Measure against the auto-compact window when one smaller than the model's is set (the
  // autoCompactWindow setting, /autocompact, CLAUDE_CODE_AUTO_COMPACT_WINDOW), as /context does.
  const { tokens: contextTokens, window: modelWindow, breakdown } = usage.context
  const contextWindow = breakdown?.isAutoCompactEnabled ? Math.min(modelWindow, breakdown.rawMaxTokens) : modelWindow

  const snap: Snapshot = {
    at: now,
    startedAt: usage.startedAt,
    prompts,
    dir: cwd.split('/').pop() || cwd,
    branch: head && head !== '(detached)' ? head : null,
    isWorktree: gitDir !== commonDir,
    ahead: Number(ab?.[1] ?? 0),
    behind: Number(ab?.[2] ?? 0),
    changed: lines.filter(l => l && !l.startsWith('#')).length,
    contextPercent: contextTokens === undefined ? null : Math.round((contextTokens / contextWindow) * 100),
    contextTokens: contextTokens ?? null,
    contextWindow,
    costUsd: usage.cost?.usd ?? null,
    limits,
    // Held and queued agents too, and teammates idle between turns; not finished ones.
    agents: agents
      .filter(a => ACTIVE.includes(a.status) || (a.status === 'idle' && a.teammateId))
      .map(a => ({ type: a.type, description: a.description, status: a.status })),
    runs,
  }

  await update($, snapshot, () => snap)
  await warn($, limits, now)
}

export const register: Register = (on, options) => {
  // The main conversation's prompt-cache TTL (the `cache_ttl` option): 1 hour on a Claude
  // subscription within plan usage, 5 minutes with pay-as-you-go API billing, a cloud provider or usage credits.
  const cacheTtlMs = options.cache_ttl === '5m' ? 5 * 60_000 : 60 * 60_000
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))

    return started
  })

  on('session.attach', { surface: 'desktop' }, async ($, e, next) => {
    const attached = await next(e)
    await refresh($)

    return attached
  })

  on('session.measure', async ($, e, next) => {
    const measured = await next(e)
    await refresh($)

    return measured
  })

  on('turn.complete', async ($, e, next) => {
    const completed = await next(e)
    if (!e.agentId) {
      const at = await $.clock.now()
      await update($, lastTurn, () => ({
        at,
        durationMs: e.durationMs,
        model: e.usage?.model ?? null,
        cacheHit: e.usage ? cacheHit(e.usage) : null,
        serverTools: serverTurnId === e.turnId ? serverTools : {},
      }))
    } else {
      // A workflow agent's answer: mark it in its run, if it is one this mod tracks.
      const agentId = e.agentId
      // Aborted, refused or errored all count as failed.
      const state = e.reason === 'answer' ? 'done' : 'failed'
      const runId = Object.entries(await read($, workflowRuns)).find(([, r]) => agentId in r.active)?.[0]
      if (runId === undefined) {
        early.set(agentId, state)
        if (early.size > EARLY_MAX) early.delete(early.keys().next().value!)
      } else {
        const at = await $.clock.now()
        await update($, workflowRuns, m => {
          const run = m[runId]
          if (!run || !(agentId in run.active)) return m
          const { [agentId]: _, ...active } = run.active
          return { ...m, [runId]: { ...run, at, active, [state]: run[state] + 1 } }
        })
      }
    }
    await refresh($)

    return completed
  })

  // A streaming event: forward the chunks untouched and read the step's result at the end.
  on('turn.step', async function* (_$, e, next) {
    const step = yield* next(e)
    if (e.agentId || !step.serverToolUses?.length) return step

    if (serverTurnId !== e.turnId) {
      serverTurnId = e.turnId
      serverTools = {}
    }
    for (const use of step.serverToolUses) serverTools[use.name] = (serverTools[use.name] ?? 0) + 1

    return step
  })

  on('agent.spawn', async ($, e, next) => {
    const spawned = await next(e)
    if (e.workflow && spawned.agentId) {
      const { runId } = e.workflow
      const agentId = spawned.agentId
      const at = await $.clock.now()
      const answered = early.get(agentId)
      early.delete(agentId)
      // Newest runs last; keep the last MAX_RUNS. Agents out past AGENT_STALE_MS are let go here.
      await update($, workflowRuns, m => {
        const { [runId]: run, ...rest } = m
        const kept = Object.fromEntries(Object.entries(run?.active ?? {}).filter(([, t]) => at - t <= AGENT_STALE_MS))
        const base = { at, done: run?.done ?? 0, failed: run?.failed ?? 0, active: kept }
        const touched = answered ? { ...base, [answered]: base[answered] + 1 } : { ...base, active: { ...kept, [agentId]: at } }
        return Object.fromEntries([...Object.entries(rest), [runId, touched]].slice(-MAX_RUNS))
      })
      // Not an Agent tool call, so the tool.call hook below misses it.
      $.clock.after(2000, () => void refresh($))
    }

    return spawned
  }).catch((_$, e, next) => next(e))

  // A spawned agent shows as running only once it has started; look again shortly after.
  on('tool.call', { tool: 'Agent' }, ($, e, next) => {
    $.clock.after(2000, () => void refresh($))

    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    const result = await next(e)
    if (e.trigger === 'precompute' || e.agentId || result.messages === undefined) return result

    const { startedAt } = await $.session.usage()
    await update($, compactions, c => ({
      since: startedAt,
      count: (c?.since === startedAt ? c.count : 0) + 1,
      before: result.tokensBefore ?? null,
      after: result.tokensAfter ?? null,
    }))

    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.surface !== 'desktop' || e.props.hasSurvey) return next(e)

    const snap = await read($, snapshot)
    if (snap === null) return next(e)

    const { Box, Svg, Text } = $.ui.resolve(e)

    const meter = (name: string, percent: number | null, detail: string, barPx: number) => (
      <Box flexDirection="row" alignItems="center">
        <Box width={LABEL_CELLS} flexShrink={0}>
          <Text>{name}</Text>
        </Box>
        {percent === null ? (
          <Text dimColor>{'waiting for first response'}</Text>
        ) : (
          <Box flexDirection="row" alignItems="center" flexShrink={0}>
            <Svg source={bar(percent, barPx)} alt={`${name} ${percent}% used`} width={barPx} height={8} />
            <Box width={PERCENT_CELLS} justifyContent="flex-end">
              <Text color={tone(percent)}>{`${percent}%`}</Text>
            </Box>
          </Box>
        )}
        <Box marginLeft={DETAIL_GAP} flexShrink={0}>
          <Text dimColor>{detail}</Text>
        </Box>
      </Box>
    )

    // Fit the limit meters on one row: try "resets in 2h 8m", then "↻ 2h 8m", and stack them
    // (the one case of 5 lines) only when even the short form leaves a bar under MIN_BAR_PX.
    const availPx = e.props.bodyColumns * COL_PX * SPARE
    const fixedPx = (LABEL_CELLS + PERCENT_CELLS + DETAIL_GAP) * CELL_PX
    const n = Math.max(1, snap.limits.length)
    const fit = (isLong: boolean) => {
      const details = snap.limits.map(l => (l.resetsAt ? `${isLong ? 'resets in ' : '↻ '}${until(l.resetsAt, snap.at)}` : ''))
      const textPx = details.reduce((sum, d) => sum + d.length * CHAR_PX, 0)
      const barPx = Math.floor((availPx - n * fixedPx - (n - 1) * METER_GAP * CELL_PX - textPx) / n)
      return { details, barPx: Math.min(MAX_BAR_PX, barPx) }
    }
    const long = fit(true)
    const { details, barPx: sharedBar } = long.barPx >= MIN_BAR_PX ? long : fit(false)
    const isStacked = sharedBar < MIN_BAR_PX
    const longestPx = Math.max(0, ...details.map(d => d.length * CHAR_PX))
    const limitBar = isStacked
      ? Math.max(40, Math.min(MAX_BAR_PX, Math.floor(availPx - fixedPx - longestPx)))
      : sharedBar
    const used = snap.contextTokens === null ? '' : `${tokens(snap.contextTokens)} / ${tokens(snap.contextWindow)}`
    const contextRoomPx = Math.floor(availPx - fixedPx - used.length * CHAR_PX)
    const contextBar = Math.max(40, Math.min(isStacked ? limitBar : 2 * limitBar, contextRoomPx))

    const where =
      `📁 ${snap.dir}` +
      (snap.branch === null ? '' : `   🌿 ${snap.branch}${snap.isWorktree ? ' 🌳' : ''}`) +
      `${snap.ahead ? ` ↑${snap.ahead}` : ''}${snap.behind ? ` ↓${snap.behind}` : ''}` +
      `${snap.changed ? `   ● ${snap.changed} changed` : ''}`
    const session =
      `session ${ago(snap.at - snap.startedAt)} · ${snap.prompts} prompt${snap.prompts === 1 ? '' : 's'}` +
      (snap.costUsd === null ? '' : ` · $${snap.costUsd.toFixed(2)}`)

    // Bottom line parts: dim unless they need attention (warning) or are live (agents).
    const activity: { text: string; emphasis?: 'warning' | 'live' }[] = []
    const turn = await read($, lastTurn)
    if (turn !== null && turn.at >= snap.startedAt) {
      const idleMs = Math.max(0, snap.at - turn.at)
      const isCold = idleMs >= cacheTtlMs
      activity.push({ text: `Last turn ${elapsed(turn.durationMs)}${turn.model ? ` on ${turn.model}` : ''}` })
      if (turn.cacheHit !== null) {
        activity.push({ text: `cache ${turn.cacheHit}%`, emphasis: turn.cacheHit < 50 ? 'warning' : undefined })
      }
      // Older snapshots (before serverTools existed) carry none.
      const used = Object.entries(turn.serverTools ?? {})
      if (used.length > 0) activity.push({ text: `🔎 ${used.map(([n, k]) => `${n.replace(/_/g, ' ')} ×${k}`).join(', ')}` })
      if (!e.props.isWorking) {
        activity.push({ text: `idle ${ago(idleMs)}${isCold ? ' (cache cold)' : ''}`, emphasis: isCold ? 'warning' : undefined })
      }
    }
    const c = await read($, compactions)
    if (c !== null && c.since === snap.startedAt) {
      const sizes = c.before === null || c.after === null ? '' : ` (last ${tokens(c.before)} → ${tokens(c.after)})`
      activity.push({ text: `compacted ${c.count}×${sizes}` })
    }
    // Running agents get rows of their own (description truncated, type kept), so a
    // long description never runs off the "Last turn" line.
    const agentRows = snap.agents.slice(0, AGENT_ROWS)
    const moreAgents = snap.agents.length - agentRows.length
    // The band is one site for every plugin: what the plugins beneath draw stays, under this one.
    const below = await next(e)

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between" width="100%">
          <Text wrap="truncate-end">{where}</Text>
          <Box marginLeft={2} flexShrink={0}>
            <Text dimColor>{session}</Text>
          </Box>
        </Box>
        {meter('Context', snap.contextPercent, used, contextBar)}
        {snap.limits.length > 0 && (
          <Box flexDirection={isStacked ? 'column' : 'row'} columnGap={METER_GAP}>
            {snap.limits.map((l, i) => meter(label(l.kind), l.percent, details[i] ?? '', limitBar))}
          </Box>
        )}
        {activity.length > 0 && (
          <Box flexDirection="row">
            {activity.map((a, i) => (
              <Box flexDirection="row">
                {i > 0 && <Text dimColor>{' · '}</Text>}
                <Text
                  wrap="truncate-end"
                  color={a.emphasis === 'warning' ? 'warning' : undefined}
                  dimColor={a.emphasis === undefined}
                >
                  {a.text}
                </Text>
              </Box>
            ))}
          </Box>
        )}
        {agentRows.map(a => (
          <Box flexDirection="row" justifyContent="space-between" width="100%">
            <Text wrap="truncate-end" dimColor={a.status !== 'running'}>
              {`${AGENT_ICON[a.status] ?? '⏳'} ${a.description || a.type}`}
            </Text>
            <Box marginLeft={2} flexShrink={0}>
              <Text dimColor>{a.type.split(':').pop()}</Text>
            </Box>
          </Box>
        ))}
        {(snap.runs ?? []).map(r => (
          <Box flexDirection="row" justifyContent="space-between" width="100%">
            <Text wrap="truncate-end">{`⚙ workflow ${r.runId} · ${r.active} active`}</Text>
            <Box marginLeft={2} flexShrink={0}>
              <Text dimColor={r.failed === 0} color={r.failed > 0 ? 'warning' : undefined}>
                {`${r.done}/${r.total} done${r.failed > 0 ? ` · ${r.failed} failed` : ''}`}
              </Text>
            </Box>
          </Box>
        ))}
        {moreAgents > 0 &&<Text dimColor>{`   +${moreAgents} more agent${moreAgents > 1 ? 's' : ''} running`}</Text>}
        {below}
      </Box>
    )
  })
}
