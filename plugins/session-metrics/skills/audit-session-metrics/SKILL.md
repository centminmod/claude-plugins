---
name: audit-session-metrics
model: haiku
description: >
  Audit a session-metrics JSON export for token-usage waste and produce a
  plain-English findings report. Trigger when the user runs
  /audit-session-metrics, when session-metrics suggests an audit after an
  HTML export, or when the user asks to audit / review / find waste in a
  saved session-metrics JSON. Two modes: "quick" (ratios + cache health +
  top expensive turns) and "detailed" (adds CLAUDE.md / settings /
  re-read scan). Args: $ARGUMENTS[0] = quick|detailed,
  $ARGUMENTS[1] = path to a session-metrics JSON export.
---

# Audit Session-Metrics

Reads a session-metrics JSON export and produces a prioritised, plain-English
audit of token-usage waste. Pinned to Haiku in frontmatter so it runs ~10×
cheaper than Sonnet for a turn that is mostly summarisation work.

## Dispatch — how to route this invocation

**First positional argument received:** `$ARGUMENTS[0]`
**Second positional argument received:** `$ARGUMENTS[1]`

Read `$ARGUMENTS[0]` and match by **literal equality**:

| `$ARGUMENTS[0]` | Route                    | Then read |
|-----------------|--------------------------|-----------|
| `quick`         | Quick audit              | [`references/quick-audit.md`](references/quick-audit.md) |
| `detailed`      | Detailed audit           | [`references/detailed-audit.md`](references/detailed-audit.md) |
| *(empty / other)* | Print usage and stop   | this file's "Usage" block below |

`$ARGUMENTS[1]` must be the path to a JSON export written by
session-metrics (e.g. `exports/session-metrics/session_<id8>_<ts>.json`).
If it is missing, empty, or the file does not exist, print:

> Usage: /audit-session-metrics {quick|detailed} <path-to-session-metrics.json>
>
> Run `/session-metrics --output json` first to produce the export, or
> point at an existing `exports/session-metrics/session_*.json`.

…and stop without further work.

## Steps

1. Read the JSON export at `$ARGUMENTS[1]` (single Read call). It already
   carries every metric you need: per-turn token records, by-skill
   aggregations, cache breaks, content-block counts, model rates,
   self-cost. **Do not** read the raw JSONL — that's what would balloon
   the audit's own context.
2. Read the matching reference file (`quick-audit.md` for `quick`,
   `detailed-audit.md` for `detailed`). Follow the playbook there
   step-by-step. Do not improvise additional phases.
3. For `detailed` mode the playbook also asks you to read the user's
   config files (`~/.claude/CLAUDE.md`, `./CLAUDE.md`, `~/.claude/settings.json`,
   `./.claude/settings.json`, `./.claudeignore`). Each is capped at
   ≤500 lines — if a file is bigger, that itself is a finding.
4. **Output contract — three artefacts.** Both playbooks specify the
   same three-artefact contract:
   - **JSON sidecar** at `<project>/exports/session-metrics/audit_<id8>_<ts>_<mode>.json` — structured findings (versioned schema, enum'd metrics).
   - **Markdown copy** at `<project>/exports/session-metrics/audit_<id8>_<ts>_<mode>.md` — same content rendered for humans.
   - **Inline chat output** — the markdown content printed in your reply.

   `<id8>` and `<ts>` are extracted from the input JSON's filename
   (pattern: `session_<id8>_<YYYYMMDD>T<HHMMSS>Z.json` or the legacy
   `session_<id8>_<YYYYMMDD_HHMMSS>.json`). Fall back to the input
   file's stem if it matches neither.
5. **Write order.** Populate the JSON object first, write the JSON
   sidecar, render the markdown using the template in the playbook,
   write the markdown copy, then print the markdown inline (without
   the H1 heading — the chat client already shows context above the
   audit). Finish with two stderr-style lines on their own:
   `[audit] saved → <json-path>`
   `[audit] saved → <md-path>`

## Tone

- **Direct and specific.** Cite the exact ratio, dollar figure, or turn
  index. No motivational language, no LLM-theory padding.
- **Prioritise by impact.** Sort findings so the costliest fix is first.
- **Quote sparingly.** Snippets capped at 5 lines each.
- **Honour the playbook.** If quick-audit.md asks for 5 rows, produce 5
  rows — don't invent a 6th to look thorough.

## Why this is a separate skill

The cost saving is real: Haiku is roughly 10× cheaper than Sonnet for the
summarisation-heavy work an audit does. The frontmatter `model: haiku`
override only takes effect when the skill is the **entry point of its own
turn**, which is why session-metrics suggests `/audit-session-metrics`
rather than invoking it programmatically — running it as a fresh slash
command keeps the model swap intact.

## Reference files

- [`references/quick-audit.md`](references/quick-audit.md) — Distilled
  ratios + cache health + top expensive turns. Read when
  `$ARGUMENTS[0]` is `quick`.
- [`references/detailed-audit.md`](references/detailed-audit.md) — Quick
  audit findings plus config + re-read scans. Read when
  `$ARGUMENTS[0]` is `detailed`.
