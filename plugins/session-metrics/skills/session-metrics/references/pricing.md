# Claude Model Pricing Reference

Prices in **USD per million tokens**. Snapshot: **2026-04-17**.
Source: https://platform.claude.com/docs/en/about-claude/pricing

The `cache_write` column stored by `_PRICING` is the **5-minute cache write**
rate (1.25× base input). The **1-hour cache write** rate is 2× base input and
is not currently tracked per-entry. **Cache read** (hits + refreshes) is
0.1× base input.

## Current models

| Model ID                    | Alias      | Input | Output | Cache read | 5m Cache write |
|-----------------------------|------------|-------|--------|------------|----------------|
| `claude-opus-4-7`           | opus-4-7   |  5.00 |  25.00 |       0.50 |           6.25 |
| `claude-opus-4-6`           | opus-4-6   |  5.00 |  25.00 |       0.50 |           6.25 |
| `claude-opus-4-5`           | opus-4-5   |  5.00 |  25.00 |       0.50 |           6.25 |
| `claude-sonnet-4-7`         | sonnet-4-7 |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-sonnet-4-6`         | sonnet-4-6 |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-sonnet-4-5`         | sonnet-4-5 |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-haiku-4-5-20251001` | haiku-4-5  |  1.00 |   5.00 |       0.10 |           1.25 |
| `claude-haiku-4-5`          | haiku-4-5  |  1.00 |   5.00 |       0.10 |           1.25 |

> **Important — pricing tier change at Opus 4.5**: Opus 4.5 / 4.6 / 4.7 moved
> to a new cheaper tier ($5 input / $25 output). Opus 4 and 4.1 retain the
> original $15 / $75 tier. Earlier snapshots of this table had Opus 4.6/4.7
> at the old rates — corrected 2026-04-17.

## Legacy / prefix-fallback entries

These entries are kept for historical JSONL files that reference older models,
and for prefix-matching fallback when a model ID isn't explicitly listed.

| Model ID (prefix match) | Input | Output | Cache read | 5m Cache write |
|-------------------------|-------|--------|------------|----------------|
| `claude-opus-4-1`       | 15.00 |  75.00 |       1.50 |          18.75 |
| `claude-opus-4`         | 15.00 |  75.00 |       1.50 |          18.75 |
| `claude-sonnet-4`       |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-3-7-sonnet`     |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-3-5-sonnet`     |  3.00 |  15.00 |       0.30 |           3.75 |
| `claude-3-5-haiku`      |  0.80 |   4.00 |       0.08 |           1.00 |
| `claude-3-opus`         | 15.00 |  75.00 |       1.50 |          18.75 |
| (default fallback)      |  3.00 |  15.00 |       0.30 |           3.75 |

## Notes

- **Prefix fallback order matters**: dict insertion order is traversed until
  the first match. More-specific entries (e.g. `claude-opus-4-7`) must appear
  **before** less-specific ones (e.g. `claude-opus-4`), otherwise an unknown
  future Opus-4.7-* model ID would fall through to the old-tier rate.
- **5m vs 1h cache writes**: `_cost` only multiplies by the 5-minute rate.
  1-hour writes (2× base input) would undercount cost when present; add
  per-entry TTL tracking if this becomes material.
- **Fast mode** (Opus 4.6 research preview): 6× standard base rates
  ($30 input / $150 output). Not currently applied by `_cost` even when
  `usage.speed == "fast"`. Cost display for fast-mode turns is therefore
  underestimated by a factor of 6 — flagged as a known limitation.
- **Data residency multiplier**: US-only inference via `inference_geo`
  adds 1.1× on top of all rates. Not tracked.
- Prices are estimates; actual billing is on Anthropic's platform.
