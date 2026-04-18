# Claude Code JSONL Log Schema

Location: `~/.claude/projects/<slug>/<session-uuid>.jsonl`

Each line is a self-contained JSON object (newline-delimited JSON / NDJSON).

This document serves two audiences:

1. **Maintainers debugging the parser** → the structural reference
   (entry types, shapes, dedup rules, subagent behaviour).
2. **Anyone deciding what data to surface in reports** → the **field
   catalogue** with a *Surfaced in reports?* column and the
   **Expansion-opportunity summary** at the bottom that lists the
   shortest path from "field is present in the JSONL" to "field is
   visible in HTML/MD/JSON/CSV output".

The catalogue below was built from empirical inspection of real
sessions in `~/.claude/projects/`; if you spot a field shape the doc
doesn't mention, append to it.

---

## Entry type index

| Type              | Purpose                                                           | Carries token usage?     |
|-------------------|-------------------------------------------------------------------|--------------------------|
| `assistant`       | Claude's API response — text, tool calls, thinking blocks         | **Yes** (`message.usage`)|
| `user`            | Human prompt **or** auto-generated `tool_result` payload          | No                       |
| `attachment`      | Wraps hook outputs, pasted files, and tool-use side payloads      | No                       |
| `queue-operation` | Prompt queue lifecycle (`enqueue`, …)                             | No                       |
| `last-prompt`     | Restore hint — last user prompt in the session                    | No                       |
| `system`          | Claude Code system events (e.g. Stop-hook summaries)              | No                       |
| `summary`         | Context-compression checkpoints (rare, not always present)        | No                       |

The parser (`scripts/session-metrics.py`) reads token data only from
`assistant` entries. Everything else is metadata today.

---

## Assistant entry — the cost-bearing shape

```json
{
  "type": "assistant",
  "uuid": "7e538ffb-…",
  "parentUuid": "49422fd5-…",
  "isSidechain": false,
  "timestamp": "2026-04-15T02:32:32.185Z",
  "sessionId": "60fb0cc8-…",
  "cwd": "/home/user/projects/myapp",
  "version": "2.1.111",
  "gitBranch": "master",
  "entrypoint": "claude-desktop",
  "userType": "external",
  "slug": "-home-user-projects-myapp",
  "requestId": "req_011Ca4moqagPBTkv4htSbuMU",
  "message": {
    "id": "msg_01GvBhABmRVqm3qv4G6innqL",
    "model": "claude-sonnet-4-6",
    "role": "assistant",
    "type": "message",
    "stop_reason": "tool_use",
    "stop_details": null,
    "stop_sequence": null,
    "content": [ /* thinking / tool_use / text blocks */ ],
    "usage": {
      "input_tokens": 10,
      "output_tokens": 208,
      "cache_read_input_tokens": 27839,
      "cache_creation_input_tokens": 468,
      "cache_creation": {
        "ephemeral_1h_input_tokens": 468,
        "ephemeral_5m_input_tokens": 0
      },
      "server_tool_use": {
        "web_search_requests": 0,
        "web_fetch_requests": 0
      },
      "service_tier": "standard",
      "speed": "standard",
      "inference_geo": "",
      "iterations": [ /* per-iteration breakdown */ ]
    }
  }
}
```

### Top-level fields (assistant entry)

| Field             | Description                                        | Surfaced in reports?                                |
|-------------------|----------------------------------------------------|-----------------------------------------------------|
| `type`            | Always `"assistant"` here.                         | filter                                              |
| `uuid`            | Per-entry UUID.                                    | N/A                                                 |
| `parentUuid`      | Threading parent.                                  | N/A                                                 |
| `sessionId`       | Session ID (matches filename).                     | **tracked** (session grouping)                      |
| `timestamp`       | ISO-8601 UTC.                                      | **tracked** (timeline, re-rendered in user tz)      |
| `cwd`             | Working directory when the turn ran.               | **tracked** (slug derivation)                       |
| `version`         | Claude Code version.                               | available-not-shown                                 |
| `gitBranch`       | Git branch at turn time.                           | available-not-shown — useful for cost-by-branch     |
| `entrypoint`      | `claude-desktop`, `claude-code`, …                 | available-not-shown                                 |
| `userType`        | `external` / `internal`.                           | N/A                                                 |
| `isSidechain`     | `true` for subagent turns.                         | **tracked** (default filter; `--include-subagents` flips it) |
| `slug`            | Project slug string.                               | redundant (derivable from `cwd`)                    |
| `requestId`       | Anthropic API request ID.                          | available-not-shown — useful for API-log x-ref      |
| `message`         | The payload (see below).                           | **tracked** (all cost data is here)                 |

