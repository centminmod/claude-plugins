# Claude Code JSONL Log Schema

Location: `~/.claude/projects/<slug>/<session-uuid>.jsonl`

Each line is a self-contained JSON object (newline-delimited JSON / NDJSON).

---

## Entry types

### `assistant` — API response (contains usage data)

```json
{
  "type": "assistant",
  "uuid": "7e538ffb-...",
  "parentUuid": "49422fd5-...",
  "isSidechain": false,
  "timestamp": "2026-04-15T02:32:32.185Z",
  "sessionId": "60fb0cc8-286f-41b4-ad12-4bad42fd20ad",
  "cwd": "/home/user/projects/myapp",
  "version": "2.1.101",
  "gitBranch": "master",
  "entrypoint": "claude-desktop",
  "userType": "external",
  "requestId": "req_011Ca4moqagPBTkv4htSbuMU",
  "message": {
    "id": "msg_01GvBhABmRVqm3qv4G6innqL",   ← deduplicate on this
    "model": "claude-sonnet-4-6",
    "role": "assistant",
    "type": "message",
    "stop_reason": "tool_use",
    "content": [ ... ],
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
      "speed": "standard"
    }
  }
}
```

### `user` — human prompt OR tool-result turn (no usage data)

```json
{
  "type": "user",
  "uuid": "...",
  "parentUuid": "...",
  "timestamp": "2026-04-15T02:32:30.000Z",
  "sessionId": "...",
  "message": {
    "role": "user",
    "content": [ ... ]   // OR a plain string — both observed in the wild
  }
}
```

**`message.content` has two observed shapes**:

1. **List of content blocks** (majority of entries). Each block is an object with a `type` field:
   - `type: "text"` — the user's typed prompt.
   - `type: "image"` — pasted image in the prompt.
   - `type: "tool_result"` — **auto-generated** after every tool call the assistant makes. These are NOT user-typed messages but have `type: "user"` at the entry level.
2. **Plain string** (~10% of entries) — a direct user-typed prompt without structured content blocks. The original schema docs only mentioned shape 1; shape 2 is present in practice.

**Filter rule for user-activity analysis**: a genuine user prompt is an entry whose `message.content` is either a non-empty string, OR a list containing at least one block with `type in {"text", "image"}`. Pure `tool_result`-only lists must be excluded — counting them inflates "user activity" metrics by 10–20× on tool-heavy sessions.

See `_is_user_prompt` in `scripts/session-metrics.py` for the implementation.

### `summary` — context compression event

```json
{
  "type": "summary",
  "summary": "...",
  "leafUuid": "..."
}
```

---

## Deduplication behaviour

Claude Code writes the same `message.id` to the JSONL at multiple lifecycle
points (start of stream, after each tool result, after final `stop_reason`).
Token counts in earlier writes may be partial or zero.

**Always keep the LAST occurrence** of each `message.id` — it reflects the
final settled usage values.

---

## Key fields for cost calculation

| Field path | Description |
|------------|-------------|
| `message.id` | Dedup key |
| `message.model` | Model ID → pricing lookup |
| `message.usage.input_tokens` | Net new input tokens |
| `message.usage.output_tokens` | Generated output tokens |
| `message.usage.cache_read_input_tokens` | Served from cache |
| `message.usage.cache_creation_input_tokens` | Written to cache |
| `message.usage.speed` | `"standard"` (normal mode) or `"fast"` (Claude Code fast mode `/fast`) |
| `message.usage.service_tier` | `"standard"` (always observed so far) |
| `timestamp` | ISO-8601 UTC, use for timeline ordering |

---

## Subagent logs

Spawned agents write to `<session-uuid>/subagents/agent-<hex>.jsonl`.
The main script ignores subagent files by default; pass `--include-subagents`
to fold them in (adds a `[subagent]` marker in the turn index).
