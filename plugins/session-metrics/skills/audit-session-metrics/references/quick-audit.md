# Quick audit playbook

This is the playbook for the `quick` mode of `audit-session-metrics`.
Aim: a short, decision-quality plain-English audit produced from the
session-metrics JSON export.

## Workflow — one Bash call to the extract helper

The audit skill ships a helper script that does all the heavy lifting:

```
scripts/audit-extract.py <input-json-path> --mode quick
```

Run it once with the input JSON path. It emits a single JSON digest to
stdout containing baseline metrics, fired triggers (with suggested
severity + estimated impact USD pre-computed), positive triggers
(celebratory findings — cache health and savings), and the top-3
expensive turns (with cross-finding correlation flags).

**Why this matters.** A direct `Read` on the session-metrics JSON
typically blows the 256KB Read cap on any session above ~50 turns. The
extract helper sidesteps that and removes the key-name guessing problem
— the script knows the export schema; the playbook never has to.

The audit skill's job is to **synthesize prose, link findings, and
write the artefacts**. Trigger evaluation is the script's job; do not
re-derive numbers.

## Output contract — three artefacts per audit

Every quick audit produces:

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json` — structured findings for tooling.
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md` — the same content rendered for humans.
3. **Inline chat output** — the markdown content printed in the assistant's reply.

Procedure: run the helper, populate the JSON object using the digest,
write it, render the markdown using the template below, write it, then
print the markdown inline. Print two `[audit] saved → <path>` lines, one
per file.

`<id8>` and `<ts>` come from the helper digest's
`session_id_short` and `ts_str` fields (the helper parses the input
filename).

## JSON schema (v1.2)

```jsonc
{
  "audit_schema_version": "1.2",
  "mode": "quick",
  "session_id_short": "<8-char id, copy from digest.session_id_short>",
  "generated_at": "<ISO8601 UTC, e.g. 2026-04-29T01:23:45Z>",
  "input_json": "<absolute path, copy from digest.input_json>",

  "session_archetype": "<one of: agent_workflow|short_test|long_debug|code_writing|exploratory_chat|unknown — copy from digest.session_archetype>",

  "baseline": { /* copy from digest.baseline verbatim — now includes first_turn_cost_usd and first_turn_cost_share_pct (v1.2) */ },

  "findings": [ <list every fired trigger from digest.fired_triggers as a finding object; cap at 7 for scannability — see "Finding cap" below> ],

  "positive_findings": [ <list every positive trigger from digest.positive_triggers; cap at 3 — see "Positive findings" below; omit the array entirely (or emit []) if digest.positive_triggers is empty> ],

  "top_expensive_turns": [ <exactly 3 turn objects, copy from digest.top_expensive_turns; preserve is_cache_break flag> ],

  "fix_first": [ <3 strings, each starting with a verb; synthesise — see "fix_first" below> ]
}
```

### Finding object

```jsonc
{
  "rank": <1..N, ordered high → low severity then by descending estimated_impact_usd>,
  "severity": "high" | "medium" | "low",
  "metric": <one of the enum below; copy digest.fired_triggers[i].metric>,
  "title": <≤80 chars; states what is wrong, not the fix>,
  "evidence": { <copy digest.fired_triggers[i].evidence verbatim> },
  "fix": <one paragraph; concrete action, names a file/flag/behaviour, reference the actual numbers from evidence>,
  "estimated_impact_usd": <copy digest.fired_triggers[i].estimated_impact_usd; null is allowed when the script could not compute one honestly>
}
```

`severity` should normally equal `digest.fired_triggers[i].suggested_severity`
(the script applies sensible downgrades — e.g. 1 cache break in 200 turns
drops `cache_break` from `medium` to `low` with a `downgrade_reason` you
can quote in the `fix` paragraph). Only override when you have a stronger
reason than the script does, and document it in the `fix`.

### Metric enum + impact-formula reference

The helper computes `estimated_impact_usd` per the formulas below.
Quote the number in your `fix` paragraph; do not re-derive it.

