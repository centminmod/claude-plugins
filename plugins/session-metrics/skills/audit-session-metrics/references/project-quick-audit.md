# Project quick audit playbook

This is the playbook for `audit-session-metrics quick` when `digest.scope == "project"`.
The input JSON is a `project_*.json` export covering all sessions in one project.
Aim: a short, decision-quality plain-English audit of project-wide usage patterns.

## Key differences from session scope

- No per-turn `top_expensive_turns` — `digest.top_expensive_turns` is populated from
  the flattened turn array but is less meaningful across 100s of sessions; use it
  for context only. The primary drilldown is `digest.project_analysis`.
- `session_archetype` is `"n/a"` — do not narrate it.
- `digest.fired_triggers` suppresses session-only metrics (`idle_gap_cache_decay`,
  `session_warmup_overhead`). All other trigger metrics fire normally.
- `digest.baseline` adds `sessions_count`, `cost_per_session_avg_usd`, and
  `weekly_rollup` (trailing vs prior 7-day cost + cache health).
- `digest.project_analysis` contains the four per-session breakdowns — read it
  alongside `fired_triggers`.

## Workflow

Run the extract helper once:

```
scripts/audit-extract.py <project_*.json> --mode quick
```

Read the digest. Then produce the three artefacts in the output contract.

## Output contract

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json`
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md`
3. **Inline chat output** — the markdown content printed in the assistant's reply.

`<id8>` = `digest.session_id_short` (will be `"project"`),
`<ts>` = `digest.ts_str` (e.g. `"20260429T031942Z"`).

## JSON schema (v1.3)

```jsonc
{
  "audit_schema_version": "1.3",
  "scope": "project",
  "mode": "quick",
  "session_id_short": "<copy from digest.session_id_short>",
  "generated_at": "<ISO8601 UTC>",
  "input_json": "<copy from digest.input_json>",

  "baseline": { /* copy from digest.baseline verbatim */ },

  "findings": [ /* fired_triggers as finding objects; cap at 7; see Finding object below */ ],

  "positive_findings": [ /* positive_triggers; cap at 3; omit array if empty */ ],

  "project_analysis": { /* copy from digest.project_analysis verbatim */ },

  "fix_first": [ /* 3 verb-led bullets; synthesise — do not paraphrase findings */ ]
}
```

### Finding object (same shape as session scope)

```jsonc
{
  "rank": <1..N, ordered high → low severity then by descending estimated_impact_usd>,
  "severity": "high" | "medium" | "low",
  "metric": "<copy digest.fired_triggers[i].metric>",
  "title": "<≤80 chars — states what is wrong>",
  "evidence": { /* copy digest.fired_triggers[i].evidence verbatim */ },
  "fix": "<one paragraph; concrete action; cite actual numbers>",
  "estimated_impact_usd": <copy from digest or null>
}
```

Applicable negative metrics at project scope (session-only metrics are pre-suppressed):

| `metric` | Trigger | Default severity |
|----------|---------|------------------|
| `cache_break` | `cache_breaks` non-empty | medium |
| `top_turn_share` | top single turn > 30% of total cost | high |
| `input_output_ratio_uncached` | uncached_input/output > 50:1 AND cache_hit < 60% | high |
| `subagent_share` | subagent_share_pct > 50 | medium |
| `cache_ttl_1h_unused` | extra_1h_cost > 0 AND cache_read < 50% of cache_write_1h | medium |
| `tool_result_bloat` | turn with cache_write > 50K after Bash/Read/WebFetch | medium |
| `heavy_reader_tools` | Read or WebFetch in tool_names_top3 | low |
| `cache_savings_low` | cache_savings < 10% of cost | low |
| `thinking_engagement_high` | thinking_turn_pct > 30 | low |
| `truncated_outputs` | any turn with stop_reason="max_tokens" | low |
| `advisor_share` | advisor_cost_usd > 5% of cost | low |

## Project analysis section

`digest.project_analysis` contains four sub-sections. Reference them in findings
prose and in the dedicated "Project session breakdown" report section:

- `top_expensive_sessions` — top 5 sessions by cost, with `cost_share_pct`, `turns`,
  `cache_hit_pct`. Name the most expensive session by its `first_ts` and
  `session_id_short` and quote its cost share.
- `poor_cache_health_sessions` — sessions with `cache_hit_pct < 80%` costing > $0.01.
  Sorted ascending by cache_hit_pct. Each entry includes `gap_from_avg_pp`
  (how far below project average). If any exist, surface the worst case.
