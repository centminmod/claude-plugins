# Instance quick audit playbook

This is the playbook for `audit-session-metrics {quick|detailed}` when
`digest.scope == "instance"`. The input JSON is an `instance/*/index.json`
export covering all projects across the entire Claude Code installation.

Both quick and detailed modes use this same playbook — there is no additional
drilldown available because instance-scope JSON contains only per-session
summaries (no per-turn data).

## Key constraints

- **No per-turn data.** `digest.top_expensive_turns` is `[]`. Do not attempt
  to surface turn-level findings.
- **No fired triggers.** `digest.fired_triggers` is `[]` — per-turn trigger
  evaluation is not possible at instance scope. Omit the Findings section.
- **Positive triggers may fire** if `totals.cache_savings` is populated.
  At instance scope this field may be `null` (not always computed). If the
  `digest.positive_triggers` array is empty, omit the Positive findings section.
- Primary analysis comes from `digest.instance_analysis`.

## Workflow

Run the extract helper:

```
scripts/audit-extract.py <instance/*/index.json> --mode quick
```

(Mode `quick` or `detailed` both produce the same digest at instance scope —
`--mode` is accepted but has no effect on the analysis.)

## Output contract

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json`
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md`
3. **Inline chat output** — the markdown content printed in the assistant's reply.

`<id8>` = `digest.session_id_short` (will be `"instance"`),
`<ts>` = `digest.ts_str` (e.g. `"2026-04-29-034750"`).

## JSON schema (v1.3)

```jsonc
{
  "audit_schema_version": "1.3",
  "scope": "instance",
  "mode": "quick",
  "session_id_short": "<copy from digest.session_id_short>",
  "generated_at": "<ISO8601 UTC>",
  "input_json": "<copy from digest.input_json>",

  "baseline": { /* copy from digest.baseline verbatim */ },

  "findings": [],           // always empty at instance scope

  "positive_findings": [ /* copy from digest.positive_triggers; cap at 3; omit if empty */ ],

  "instance_analysis": { /* copy from digest.instance_analysis verbatim */ },

  "fix_first": [ /* 1–3 verb-led bullets synthesising actionable cross-project patterns;
                    omit if nothing concrete to recommend */ ]
}
```

## Instance analysis section

`digest.instance_analysis` has four sub-sections:

- `top_expensive_projects` — top 5 projects by cost with `cost_share_pct`,
  `session_count`, `turn_count`. Surface the top 1–2 clearly in the report.
- `poor_cache_health_projects` — projects with avg session cache_hit_pct < 80%
  and cost > $0.10. Each entry has `slug`, `avg_cache_hit_pct`, `cost_usd`,
  `gap_from_avg_pp`. If non-empty, name the worst project.
- `instance_cache_hit_avg_pct` — overall cache hit average across all projects.
- `total_projects` / `total_sessions` — scope breadth.

## Weekly trend

`digest.baseline.weekly_rollup` provides trailing vs prior 7-day cost and cache
health delta (same structure as project scope). If present and `prior_7d_cost_usd > 0`,
report cost delta as +/-% and cache delta in pp.

## Markdown render template

```markdown
# Instance audit — {session_id_short} @ {generated_at}

## 1. Baseline

**{baseline.projects_count} projects**, **{baseline.sessions_count:,} sessions**,
**{baseline.turns:,} turns**. Total cost **${baseline.total_cost_usd:,.2f}**.
Cache hit ratio **{baseline.cache_hit_pct:.1f}%**{cache_savings_clause}.

{if baseline.weekly_rollup is not null:}
**Last 7 days:** ${trailing_7d_cost_usd:.2f} ({cost_delta_pct:+.1f}% vs prior week).
Cache hit {trailing_7d_cache_hit_pct:.1f}% ({cache_hit_delta_pp:+.1f}pp).

## 2. Project breakdown

**Top projects by cost:**
| Project | Cost | Share | Sessions | Turns |
|---------|------|-------|----------|-------|
{for each project in instance_analysis.top_expensive_projects:}
| {slug_short} | ${cost_usd:.2f} | {cost_share_pct:.1f}% | {session_count} | {turn_count:,} |

{if instance_analysis.poor_cache_health_projects is non-empty:}
**Projects with poor cache health (<{instance_analysis.poor_cache_threshold_pct:.0f}%):**
{for each project in poor_cache_health_projects:}
- {slug_short}: {avg_cache_hit_pct:.1f}% hit ({gap_from_avg_pp:+.1f}pp vs avg), ${cost_usd:.2f}

## 3. Positive findings

{omit if positive_findings is empty}
{for each positive finding:}
- 🟢 {title} — {evidence_inline}{savings_suffix}

## 4. What to fix first

{omit if fix_first is empty}
{for each bullet in fix_first:}
- {bullet}
```

Render rules:
- `{slug_short}`: display the slug without the leading `-Volumes-...` path prefix
  where it aids readability (e.g. `session-metrics` instead of
  `-Volumes-AMZ3-AI-vibe-coding-session-metrics`). Keep the full slug in the JSON.
- `{cache_savings_clause}`: if `baseline.cache_savings_usd > 0`,
  append ` — caching saved $<savings:.2f>`.
- `{savings_suffix}`: if `estimated_savings_usd` non-null, append ` — saved $<n:.2f>`.
- Omit sections 3 and 4 entirely if their sources are empty.

The markdown copy on disk includes the H1. The inline chat version omits it.

## Finding cap

No negative findings at instance scope (always `[]`). Positive findings capped
at 3. `"other"` enum forbidden.

## Final step (write order)

1. Run `scripts/audit-extract.py <input-json> --mode quick` once.
2. Build the audit JSON using digest values.
3. Write JSON sidecar to `exports/session-metrics/audit_<id8>_<ts>_quick.json`.
4. Render to markdown using the template.
5. Write markdown copy to `exports/session-metrics/audit_<id8>_<ts>_quick.md`.
6. Print the same markdown inline (without the H1 heading).
7. Print two `[audit] saved → <path>` lines.
