import { test, expect, mock } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { AgentInfo, On } from 'claude-code'

type World = { beneath?: string; agents: AgentInfo[]; weekly: number; isNotifySent: boolean; toasts: string[]; notes: string[] }

// The engine beneath the plugin: a desktop session in a clean repo, its agents and limits from `w`.
const world = (on: On, w: World) => {
  const clock = mock.clock(on, { now: 1_000_000 })
  on('session.surfaces', () => ({ value: ['desktop'] }) as never)
  on('session.cwd', () => ({ value: '/p/repo' }) as never)
  on('session.turns', () => ({ value: 3 }) as never)
  on('session.usage', () =>
    ({
      value: {
        startedAt: 0,
        context: { tokens: 50_000, window: 1_000_000 },
        cost: { usd: 1.5 },
        rateLimits: [{ kind: 'seven_day', percentUsed: w.weekly, resetsAt: null }],
      },
    }) as never,
  )
  on('agent.list', () => ({ value: w.agents }) as never)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '# branch.head main\n', stderr: '' } }) as never)
  on('ui.notify', (_$, e) => {
    w.notes.push(e.text)
    return { value: w.isNotifySent ? { isSent: true, channel: 'terminal_bell' } : { isSent: false, reason: 'no-surface' } } as never
  })
  on('ui.toast', (_$, e) => {
    w.toasts.push(e.text)
    return { value: undefined } as never
  })
  let n = 0
  // A remote workflow agent has no local loop, so no agentId: spelled here by description 'remote'.
  on('agent.spawn', (_$, e) =>
    (e.description === 'remote' ? { model: 'claude-sonnet-5-5' } : { model: 'claude-sonnet-5-5', agentId: `w${++n}` }) as never,
  )
  on('turn.complete', (_$, e) => ({ text: e.answer }) as never)
  // Stand-in for the engine's drawing beneath the plugins (nothing, unless a test hooks above it).
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    return (w.beneath ? h(Box, { key: 'beneath' }, h(Text, {}, w.beneath)) : h(Box, { key: 'engine' })) as never
  })
  // Beneath the plugin, a model request that ran two web searches and the advisor.
  on('turn.step', async function* (_$, e) {
    const use = (name: string) => ({ id: name, name, input: {}, startedAt: 0, endedAt: 1 })
    return {
      turnId: e.turnId,
      index: e.index,
      answer: '',
      toolUses: [],
      serverToolUses: e.agentId ? [use('web_search')] : [use('web_search'), use('web_search'), use('advisor')],
      stopReason: 'end_turn',
      usage: null,
    } as never
  })
  return clock
}

const agent = (id: string, status: AgentInfo['status'], extra: Partial<AgentInfo> = {}): AgentInfo => ({
  id,
  description: `task ${id}`,
  type: 'general-purpose',
  status,
  ...extra,
})

const bandText = async ($: Engine) => {
  const band = await $.ui.mount({
    plugin: 'desktop-statusline',
    surface: 'desktop',
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, bodyColumns: 140 } as never,
  })
  const text = (await band.findAll({ type: 'Text' })).map(t => (t as { text?: string }).text ?? '').join('\n')
  await band.unmount()
  return text
}

const drainStep = async ($: Engine, turnId: string, agentId?: string) => {
  for await (const _ of $.turn.step({ turnId, index: 0, model: 'm', messageCount: 1, agentId } as never)) {
    // forwarded chunks; the result is what the plugin reads
  }
}

const completeTurn = ($: Engine, turnId: string) =>
  $.turn.complete({ answer: 'ok', durationMs: 4000, isAborted: false, turnId, reason: 'answer' } as never)

const wf = (runId: string, agentIndex: number, description = 'd') =>
  ({ prompt: 'p', description, subagentType: 'general-purpose', workflow: { runId, agentIndex } }) as never

const agentAnswer = ($: Engine, agentId: string, extra: object = {}) =>
  $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: `t-${agentId}`, agentId, reason: 'answer', ...extra } as never)

// As the engine does, `agent.list` here never names a workflow's agents: the run row is built
// from the spawns and the agents' own answers.
test('a workflow run is one row, counted from its spawns and answers', async ($, on) => {
  const w: World = { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)

  await $.agent.spawn(wf('wf_abc123', 1)) // w1
  await $.agent.spawn(wf('wf_abc123', 2)) // w2
  await $.agent.spawn(wf('wf_abc123', 3)) // w3
  await completeTurn($, 't0')
  expect(await bandText($)).toContain('⚙ workflow wf_abc123 · 3 active')

  await agentAnswer($, 'w2')
  await agentAnswer($, 'w3', { isAborted: true, reason: 'aborted' })
  const text = await bandText($)
  expect(text).toContain('⚙ workflow wf_abc123 · 1 active')
  expect(text).toContain('1/3 done · 1 failed')

  // The last agent answers: the run row goes.
  await agentAnswer($, 'w1')
  expect(await bandText($)).not.toContain('wf_abc123')

  // A later phase of the same run brings it back with the earlier counts kept.
  await $.agent.spawn(wf('wf_abc123', 4)) // w4
  await completeTurn($, 't1')
  expect(await bandText($)).toContain('2/4 done · 1 failed')
})

test('an ordinary subagent is never folded into a run, and its answer touches no run', async ($, on) => {
  const w: World = { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)
  await $.agent.spawn(wf('wf_one', 1)) // w1
  await $.agent.spawn({ prompt: 'p', description: 'plain', subagentType: 'general-purpose' } as never) // w2
  w.agents = [agent('w2', 'running')]
  await agentAnswer($, 'w2')
  const text = await bandText($)
  expect(text).toContain('⏳ task w2')
  expect(text).toContain('⚙ workflow wf_one · 1 active')
  expect(text).toContain('0/1 done')
})