| `metric` | Trigger | Default severity | Impact formula |
|----------|---------|------------------|----------------|
| `cache_break` | `cache_breaks` non-empty | medium (downgrades to low if breaks/turns < 2%) | `sum(uncached_tokens) × $5/M` (Opus input rate) |
| `top_turn_share` | top single turn > 30% of total cost | high | `null` — already-realised cost, not a recoverable saving |
| `input_output_ratio_uncached` | uncached_input/output > 50:1 AND `cache_hit_pct` < 60 | high | `null` — depends on prompt-caching applicability |
| `subagent_share` | `subagent_share_stats.share_pct` > 50 | medium | `subagent_share_stats.attributed_cost` (already realised) |
| `cache_ttl_1h_unused` | `extra_1h_cost` > 0 AND `cache_read` < 50% of `cache_write_1h` | medium | `totals.extra_1h_cost` (1h-tier surcharge) |
| `session_warmup_overhead` | `first_turn_cost / total_cost > 0.20` AND `total_turns ≤ 15` | medium | `null` — short-session warmup |
| `tool_result_bloat` | A turn with `cache_write_tokens > 50000` immediately after a turn whose `tool_use_names` included `Bash`, `Read`, or `WebFetch` | medium | `null` — savings depend on cache reuse |
| `heavy_reader_tools` | `Read` or `WebFetch` in `tool_names_top3` | low | `null` — informational |
| `cache_savings_low` | `cache_savings` < 10% of `cost` | low | `null` — depends on prompt-reuse pattern |
| `thinking_engagement_high` | `thinking_turn_pct` > 30 | low | `null` — savings depend on tolerance for shallower reasoning |
| `truncated_outputs` | any turn with `stop_reason="max_tokens"` | low | `null` — quality issue, not a cost issue |
| `advisor_share` | `advisor_cost_usd` > 5% of `cost` | low | `totals.advisor_cost_usd` (already realised) |
| `idle_gap_cache_decay` | A turn with `cache_creation_input_tokens > 50%` of billable input following a >5min gap from the prior turn (cache TTL boundary; aggregates top-3 events) | medium (scales by total rebuild cost: low <$0.30, medium $0.30–$1, high >$1) | `sum(rebuild_tokens) × $5/M` |
| `other` | Pattern not covered above (use sparingly; **forbidden in this version**, see "Finding cap" below) | low | `null` |

### Positive finding object

Positive findings come from `digest.positive_triggers` and represent
**good** patterns worth surfacing. They use `estimated_savings_usd`
(direction: money saved) rather than `estimated_impact_usd`
(direction: money wasted).

```jsonc
{
  "rank": <1..N, ordered by descending estimated_savings_usd; null savings ranked last>,
  "metric": <"cache_savings_high" | "cache_health_excellent">,
  "title": <≤80 chars; states what is good>,
  "evidence": { <copy digest.positive_triggers[i].evidence verbatim> },
  "note": <one short sentence; what made this work, no advice needed>,
  "estimated_savings_usd": <copy from digest.positive_triggers[i].estimated_savings_usd; null is allowed>
}
```

| `metric` (positive) | Trigger | Savings basis |
|---------------------|---------|---------------|
| `cache_savings_high` | `cache_savings > 3× cost` OR `cache_savings > $5` | `totals.cache_savings` (already realised) |
| `cache_health_excellent` | `cache_hit_pct > 90` AND zero `cache_breaks` | `null` — informational |

### Session archetype (v1.2 — detect-only)

The helper emits a top-level `session_archetype` string and an
`archetype_signals` debugging dict. Quick mode does **not** narrate the
archetype — it stays in the JSON sidecar only, keeping the markdown tight.
Detailed mode does narrate it; see [`detailed-audit.md`](detailed-audit.md).

Priority order (first match wins; biased toward `unknown`):

