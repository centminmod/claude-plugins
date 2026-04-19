---
name: session-metrics
description: >
  Tally Claude Code session token usage and cost estimates from the raw JSONL
  conversation log. Trigger when the user asks about session cost, token usage,
  API spend, cache hit rate, input/output tokens, or wants a breakdown of how
  much a Claude Code session has cost. Also trigger for "how much have we spent",
  "show me token usage", "session summary", "cost so far", or any request to
  analyse or display per-turn metrics from the current or a past session.

  Do NOT auto-dispatch compare mode (--compare / --compare-prep /
  --count-tokens-only) from natural-language phrases. The skill body uses
  $ARGUMENTS[0] as the dispatch key — if the first positional argument is not
  literally "compare", "compare-prep", or "count-tokens", route to the default
  single-session report.
---

# Session Metrics

Runs `scripts/session-metrics.py` against the Claude Code JSONL log to produce
a timeline-ordered cost summary with per-turn and cumulative totals.

## Dispatch — how to route this invocation

**First positional argument received:** `$ARGUMENTS[0]`
**Full argument string:** `$ARGUMENTS`

Read `$ARGUMENTS[0]` above and match it by **literal equality** against the
table below. Claude Code already tokenized the arguments shell-style, so no
parsing is required — just compare strings.

| `$ARGUMENTS[0]`     | Route                                     | Then read |
|---------------------|-------------------------------------------|-----------|
| `compare`           | Two-session compare                       | `## Model comparison` below, then [`references/model-compare.md`](references/model-compare.md) before running |
| `compare-prep`      | Print capture protocol + 10-prompt suite  | `## Model comparison` below |
| `count-tokens`      | API-key-only tokenizer check              | `## Model comparison` below |
| *(empty, or any other value)* | Default single-session report   | `## Quick usage` below |

This is the single gate that keeps compare mode off the natural-language
path. **Do not infer the route from the user's chat history; only use the
literal value of `$ARGUMENTS[0]` above.**

