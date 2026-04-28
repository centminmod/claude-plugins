# Detailed audit playbook

This is the playbook for the `detailed` mode of `audit-session-metrics`.
It builds on the quick-audit findings by also reading the user's
configuration files and scanning the JSON export for re-read /
paste-bomb / wrong-model patterns. Aim: a 12-finding prioritised
report plus quick-wins / structural-fixes / estimated-savings
sections, all serialised through the same JSON schema as the quick
audit (with detailed-only fields added).

## Context-budget rules — non-negotiable

These rules exist so the audit fits comfortably in Haiku's context
even on long sessions:

1. **Do not read the raw `.jsonl`.** Operate from the JSON export
   alone — `turns[]` already carries `input_tokens`, `output_tokens`,
   `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `model`,
   `slash_command`, `tools`, `prompt_text` (truncated), and
   `attributed_subagent_*`. Re-parsing the raw JSONL is what would
   blow context — and it would not give you anything you cannot
   already see in the export.
2. **Cap CLAUDE.md / settings reads at 500 lines each.** If the file
   is larger, that itself is a finding ("CLAUDE.md is X lines,
   roughly Y tokens — every turn pays this") — read only the first
   500 lines for content patterns and stop.
3. **Cap quoted snippets in findings at 5 lines.** Quote the worst
   offender; do not paste an entire section.
4. **No motivational language. No LLM theory.** Cite the exact
   number, file, or turn index. If a phase has nothing to say, write
   "Nothing material" and move on.

## Output contract — three artefacts per audit

Every detailed audit produces:

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.json` — structured findings for tooling.
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.md` — the same content rendered for humans.
3. **Inline chat output** — the markdown content printed in the assistant's reply.

Same write order as `quick-audit.md`: populate JSON → write JSON →
render markdown → write markdown → print inline → emit two
`[audit] saved → <path>` lines.

## Phase 1 — Re-run the quick audit signals

Mentally execute the quick-audit metric triggers in
[`quick-audit.md`](quick-audit.md). Their findings carry into the
detailed report — they are appended to the same `findings` array, not
duplicated into a separate section.

## Phase 2 — Config audit (read these files)

In this exact order, `Read` each file. If a file does not exist, note
"Not present" via the `evidence.note` field on the relevant finding
and move on. Apply the line cap (≤ 500).

1. `~/.claude/CLAUDE.md` — global, **loaded on every turn**.
2. `./CLAUDE.md` — project-local, also re-loaded every turn.
3. Subdirectory `CLAUDE.md` files — only if the JSON export's `slug`
   suggests the user works in a subtree (best-effort skip if in doubt).
4. `~/.claude/settings.json` — global Claude Code settings.
5. `./.claude/settings.json` and `./.claude/settings.local.json` —
   project settings.
6. `./.claudeignore` — if absent and the project root has signs of
   `node_modules`, `dist`, `build`, `.next`, `vendor`, lockfiles, or
   large data directories, that is a finding.

## Phase 3 — Session-log scan (from the JSON export only)

From the export's `turns[]`:

- **Re-reads.** Group `turns[].tools[]` entries where `name == "Read"`
  by their `input.file_path`. Flag any path read more than twice.
- **Paste bombs.** Flag user prompts where
  `len(prompt_text) > 5000` characters (or
  `input_tokens > 8000` if `prompt_text` is truncated).
- **Wrong-model turns.** Turns where `model` contains "opus" and
  total `cost_usd < 0.05` (Opus on a trivial task). List up to three.
- **Subagent waste.** Turns where `attributed_subagent_cost`
  exceeds the parent turn's own cost by 5×. List up to two.

## JSON schema (v1.0)

Same spine as `quick-audit.md`'s schema, with `mode: "detailed"`,
**up to 16 findings** (instead of exactly 7), and three additional
top-level fields: `quick_wins`, `structural_fixes`,
`estimated_savings`.

```jsonc
{
  "audit_schema_version": "1.0",
  "mode": "detailed",
  "session_id_short": "<8-char id>",
  "generated_at": "<ISO8601 UTC>",
  "input_json": "<absolute path>",

  "baseline": { /* same shape as quick */ },

  "findings": [ <up to 16 finding objects, sorted high → low severity, then by descending estimated_impact_usd> ],

  "top_expensive_turns": [ <exactly 3 turn objects> ],

  // Detailed-only sections:
  "quick_wins": [ <3-6 strings, ≤10-min fixes, each starting with a verb> ],

  "structural_fixes": [ <2-4 strings, habit-shift fixes, each starting with a verb> ],

  "estimated_savings": {
    "quick_wins_pct": <number> | null,
    "structural_pct": <number> | null,
    "approx_per_session_usd": <number> | null,
    "confidence": "low" | "medium" | "high",
    "note": <string explaining the confidence level>
  }
}
```

`fix_first` (the quick-mode 3-bullet list) is **omitted** in detailed
mode — its role is taken by `quick_wins` + `structural_fixes`.

`estimated_savings.confidence`:

- `"high"` — multiple ≥medium findings with concrete dollar impacts.
- `"medium"` — at least one ≥medium finding with quantified evidence.
- `"low"` — only `low`-severity findings, or insufficient data; set
  the numeric fields to `null` and explain why in `note`.

### Detailed-mode metric enum (additions)

The quick-mode metric enum (cache_break / top_turn_share /
input_output_ratio_uncached / subagent_share / cache_ttl_1h_unused /
session_warmup_overhead / tool_result_bloat / heavy_reader_tools /
cache_savings_low / thinking_engagement_high / truncated_outputs /
advisor_share / other) carries forward unchanged. Detailed mode adds:

| `metric` | Trigger | Default severity | `evidence` fields |
|----------|---------|------------------|--------------------|
| `claudemd_oversize` | `~/.claude/CLAUDE.md` or `./CLAUDE.md` > 2000 tokens (chars/4) | high | `path` (string), `line_count` (int), `token_estimate` (int), `worst_section_excerpt` (string, ≤5 lines) |
| `claudemd_duplication` | Same rule appears in both global and project CLAUDE.md | medium | `paths` (array of 2), `duplicated_rule` (string, ≤3 lines) |
| `missing_claudeignore` | `./.claudeignore` absent AND heavy paths present (`node_modules`, `dist`, etc.) | medium | `present_paths` (array of strings), `suggested_block` (string with newline-separated entries) |
| `mcp_unused` | MCP server in `settings.json` with no matching `tools[].name` in any turn this session | medium | `server_name` (string), `tool_descriptors_estimated` (int\|null) |
| `default_model_overkill` | Default model in settings is Opus AND > 70% of turns did formatting/lookup/single-file edits | medium | `default_model` (string), `routine_turn_pct` (number), `total_turns` (int) |
| `file_re_read` | Same `input.file_path` Read more than twice | low | `file_path` (string), `read_count` (int), `turn_indices` (array of int) |
| `paste_bomb` | User prompt with `len(prompt_text) > 5000` chars | low | `turn_index` (int), `chars` (int), `excerpt` (string, ≤80 chars) |
| `subagent_dominant_parent` | `attributed_subagent_cost` > 5× parent `cost_usd` | medium | `turn_index` (int), `parent_cost_usd` (number), `subagent_cost_usd` (number), `ratio` (number) |
| `wrong_model_turn` | `model` contains "opus" AND turn `cost_usd` < 0.05 | low | `turn_indices` (array of int), `examples` (array of `{turn_index, cost_usd, prompt_excerpt}` objects, ≤3) |
| `verbose_response` | In > 30% of turns where `input_tokens > 0`, `output_tokens / input_tokens > 5` (the model dumps rather than replies). Skip if `total_turns < 10` (too few samples) | medium | `pct_of_turns` (number), `total_turns_sampled` (int), `output_input_ratio_p90` (number), `sample_turn_indices` (array of int, ≤3) |
| `weekly_rollup_regression` | `report.weekly_rollup.cost_delta_pct > 50` (trailing-7d cost up > 50% vs prior 7d) OR `cache_hit_delta_pp < -10` (cache hit dropped > 10pp). Skip if `weekly_rollup` is missing or only one week of data exists | high | `cost_delta_pct` (number\|null), `trailing_7d_cost_usd` (number), `prior_7d_cost_usd` (number), `cache_hit_delta_pp` (number\|null), `trailing_7d_cache_hit_pct` (number), `prior_7d_cache_hit_pct` (number) |
| `peak_hour_concentration` | `report.peak` is configured AND > 70% of total cost lands inside the peak-hours band. Skip if `peak` is null | medium | `peak_band` (string, e.g. `"5-11 America/Los_Angeles"`), `peak_cost_usd` (number), `total_cost_usd` (number), `pct_of_total` (number) |
| `subagent_attribution_orphan` | `report.subagent_attribution_summary.orphan_turns > 0` (cost can't be traced back to a parent prompt; masks where to optimize) | low | `orphan_turns` (int), `attributed_turns` (int), `nested_levels_seen` (int), `cycles_detected` (int) |

Severity may be **upgraded** when extreme (e.g. CLAUDE.md at 8000
tokens is still `high` but flag the magnitude in `evidence.note`).
Never downgrade below the default.

### Sixteen-finding contract

Up to 16 findings — fewer if the data does not support 16. **Merge
similar findings** rather than padding (e.g. three separate
`file_re_read` findings on different paths roll into one with
`evidence.examples[]`). The 7-finding contract from quick-audit does
NOT apply here; 5 high-quality findings beats 16 padded ones.

If the detailed audit produces fewer than the quick audit's 7 enum
triggers (extremely rare), match the quick-mode contract and fall
back to `"other"` rows.

## Markdown render template

Same baseline / findings table / top-3 turns as quick-audit, then
three new sections instead of `## 4. What to fix first`:

```markdown
# Detailed audit — session {session_id_short} @ {generated_at}

## 1. Baseline
{same as quick-audit}

## 2. Findings
{same table; up to 12 rows; merge similar findings}

## 3. Top 3 expensive turns
{same as quick-audit}

## 4. Quick wins (≤10 min each)

{for each bullet in quick_wins:}
- {bullet}

## 5. Structural fixes (require habit shift)

{for each bullet in structural_fixes:}
- {bullet}

## 6. Estimated savings

{rendered from estimated_savings:}

Implementing all quick wins should reduce per-session token cost by
roughly **{quick_wins_pct}%**. Adding the structural fixes brings it
to **{structural_pct}%**. At current usage that is roughly
**${approx_per_session_usd}**/session. Confidence: **{confidence}**.

{if any field is null, render the corresponding sentence as:}
*Estimated savings: not enough signal — re-run after applying the
quick wins to measure delta.*
```

Render rules from the quick-audit template (severity emojis,
evidence inline, model-split clause, cache-savings clause) carry over
unchanged.

## Tone

- **Direct and specific.** Cite exact line numbers from CLAUDE.md
  reads, exact turn indices from the JSON, exact dollar figures from
  `cost_usd`. No motivational language.
- **Quote sparingly.** ≤5 lines per `evidence.worst_section_excerpt`
  / `evidence.duplicated_rule`.
- **Be honest about confidence.** If the data is thin (short
  session, no config files present), set
  `estimated_savings.confidence` to `"low"` and the numeric fields
  to `null` rather than inventing percentages.
- **Stop after section 6.** Do not append "summary" or "next steps".

## Final step (write order)

1. Populate the full JSON object in memory (with up to 12 findings,
   `quick_wins`, `structural_fixes`, `estimated_savings`).
2. Write the JSON sidecar to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.json`.
3. Render to markdown using the template.
4. Write the markdown copy to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.md`.
5. Print the same markdown content inline (without the H1 heading).
6. Print two stderr-style lines:
   `[audit] saved → <json-path>`
   `[audit] saved → <md-path>`

## Schema versioning

Same versioning rules as quick-audit. Quick and detailed share
`audit_schema_version`; bump them together. The detailed-only fields
(`quick_wins`, `structural_fixes`, `estimated_savings`) are
optional from a quick-audit consumer's perspective — tooling should
gate on `mode: "detailed"` before reading them.

Phases 4 (interactive Q&A) and 5 (CLAUDE.md rewrite) from earlier
audit drafts remain **out of scope for v1**.