| Archetype | Trigger |
|-----------|---------|
| `agent_workflow` | `subagent_share_pct >= 30` |
| `short_test` | `0 < turns <= 5` |
| `long_debug` | `turns > 30` AND (`cache_break_pct > 2%` OR `cache_hit_pct < 70`) |
| `code_writing` | `turns > 5` AND `Edit + Write >= 25%` of tool calls |
| `exploratory_chat` | `turns > 5` AND `tool_call_total / turns < 1.0` |
| `unknown` | default — no clear pattern (do **not** force-label) |

The 2% cache-break threshold mirrors the `cache_break` trigger's
downgrade rule so a single break in 200 turns doesn't pin the session
as "debug" while the trigger itself downgrades to low.

Severity overrides conditional on archetype come in v1.31.0 once we
have ~10 audit sidecars to validate the override matrix against.
v1.30.0 ships **detect-only**.

### Top expensive turn object

```jsonc
{
  "turn_index": <int>,
  "cost_usd": <number>,
  "label": <slash_command if non-empty, else first 80 chars of prompt_text, else "(no prompt text — tool-result follow-up)">,
  "hypothesis": <≤120 chars; pre-computed by helper>,
  "is_cache_break": <bool; true when this turn coincides with a cache_break finding>,
  "drivers": { /* token-bucket breakdown — copy from digest verbatim */ }
}
```

Copy these objects directly from `digest.top_expensive_turns`. The
`is_cache_break` flag is the cross-finding correlation hint — if true,
explicitly link this turn to the `cache_break` finding's `fix`
paragraph (mention the same `turn_index` so a reader sees the
connection).

### Finding cap

**Per-array caps — independent.** The negative `findings` array is
capped at **7**. The `positive_findings` array is capped at **3**. They
do **not** compete for slots — emitting 7 negative findings does not
displace positives, and vice versa. Each array has no floor.

If more than 7 negative triggers fired (rare on a single session),
keep the 7 with the highest `estimated_impact_usd` (or, where impact
is null, those with `high` then `medium` severity). If more than 3
positive triggers fired, keep the 3 with the highest
`estimated_savings_usd`. Drop the rest silently.

**No padding, ever.** Do **not** add `"other"` rows to either array to
reach a target count. The `other` enum is forbidden in v1.1 outputs.
Do not invent positives. Both arrays may be empty if no triggers fired —
that is the correct outcome, not a defect to fix.

### `fix_first`

Three bullets, each starting with a verb. Synthesise — do not paraphrase
the `fix` field of findings #1, #2, #3 directly. The point is to give
the reader the highest-leverage action plan, which often means:

- Picking the single highest-impact action across all findings (often
  `cache_break` or `tool_result_bloat`).
- Naming a concrete behaviour change (a flag, a workflow, a habit).
- Mentioning the specific dollar figure from evidence when it strengthens
  the case.

If fewer than 3 findings fired, emit fewer bullets — do not invent
generic "review your prompts" advice to fill space.

## LLM division of labor

To keep the audit deterministic and cheap, this is the split:

**Helper script (`scripts/audit-extract.py`) does:**
- Parse the JSON export.
- Evaluate every metric trigger.
- Compute `estimated_impact_usd` where a formula exists.
- Suggest severity downgrades when data is milder than the threshold.
- Pre-classify the top-3 turn hypothesis.
- Flag cross-finding correlation (`is_cache_break`).

**Audit skill (this playbook on Haiku) does:**
- Decide which fired triggers make the cut (cap at 7 by impact).
- Write concrete `fix` prose tied to the actual evidence numbers.
- Synthesize `fix_first` (highest-leverage subset, not duplication).
- Render the markdown, write both artefacts, print inline.

If you find yourself recomputing a number the helper already returned,
stop — copy it from the digest.

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
- Turn #{turn_index} · ${cost_usd:.4f} · {label} — {hypothesis}{cache_break_suffix}

## 4. Positive findings

{omit this entire section if positive_findings is empty.}
{for each positive in positive_findings, in rank order:}
- 🟢 {title} — {evidence_inline}{savings_suffix}

## 5. What to fix first