### `message` fields (assistant role)

| Field           | Description                                              | Surfaced in reports?                               |
|-----------------|----------------------------------------------------------|----------------------------------------------------|
| `id`            | `msg_…` Anthropic message ID.                            | **tracked** (dedup key — see below)                |
| `model`         | Pricing-lookup key.                                      | **tracked** (Model column)                         |
| `role`          | Always `"assistant"`.                                    | filter                                             |
| `type`          | Always `"message"`.                                      | N/A                                                |
| `stop_reason`   | `end_turn`, `tool_use`, `max_tokens`, `stop_sequence`.   | available-not-shown — flag truncated responses     |
| `stop_details`  | Sub-object when the stop reason has nuance. Often null.  | N/A                                                |
| `stop_sequence` | Matched stop-sequence string, if any.                    | N/A                                                |
| `content`       | Array of content blocks — see **Content blocks** below.  | partially surfaced (Model column shows some info; see Proposal B) |
| `usage`         | Token usage dictionary — see next table.                 | **tracked**                                        |

### `message.usage` — billable vs. metadata field dictionary

| Field                                         | Billable?                          | Description                                                                                          | Surfaced in reports?                                              |
|-----------------------------------------------|------------------------------------|------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| `input_tokens`                                | **Yes** — input rate               | Net new input tokens (excludes cached).                                                              | **tracked** (Input column, cost)                                  |
| `output_tokens`                               | **Yes** — output rate              | All output. **Includes thinking-block tokens** and tool_use serialised args — these roll up here.    | **tracked** (Output column, cost)                                 |
| `cache_read_input_tokens`                     | **Yes** — cache_read rate (0.1× input) | Tokens served from prompt cache.                                                                 | **tracked** (CacheRd column, cost)                                |
| `cache_creation_input_tokens`                 | **Yes** — cache_write rate         | Tokens written into the cache. **Sum** of the 5m and 1h ephemeral buckets.                           | **tracked** (CacheWr column). Currently all priced at 5m rate — see Proposal A. |
| `cache_creation.ephemeral_5m_input_tokens`    | **Yes** — 1.25× input (5m rate)    | Portion of the cache write landing in the 5-minute TTL tier.                                         | **available-not-shown** — Proposal A                              |
| `cache_creation.ephemeral_1h_input_tokens`    | **Yes** — 2× input (1h rate)       | Portion of the cache write landing in the 1-hour TTL tier. **Currently under-costed.**               | **available-not-shown** — Proposal A                              |
| `server_tool_use.web_search_requests`         | **Yes** — per-request charge       | Count of web-search requests Claude made server-side this turn.                                      | **available-not-shown** — see Adjacent                            |
| `server_tool_use.web_fetch_requests`          | **Yes** — per-request charge       | Count of web-fetch requests Claude made server-side this turn.                                       | **available-not-shown** — see Adjacent                            |
| `service_tier`                                | Metadata                           | `"standard"` observed; priority tier is a possible other value.                                      | N/A                                                               |
| `speed`                                       | Metadata (drives multiplier)       | `"standard"` or `"fast"` (Claude Code `/fast` mode).                                                 | **tracked** (Mode column). 6× fast-mode multiplier is **not** applied in cost math — known limitation, see `references/pricing.md`. |
| `inference_geo`                               | Multiplier (1.0× or 1.1×)          | Empty string in observed data. Anthropic documents US-only inference at 1.1× (data-residency surcharge). | available-not-shown                                               |
| `iterations`                                  | Metadata                           | Array of per-iteration usage for turns that stream across multiple passes. Length 1 in all sampled turns. | N/A                                                               |