- `sessions_with_cache_breaks` — sessions that had cache break events, sorted by
  `break_count` desc. Cross-reference with `fired_triggers.cache_break` if it fired.
- `project_cache_hit_avg_pct` — project-wide average cache hit %. Use for context
  when discussing `poor_cache_health_sessions`.

## Weekly trend

`digest.baseline.weekly_rollup` provides trailing vs prior 7-day cost and cache
health delta. If present and `prior_7d_cost_usd > 0`:
- Report cost delta as a +/- percentage and note direction (rising/falling).
- Report cache hit delta in percentage points.
- Include this in the Baseline section of the markdown.

## Markdown render template

```markdown
# Project quick audit — {session_id_short} @ {generated_at}

## 1. Baseline

Project total **${baseline.total_cost_usd:.2f}** across **{baseline.sessions_count} sessions**
({baseline.turns:,} turns), avg **${baseline.cost_per_session_avg_usd:.2f}/session**.
Cache hit ratio **{baseline.cache_hit_pct:.1f}%** — caching saved
**${baseline.cache_savings_usd:.2f}** vs a no-cache run.

{if baseline.weekly_rollup is not null:}
**Last 7 days:** ${trailing_7d_cost_usd:.2f} ({cost_delta_pct:+.1f}% vs prior week).
Cache hit {trailing_7d_cache_hit_pct:.1f}% ({cache_hit_delta_pp:+.1f}pp).

## 2. Project session breakdown

**Top expensive sessions:**
| Session | Date | Cost | Share | Turns | Cache hit |
|---------|------|------|-------|-------|-----------|
{for each session in project_analysis.top_expensive_sessions:}
| {session_id_short} | {first_ts[:10]} | ${cost_usd:.2f} | {cost_share_pct:.1f}% | {turns} | {cache_hit_pct:.1f}% |

{if project_analysis.poor_cache_health_sessions is non-empty:}
**Sessions with poor cache health (<{project_analysis.poor_cache_threshold_pct:.0f}%):**
{for each session in poor_cache_health_sessions (top 5):}
- {session_id_short} ({first_ts[:10]}): {cache_hit_pct:.1f}% hit ({gap_from_avg_pp:+.1f}pp vs avg), ${cost_usd:.4f}

{if project_analysis.sessions_with_cache_breaks is non-empty:}
**Sessions with cache breaks:**
{for each session in sessions_with_cache_breaks (top 5):}
- {session_id_short} ({first_ts[:10]}): {break_count} break(s), ${cost_usd:.4f}

## 3. Findings

| # | Severity | Finding | Evidence | Fix |
|---|----------|---------|----------|-----|
{for each finding in findings, in order:}
| {rank} | {severity_emoji} {severity} | {title} | {evidence_inline} | {fix} |

## 4. Positive findings

{omit if positive_findings is empty}
{for each positive finding:}
- 🟢 {title} — {evidence_inline}{savings_suffix}

## 5. What to fix first

{for each bullet in fix_first:}
- {bullet}
```

Render rules follow the same conventions as `quick-audit.md`:
- `{severity_emoji}`: 🔴 high, 🟡 medium, 🟢 low.
- `{evidence_inline}`: comma-separated `key=value` pairs.
- `{savings_suffix}`: if `estimated_savings_usd` non-null, append ` — saved $<n:.2f>`.
- Omit sections 4 and 5 bullets if their sources are empty.

The markdown copy on disk includes the `# Project quick audit —` H1.
The inline chat version omits it.

## Finding cap

Same rules as session scope: negative findings capped at 7, positive at 3,
no floor, `"other"` enum forbidden.

## Final step (write order)

> **IMPORTANT — use the Write tool directly. Do NOT generate a Python script to
> produce the JSON or markdown.** Creating an intermediate script adds unnecessary
> failure modes. Steps 2–5 are performed by the AI itself using the Write tool.

1. Run `scripts/audit-extract.py <input-json> --mode quick` once.
2. Build the audit JSON using digest values (in the AI's own context).
3. Call the **Write tool** to write JSON sidecar to `exports/session-metrics/audit_<id8>_<ts>_quick.json`.
4. Render to markdown using the template (in the AI's own context).
5. Call the **Write tool** to write markdown copy to `exports/session-metrics/audit_<id8>_<ts>_quick.md`.
6. Print the same markdown inline (without the H1 heading).
7. Print two `[audit] saved → <path>` lines.
