# Claude Model Pricing Reference

Prices in **USD per million tokens**. Snapshot: **2026-04-18**.
Source: https://platform.claude.com/docs/en/about-claude/pricing

Anthropic bills **two cache-write tiers**:

- **5-minute TTL** (`cache_write` column): **1.25× base input**
- **1-hour TTL** (`cache_write_1h` column): **2× base input**

As of **v1.2.0** the per-entry split is read from
`message.usage.cache_creation.ephemeral_{5m,1h}_input_tokens` when the
nested object is present. Legacy transcripts without that object fall
back to the 5-minute rate — preserves pre-v1.2.0 numbers for those
files.

**Cache read** (hits + refreshes) is **0.1× base input** regardless
of TTL.

## Current models

| Model ID                    | Alias      | Input | Output | Cache read | 5m Cache write | 1h Cache write |
|-----------------------------|------------|-------|--------|------------|----------------|----------------|
| `claude-opus-4-7`           | opus-4-7   |  5.00 |  25.00 |       0.50 |           6.25 |          10.00 |
| `claude-opus-4-6`           | opus-4-6   |  5.00 |  25.00 |       0.50 |           6.25 |          10.00 |
| `claude-opus-4-5`           | opus-4-5   |  5.00 |  25.00 |       0.50 |           6.25 |          10.00 |
| `claude-sonnet-4-7`         | sonnet-4-7 |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-sonnet-4-6`         | sonnet-4-6 |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-sonnet-4-5`         | sonnet-4-5 |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-haiku-4-5-20251001` | haiku-4-5  |  1.00 |   5.00 |       0.10 |           1.25 |           2.00 |
| `claude-haiku-4-5`          | haiku-4-5  |  1.00 |   5.00 |       0.10 |           1.25 |           2.00 |

> **Important — pricing tier change at Opus 4.5**: Opus 4.5 / 4.6 / 4.7 moved
> to a new cheaper tier ($5 input / $25 output). Opus 4 and 4.1 retain the
> original $15 / $75 tier. Earlier snapshots of this table had Opus 4.6/4.7
> at the old rates — corrected 2026-04-17.

## Legacy / prefix-fallback entries

These entries are kept for historical JSONL files that reference older models,
and for prefix-matching fallback when a model ID isn't explicitly listed.

| Model ID (prefix match) | Input | Output | Cache read | 5m Cache write | 1h Cache write |
|-------------------------|-------|--------|------------|----------------|----------------|
| `claude-opus-4-1`       | 15.00 |  75.00 |       1.50 |          18.75 |          30.00 |
| `claude-opus-4`         | 15.00 |  75.00 |       1.50 |          18.75 |          30.00 |
| `claude-sonnet-4`       |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-3-7-sonnet`     |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-3-5-sonnet`     |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |
| `claude-3-5-haiku`      |  0.80 |   4.00 |       0.08 |           1.00 |           1.60 |
| `claude-3-opus`         | 15.00 |  75.00 |       1.50 |          18.75 |          30.00 |
| (default fallback)      |  3.00 |  15.00 |       0.30 |           3.75 |           6.00 |

## Non-Anthropic models

These entries use OpenRouter as the pricing source of truth. Cache columns are
all 0 (prompt caching is Claude-specific and not charged for by OpenRouter).
The `gemma4` entry is a prefix fallback that covers Ollama local variants
(`gemma4-26b-32k`, `gemma4-26b-48k`, `gemma4:e4b`, etc.) at the Gemma 4 26B A4B
OpenRouter rate — a reasonable estimate for mixed-environment JSONL files.

Source: [OpenRouter pricing](https://openrouter.ai/pricing) — snapshot 2026-04-25.

| Model ID (prefix match)      | Input | Output | Cache read | 5m Cache write | 1h Cache write |
|------------------------------|-------|--------|------------|----------------|----------------|
| `glm-4.7`                    |  0.38 |   1.74 |       0.00 |           0.00 |           0.00 |
| `glm-5`                      |  0.60 |   2.08 |       0.00 |           0.00 |           0.00 |
| `glm-5.1`                    |  1.05 |   3.50 |       0.00 |           0.00 |           0.00 |
| `google/gemma-4-26b-a4b`     |  0.06 |   0.33 |       0.00 |           0.00 |           0.00 |
| `gemma4`                     |  0.06 |   0.33 |       0.00 |           0.00 |           0.00 |
| `qwen3.5:9b`                 |  0.10 |   0.15 |       0.00 |           0.00 |           0.00 |

> **GLM ordering note**: `glm-5.1` and `glm-5` are exact-match lookups (not
> prefix), so they will never cross-match. Order within `_PRICING` does not
> affect them.

## Notes

- **Prefix fallback order matters**: dict insertion order is traversed until
  the first match. More-specific entries (e.g. `claude-opus-4-7`) must appear
  **before** less-specific ones (e.g. `claude-opus-4`), otherwise an unknown
  future Opus-4.7-* model ID would fall through to the old-tier rate.
- **5m vs 1h cache writes** (v1.2.0+): `_cost` splits
  `cache_creation_input_tokens` into its two ephemeral buckets using
  `message.usage.cache_creation.ephemeral_{5m,1h}_input_tokens` and charges
  each at the correct rate. Turns without the nested object (legacy
  transcripts) fall back to the 5-minute rate, preserving their prior cost.
- **Fast mode** (Opus 4.6 research preview): 6× standard base rates
  ($30 input / $150 output). Not currently applied by `_cost` even when
  `usage.speed == "fast"`. Cost display for fast-mode turns is therefore
  underestimated by a factor of 6 — flagged as a known limitation.
- **Data residency multiplier**: US-only inference via `inference_geo`
  adds 1.1× on top of all rates. Not tracked.
- Prices are estimates; actual billing is on Anthropic's platform.