**Derived per-turn values the parser computes** (not fields in the JSONL):
`total_tokens` (sum of the four billable token buckets) and `cost_usd`
(per `_cost()` in `scripts/session-metrics.py:92`).

---

## Content blocks (`message.content[]`)

Each element of `content` is an object with a `type`. Empirical counts
across two sampled sessions: `thinking` × 47, `tool_use` × 105,
`tool_result` × 105, `text` × 24, `image` × 1.

### `thinking` (assistant-message block)

Anthropic extended-thinking block.

- **Billing.** Thinking tokens are **rolled into `output_tokens`** and
  billed at the output rate. There is **no** separate
  `thinking_tokens` field on `usage`.
- **Storage in Claude Code JSONL.** The `thinking` string is stored
  **empty** and only a `signature` is retained (signature-only block).
  Per-turn thinking-token counts are **not recoverable** from the
  transcript alone.
- **What *is* possible:** counting the number of `thinking` blocks
  per turn and per session — see Proposal B.

Observed shape:

```json
{"type": "thinking", "signature": "<opaque>", "thinking": ""}
```

### `tool_use` (assistant-message block)

A tool call the model is requesting. The block carries the tool's
`name`, serialised `input` (arguments), and its own `id`. Tokens for
the block are inside `output_tokens`; the block count is an
independent behavioural signal.

Tool names observed in sampled sessions include `Read`, `Bash`,
`Edit`, `Write`, `Glob`, `Grep`, `Agent`, `TodoWrite`, `WebSearch`,
`ExitPlanMode`, `AskUserQuestion`, `ToolSearch`.

### `text` (assistant-message block)

Plain prose output from Claude. Tokens counted in `output_tokens`.

### `tool_result` (user-entry block)

The tool's response, written to the JSONL as a `user`-type entry
immediately after the assistant's `tool_use`. **Must be filtered out
when counting user-prompt activity** — otherwise user-activity
metrics inflate 10-20× on tool-heavy sessions. Implementation:
`_is_user_prompt` in `scripts/session-metrics.py`.

### `image` (user-entry block)

Pasted / attached image. Rare in shell-bound sessions.

### `text` (user-entry block)

The user's typed prompt. Also observed as a **plain string** (see
**User entry** below) rather than a structured block.

---

## User entry

```json
{
  "type": "user",
  "uuid": "…",
  "parentUuid": "…",
  "timestamp": "2026-04-15T02:32:30.000Z",
  "sessionId": "…",
  "message": {
    "role": "user",
    "content": [ /* blocks */ ]   // OR a plain string — both shapes observed
  }
}
```

`message.content` has **two** observed shapes:

1. **List of content blocks** — blocks of `type`: `text`, `image`,
   `tool_result`.
2. **Plain string** (~10% of entries) — a direct user prompt with no
   structured wrapper.

**Filter rule for user-activity metrics.** A genuine user prompt is a
user entry whose `message.content` is either a non-empty string **or**
a list containing at least one `text`/`image` block. Pure
`tool_result`-only lists must be excluded — see `_is_user_prompt` in
`scripts/session-metrics.py`.

Top-level fields unique to user entries (beyond the assistant-entry
set): `isMeta`, `permissionMode` (e.g. `"plan"`), `promptId`,
`toolUseResult`, `sourceToolAssistantUUID`.

---

## Specialty entry types

### `attachment`

Wraps hook outputs, pasted files, and tool-use side payloads. The
top-level `attachment` sub-object carries `type` (e.g. `hook_success`),
`hookName`, `toolUseID`, and the rich payload inline. Not cost-bearing.
Relevant if you ever want to surface hook-firing counts — currently
ignored by the parser.

### `queue-operation`

Tiny prompt-queue lifecycle events: `type`, `operation` (e.g.
`enqueue`), `sessionId`, `timestamp`, `content` (the queued prompt
text). Not cost-bearing.

### `last-prompt`

Session restore hint. Fields: `type`, `sessionId`, `lastPrompt`. Not
cost-bearing.

### `system`