When the skill auto-triggers from a natural-language question ("how much did
this session cost?", "show me token usage"), there are no positional
arguments — `$ARGUMENTS[0]` is empty — and you always route to the default.
Phrases like "compare 4.6 vs 4.7 cost" arriving as natural language do NOT
produce `$ARGUMENTS[0] = compare` and must not route into compare mode;
answer them by running the default report on the current session and
suggesting `/session-metrics compare-prep` if the user wants a real
benchmark.

## Quick usage

```bash
# Current session (auto-detected from cwd)
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py

# Specific session ID
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --session <uuid>

# Specific project slug (use = when slug starts with "-")
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --slug=-home-user-projects-myapp
# Or via env var (always safe):
CLAUDE_PROJECT_SLUG="-home-user-projects-myapp" uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py

# List available sessions for this project
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --list

# All sessions — timeline + per-session subtotals + grand project total
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --project-cost

# Export to exports/session-metrics/ (one or more formats)
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --output json
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --output json csv md html
uv run python ${CLAUDE_SKILL_DIR}/scripts/session-metrics.py --project-cost --output html
```

> `${CLAUDE_SKILL_DIR}` is expanded by Claude Code to the skill's install directory (plugin cache, project-local copy, or bundled template — whichever applies). When running the script manually from a shell, substitute the actual path.

## Export formats

`--output` accepts one or more of: `json` `csv` `md` `html`

Text is always printed to stdout. Exports go to `exports/session-metrics/` in the
project root, named `session_<id8>_<YYYYMMDD_HHMMSS>.<ext>` (single) or
`project_<YYYYMMDD_HHMMSS>.<ext>` (project mode).

| Format | Contents |
|--------|----------|
| `json` | Full structured report with all turns, subtotals, model rates |
| `csv`  | One row per turn: session_id, index, timestamp, model, tokens, cost |
| `md`   | Summary table + per-session Markdown tables |
| `html` | Dark-theme report with summary cards + insights + chart. 2-page split by default (`<stem>_dashboard.html` + `<stem>_detail.html`); pass `--single-page` for one file. |

### HTML-specific flags

| Flag | Purpose |
|------|---------|
| `--single-page`              | Emit one self-contained HTML instead of the dashboard+detail split. |
| `--chart-lib {highcharts,uplot,chartjs,none}` | Choose the chart renderer. Default `highcharts` (richest visualization, vendored, SHA-256-verified, non-commercial license). `uplot` and `chartjs` are MIT-licensed alternatives. `none` emits a detail page with no JS dependency. See [`scripts/vendor/charts/README.md`](scripts/vendor/charts/README.md) for per-library license terms. |
| `--peak-hours H-H`           | Translucent band on the hour-of-day chart (e.g. `5-11`). Community-reported, not an Anthropic SLA. |
| `--peak-tz <IANA>`           | Timezone the peak hours are defined in (default `America/Los_Angeles`). |

### Other useful flags

| Flag | Purpose |
|------|---------|
| `--tz <IANA>`                | IANA timezone for time-of-day bucketing **and timeline/export timestamps**. Defaults to the system local tz (auto-detected via `TZ` env var or the OS setting). |
| `--utc-offset <H>`           | Fixed UTC offset, DST-naive. Use `--tz` for DST-aware. |
| `--no-cache`                 | Skip `~/.cache/session-metrics/parse/` and always re-parse from scratch. |
| `--include-subagents`        | Also tally spawned subagent JSONL files. |

> **Invocation note for the AI.** Don't pass `--tz` or `--utc-offset` unless the user explicitly asks for a specific timezone. The script auto-detects the user's system tz and renders all human-facing timestamps (timeline, session headers, generated-at banner, block anchors) in that tz. JSON/CSV raw `timestamp` fields stay UTC ISO-8601 as a machine-readable audit trail.

## Output columns

| Column    | Meaning                                      |
|-----------|----------------------------------------------|
| `#`       | Deduplicated turn index                      |
| `Time`    | Timestamp of the turn in the user's local timezone (auto-detected; override with `--tz` / `--utc-offset`). Header shows the active tz label. Raw `timestamp` fields in JSON/CSV exports remain UTC ISO-8601 (`...Z`) for machine-readability. |
| `Input`   | Net new input tokens (uncached portion only — cache reads/writes are shown separately) |
| `Output`  | Output tokens generated (includes thinking + tool_use block tokens) |
| `CacheRd` | Tokens served from prompt cache (cheap)      |
| `CacheWr` | Tokens written to prompt cache (one-time). `1h` / `mix` badge marks turns that used the 1-hour TTL tier; CSV/JSON expose `cache_write_5m_tokens` and `cache_write_1h_tokens` as dedicated columns. |
| `Content` | Per-turn content-block distribution. Letter encoding `T` thinking, `u` tool_use, `x` text, `r` tool_result, `i` image (zero counts omitted). Renders only when at least one turn carries any content block. CSV/JSON expose `thinking_blocks` / `tool_use_blocks` / `text_blocks` / `tool_result_blocks` / `image_blocks` as dedicated per-turn columns. |
| `Total`   | Sum of the four billable token buckets       |
| `Cost $`  | Estimated USD for this turn                  |

Deep-dive on exact column semantics, JSON keys, and detection rules:
[`references/jsonl-schema.md`](references/jsonl-schema.md).

Footer shows session totals + **cache savings** vs a hypothetical no-cache
run. Conditional dashboard cards appear when their feature was used in the
session: **Cache TTL mix** (when any 1h-tier cache writes happened),
**Extended thinking engagement** (when any turn carried a `thinking` block),
**Tool calls** (top-3 tool names), **Session resumes** (timeline divider at
each `claude -c` resume point, detected from `/exit` + synthetic-turn
fingerprint — lower-bound count), and the **Usage Insights** panel
(prose-style pattern characterisations inspired by Anthropic's `/usage`
command, auto-hide below threshold, exposed in JSON under `usage_insights`
and in Markdown under `## Usage Insights`).

## Model comparison

Reached only when `$ARGUMENTS[0]` is `compare`, `compare-prep`, or
`count-tokens` (see the Dispatch section at the top of this file). Covers
two modes — **controlled** session pair and **observational** project
aggregate — plus a separate API-key-only `--count-tokens-only` tool.

**For Claude subscription-plan users, `--compare` is the mode you want.**
It reads local JSONLs — no API key needed. `--count-tokens-only` is a
narrow pre-capture API-key tool, **not** the subscription path; do not
suggest it when the user is comparing two subscription-plan sessions.

**Before proposing any compare-mode command, read
[`references/model-compare.md`](references/model-compare.md).** That doc
covers the full capture protocol, CLI flag table, Mode 1 vs Mode 2 output
contracts, prompt-suite catalogue, IFEval predicates, HTML variant layout,
`count_tokens` caveats, and troubleshooting. The eager content in this
file deliberately stays minimal so single-session reports don't pay for
compare-mode context they don't use.

## Reference files

- [`references/pricing.md`](references/pricing.md) — Per-model token prices used
  for cost calculation. Read when the user asks about pricing or you need to
  add a new model.
- [`references/jsonl-schema.md`](references/jsonl-schema.md) — JSONL entry
  structure + full output-column semantics, cache-TTL split rationale, content-
  block distribution, resume detection. Read when debugging missing data,
  extending the script, or interpreting any non-obvious column/key.
- [`references/model-compare.md`](references/model-compare.md) — `--compare`
  workflow, prompt-suite catalogue, IFEval predicates, interpretation guide.
  Read when `$ARGUMENTS[0]` routes into compare mode.

## How session detection works

1. Derives the project slug from `cwd`: replaces `/` → `-`, strips leading `-`.
2. Scans `~/.claude/projects/<slug>/` for `*.jsonl` files (excludes `subagents/`).
3. Picks the most recently modified file as the current session.
4. Override with `--session <uuid>` or `--slug <slug>` when needed.

## Deduplication

Each API response is written to the JSONL multiple times (streaming, tool
completion, final). The script deduplicates on `message.id` — keeping only the
**last** occurrence so token counts reflect the final settled value.