test('a refused or errored answer counts as failed', async ($, on) => {
  world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })
  for (let i = 1; i <= 3; i++) await $.agent.spawn(wf('wf_bad', i)) // w1..w3
  await agentAnswer($, 'w1', { reason: 'refusal' })
  await agentAnswer($, 'w2', { reason: 'error' })
  expect(await bandText($)).toContain('0/3 done · 2 failed')
})

test('an answer that lands before its spawn is recorded still counts', async ($, on) => {
  world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })
  // w1 answers first, then the spawn hook records it: the run never shows w1 as active.
  await agentAnswer($, 'w1')
  await $.agent.spawn(wf('wf_fast', 1)) // w1
  await $.agent.spawn(wf('wf_fast', 2)) // w2
  await completeTurn($, 't0')
  const text = await bandText($)
  expect(text).toContain('⚙ workflow wf_fast · 1 active')
  expect(text).toContain('1/2 done')
})

test('an agent out an hour with no answer stops counting, and a later spawn does not revive it', async ($, on) => {
  const clock = world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })
  await $.agent.spawn(wf('wf_stuck', 1)) // w1, never answers
  await completeTurn($, 't0')
  expect(await bandText($)).toContain('wf_stuck · 1 active')
  await clock.advance(61 * 60_000)
  await completeTurn($, 't1')
  expect(await bandText($)).not.toContain('wf_stuck')
  // A later phase of the same run: only the new agent is active.
  await $.agent.spawn(wf('wf_stuck', 2)) // w2
  await completeTurn($, 't2')
  expect(await bandText($)).toContain('wf_stuck · 1 active')
})

test('a workflow spawn with no local agentId (remote) adds no run', async ($, on) => {
  world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })
  await $.agent.spawn(wf('wf_remote', 1, 'remote'))
  await completeTurn($, 't0')
  expect(await bandText($)).not.toContain('wf_remote')
})

test('only the newest 20 runs are kept', async ($, on) => {
  world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })
  for (let i = 1; i <= 21; i++) await $.agent.spawn(wf(`wf_${i}`, 1))
  await completeTurn($, 't0')
  const text = await bandText($)
  expect(text).not.toContain('wf_1 ')
  expect(text).toContain('wf_2 ')
  expect(text).toContain('wf_21 ')
})

test('other agents show their state', async ($, on) => {
  const w: World = { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)
  w.agents = [
    agent('a1', 'running'),
    agent('a2', 'waiting'),
    agent('a3', 'pending'),
    agent('t1', 'idle', { teammateId: 'rev@team' }),
    agent('a4', 'idle'),
    agent('a5', 'completed'),
  ]
  await completeTurn($, 't0')

  const text = await bandText($)
  expect(text).toContain('⏳ task a1')
  expect(text).toContain('⏸ task a2')
  expect(text).toContain('⋯ task a3')
  // Shown: at most AGENT_ROWS (3) rows, the rest counted; the idle teammate is the 4th.
  expect(text).toContain('+1 more agent running')
  // Not shown: a plain agent that went idle, a finished one.
  expect(text).not.toContain('task a4')
  expect(text).not.toContain('task a5')
})

test('the idle teammate shows when there is room', async ($, on) => {
  const w: World = { agents: [agent('t1', 'idle', { teammateId: 'rev@team' })], weekly: 10, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)
  await completeTurn($, 't0')
  expect(await bandText($)).toContain('💤 task t1')
})

test('server tools of the main loop are counted per turn', async ($, on) => {
  world(on, { agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] })

  await drainStep($, 't1')
  await drainStep($, 't1')
  // A subagent's step does not count toward the main turn.
  await drainStep($, 't1', 'sub-1')
  await completeTurn($, 't1')
  expect(await bandText($)).toContain('🔎 web search ×4, advisor ×2')

  // A new turn counts afresh.
  await drainStep($, 't2')
  await completeTurn($, 't2')
  expect(await bandText($)).toContain('🔎 web search ×2, advisor ×1')

  // A turn with no server tools clears the item.
  await completeTurn($, 't3')
  expect(await bandText($)).not.toContain('🔎')
})

test('a usage-limit warning goes out as a notification, the toast only when none was sent', async ($, on) => {
  const w: World = { agents: [], weekly: 96, isNotifySent: true, toasts: [], notes: [] }
  world(on, w)
  await completeTurn($, 't1')
  expect(w.notes).toEqual(['Weekly usage limit at 96%'])
  expect(w.toasts).toEqual([])
})

test('the toast stands in when no notification channel sent it', async ($, on) => {
  const w: World = { agents: [], weekly: 85, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)
  await completeTurn($, 't1')
  expect(w.notes).toEqual(['Weekly usage limit at 85%'])
  expect(w.toasts).toEqual(['Weekly usage limit at 85%'])
  // Warned once per threshold: a later refresh at the same level stays quiet.
  await completeTurn($, 't2')
  expect(w.notes).toHaveLength(1)
})

test('the band keeps what the plugins beneath draw', async ($, on) => {
  const w: World = { beneath: 'BENEATH-MARKER', agents: [], weekly: 10, isNotifySent: false, toasts: [], notes: [] }
  world(on, w)
  await completeTurn($, 't0')
  const text = await bandText($)
  expect(text).toContain('Context')
  expect(text).toContain('BENEATH-MARKER')
})
