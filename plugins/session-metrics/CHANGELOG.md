# Changelog — session-metrics

All notable changes to the session-metrics skill.
Versions match the `plugin.json` / `marketplace.json` version field.

---

## v1.30.0 — 2026-04-29

### Feature — session archetype classifier (Tier-2 batch 2: detect-only) + first-turn cost share

The audit-session-metrics skill gains a top-level `session_archetype` classifier and adds `first_turn_cost_usd` / `first_turn_cost_share_pct` to the baseline. Both are forward-looking digest fields that v1.31.0's archetype-conditional severity overrides will consume; v1.30.0 ships the classifier as **detect-only** so it can be calibrated against real audit sidecars before the override matrix lands.

### Schema

`digest_schema_version` and `audit_schema_version` bump to **1.2** (additive — no breaking changes; v1.1 readers continue to work):

- New top-level field `session_archetype` (string enum): `agent_workflow` | `short_test` | `long_debug` | `code_writing` | `exploratory_chat` | `unknown`. The default `unknown` is intentional — biased toward not labelling at low confidence (same lesson as v1.29.0's forbidden `"other"` enum).
- New top-level field `archetype_signals` (debugging dict): `turns`, `subagent_share_pct`, `cache_hit_pct`, `cache_break_count`, `cache_break_pct`, `thinking_turn_pct`, `tool_call_total`, `edit_write_pct_of_tools`, `read_pct_of_tools`, `bash_pct_of_tools`, `tools_per_turn`. Non-negotiable for the v1.31.0 override matrix to read; also a debugging surface when archetype labels feel wrong.
- New baseline fields `first_turn_cost_usd` and `first_turn_cost_share_pct`. The "first turn" skips `<synthetic>` and `is_resume_marker` turns so resumed sessions don't mis-attribute warmup cost to a placeholder.

### Classifier priority chain (first match wins)

1. `agent_workflow` — `subagent_share_pct >= 30`
2. `short_test` — `0 < turns <= 5`
3. `long_debug` — `turns > 30` AND (`cache_break_pct > 2%` OR `cache_hit_pct < 70`)
4. `code_writing` — `turns > 5` AND `Edit + Write >= 25%` of tool calls
5. `exploratory_chat` — `turns > 5` AND `tool_call_total / turns < 1.0`
6. `unknown` — default

The 2% cache-break threshold mirrors the existing `cache_break` trigger's downgrade rule: a single break in 200 turns is below typical concern, so the existing trigger downgrades to low — and the archetype must not pin the same session as `long_debug` while the trigger is calling it routine.

### Why detect-only first

Splitting the classifier from the v1.31.0 severity-override matrix means v1.30.0's archetype labels can be observed against real audit runs before the matrix locks in any decisions on guessed thresholds. If a label feels wrong on a real session, fix the threshold; only then ship the override matrix.

### Playbook contract

Both quick-audit.md and detailed-audit.md updated with v1.1 → v1.2 additive migration notes. Quick mode does **not** narrate the archetype (stays in JSON sidecar only); detailed mode narrates it in the Baseline section. `first_turn_cost_share` is **not** a finding and never enters `quick_wins` / `structural_fixes` — first-turn setup is unavoidable, so it's framing context, not actionable advice.

### Tests

15 new unit tests in `tests/test_session_metrics.py` (576 total, +15 since v1.29.0).

**No `_SCRIPT_VERSION` bump.** This is an audit-skill change; the session-metrics parse cache is untouched.

---

## v1.29.0 — 2026-04-29

### Feature — cache-aware audit pass (Tier-2 batch 1: positive findings + idle-gap cache decay)

The audit-session-metrics skill gains a positive findings array and an idle-gap-cache-decay trigger. Both fix structural problems the v1.28.0 integration test surfaced: Haiku padding the negative findings array with `"other"` filler when no real waste pattern fired, and the audit having no way to celebrate good cache hygiene.

### Schema

`digest_schema_version` and `audit_schema_version` bump to **1.1** (additive — no breaking changes to v1.0 consumers):

- New top-level array `positive_findings` (capped at 3, parallel to `findings`). Findings carry `estimated_savings_usd` rather than `estimated_impact_usd` to signal direction.
- New positive metric enum: `cache_savings_high` (savings > 3× cost OR > $5 absolute) and `cache_health_excellent` (hit ratio > 90% AND zero `cache_breaks`).
- New negative metric enum entry: `idle_gap_cache_decay` — fires when a > 5-minute gap (the 5m ephemeral cache TTL boundary) is followed by a turn where `cache_creation_input_tokens > 50%` of billable input. Aggregates the top 3 events into one finding; severity scales by total rebuild cost (low < $0.30, medium $0.30–$1, high > $1).
- The `"other"` enum is now **forbidden** in v1.1 outputs (was "use sparingly" in v1.0). Both `findings` and `positive_findings` arrays may be empty — that is the correct outcome when no triggers fire, not a defect.

### Why these specific triggers

- **Empirical**: the v1.28.0 integration test (session 8461c187) ran the helper script and found 4 honest negative triggers + 1 obvious positive (cache_savings $11.45 vs $40.83 cost = 28% savings). Haiku padded the audit with 2 `"other"` rows describing the cache savings and a synthetic-turn note. `positive_findings` is the structural fix — Haiku now has a place to put that observation rather than treating it as filler.
- **Cache-relevant**: idle gaps > 5 min cross the cache TTL boundary; cache rebuilds afterwards are a real recoverable cost. The 5m threshold matches the actual cache TTL (independent of the HTML `--idle-gap-minutes` UI default of 10 min, which is a *visual* threshold).
- **Backed by digest data, not vibes**: every threshold uses values already in the digest (`cache_savings`, `cache_hit_pct`, `cache_breaks`, per-turn timestamps, `cache_write_tokens`).

### Playbook contract

Both `quick-audit.md` and `detailed-audit.md` updated:

- Per-array caps are explicit and **independent** — 7 negative + 3 positive in quick mode, 16 negative + 3 positive in detailed mode. The arrays do not compete for slots.
- The "no padding" rule is now stronger: the `other` enum is forbidden in v1.1 outputs.
- New "Positive findings" markdown section renders after the findings table and top-3 turns, before `fix_first` / `quick_wins`. Section is omitted when `positive_findings` is empty.
- New `{savings_suffix}` render rule: appends ` — saved $<savings:.2f>` when `estimated_savings_usd` is non-null.

### Tests

10 new tests in `test_session_metrics.py` (561 total, +10 since v1.28.0):

- Schema version bump asserts.
- `cache_savings_high` fires on ratio threshold (3×) and absolute threshold ($5); does not fire when low.
- `cache_health_excellent` fires above 90% AND requires zero cache breaks (hard suppression).
- `idle_gap_cache_decay` fires after a > 5-min gap with cache rebuild; skips short gaps; skips when no rebuild; severity scales with cost.

The two playbook anchor tests gained v1.29.0 anchors (`positive_findings`, `cache_savings_high`, `cache_health_excellent`, `idle_gap_cache_decay`, `1.1`).

### Files

- `scripts/audit-extract.py` — `evaluate_positive_triggers()`, `_detect_idle_gap_cache_decay()`, idle_gap branch in `evaluate_triggers()`, `CACHE_TTL_5M_SECONDS` constant, `DIGEST_SCHEMA_VERSION = "1.1"`.
- `references/quick-audit.md` and `references/detailed-audit.md` — schema, metric enum tables, finding-cap section, render template.

No `_SCRIPT_VERSION` bump on `session-metrics.py` (this is an audit-skill change; the parse cache is untouched).

---

## v1.28.0 — 2026-04-29

### Added — `audit-extract.py` helper script

The audit-session-metrics skill now ships a Python helper script at
`scripts/audit-extract.py` that pre-computes every audit trigger,
suggested severity, estimated impact USD, top-3 expensive turns
(with cross-finding correlation flags), and — in detailed mode —
all session-log scans (file re-reads, paste-bombs, wrong-model turns,
verbose responses, weekly delta, subagent orphans). The Haiku audit
turn now consumes a single JSON digest from stdout instead of
incrementally `Read`-ing the (often >256 KB) session JSON export.

Practical impact:

- **Fewer tool roundtrips.** A typical quick audit drops from ~7 Bash
  exploration calls to 1.
- **Real `estimated_impact_usd` figures.** Where a formula exists
  (`cache_break: uncached × $5/M`, `advisor_share: realised cost`,
  etc.) the helper computes the dollar figure; Haiku no longer guesses
  or leaves `null`.
- **Severity downgrades.** When data is milder than the trigger
  threshold (e.g. 1 cache break in 200 turns), the script emits
  `suggested_severity: "low"` with a `downgrade_reason` Haiku can
  quote in the fix paragraph.
- **Cross-finding correlation.** `top_expensive_turns[i].is_cache_break`
  flag lets the audit link the same turn across findings.

### Changed — finding cap is now soft

Both playbooks relaxed the cap: quick mode is "list every fired
trigger, capped at 7 for scannability" (was "exactly 7 findings");
detailed mode is "up to 16" (no floor). Padding with `"other"` rows
to reach a target count is now explicitly forbidden — 3 fired triggers
means 3 findings, not 7 with two filler rows.

### Changed — verbose_response trigger uses cache-aware denominator

The detailed-mode verbose-response scan now uses
`output_tokens / (input_tokens + cache_read_tokens)` as the ratio,
not `output_tokens / input_tokens`. Cache-heavy sessions previously
had this trigger fire on 100% of turns because uncached `input_tokens`
alone drove the ratio to absurd values once cache hit > 50%.

### Changed — `weekly_rollup_regression` suppressed on first week

The detailed-mode weekly delta is now suppressed (rendered as `null`
in `detailed_candidates.weekly_rollup`) when the prior 7-day window
has zero usage. The previous behaviour reported a meaningless
`+98.2pp cache delta` because prior cache-hit was 0 by absence, not
by regression.

### Changed — playbook now spells out the helper / Haiku division of labour

Both `quick-audit.md` and `detailed-audit.md` gained an "LLM division
of labor" section so future-you doesn't drift back to recomputing
metrics Haiku already received from the digest.

### Removed — unverified `~$0.01 / ~$0.03` cost claims

session-metrics' SKILL.md previously advertised the audit suggestion
as costing "~$0.01 on Haiku" / "~$0.03 on Haiku". Those numbers were
estimates that never got measured against the helper-script-driven
workflow. Replaced with "Haiku — ~10× cheaper than Sonnet" which
follows from public pricing without claiming a specific dollar figure.

### Tests

20 new unit tests in `tests/test_session_metrics.py` exercise the
helper script with synthetic JSON fixtures per trigger:
filename-parser variants, baseline ratio computation, cache_break
fires + impact, severity-downgrade reason, top_turn_share threshold,
advisor share realised-cost, thinking_engagement, truncated_outputs,
session_warmup_overhead, tool_result_bloat (after Bash), top-turn
cache-break correlation flag, hypothesis classification, detailed
mode populates candidates, quick mode omits them, verbose_response
cache-aware denominator, weekly_rollup first-week suppression. The
existing playbook-anchor tests were updated to assert the new
"capped at 7", "no floor", "audit-extract.py", "Impact formula", and
"LLM division of labor" anchors.

---

## v1.27.0 — 2026-04-29

### Added — Self-cost meta-metric

session-metrics now reports its own running cost in the current session
alongside the regular report. Built from the existing `by_skill`
aggregation (filtered for `session-metrics` plus the namespaced
`session-metrics:session-metrics` plugin alias) and surfaced three ways:

- **stderr summary line.** After the `[export] FMT → path` lines a new
  `[self-cost] session-metrics consumed N prior turns this session,
  $X.XXXX, K tokens (current run not yet logged).` line prints. The
  caveat is intentional: the current invocation's tokens aren't written
  to the JSONL when the script reads it, so the figure reflects only
  prior session-metrics turns this session.
- **HTML dashboard KPI card.** The summary cards gain a
  *Skill self-cost · prior runs this session* card with `$ · turns ·
  tokens` and a tooltip explaining the chicken-and-egg lag. Auto-hides
  on first-ever invocation (zero prior turns).
- **JSON export top-level key.** New `self_cost: {turns, input,
  output, cache_read, cache_write, total_tokens, cost_usd,
  matched_skill_names, note}` block.

### Added — `--no-self-cost` flag

Suppresses all three self-cost surfaces. Only meaningful at session
and project scope; instance scope (`--all-projects`) does not compute
self-cost.

### Added — `audit-session-metrics` companion skill

A new sibling skill ships alongside `session-metrics` in the same
plugin. Pinned to `model: haiku` in frontmatter so a `quick` audit
costs ~$0.01 and a `detailed` audit costs ~$0.03 (vs ~$0.10 / ~$0.30
on Sonnet). After session-metrics writes a JSON export, its SKILL.md
prints a two-line suggestion telling the user how to invoke the audit:

```
/audit-session-metrics quick   <json-path>
/audit-session-metrics detailed <json-path>
```

The split into a separate skill is what unlocks the Haiku cost win:
the `model:` frontmatter override only takes effect when the audit
skill is the entry point of its own turn, not when called via the
Skill tool inside session-metrics' Sonnet turn. session-metrics
therefore *suggests* the slash command rather than invoking it.

### Standardised audit format (JSON sidecar + markdown)

Every audit run produces three artefacts:

- **JSON sidecar** at `exports/session-metrics/audit_<id8>_<ts>_<mode>.json` —
  versioned schema (`audit_schema_version: 1.0`) with structured
  findings, evidence objects, and an enum'd `metric` field.
- **Markdown copy** at `exports/session-metrics/audit_<id8>_<ts>_<mode>.md` —
  human-readable rendering of the same data.
- **Inline chat output** — the markdown content printed in the
  assistant's reply.

Quick mode emits exactly 7 findings; detailed mode emits up to 16.
Both modes share the same `audit_schema_version`. `estimated_impact_usd`
on each finding is optional (nullable) — the playbook explicitly tells
the model never to guess.

### Tier-1 audit insights (additional metrics)

Six new metric enum entries surface workload-shape and trend signals
the original metric set didn't catch:

- **Quick mode** (cap raised from 5 → 7 findings):
  - `session_warmup_overhead` — first turn > 20% of total cost on a
    short session (≤ 15 turns).
  - `tool_result_bloat` — `cache_write_tokens > 50K` immediately
    following a Bash/Read/WebFetch tool call.
- **Detailed mode** (cap raised from 12 → 16 findings):
  - `verbose_response` — model produces > 5× output:input tokens on
    > 30% of turns.
  - `weekly_rollup_regression` — trailing-7d cost up > 50% vs prior
    7d, or cache hit dropped > 10pp week-over-week.
  - `peak_hour_concentration` — > 70% of cost lands in the
    `--peak-hours` band when configured.
  - `subagent_attribution_orphan` — `orphan_turns > 0` in
    `subagent_attribution_summary`.

`fix_first` stays at exactly 3 bullets — picks the highest-impact
subset of findings regardless of total findings count.

---

## v1.26.2 — 2026-04-28

### Bug fix — accumulate user content blocks across the gap (parallel-spawn sibling fix)

Sibling fix to v1.26.1's `agent_links` accumulator. `_extract_turns` was overwriting `last_user_content` on every user JSONL entry, so when N parallel Task tool_results landed in N separate user entries between two assistant turns, only the last entry's content survived into `_preceding_user_content`. Downstream content-block counters under-counted `tool_result` (and `image`) blocks on the next assistant turn by N−1.

Concrete example on the dev project's mini fixture: gap before `msg_C` contains both `u4` (tool_result) and `u5` (sidechain text). Pre-fix the parser kept only `u5`'s text block — `u4`'s tool_result was dropped from the count entirely. Post-fix both survive. Project-wide on the live dev repo, the totals `tool_result` count rises to reflect every parallel-spawn fan-in.

### Fix

`_extract_turns()` now accumulates blocks from every user entry in the inter-assistant gap into `gap_user_blocks`, falls back to `gap_user_str` when only a string-form content (compaction summary) appeared, and resets both on assistant first-occurrence. The per-iteration `last_user_content` is preserved for the inner-loop logic (compaction guard, slash-command detection, agent_link extraction) — only the SNAPSHOT shape changes.

```python
# in user branch (after agent_links extension):
if isinstance(last_user_content, list):
    gap_user_blocks.extend(last_user_content)
elif isinstance(last_user_content, str):
    gap_user_str = last_user_content

# in assistant first-occurrence:
if gap_user_blocks:
    preceding_user[msg_id] = list(gap_user_blocks)
elif gap_user_str is not None:
    preceding_user[msg_id] = gap_user_str
else:
    preceding_user[msg_id] = last_user_content   # back-to-back-assistants fallback
gap_user_blocks = []
gap_user_str = None
```

No `_SCRIPT_VERSION` bump — `_extract_turns` runs after the parse cache, not before.

### Tests

- New: `test_extract_turns_accumulates_parallel_tool_result_blocks` — three parallel Task spawns + three user-entry tool_results between two assistant turns; asserts all three tool_result blocks survive into `_preceding_user_content`.
- Updated: `test_fixture_content_block_counts_per_turn` and `test_fixture_totals_content_blocks_aggregate` — the existing mini fixture's gap before `msg_C` already had two user entries (line 8 tool_result + line 9 sidechain text). Pre-fix the line-8 tool_result was dropped from `msg_C`'s preceding-user content; post-fix it's counted. The tests previously asserted the buggy old count (0) and the buggy total (2); both are now corrected to reflect the accurate behaviour (1 and 3).

517 tests pass (515 existing + 2 new since v1.26.1).

### Severity

Cost/token math was unaffected (those come from assistant `usage` fields, not user content). The fix corrects display-layer signals: `content_blocks.tool_result` and `content_blocks.image` per turn and project-wide, plus any downstream that reads them (turn-character classification, content-block waste analysis).

---

## v1.26.1 — 2026-04-28

### Bug fix — recover subagent attribution lost on parallel Task spawns

`_extract_turns` was overwriting `last_user_agent_links` on every user JSONL entry instead of accumulating, so when the assistant emitted N parallel Task tool_uses in one turn, only the LAST `(tool_use_id, agentId)` pair survived. The other N−1 spawns lost their linkage and every subagent turn from those spawns counted as an orphan.

**Real impact on the dev project (35 session blocks, $1,041 total spend):**

| Signal | Before fix | After fix |
|---|---:|---:|
| Orphan subagent turns | 477 | 8 |
| Attributed subagent turns | 1,221 | 1,697 |
| Spawns recognised | 92 | 93 |
| Subagent share of cost | 3.5% | 4.62% |

The headline 3.5% share was understated by ~30% because the parser was dropping a third of all `(tool_use_id, agentId)` pairs from the JSONL even though the data was present in every parent log.

### Fix

`scripts/session-metrics.py:_extract_turns()` — change overwrite to extend, and reset on assistant first-occurrence so pairs from one inter-assistant gap don't leak into the next:

```python
# was:  last_user_agent_links = agent_links
last_user_agent_links.extend(agent_links)
...
# inside `if msg_id not in preceding_user:` block, after capture:
last_user_agent_links = []
```

Render-time only — no parser-cache schema change, no `_SCRIPT_VERSION` bump, parse cache stays valid.

### Tests

Two regression tests added near the existing Phase-B suite:

- `test_extract_turns_accumulates_parallel_task_agent_links` — synthesises an assistant turn with two parallel Task tool_uses + two separate user `tool_result` entries, asserts both `(tuid, agentId)` pairs survive into the next assistant's `_preceding_user_agent_links`.
- `test_extract_turns_resets_agent_links_after_assistant_first_occurrence` — asserts that pairs do NOT leak from one assistant gap into a later assistant's `_preceding_user_agent_links`.

516 tests pass (514 existing + 2 new).

### Caveat

8 turns remain orphaned in the dev project. These are genuine unrecoverable cases — two subagent JSONL files (`a51a9e01fd9c84bd2`, `af258417369f5ebc6`) lack any `toolUseResult.agentId` in their parent log, most likely because the subagent crashed/was killed before its tool_result could be written back. The headline keeps its `lower bound — N orphan turns excluded` caveat for the residual cases.

---

## v1.26.0 — 2026-04-28

### Observational subagent-cost framing — share, coverage, within-session split, warm-up signals

Builds on v1.7.0 Phase-B parent-prompt attribution to answer the question "what fraction of my session went to subagents, and how should I read that number?". Render-time only — no parser changes, no `_SCRIPT_VERSION` bump, parse cache stays valid.

### What's new

**Headline `Subagent share of cost` card** — top-of-report KPI in HTML (single + instance) and a row in the MD summary table. Reads `sum(attributed_subagent_cost) / totals.cost` and renders as `X% ($Y of $Z) across N spawns`. Branches on `--include-subagents`:
- on, with attributed turns: shows the share, with `lower bound — N orphan turns excluded` when `subagent_attribution_summary.orphan_subagent_turns > 0`.
- on, no subagent activity: `0% — no subagent activity`.
- off: `attribution disabled — re-run with --include-subagents` (avoids the deceptive 0% reading the previous default would have produced).

Card structure mirrors the other KPI cards: `kpi-label` / big `kpi-val` (just the percent) / two thin `kpi-sub` lines for the supporting numbers and the lower-bound disclosure. The full prose explanation moves to the hover tooltip.

**Attribution coverage block** — two-column `models-table` under the by-subagent table that surfaces what was previously buried in `subagent_attribution_summary`: orphan turn count, cycles detected, max nesting depth, and spawn → attributed-turn fanout. Theme-aware styling across console / lattice / dark / light. Frames the headline as observational signal, not a precise measurement.

**Within-session spawning split** — per-session table comparing median *combined* turn cost (parent direct + attributed subagent) on spawning vs. non-spawning turns. Renders only for sessions with ≥3 turns in each bucket. Holds task / model / context constant within a session, but is explicitly labelled descriptive — selection bias remains because users delegate the hardest sub-tasks. *Not* a counterfactual estimate.

**Warm-up columns in `by_subagent_type`**:
- `First-turn %` — median across invocations of `first_turn.cost_usd / total_invocation_cost`. High = short-lived agents pay setup tax without amortising.
- `SP amortised %` — fraction of invocations whose turn ≥2 read from cache (system-prompt cache write paid back at least once).
- Visible only when `--include-subagents` is on AND at least one invocation was observed.

**Per-prompt badge** — appended `(NN% of combined cost)` to the existing `+N subagents` annotation. Labelled "combined", not "of turn", because the visible Cost column shows direct cost only; "% of turn" would mathematically imply the parent was 37% of itself.

### Honesty notes baked into the surfaces

- "Share" is used everywhere instead of "overhead" — overhead implies the cost would otherwise be unpaid, exactly the unanswered counterfactual.
- The headline is documented as a lower bound whenever orphans exist.
- The within-session split's body text states explicitly that descriptive correlation is *not* a counterfactual estimate.
- The synthetic-A/B benchmark and analytical crossover calculator are deferred to follow-ups; this release does not pretend to answer the causal "did delegating cost more" question.

### What changed in code

- `_empty_subagent_row` gains `invocation_count`, `first_turn_share_pct`, `sp_amortisation_pct`.
- `_build_by_subagent_type` groups subagent turns by `subagent_agent_id` per-invocation and rolls per-invocation metrics up to type rows. Aggregation is at report-build time, not per-turn — no parse-cache schema change.
- New helpers: `_compute_subagent_share`, `_compute_within_session_split`, `_compute_instance_subagent_share`, `_median`, `_build_subagent_share_card_html`, `_build_attribution_coverage_html`, `_build_within_session_split_html`, `_build_subagent_share_md`, `_build_within_session_split_md`.
- `_build_report` precomputes `subagent_share_stats` + `subagent_within_session_split` and stashes them on the report dict so JSON/CSV/MD/HTML all see the same values.
- `_build_instance_report` aggregates per-project shares and runs the within-session split over the flattened `all_sessions_out`. Instance report now propagates `include_subagents`.
- `render_html`, `render_md`, `_render_instance_html`, `_render_instance_md` updated.
- CSV `by_subagent_type` block gains `invocation_count`, `first_turn_share_pct`, `sp_amortisation_pct` columns.
- 8 new tests in `tests/test_session_metrics.py`. Existing 506 tests remain green.

### Known limitations

- The headline relies on Phase-B attribution; orphan rate matters. On a real session during manual verification, 45 of ~150 subagent turns were orphans (chains the three-pass linkage couldn't resolve back to a root prompt) — the share was clearly disclosed as a lower bound.
- The within-session split has within-session selection bias and does not replace a synthetic A/B test for the causal question.
- The compression-ratio signal (parent `tool_result` payload size vs. subagent gross spend) was considered but deferred — would require a parser change to capture `tool_result` text length and bump `_SCRIPT_VERSION`.

---

## v1.25.1 — 2026-04-28

### Bug fix — `iterations:null` crash when advisor is not enabled

`<synthetic>` resume-marker turns written by environments where the advisor feature
is not active (e.g. the desktop app) emit `"iterations": null` in the usage dict
rather than omitting the key. `u.get("iterations", [])` returns `None` when the key
exists with a null value, causing `TypeError: 'NoneType' object is not iterable` in
`_cost` and `_advisor_info` whenever a project-scope run included those sessions.

- Replace `u.get("iterations", [])` with `u.get("iterations") or []` in both
  `_cost` and `_advisor_info`. Handles absent, null, and valid-list cases identically.

---

## v1.25.0 — 2026-04-28

### Advisor turn support — cost correction + surface

The Claude Code Advisor (`advisor()` tool) runs a second model against the full conversation
transcript. Its tokens were previously hidden in `usage.iterations[]` and not counted, causing
advisor turns to be silently under-priced by up to 6.6×.

- **Cost correction**: `_cost()` now reads `usage.iterations[type=="advisor_message"]` and
  bills advisor tokens at the advisor model's list rates. The corrected `cost_usd` propagates
  to all session/project/instance aggregates.
- **New per-turn fields**: `advisor_calls`, `advisor_cost_usd`, `advisor_model`,
  `advisor_input_tokens`, `advisor_output_tokens`.
- **Session field**: `advisor_configured_model` from the top-level `advisorModel` JSONL field.
- **Content classification**: `server_tool_use` → letter `v`; `advisor_tool_result` → letter `R`.
  `"advisor"` appears in tool names and the drawer tools list.
- **Dashboard card**: "Advisor calls" (amber badge, auto-hidden when unused).
- **Session table**: amber annotation/badge in `--project-cost` HTML and text output.
- **CLI footer**: `Advisor calls : N call(s)  +$X.XXXX` when advisor was used.
- **Per-turn drawer**: cost section shows Primary / Advisor / Cost breakdown; TOKENS section
  shows Advisor input / Advisor output rows. Both hidden on non-advisor turns.
- **Schema docs** (`references/jsonl-schema.md`): four new fields documented.
- Graceful degradation — sessions without advisor activity produce identical output.

## v1.24.0 — 2026-04-28

### Fix: `file_reread` classification accuracy

- First access in any context segment no longer flagged as a wasteful re-read (only the
  2nd+ read in the same segment counts).
- Subagent-boundary re-reads (model switch or session resume) are now shown as informational
  — no ⚠ badge — because accessing files in a fresh context is expected and unavoidable.
- Drawer explanation splits into two branches: cross-context reads get tips on `offset`/`limit`;
  same-context re-reads get tips on `Grep` / `Read` with offsets.
- `_BASH_PATH_RE` extended-allowlist: hidden directories (`.claude`, `.git`) no longer produce
  false path entries in the classification detail.

## v1.23.0 — 2026-04-28

### Turn Character section in every turn drawer + cross-browser overflow fix

- Clicking any timeline row now shows a "Turn Character" section in the detail drawer with a
  colour-coded classification label and a one-sentence explanation derived from that turn's
  actual token data (file basenames, cache percentages, block counts, etc.).
- Fixed the ⚠ risk badge overflowing outside the timeline cell in Opera and other non-Chromium
  browsers.

## v1.22.0 — 2026-04-28

### 9-category turn waste classification

Classifies every assistant turn into one of: `productive`, `retry_error`, `file_reread`,
`oververbose_edit`, `dead_end`, `cache_payload`, `extended_thinking`, `subagent_dispatch`,
or `normal`.

- Turn Character column in the HTML timeline with colour-coded labels and ⚠ risk badges.
- Stacked-bar chart in the dashboard (waste distribution by session).
- Drill-down cards per waste category with turn count, token share, and examples.
- `turn_character` / `turn_risk` fields in JSON and CSV output.

## v1.21.0 — 2026-04-27

### Four inline markers in the HTML detail timeline

- Idle-gap dividers: slate pill `▮ N min idle` between turns when wall-clock gap ≥ threshold
  (`--idle-gap-minutes`, default 10; set 0 to disable).
- Model-switch dividers: cyan pill `⇄ Model: prev → cur` when the model changes mid-session.
- Truncated-response badge: orange `✂ truncated` on `max_tokens` turns + dashboard KPI card.
- Cache-break inline badge: amber `⚡` on turns that invalidate the prompt cache.

`stop_reason` and `is_cache_break` added as CSV columns.

## v1.20.1 — 2026-04-27

### Fix: spurious skill-tag badge after context compaction

Context-compaction summaries contain verbatim prior-session text including slash-command XML
tags. These were producing a false badge on the first post-compaction turn. Fixed by detecting
the compaction sentinel and skipping slash-command extraction for those entries.

## v1.20.0 — 2026-04-27

### Skill/slash-command badge in HTML timeline model column

When a turn was triggered by a skill invocation or slash command (e.g. `session-metrics`), a
small purple badge appears inline in the timeline. The turn drawer also shows a "Skill" row.

## v1.19.0 — 2026-04-26

### Per-turn latency + session wall-clock

- `latency_seconds` per turn: wall-clock seconds from preceding user entry to the assistant
  response.
- `wall_clock_seconds` per session (first user prompt → last assistant).
- Markdown summary gains `Wall clock` and `Mean turn latency` rows.
- `--compare-run-prompt-steering` wrapper for prompt-steering sweeps via `--compare-run`.

## v1.18.2 — 2026-04-25

### Fix: Console theme turn drawer transparent background

## v1.18.1 — 2026-04-25

### Fix: cache-breaks/skills/subagents sections duplicated in detail.html

The cross-cutting summary sections (cache breaks, skills, subagents) now appear only in the
dashboard page, not in both the dashboard and the detail page.

## v1.18.0 — 2026-04-25

### `--include-subagents` on by default

Subagent JSONL files are now included in session reports automatically. Opt out with
`--no-include-subagents`. Also fixes the subagent hint label in the Insights dashboard card.

## v1.17.1 — 2026-04-25

### Fix: cache-breaks section unstyled in non-default themes

Cache-break section elements now have correct colours across all four themes (Beacon, Console,
Lattice, Pulse).

## v1.17.0 — 2026-04-25

### Subagent → parent-prompt token attribution

Maps every subagent turn's tokens back to the originating user prompt via a three-stage
linkage chain (`tool_use.id → prompt_anchor → agent_id → root`).

- HTML prompts table sorts by `cost_usd + attributed_subagent_cost` by default — the "what
  action cost the most" lens.
- "Subagents +$" column and "+N subagents" row badge auto-appear when attribution is present.
- `--sort-prompts-by {total,self}` and `--no-subagent-attribution` flags.
- Three new CSV columns: `attributed_subagent_tokens`, `attributed_subagent_cost`,
  `attributed_subagent_count`.

## v1.16.0 — 2026-04-25

### Cross-cutting sections: cache breaks, skills & slash commands, subagent summary

Four new summary sections in the HTML dashboard for every session / project export:
cache-break cost analysis, skill/slash-command invocation table, and subagent type breakdown.
`--cache-break-threshold N` (default 500 tokens) controls the minimum re-fill size to report.

## v1.15.2 — 2026-04-25

### 10 additional model pricing entries + regex/prefix matching tier

Extended `_PRICING` with 10 more models. Prefix matching covers entire model families without
requiring exact `model_id` entries. Stderr advisory emitted for truly unknown models.

## v1.15.1 — 2026-04-25

### Non-Claude model pricing: GLM, Gemma 4, Qwen 3.5

Correct per-token rates for GLM-4.7 / GLM-5 / GLM-5.1 (Z.ai), Gemma 4 (Google / Ollama
local variants), and Qwen 3.5:9b. Prevents silent Sonnet-rate mis-attribution on mixed-model
sessions.

## v1.15.0 — 2026-04-24

### 4-theme picker embedded in every HTML export

All four themes (Beacon, Console, Lattice, Pulse) are embedded in every generated HTML file.
Users switch at view-time via a top-nav button strip; choice persists across Dashboard↔Detail
and instance→project drill-down links via URL hash + localStorage. Console is the default.
Also: 25% font size increase, Highcharts bundle gated to single-page variant only.

## v1.14.1 — 2026-04-23

### Fix: instance dashboard chart shows real token breakdowns

Instance daily chart now shows stacked input/output/cache-read/cache-write token breakdown per
day (was showing cost-only bars). Day axis label added.

## v1.14.0 — 2026-04-22

### Instance-level "all projects" dashboard

`--all-projects` generates a single dashboard aggregating every project under your Claude Code
install. Summary cards, daily cost timeline, projects table (sorted by cost, with clickable
drilldown links to per-project dashboards), and reused weekly/punchcard/time-of-day insights.
`--no-project-drilldown` fast path, `--projects-dir PATH` override for custom installs.
Output lands in `exports/session-metrics/instance/YYYY-MM-DD-HHMMSS/`.

## v1.13.1 — 2026-04-22

### Fix: `_resolve_tz` docstring correction

Corrected internal docstring that incorrectly described an `Intl.DateTimeFormat` implementation.

## v1.13.0 — 2026-04-22

### IFEval paired-samples statistics: McNemar test + Wilson CI

`--compare` HTML report gains a statistical significance table: McNemar χ² + p-value and
Wilson 95% CI for each IFEval pass-rate comparison. Small-N banner suppresses stats when
fewer than 6 paired samples are available.

## v1.12.0 — 2026-04-22

### `--strict-tz` flag + Windows tzdata hint

`--strict-tz` exits with a clear error when the system's zoneinfo database cannot resolve the
requested IANA timezone (the default is lenient — falls back to UTC). On Windows, an advisory
hints to install the `tzdata` pip package when `ZoneInfo` fails to load.

## v1.11.3 — 2026-04-21

### Audit Tier 3 fixes: test hygiene + cost note

Added a comment inside `_cost()` pointing to the fast-mode 6× multiplier caveat in
`references/pricing.md`. Test temp-directory randomisation and `atexit` contract pin.

## v1.11.2 — 2026-04-21

### Audit Tier 2 hardening: contract pin

`atexit` advisory handler is now registered at module load time (not lazily), so it fires even
in early-exit paths.

## v1.11.1 — 2026-04-21

### Audit Tier 1 hardening + `--allow-unverified-charts` flag

- Theme-aware drawer backdrop, `<meta name="chart-lib">` in every HTML export, `@media print`
  hide rules for cleaner PDF output.
- Unknown-model `stderr` advisory at process exit (lists models that fell through to Sonnet
  default pricing).
- Fast-mode `stderr` advisory with count of `usage.speed == "fast"` turns.
- `--compare`, `--compare-prep`, `--compare-run`, `--count-tokens-only` are now mutually
  exclusive via argparse group.
- `--allow-unverified-charts` opt-in to skip Highcharts vendor SHA-256 check for offline
  workflows.

## v1.11.0 — 2026-04-21

### Clickable per-turn timeline rows with full detail drawer

Every row in the HTML detail timeline is now clickable. Clicking opens a right-side sliding
drawer showing: turn metadata (model, cost, tokens, stop reason), prompt text, all tool calls
with input previews, and a linked prompts table. Keyboard-accessible (Enter/Escape).

## v1.10.0 — 2026-04-20

### Custom prompt commands in SKILL.md

SKILL.md dispatch extended with custom prompt-command rows so Claude routes natural-language
requests like "compare these two sessions" or "run a headless compare" to the correct flags
without ambiguity. README updated with command examples.

## v1.9.0 — 2026-04-20

### `--compare-run` headless automation

`--compare-run` spawns two `claude -p` sessions headlessly, feeds each one the same prompt
suite, and then calls `--compare` on the resulting JSONLs — a single command for an end-to-end
A/B model benchmark. `[1m]` default effort prefix added to prompt suite entries.

## v1.8.0 — 2026-04-20

### Session-resume detection: `claude -c` and terminal-exit markers

Detects two resume patterns in the JSONL: the `<synthetic>` model marker (auto-continuation
after context limit) and the `/exit` + re-open pattern (manual terminal-exit resume). Both are
surfaced as timeline dividers and counted in the dashboard "Session resumes" card. Terminal
exits are visually distinguished from normal resumes.

## v1.7.1 — 2026-04-19

### Subagent-related fixes

Minor UI fixes to subagent display in the dashboard and timeline.

## v1.7.0 — 2026-04-19

### `--compare` two-session A/B comparison (Phases 1–9 + trigger hardening)

`session-metrics --compare A.jsonl B.jsonl` produces a paired comparison: side-by-side token/
cost/cache metrics, IFEval-style pass-rate evaluation (sentinel-tagged prompt suite, 10 built-in
predicates), paired-turn table, quality-vs-cost verdict, and a shareable single-page HTML
report. Also includes `--compare-prep` to generate a canonical prompt suite, and
`--count-tokens-only` (API-key path) to estimate token counts before running.

Three-layer trigger discipline: argparse mutex, SKILL.md `$ARGUMENTS[0]` dispatch gate, and
description-level LLM guard prevent accidental invocation on unrelated prompts.

## v1.6.0 — 2026-04-19

### `/usage`-style Usage Insights panel on the dashboard

New dashboard section mirroring the data Claude Code's `/usage` command surfaces: total spend,
cache efficiency, model breakdown, top-sessions table, and conditional insight cards
(model-compare nudge, fast-mode advisory, etc.). Threshold-gated so cards only appear when the
data is meaningful.

## v1.5.0 — 2026-04-18

### Resume-marker cost tracking

Session-resume markers now carry a token/cost estimate for the context re-fill cost incurred
by resuming the conversation. Surfaced in the dashboard card and timeline divider subtitle.

## v1.4.1 — 2026-04-18

### Fix: terminal-exit marker visually distinguished from resume marker

The dashboard card correctly reported "2 resumes · 1 terminal exit" but the timeline dividers
were rendering all three as identical "↻ Session resumed" pills. Terminal-exit markers now
render with a distinct visual style (`⊠ Session ended`) so both surfaces tell a consistent
story.

## v1.4.0 — 2026-04-18

### Session-resume detection (initial)

Detects `claude -c` resumes via the `/exit` + `<synthetic>` fingerprint and surfaces resume
events in the dashboard and HTML timeline.

## v1.3.0 — 2026-04-18

### Content-block distribution (Proposal B) + streaming-dedup fix

Per-turn and aggregate counts for `thinking`, `tool_use`, `text`, `tool_result`, and `image`
content blocks. HTML Content column with compact letter encoding and tooltips. Extended-thinking
and Tool-calls dashboard cards. CSV gains five new block-count columns.

Fix: multi-entry streaming messages were losing all but the last content block. `_extract_turns`
now unions blocks across all occurrences of the same `message.id`.

## v1.2.0 — 2026-04-18

### Ephemeral cache TTL drilldown (Proposal A) — pricing accuracy

Splits `cache_creation_input_tokens` into 5-minute and 1-hour buckets and prices each at its
correct Anthropic rate. Previously all cache writes were charged at the 5m rate, causing
up to 60% undercount of the cache-write component for sessions that used 1-hour TTL.

HTML: TTL badge on CacheWr cells. Text/MD: `*` suffix on affected cells. CSV/JSON: three new
per-turn fields. Dashboard: Cache TTL mix card.

## v1.1.0 — 2026-04-18

### uPlot + Chart.js MIT-licensed chart alternatives

`--chart-lib {highcharts,uplot,chartjs,none}`. uPlot (~45 KB, MIT) and Chart.js (~70 KB, MIT)
are fully vendored with SHA-256 manifest verification. Use `--chart-lib uplot` for a fully
MIT-licensed export; `--chart-lib none` for a zero-JS archive.

## v1.0.0 — 2026-04-17

### First stable release

- Per-turn token/cost/cache breakdown across 5-hour session blocks.
- Multi-format export: text, JSON, CSV, Markdown, HTML (2-page dashboard + detail).
- Usage insights: weekly roll-up, session duration + burn rate, hour-of-day punchcard,
  weekday × hour heatmap, 5-hour session-block analysis.
- Vendored Highcharts (`--chart-lib highcharts`) with SHA-256 integrity check.
- Parse cache (`~/.cache/session-metrics/`) for fast re-analysis of unchanged JSONLs.
- Input validation, path containment, timezone support (`--tz`, `--utc-offset`).
- Pricing table covers claude-opus-4-7 / sonnet-4-6 / haiku-4-5 + historical models.
