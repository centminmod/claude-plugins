# Project detailed audit playbook

This is the playbook for `audit-session-metrics detailed` when `digest.scope == "project"`.
Extends the project quick audit with a per-session turn-level drilldown on the
most expensive sessions (available because project JSON includes full turn arrays).

## Relationship to project-quick-audit.md

Run the same extract helper with `--mode detailed`:

```
scripts/audit-extract.py <project_*.json> --mode detailed
```

Read [`references/project-quick-audit.md`](project-quick-audit.md) first —
all quick-mode sections apply verbatim. Then add the **Detailed extensions**
described below.

## Detailed extensions (run after the quick sections)

### Extension A — Per-session turn drilldown

The project JSON contains full turn arrays for every session. The extract
helper's `digest.top_expensive_turns` is populated from all turns flattened
across every session. Use this to surface turn-level patterns within the
most expensive sessions identified by `project_analysis.top_expensive_sessions`.

For the top-3 expensive sessions (from `project_analysis.top_expensive_sessions`):

1. Report the `session_id_short`, date (`first_ts[:10]`), total cost, and turn count.
2. Pull the top-3 expensive turns for that session from `digest.top_expensive_turns`
   (match by `turn_index` range if session boundaries are inferrable, or just
   report the project-wide top-3 and note which session they belong to).
3. Flag any `is_cache_break` correlations.

Keep the drilldown to 3–5 lines per session. The goal is to identify *why* a
session was expensive (a single large turn? many medium turns? a warmup spike?),
not to reproduce the full turn table.

### Extension B — Model distribution by session

From `digest.baseline.models`, report how turns are split across models. Identify
whether Opus turns dominate in expensive sessions. Do not re-derive costs; use
the evidence from `fired_triggers` if `subagent_share` or `thinking_engagement_high`
fired.

### Extension C — Cache health outlier diagnosis

For `project_analysis.poor_cache_health_sessions`, attempt a root-cause hypothesis
for the worst session (lowest cache_hit_pct). Options:

- Very short session (few turns → no cache warm-up opportunity).
- Session starts after a long idle gap (check `sessions_with_cache_breaks` for
  overlap with the same session_id_short).
- Model switch mid-session (causes cache invalidation).

State the hypothesis clearly as a hypothesis, not a finding. Only raise as a
finding if `cache_break` or another trigger directly corroborates it.

## JSON schema (v1.3)

Same as `project-quick-audit.md` JSON schema with `"mode": "detailed"` and one
additional key:

```jsonc
{
  "audit_schema_version": "1.3",
  "scope": "project",
  "mode": "detailed",
  // ... all quick fields ...
  "detailed_extensions": {
    "top_session_drilldown": [
      {
        "session_id_short": "<from project_analysis.top_expensive_sessions>",
        "first_ts": "<date>",
        "cost_usd": <number>,
        "turns": <int>,
        "top_turn_hypotheses": [ "<≤120 chars each; max 3>" ],
        "cache_break_in_session": <bool>
      }
      // ... up to 3 sessions
    ],
    "model_distribution_note": "<≤1 sentence; e.g. '87% Opus 4.7 by turns'>",
    "cache_outlier_hypothesis": "<null | ≤2 sentences on worst poor-cache session>"
  }
}
```

## Markdown render template

Render all quick sections first (sections 1–5 from `project-quick-audit.md`),
then append:

```markdown
## 6. Session drilldown (top 3 most expensive)

{for each session in detailed_extensions.top_session_drilldown:}
**{session_id_short}** ({first_ts[:10]}) — ${cost_usd:.2f}, {turns} turns
{cache_break_in_session ? "⚠ had a cache break event" : ""}
{for each hypothesis in top_turn_hypotheses:}
  - {hypothesis}

{if detailed_extensions.cache_outlier_hypothesis is not null:}
**Cache health outlier note:** {cache_outlier_hypothesis}
```

## Output contract

Same three artefacts as project quick, with `_detailed` suffix:
- `exports/session-metrics/audit_<id8>_<ts>_detailed.json`
- `exports/session-metrics/audit_<id8>_<ts>_detailed.md`
- Inline markdown (without H1).

## Finding cap

Same rules as project quick: negative cap 7, positive cap 3, `"other"` forbidden.