{for each bullet in fix_first:}
- {bullet}
```

Render rules:

- `{model_split_clause}`: if `len(baseline.models) == 1`, write
  `, all on \`<model_id>\``. If > 1, render the split **by cost** using
  each entry's `cost_pct`:
  `, split <cost_pct1>% cost <model_id_1> / <cost_pct2>% cost <model_id_2>`
  (round to nearest 1%, drop models with `cost_pct < 5%`, sort
  descending by `cost_pct`). Cost share is the headline because a
  small share of expensive turns can dominate spend (e.g. 22% of
  turns on Sonnet → 37% of cost). When all `cost_pct` values are
  null (legacy export pre-dating per-model cost), fall back to
  `turns_pct` and replace `cost` with `turns` in the rendered
  string. If both cost and turn shares are notable and divergent
  (≥10pp gap on the top model), follow with one extra clause
  ` (turn share <turns_pct>%)` on that model so the reader sees the
  effort-vs-spend mismatch.
- `{cache_savings_clause}`: if the input digest has `baseline.cache_savings_usd > 0`,
  append ` — caching saved $<savings:.2f> vs. a no-cache run`.
- `{severity_emoji}`: `🔴` for high, `🟡` for medium, `🟢` for low.
- `{evidence_inline}`: render the structured `evidence` object as a
  short comma-separated list of `key=value` pairs (e.g.
  `turn_index=107, uncached_tokens=320,234`). For long fields like
  `tool_names_top3`, render as `['Bash','Edit','Read']`.
- `{cache_break_suffix}`: if `is_cache_break` is true, append
  ` (also flagged as cache_break)`.
- `{savings_suffix}`: if `estimated_savings_usd` is non-null, append
  ` — saved $<savings:.2f>`. Otherwise, append nothing.

The markdown copy on disk is identical to what is printed inline,
**except** it gains the `# Quick audit — session ...` H1 heading at
the top (the inline chat version skips the H1 because the chat client
already shows context above the audit).

## Tone

- **Direct and specific.** Cite exact numbers from the digest. No
  motivational language, no LLM theory padding.
- **Quote sparingly.** Free-text fields like `fix` are capped at one
  paragraph each.
- **Honour the cap, not a target.** If 3 triggers fired, emit 3 findings —
  don't pad.
- **Stop after section 5** (or section 4 if positive findings is empty
  and that section was omitted). Do not append "summary" or "next steps".

## Final step (write order)

1. Run `scripts/audit-extract.py <input-json> --mode quick` once.
2. Build the audit JSON object using the digest values.
3. Write the JSON sidecar to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.json`.
4. Render to markdown using the template.
5. Write the markdown copy to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_quick.md`.
6. Print the same markdown content inline (without the H1 heading).
7. Print two stderr-style lines:
   `[audit] saved → <json-path>`
   `[audit] saved → <md-path>`

## Schema versioning

Bumping the major number of `audit_schema_version` (currently `1.2`)
is breaking — any tooling that consumes the JSON sidecar will need to
handle the new shape. Bump the major number for breaking changes
(renamed fields, removed enum values), the minor number for additive
changes (new optional fields, new enum values).

**v1.0 → v1.1** (additive): added `positive_findings` array (parallel
to `findings`), added `idle_gap_cache_decay` to the negative metric
enum, added `cache_savings_high` and `cache_health_excellent` positive
metric enum. The `other` enum is **forbidden** in v1.1+ outputs (was
"use sparingly" in v1.0).

**v1.1 → v1.2** (additive): added top-level `session_archetype` string
(enum: `agent_workflow`, `short_test`, `long_debug`, `code_writing`,
`exploratory_chat`, `unknown`); added `baseline.first_turn_cost_usd`
and `baseline.first_turn_cost_share_pct` (synthetic + resume-marker
turns are skipped when picking the first turn). v1.1 outputs continue
to validate against v1.2 readers — both new fields are optional. v1.2
ships archetype as **detect-only**; severity overrides conditional on
archetype are queued for v1.31.0 once ~10 sidecars exist for
calibration.
