# Detailed audit playbook

This is the playbook for the `detailed` mode of `audit-session-metrics`.
It builds on the quick-audit findings by also reading the user's
configuration files and consuming the helper script's `detailed_candidates`
section (re-reads, paste-bombs, wrong-model turns, verbose responses,
weekly rollup deltas, subagent orphans). Aim: a prioritised report up
to 16 findings plus quick-wins / structural-fixes / estimated-savings
sections, all serialised through the same JSON schema spine as the
quick audit (with detailed-only fields added).

## Workflow — one Bash call to the extract helper

Same script as quick mode, with `--mode detailed`:

```
scripts/audit-extract.py <input-json-path> --mode detailed
```

The digest gains a `detailed_candidates` block that pre-computes:

- `file_re_reads` — paths read more than twice.
- `paste_bombs` — turns whose `prompt_text` exceeds 5000 characters.
- `wrong_model_turns` — Opus turns with `cost_usd < 0.05`.
- `subagent_dominant_parents` — turns whose attributed subagent cost
  exceeds parent cost by 5×.
- `verbose_response` — fraction of turns where output / (input + cache_read) > 5.
- `weekly_rollup` — trailing-7d vs prior-7d cost / cache deltas (null if
  prior week has no data).
- `subagent_orphan` — orphan-turn counts when the attribution graph
  could not link a subagent to a parent.

You still **must** read the user's CLAUDE.md and settings files (Phase 2
below) — that is the part the helper cannot do.

## Context-budget rules — non-negotiable

These rules exist so the audit fits comfortably in Haiku's context
even on long sessions:

1. **Do not read the raw `.jsonl`.** The helper script already walked
   the export and extracted every metric you need. Re-parsing the
   raw JSONL is what would blow context.
2. **Cap CLAUDE.md / settings reads at 500 lines each.** If the file
   is larger, that itself is a finding (`claudemd_oversize`) — read
   only the first 500 lines for content patterns and stop.
3. **Cap quoted snippets in findings at 5 lines.** Quote the worst
   offender; do not paste an entire section.
4. **No motivational language. No LLM theory.** Cite the exact
   number, file, or turn index from the digest. If a phase has
   nothing to say, write "Nothing material" and move on.

## Output contract — three artefacts per audit

Every detailed audit produces:

1. **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.json` — structured findings for tooling.
2. **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.md` — the same content rendered for humans.
3. **Inline chat output** — the markdown content printed in the assistant's reply.

Same write order as `quick-audit.md`: run helper → build JSON → write
JSON → render markdown → write markdown → print inline → emit two
`[audit] saved → <path>` lines.

## Phase 1 — Re-use the quick audit's fired triggers

`digest.fired_triggers` already lists every quick-mode trigger that
fired. Append them to the same `findings` array — they are not
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

## Phase 3 — Consume helper's detailed_candidates

The digest's `detailed_candidates` section already pre-computed every
session-log scan you used to do by hand. For each populated subsection,
emit one finding with the matching metric:

- `file_re_reads` → one `file_re_read` finding (or merge similar paths
  into one finding's `evidence.examples`).
- `paste_bombs` → one `paste_bomb` finding per turn (cap at 3).
- `wrong_model_turns` → one `wrong_model_turn` finding consolidating
  up to 3 examples.
- `subagent_dominant_parents` → one `subagent_dominant_parent` finding
  per turn (cap at 2).
- `verbose_response.pct_of_turns > 30` → one `verbose_response` finding.
- `weekly_rollup` non-null AND `cost_delta_pct > 50` OR
  `cache_hit_delta_pp < -10` → one `weekly_rollup_regression` finding.
- `subagent_orphan` non-null → one `subagent_attribution_orphan` finding.

Do not invent additional triggers from raw turn data — if the helper
did not surface a pattern, it did not pass the threshold.

## JSON schema (v1.2)

Same spine as `quick-audit.md`'s schema, with `mode: "detailed"`,
**up to 16 findings**, the `positive_findings` array (capped at 3,
same shape as quick-audit), and three additional top-level fields:
`quick_wins`, `structural_fixes`, `estimated_savings`.

```jsonc
{
  "audit_schema_version": "1.2",
  "mode": "detailed",
  "session_id_short": "<8-char id, copy from digest.session_id_short>",
  "generated_at": "<ISO8601 UTC>",
  "input_json": "<absolute path, copy from digest.input_json>",

  "session_archetype": "<one of: agent_workflow|short_test|long_debug|code_writing|exploratory_chat|unknown — copy from digest.session_archetype>",

  "baseline": { /* copy from digest.baseline — now includes first_turn_cost_usd and first_turn_cost_share_pct (v1.2) */ },

  "findings": [ <list every fired trigger + every detailed-only finding, capped at 16, sorted high → low severity, then by descending estimated_impact_usd> ],

  "positive_findings": [ <list every positive trigger from digest.positive_triggers; cap at 3; same shape as quick-audit positive finding object; omit/[] when empty> ],

  "top_expensive_turns": [ <exactly 3 turn objects, copy from digest.top_expensive_turns> ],

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
advisor_share / idle_gap_cache_decay / other — note `other` is forbidden
in v1.1 outputs) carries forward unchanged. The positive enum
(cache_savings_high / cache_health_excellent) also carries forward and
populates `positive_findings` exactly as in quick-audit. Detailed mode
adds:

| `metric` | Trigger (digest source) | Default severity | Impact formula |
|----------|-------------------------|------------------|----------------|
| `claudemd_oversize` | Phase 2 read of `~/.claude/CLAUDE.md` or `./CLAUDE.md` > 2000 tokens (chars/4) | high | `null` — repeated context cost is hard to estimate without recomputing per-turn |
| `claudemd_duplication` | Same rule appears in both global and project CLAUDE.md | medium | `null` |
| `missing_claudeignore` | `./.claudeignore` absent AND heavy paths present (`node_modules`, `dist`, etc.) | medium | `null` |
| `mcp_unused` | MCP server in `settings.json` with no matching tool name in any turn | medium | `null` |
| `default_model_overkill` | Default model in settings is Opus AND > 70% of turns did formatting/lookup/single-file edits | medium | `null` |
| `file_re_read` | `digest.detailed_candidates.file_re_reads` non-empty | low | `null` (small per-turn delta, hard to quantify cleanly) |
| `paste_bomb` | `digest.detailed_candidates.paste_bombs` non-empty | low | `null` |
| `subagent_dominant_parent` | `digest.detailed_candidates.subagent_dominant_parents` non-empty | medium | `null` (subagent cost is already in parent's `attributed_subagent_cost`; not double-counted) |
| `wrong_model_turn` | `digest.detailed_candidates.wrong_model_turns` non-empty | low | `null` |
| `verbose_response` | `digest.detailed_candidates.verbose_response.pct_of_turns > 30` AND `total_turns_sampled ≥ 10`. The digest's denominator is `input_tokens + cache_read_tokens` (cache-aware), so cache-heavy sessions don't false-fire | medium | `null` |
| `weekly_rollup_regression` | `digest.detailed_candidates.weekly_rollup.cost_delta_pct > 50` OR `cache_hit_delta_pp < -10`. Helper suppresses when prior week has no data | high | `null` (delta is descriptive, not a recoverable saving) |
| `peak_hour_concentration` | `report.peak` configured AND > 70% of cost lands inside the peak band. Skip if `peak` is null | medium | `null` |
| `subagent_attribution_orphan` | `digest.detailed_candidates.subagent_orphan.orphan_turns > 0` | low | `null` |

Severity may be **upgraded** when the data is more extreme (e.g.
CLAUDE.md at 8000 tokens is still `high` but flag the magnitude in
`evidence.note`). For `cache_break` etc. that the helper already
downgrades, copy `digest.fired_triggers[i].suggested_severity` into the
finding's `severity` and quote `downgrade_reason` in the `fix`
paragraph.

### Session archetype + first-turn warmup (v1.2 — detect-only)

The helper emits a top-level `session_archetype` and an
`archetype_signals` debugging dict. See [`quick-audit.md`](quick-audit.md)
for the priority order and trigger thresholds — the same enum applies
to detailed mode.

Detailed mode **does** narrate the archetype (one short sentence in
the Baseline section, e.g. *"Archetype: code_writing — Edit/Write
36% of tool calls."*). Use the `archetype_signals` to ground the
sentence; do not narrate when archetype is `unknown` (silently skip
the sentence rather than guess at why).

`baseline.first_turn_cost_share_pct` is informational. Mention it
in the Baseline section narrative **only when both** of these hold:

- `baseline.turns > 30` (long-session archetypes only — the share is
  meaningless on a 5-turn session because every turn is a large share)
- `baseline.first_turn_cost_share_pct > 5`

When mentioning, frame it as context for the per-turn average rather
than as actionable advice — first-turn setup cost is unavoidable, so
this is **not** a separate `finding` and never appears in
`quick_wins` or `structural_fixes`.

### Sixteen-finding cap (negative) + three-finding cap (positive)

Two independent caps. Negative `findings` is capped at **16**; positive
`positive_findings` is capped at **3**. The arrays do not compete for
slots. Each has no floor — emit only the findings the helper + Phase 2
produced. **Merge similar findings** rather than padding (three separate
`file_re_read` paths roll into one finding with `evidence.examples[]`).

If more than 16 negative triggers fire (rare), keep the 16 with the
highest `estimated_impact_usd` (or, where impact is null, those with
`high` then `medium` severity). If more than 3 positive triggers fire,
keep the 3 with the highest `estimated_savings_usd`. Drop the rest
silently.

**No padding, ever.** The `other` enum is **forbidden** in v1.1
outputs — do not add `"other"` rows to either array, and do not invent
positives. 5 high-quality findings beat 16 padded ones.

## LLM division of labor

To keep the detailed audit deterministic, the split mirrors the quick
playbook:

**Helper script does:**
- Everything the quick mode helper does.
- Plus all session-log scans (re-reads, paste-bombs, wrong-model,
  verbose, weekly delta, subagent orphans).

**Audit skill (this playbook on Haiku) does:**
- Read CLAUDE.md / settings / `.claudeignore` (Phase 2 — script can't
  see filesystem outside the export).
- Decide which fired triggers + detailed_candidates make the cut (cap
  at 16 by impact).
- Write concrete `fix` prose tied to the actual evidence numbers.
- Synthesise `quick_wins` + `structural_fixes` + `estimated_savings`.
- Render the markdown, write both artefacts, print inline.

## Markdown render template

Same baseline / findings table / top-3 turns as quick-audit, then
three new sections instead of `## 4. What to fix first`:

```markdown
# Detailed audit — session {session_id_short} @ {generated_at}

## 1. Baseline
{same as quick-audit}

## 2. Findings
{same table; up to 16 rows; merge similar findings}

## 3. Top 3 expensive turns
{same as quick-audit, including the (also flagged as cache_break) suffix when applicable}

## 4. Positive findings

{omit this entire section if positive_findings is empty.}
{for each positive in positive_findings, in rank order:}
- 🟢 {title} — {evidence_inline}{savings_suffix}

## 5. Quick wins (≤10 min each)

{for each bullet in quick_wins:}
- {bullet}

## 6. Structural fixes (require habit shift)

{for each bullet in structural_fixes:}
- {bullet}

## 7. Estimated savings

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
evidence inline, model-split clause, cache-savings clause,
cache-break suffix on top turns) carry over unchanged.

## Tone

- **Direct and specific.** Cite exact line numbers from CLAUDE.md
  reads, exact turn indices from the digest, exact dollar figures from
  evidence.
- **Quote sparingly.** ≤5 lines per `evidence.worst_section_excerpt`
  / `evidence.duplicated_rule`.
- **Be honest about confidence.** If the data is thin (short
  session, no config files present), set
  `estimated_savings.confidence` to `"low"` and the numeric fields
  to `null` rather than inventing percentages.
- **Stop after section 7** (or earlier if the positive findings section
  is omitted because no positive triggers fired). Do not append
  "summary" or "next steps".

## Final step (write order)

> **IMPORTANT — use the Write tool directly. Do NOT generate a Python script to
> produce the JSON or markdown.** Creating an intermediate script adds unnecessary
> failure modes (syntax errors, f-string escaping, exec failures) and is never
> required. Steps 3–6 below are performed by the AI itself using the Write tool.

1. Run `scripts/audit-extract.py <input-json> --mode detailed` once.
2. Read the Phase 2 config files (CLAUDE.md / settings / `.claudeignore`).
3. Populate the full JSON object in memory (in the AI's own context — up to 16
   findings, `quick_wins`, `structural_fixes`, `estimated_savings`).
4. Call the **Write tool** to write the JSON sidecar to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.json`.
5. Render to markdown using the template (in the AI's own context).
6. Call the **Write tool** to write the markdown copy to
   `<project>/exports/session-metrics/audit_<id8>_<ts>_detailed.md`.
7. Print the same markdown content inline (without the H1 heading).
8. Print two stderr-style lines:
   `[audit] saved → <json-path>`
   `[audit] saved → <md-path>`

## Schema versioning

Same versioning rules as quick-audit (currently `1.2`). Quick and
detailed share `audit_schema_version`; bump them together. The
detailed-only fields (`quick_wins`, `structural_fixes`,
`estimated_savings`) are optional from a quick-audit consumer's
perspective — tooling should gate on `mode: "detailed"` before
reading them.

**v1.1 → v1.2** (additive): added top-level `session_archetype` and
`baseline.first_turn_cost_usd` / `baseline.first_turn_cost_share_pct`.
v1.2 ships archetype as **detect-only**; severity overrides
conditional on archetype come in v1.31.0.

Phases 4 (interactive Q&A) and 5 (CLAUDE.md rewrite) from earlier
audit drafts remain **out of scope for v1**.
