# Quick audit playbook

This is the playbook for the `quick` mode of `audit-session-metrics`.
Aim: a short, decision-quality plain-English audit produced from the
session-metrics JSON export alone — no other reads.

The user's session-metrics JSON has already been read by SKILL.md.
**Do not** read the raw `.jsonl`; the export already rolled up every
per-turn datum you need (`turns[].input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `model`,
`slash_command`, `tools`, etc., plus session totals and `by_skill`).

## Output contract — three artefacts per audit

Every quick audit produces:

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json` — structured findings for tooling.
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md` — the same content rendered for humans.
3. **Inline chat output** — the markdown content printed in the assistant's reply.

Procedure: populate the JSON object first, write it, render the
markdown using the template below, write it, then print the markdown
inline. Print two `[audit] saved → <path>` lines, one per file.

`<id8>` and `<ts>` are extracted from the input JSON's filename
(pattern `session_<id8>_<YYYYMMDD>T<HHMMSS>Z.json` or the legacy
`session_<id8>_<YYYYMMDD_HHMMSS>.json`). If the input file does not
match either pattern, fall back to the input file's stem.

## JSON schema (v1.0)

```jsonc
{
  "audit_schema_version": "1.0",
  "mode": "quick",
  "session_id_short": "<8-char id from input filename>",
  "generated_at": "<ISO8601 UTC, e.g. 2026-04-29T01:23:45Z>",
  "input_json": "<absolute path passed as $ARGUMENTS[1]>",

  "baseline": {
    "total_cost_usd": <number>,
    "turns": <int>,
    "models": { "<model_id>": <int>, ... },
    "input_output_ratio": <int>,         // round to nearest integer ratio
    "cache_hit_pct": <number>            // one decimal place
  },

  "findings": [ <exactly 7 finding objects, sorted high → low severity, then by descending estimated_impact_usd> ],

  "top_expensive_turns": [ <exactly 3 turn objects> ],

  "fix_first": [ <3 strings, each starting with a verb; pick the highest-impact subset of findings> ]
}
```

### Finding object

```jsonc
{
  "rank": <1..7>,
  "severity": "high" | "medium" | "low",
  "metric": <one of the enum below>,
  "title": <≤80 chars; states what is wrong, not the fix>,
  "evidence": { <structured fields per the metric — see table> },
  "fix": <one paragraph; concrete action, names a file/flag/behaviour>,
  "estimated_impact_usd": <number> | null
}
```

`estimated_impact_usd` is **optional** — set to `null` when you
cannot estimate honestly. Never guess. Sorting falls back to severity
when impact is null.

### Metric enum + evidence shapes

Pick `metric` from this enum. Each entry lists the trigger, default
severity, and the `evidence` fields you must populate. If a finding
genuinely doesn't fit any enum entry, use `"other"` and document it
in `evidence.note`.

| `metric` | Trigger | Default severity | `evidence` fields |
|----------|---------|------------------|--------------------|
| `cache_break` | `cache_breaks` non-empty | medium | `turn_index` (int), `uncached_tokens` (int), `count` (int) |
| `top_turn_share` | top single turn > 30% of total cost | high | `turn_index` (int), `cost_usd` (number), `pct_of_total` (number), `slash_command` (string\|null), `prompt_excerpt` (string\|null) |
| `input_output_ratio_uncached` | ratio > 50:1 AND `cache_hit_pct` < 60 | high | `ratio` (int), `cache_hit_pct` (number), `total_input` (int), `output` (int) |
| `subagent_share` | `subagent_share_stats.share_pct` > 50 | medium | `share_pct` (number), `total_cost_usd` (number), `attributed_cost_usd` (number) |
| `cache_ttl_1h_unused` | `extra_1h_cost` > 0 AND `cache_read` < 50% of `cache_write_1h` | medium | `extra_1h_cost_usd` (number), `cache_write_1h` (int), `cache_read` (int) |
| `session_warmup_overhead` | `sessions[0].turns[0].cost_usd / totals.cost > 0.20` AND `totals.turns ≤ 15` | medium | `first_turn_cost_usd` (number), `total_cost_usd` (number), `pct_of_total` (number), `total_turns` (int) |
| `tool_result_bloat` | Any turn with `cache_write_tokens > 50000` immediately following a turn whose `tools[]` included `Bash`, `Read`, or `WebFetch` (the bloat is the prior tool's result baked into the cache prefix) | medium | `turn_index` (int, the bloated turn), `prior_turn_index` (int), `prior_tool` (string), `cache_write_tokens` (int), `examples` (array of `{turn_index, prior_tool, cache_write_tokens}`, ≤3) |
| `heavy_reader_tools` | `Read` or `WebFetch` in `tool_names_top3` | low | `tool_names_top3` (array of strings), `tool_call_total` (int) |
| `cache_savings_low` | `cache_savings` < 10% of `cost` | low | `cache_savings_usd` (number), `cost_usd` (number), `pct` (number) |
| `thinking_engagement_high` | `thinking_turn_pct` > 30 | low | `thinking_turn_pct` (number), `thinking_turn_count` (int), `total_turns` (int) |
| `truncated_outputs` | any turn with `stop_reason="max_tokens"` | low | `truncated_count` (int), `turn_indices` (array of int) |
| `advisor_share` | `advisor_cost_usd` > 5% of `cost` | low | `advisor_call_count` (int), `advisor_cost_usd` (number), `pct_of_total` (number), `advisor_model` (string\|null) |
| `other` | Pattern not covered above (use sparingly) | low | `note` (string explaining the pattern), plus any data fields you cite |

Severity may be **upgraded** when the data is more extreme than the
trigger (e.g. cache hit at 30% with 100 turns is `high` not `medium`),
but never **downgraded** below the default — the trigger thresholds
already filtered the easy-to-dismiss cases.

### Top expensive turn object

```jsonc
{
  "turn_index": <int>,
  "cost_usd": <number>,
  "label": <slash_command if non-empty, else first 80 chars of prompt_text, else "(no prompt text — tool-result follow-up)">,
  "hypothesis": <≤120 chars; one of the patterns below>
}
```

`hypothesis` picks the highest-signal pattern on that turn:

- `tools` contains `Read` and an input file is named: `"large file Read of <path>"`
- `prompt_text` length > 5 KB: `"paste-bomb prompt (~<size> KB)"`
- `model` is Opus and `cost_usd` < $0.05: `"Opus on a trivial-looking task"`
- `attributed_subagent_cost` > parent `cost_usd`: `"expensive subagent spawned from a small prompt"`
- otherwise: dominant token bucket — `"output-heavy"` / `"cache-write heavy (<N>K cw)"` / `"cache-read heavy"`

### Seven-finding contract

Always emit **exactly 7 findings**. If fewer than 7 enum triggers
fire, fill remaining rows by lowering severity thresholds (e.g. a
3% advisor share still earns a `low` row even though the trigger is
5%) or using `"other"`. Do not pad with empty findings.

If genuinely fewer than 7 patterns are present (rare on any session
above 30 turns), use `"other"` for the last row(s) with
`evidence.note: "no further material patterns"` and `severity: "low"`.

`fix_first` stays at exactly 3 bullets — pick the highest-impact
subset of findings, regardless of how many findings the table has.

## Markdown render template

Render the JSON to markdown using this exact layout. Field references
are `{baseline.total_cost_usd:.2f}`-style format strings — substitute
the JSON value with the format applied.

```markdown
# Quick audit — session {session_id_short} @ {generated_at}

## 1. Baseline

Total cost **${baseline.total_cost_usd:.2f}** across **{baseline.turns} turns**{model_split_clause}. Input:output ratio is roughly **{baseline.input_output_ratio}:1**. Cache hit ratio **{baseline.cache_hit_pct:.1f}%**{cache_savings_clause}.

## 2. Findings

| # | Severity | Finding | Evidence | Fix |
|---|----------|---------|----------|-----|
{for each finding in findings, in order:}
| {rank} | {severity_emoji} {severity} | {title} | {evidence_inline} | {fix} |

## 3. Top 3 expensive turns

{for each turn in top_expensive_turns:}
- Turn #{turn_index} · ${cost_usd:.4f} · {label} — {hypothesis}

## 4. What to fix first

{for each bullet in fix_first:}
- {bullet}
```

Render rules:

- `{model_split_clause}`: if `len(baseline.models) == 1`, write
  `, all on \`<model_id>\``. If > 1, write
  `, split <pct1>% <model_id_1> / <pct2>% <model_id_2>` (round to
  nearest 1%, drop models < 5%).
- `{cache_savings_clause}`: if the input JSON has `totals.cache_savings`,
  append ` — caching saved $<savings:.2f> vs. a no-cache run`.
- `{severity_emoji}`: `🔴` for high, `🟡` for medium, `🟢` for low.
- `{evidence_inline}`: render the structured `evidence` object as a
  short comma-separated list of `key=value` pairs (e.g.
  `turn_index=107, uncached_tokens=320,234`). For long fields like
  `tool_names_top3`, render as `['Bash','Edit','Read']`.

The markdown copy on disk is identical to what is printed inline,
**except** it gains the `# Quick audit — session ...` H1 heading at
the top (the inline chat version skips the H1 because the chat client
already shows context above the audit).

## Tone

- **Direct and specific.** Cite exact numbers, file paths, turn
  indices. No motivational language, no LLM theory padding.
- **Quote sparingly.** Free-text fields like `fix` are capped at one
  paragraph each.
- **Honour the contract.** 5 findings, 3 top turns, 3 fix-first
  bullets — no fewer, no more.
- **Stop after section 4.** Do not append "summary" or "next steps".

## Final step (write order)

1. Populate the full JSON object in memory.
2. Write the JSON sidecar to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json`.
3. Render to markdown using the template.
4. Write the markdown copy to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md`.
5. Print the same markdown content inline (without the H1 heading).
6. Print two stderr-style lines:
   `[audit] saved → <json-path>`
   `[audit] saved → <md-path>`

## Schema versioning

Bumping `audit_schema_version` (currently `1.0`) is breaking — any
tooling that consumes the JSON sidecar will need to handle the new
shape. Bump the major number for breaking changes (renamed fields,
removed enum values), the minor number for additive changes (new
optional fields, new enum values).
