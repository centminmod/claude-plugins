---
name: session-metrics
description: >
  Tally Claude Code session token usage and cost estimates from the raw JSONL
  conversation log. Trigger when the user asks about session cost, token usage,
  API spend, cache hit rate, input/output tokens, or wants a breakdown of how
  much a Claude Code session has cost. Also trigger for "how much have we spent",
  "show me token usage", "session summary", "cost so far", or any request to
  analyse or display per-turn metrics from the current or a past session.
---

# Session Metrics

Runs `scripts/session-metrics.py` against the Claude Code JSONL log to produce
a timeline-ordered cost summary with per-turn and cumulative totals.

## Quick usage

```bash
# Current session (auto-detected from cwd)
uv run python .claude/skills/session-metrics/scripts/session-metrics.py

# Specific session ID
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --session <uuid>

# Specific project slug (use = when slug starts with "-")
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --slug=-home-user-projects-myapp
# Or via env var (always safe):
CLAUDE_PROJECT_SLUG="-home-user-projects-myapp" uv run python .claude/skills/session-metrics/scripts/session-metrics.py

# List available sessions for this project
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --list

# All sessions — timeline + per-session subtotals + grand project total
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --project-cost

# Export to exports/session-metrics/ (one or more formats)
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --output json
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --output json csv md html
uv run python .claude/skills/session-metrics/scripts/session-metrics.py --project-cost --output html
```

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

> **Invocation note for the AI.** Don't pass `--tz` or `--utc-offset` unless the user explicitly asks for a specific timezone. The script auto-detects the user's system tz and renders all human-facing timestamps (timeline, session headers, generated-at banner, block anchors) in that tz. JSON/CSV raw `timestamp` fields stay UTC ISO-8601 as a machine-readable audit trail.
| `--no-cache`                 | Skip `~/.cache/session-metrics/parse/` and always re-parse from scratch. |
| `--include-subagents`        | Also tally spawned subagent JSONL files. |

## Output columns

| Column   | Meaning                                      |
|----------|----------------------------------------------|
| `#`      | Deduplicated turn index                      |
| `Time`   | Timestamp of the turn in the user's local timezone (auto-detected from system; override with `--tz` or `--utc-offset`). The header shows the active tz label, e.g. `Time (UTC+10)` or `Time (Australia/Brisbane)`. Raw `timestamp` fields in JSON/CSV exports remain UTC ISO-8601 (`...Z`) for machine-readability. |
| `Input`  | Net new input tokens (uncached portion only — cache reads/writes are shown separately) |
| `Output` | Output tokens generated (includes thinking + tool_use block tokens) |
| `CacheRd`| Tokens served from prompt cache (cheap)      |
| `CacheWr`| Tokens written to prompt cache (one-time). **v1.2.0+**: a `1h` / `mix` badge (HTML) or `*` suffix (text / Markdown) marks turns that used the 1-hour TTL tier; hover or scroll to the footer for the 5m / 1h split. CSV/JSON expose `cache_write_5m_tokens` and `cache_write_1h_tokens` as dedicated columns alongside the existing `cache_write_tokens` sum. |
| `Cost $` | Estimated USD for this turn                  |

A short **column legend** renders near the Timeline header in every
human-facing format (text, Markdown, HTML). CSV and JSON are
self-describing via their header row / key names — no inline legend.

Footer shows session totals + **cache savings** vs a hypothetical
no-cache run. When any turn used the 1-hour cache TTL tier, an extra
`Extra cost paid for 1h cache tier` line breaks out the premium paid
for the longer reuse window, and a **Cache TTL mix** dashboard card
appears on the HTML report.

## Reference files

- [`references/pricing.md`](references/pricing.md) — Per-model token prices used
  for cost calculation. Read this when the user asks about pricing or if you need
  to add a new model.
- [`references/jsonl-schema.md`](references/jsonl-schema.md) — JSONL entry
  structure. Read this when debugging missing data or extending the script.

## How session detection works

1. Derives the project slug from `cwd`: replaces `/` → `-`, strips leading `-`.
2. Scans `~/.claude/projects/<slug>/` for `*.jsonl` files (excludes `subagents/`).
3. Picks the most recently modified file as the current session.
4. Override with `--session <uuid>` or `--slug <slug>` when needed.

## Deduplication

Each API response is written to the JSONL multiple times (streaming, tool
completion, final). The script deduplicates on `message.id` — keeping only the
**last** occurrence so token counts reflect the final settled value.