Claude Code system events. Most common subtype observed is
`stop_hook_summary`, with fields: `subtype`, `hookCount`, `hookInfos[]`,
`hookErrors[]`, `preventedContinuation`, `level`, `toolUseID`,
`stopReason`, `hasOutput`. Useful if you ever want to report hook
failure rate or prevented-continuation events. Currently ignored by
the parser.

### `summary`

Context-compression checkpoints — `type`, `summary`, `leafUuid`. Not
observed in every session. Not cost-bearing.

---

## Deduplication behaviour

Claude Code writes the same `message.id` to the JSONL at multiple
lifecycle points (start of stream, after each tool result, after
final `stop_reason`). Token counts in earlier writes may be partial
or zero.

**Always keep the LAST occurrence** of each `message.id` — it reflects
the final settled usage values.

---

## Subagent logs

Spawned agents write to `<session-uuid>/subagents/agent-<hex>.jsonl`.
The main script ignores subagent files by default; pass
`--include-subagents` to fold them in (adds a `[subagent]` marker in
the turn index).

---

## Expansion-opportunity summary

Shortlist of untracked-but-available fields, ordered highest-ROI
first. Each row is a candidate for a future report-expansion plan.

| Field / signal                                                    | If surfaced, the report gains…                                                                                                              |
|-------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| `cache_creation.ephemeral_{5m,1h}_input_tokens`                   | **Proposal A.** Fixes 1h-tier cost under-count (up to 60% on cache writes) + new "Cache TTL mix" dashboard card.                           |
| `message.content[].type` counts (thinking / tool_use / text / tool_result / image) | **Proposal B.** Per-turn "Content" column + "Extended thinking engagement" and "Tool calls" dashboard cards. No cost-math change. |
| `server_tool_use.{web_search,web_fetch}_requests`                 | Separate per-request billing currently not applied; sessions that touch server-side web tools silently under-report cost. See **Adjacent**. |
| `usage.speed == "fast"` cost multiplier                           | Fast-mode turns under-costed 6×. Already in `references/pricing.md` as a known limitation.                                                  |
| `usage.inference_geo`                                             | 1.1× multiplier for US-only inference. Untracked; no non-empty values observed yet.                                                         |
| `message.stop_reason`, `message.stop_details`                     | Flag truncated-response turns (`max_tokens`), surface non-standard stops in a "Notes" column.                                               |
| `message.content[].tool_use.name`                                 | Top-N called tools in the dashboard. Cheap to extract.                                                                                      |
| `gitBranch`                                                       | Cost-by-branch aggregation — useful for feature-cost accounting.                                                                            |
| `version`                                                         | Cost-by-Claude-Code-version trend; minor value.                                                                                             |
| `system.stop_hook_summary` fields (`hookErrors`, `preventedContinuation`) | Hook-failure rate / prevention rate as a session-health signal.                                                                  |

---

## Proposal A — Ephemeral cache TTL drilldown

**Status:** **Implemented in v1.2.0.** Cost math, per-turn records,
CSV/JSON exports, the Markdown legend + annotation, the HTML TTL
badge + "Cache TTL mix" dashboard card, and the new column legend
all ship in this release. The sections below are retained as
historical design context.

**Fields.** `cache_creation.ephemeral_1h_input_tokens` and
`cache_creation.ephemeral_5m_input_tokens` (both nested inside
`message.usage.cache_creation`).

**Why it matters.** Anthropic bills the two TTL tiers differently:
5-minute cache writes cost **1.25× base input**, 1-hour writes cost
**2× base input**. The skill's pricing table today stores only one
`cache_write` rate per model (the 5-minute rate — see
[`references/pricing.md`](pricing.md) lines 51-53). Turns that pay
the 1-hour premium are **under-costed by up to 60%** on the
cache-write component. This drilldown turns the existing known
limitation into a fix.

**What to surface.**

1. **Pricing accuracy fix.** Extend `_PRICING` with a `cache_write_1h`
   rate per model (2× base input). `_cost()` splits
   `cache_creation_input_tokens` into its 1h and 5m buckets using the
   `cache_creation.ephemeral_*_input_tokens` fields and charges each
   at the correct rate. Falls back to the existing 5m rate when the
   drilldown is absent (legacy / foreign transcripts).
2. **Per-turn display (HTML detail + MD).** Keep the single `CacheWr`
   column for scanability, but have the tooltip / md cell show
   `A + B (1h + 5m)`. Add a compact TTL badge — `1h` / `5m` / `mix` —
   next to the value so 1h-heavy turns are visible at a glance.
3. **CSV/JSON exports.** Two new per-turn numeric fields:
   `cache_write_5m_tokens`, `cache_write_1h_tokens`. Existing
   `cache_write_tokens` stays as the sum for backwards compatibility.
4. **HTML dashboard card — "Cache TTL mix".** Totals for the session
   (and per-session in project mode): share of cache writes that were
   1-hour vs 5-minute, and the **extra cost paid for 1h tier**
   (`1h_tokens × (1h_rate − 5m_rate) / 1_000_000`). Makes the
   trade-off explicit.
5. **Cache savings footer.** The existing "cache savings vs no-cache"
   footer gains a 1h-tier line so the 1h investment is accounted for
   distinctly.

**Script touchpoints.** `_PRICING` (lines 57-80), `_cost()` (line 92),
`_build_turn_record()` (lines 787-806), HTML table header/row
(~2697-2791), CSV header (line 1164), JSON schema, dashboard card
templates.

---

## Proposal B — Content-block distribution

**Fields.** Per-turn counts of `message.content[].type` values:
`thinking`, `tool_use`, `text` on assistant entries, and
`tool_result`, `image` on the preceding user entry.

**Why it matters.** Cost columns tell users *how expensive* a turn
was, not *what the model was doing*. Block counts cheaply distinguish:

- **Agentic turns** — high `tool_use`, few `text`.
- **Conversational turns** — `text`-dominant, no `tool_use`.
- **Extended-thinking turns** — `thinking` blocks present.
  (Signature-only storage: the block count is real but the per-turn
  thinking-token count is **not** recoverable — thinking tokens
  already flow through `output_tokens` and its cost.)
- **Multimodal turns** — `image` blocks on the paired user entry.

None of these shapes are inferable from token counts alone.

**What to surface.**

1. **Per-turn "Content" column (HTML detail + MD).** Compact letter
   encoding such as `T3 u2 x1` (3 thinking, 2 tool_use, 1 text).
   Tooltip / md footnote explains the legend. Zero counts omitted
   so short rows stay clean. Emoji variant possible if the user
   explicitly opts in.
2. **CSV/JSON exports.** Per-turn integer fields:
   `thinking_blocks`, `tool_use_blocks`, `text_blocks`, plus
   `tool_result_blocks` and `image_blocks` attributed from the
   preceding user entry.
3. **HTML dashboard cards.**
   - *Extended thinking engagement* — "N of M assistant turns
     (X%) contained thinking blocks; Y thinking blocks total." Plain
     counts, no token claim. A short tooltip explains the
     signature-only caveat so nobody over-interprets it.
   - *Tool calls* — total `tool_use` blocks, average per assistant
     turn, top-3 most-called tool names (from `tool_use.name`).
4. **Optional chart (HTML detail).** Stacked bar per turn showing
   `thinking / tool_use / text` counts — a behavioural timeline
   paired with the existing cost timeline. Opt-in via the existing
   `--chart-lib` wiring so it inherits the lib choice.
5. **Explicit non-scope note.** There will **not** be a "thinking
   tokens" column. Anthropic rolls thinking tokens into
   `output_tokens` and Claude Code stores thinking text
   signature-only. Any column purporting to report thinking tokens
   from the JSONL would be an estimate, not a measurement.

**Script touchpoints.** `_extract_turns()` (line 196) gains
content-block counting; per-turn record schema grows five integer
fields; CSV header + JSON schema + HTML templates gain matching
columns/cards.

---

## Adjacent — server-side tool billing

`server_tool_use.web_search_requests` and
`server_tool_use.web_fetch_requests` are billed **per request** by
Anthropic, outside the token rate. `_cost()` today ignores them. When
they become non-zero on real sessions, the reported cost silently
under-reports by (request_count × per-request price). Worth tracking
before the first session that uses the server-side web tools lands.
