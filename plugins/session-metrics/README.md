# session-metrics

**A Claude Code plugin that measures how much your sessions cost, where
your tokens go, and — if you want — whether switching between Opus 4.6
and 4.7 would change your bill.**

- Reads the conversation logs Claude Code already writes to your disk.
- No API key. No network calls. No data leaves your machine.
- Works on any plan — Pro, Max, Team, Enterprise.

---

## What questions does this plugin answer?

1. **"How much did this session cost?"** — per-turn breakdown + totals,
   cache savings, burn rate, hour-of-day usage patterns.
2. **"Am I tracking toward the weekly session cap?"** — 5-hour session
   blocks, trailing 7-day counters.
3. **"Should I switch Opus versions? Is 4.7 really more expensive on my
   content?"** — automated side-by-side benchmark against a canonical
   10-prompt suite, no manual capture needed.

---

## Install

Inside the Claude Code terminal CLI (`claude` in your shell):

```
/plugin marketplace add centminmod/claude-plugins
/plugin install session-metrics@centminmod
```

Restart Claude Code. You're done.

The skill auto-triggers whenever you ask in natural language — *"how
much has this session cost?"*, *"show me token usage"*, *"is my
cache hit rate good?"* — so there's nothing new to remember for the
common case.

---

## Your first session report

In any project where you've been using Claude Code, just ask:

> how much has this session cost so far?

Claude will run the skill and print a per-turn table, a summary strip,
and a dashboard. No flags, no options needed.

---

## The three most-used commands

These are what almost everyone ends up wanting: **a shareable HTML
report for the current session**, **one for the whole project**, or
**a dashboard across every project on your machine**. Session and
project reports land in `exports/session-metrics/` under your project
root; the all-projects dashboard lands in a dated subfolder under
`exports/session-metrics/instance/`.

### 1 — Export the current session to HTML

**Natural-language ask (easiest):**

> export this session's metrics to HTML

**Explicit invocation (from inside Claude Code):**

```
/session-metrics --output html
```

**Direct shell (for scripts or CI):**

```bash
uv run python ~/.claude/plugins/cache/centminmod/session-metrics/<version>/skills/session-metrics/scripts/session-metrics.py --output html
```

You get a 2-page split by default — a high-level dashboard + a detail
page — named like `session_<id8>_<YYYYMMDD_HHMMSS>_dashboard.html` +
`_detail.html`. Pass `--single-page` if you want one combined file:

```
/session-metrics --output html --single-page
```

### 2 — Export the whole project (all sessions) to HTML

**Natural-language ask:**

> export this entire project's session metrics to HTML

**Explicit invocation:**

```
/session-metrics --project-cost --output html
```

**Direct shell:**

```bash
uv run python ~/.claude/plugins/cache/centminmod/session-metrics/<version>/skills/session-metrics/scripts/session-metrics.py --project-cost --output html
```

This one rolls up **every** session in the current project — per-turn
timeline, per-session subtotals, grand project total, 5-hour session
blocks, weekly roll-up, hour-of-day punchcard. Output file:
`project_<YYYYMMDD_HHMMSS>_dashboard.html` + `_detail.html` (or a
single file with `--single-page`).

### 3 — Instance dashboard (every project on your machine)

**Natural-language ask:**

> how much have I spent on Claude Code across all my projects?

**Explicit invocation:**

```
/session-metrics all-projects
```

**Direct shell:**

```bash
uv run python ~/.claude/plugins/cache/centminmod/session-metrics/<version>/skills/session-metrics/scripts/session-metrics.py --all-projects --output html
```

Aggregates every project under `~/.claude/projects/` into one dashboard — total
cost-to-date, per-project breakdown, and daily timeline. Output lands in
`exports/session-metrics/instance/YYYY-MM-DD-HHMMSS/` (dated subfolder so successive
runs don't overwrite each other): `index.html` hyperlinks each project row to a
pre-rendered per-project HTML drilldown. Add `--no-project-drilldown` for a flat
index in seconds.

### Useful HTML-specific flags (scopes 1 and 2)

| Flag | Purpose |
|------|---------|
| `--single-page` | Emit one combined file instead of the dashboard + detail split. |
| `--chart-lib highcharts\|uplot\|chartjs\|none` | Pick the chart renderer. `highcharts` is the default (richest, non-commercial licence). `uplot` / `chartjs` are MIT. `none` skips JS entirely. |
| `--peak-hours 5-11 --peak-tz America/Los_Angeles` | Shade the hour-of-day chart with a peak-usage band. Community-reported, not an Anthropic SLA. |
| `--tz Australia/Brisbane` | Render timestamps in a specific IANA timezone. Auto-detects your system tz by default. |

Combine them freely:

```
/session-metrics --project-cost --output html --single-page --chart-lib uplot
```

### Other export formats

`--output` takes any combo of `text` (stdout), `json`, `csv`, `md`,
`html`. Export to several at once:

```
/session-metrics --output json csv md html
```

---

## Your first instance dashboard (every project on your machine)

**New in v1.14.0.** The plugin also aggregates every project under
`~/.claude/projects/` into a single **instance dashboard** — total
cost-to-date across everything Claude Code has touched on this
machine, with drill-downs into each individual project.

**Natural-language ask:**

> how much have I spent on Claude Code across all my projects?

**Explicit invocation:**

```
/session-metrics all-projects
```

**Direct shell:**

```bash
uv run python ~/.claude/plugins/cache/centminmod/session-metrics/<version>/skills/session-metrics/scripts/session-metrics.py --all-projects --output html md csv json
```

You get a dated subfolder under `exports/session-metrics/instance/YYYY-MM-DD-HHMMSS/`
containing:

- `index.html` — the instance dashboard (summary cards, daily cost
  timeline chart stacked by tokens, projects breakdown table sorted
  by cost descending, models table, weekly/hour-of-day rollups).
- `projects/<slug>.html` — full per-project HTML drilldowns,
  hyperlinked from each row of the breakdown table. Click any
  project to open its per-turn report.
- `index.md` / `index.csv` / `index.json` — same data in portable formats.

### Useful instance-mode flags

| Flag | Purpose |
|------|---------|
| `--no-project-drilldown` | Skip the per-project HTMLs and render only `index.html`. Completes in seconds. Useful in CI or for quick-glance runs. |
| `--projects-dir /path/to/projects` | Point at a non-default projects directory — e.g. a second Claude Code install under `CLAUDE_CONFIG_DIR=/opt/claude-work`. Also honours the `CLAUDE_PROJECTS_DIR` env var. |
| `--chart-lib uplot\|chartjs\|none` | Same chart-library choice as session/project mode. |

The folder is fully portable: zip it, move it, serve it as static
files — the hyperlinks between `index.html` and `projects/<slug>.html`
are relative paths and stay working anywhere.

---

## Your first model comparison

The scenario: Anthropic shipped Opus 4.7. Community reports say it
uses 20–30% more tokens on real Claude-Code-shaped content because
of a tokenizer change, even though the per-token price is identical
([source article](https://www.claudecodecamp.com/p/i-measured-claude-4-7-s-new-tokenizer-here-s-what-it-costs-you)).
The plugin turns that one-off study into a reproducible test against
**your own workload shape**.

### The one-command version

```
/session-metrics compare-run
```

That's it. No arguments needed. The plugin will:

1. Create a throwaway scratch directory.
2. Show you: *"about to run N × 2 headless Claude Code calls against
   your subscription quota — proceed?"* (N = 10 built-in prompts plus
   any you've added to `~/.session-metrics/prompts/`) and wait for `y`.
3. Spawn two [headless](https://code.claude.com/docs/en/headless)
   `claude -p` sessions — one for Opus 4.6, one for Opus 4.7. Each
   runs the same canonical prompts in the same order. Takes about
   10–20 minutes total, unattended.
4. Render a single HTML report: per-turn token ratios, cost delta,
   a quality-vs-cost card, a "should I switch?" recommendation.

### What pair does it actually run?

By default, the **1M-context tier** of both models — the same one
Claude Code routes you to when you type `/model claude-opus-4-6` or
`claude-opus-4-7` in an interactive session. Specifically:

- Side A: `claude-opus-4-6[1m]`
- Side B: `claude-opus-4-7[1m]`

This is deliberate — it matches real-world usage. Comparing the 200k
variants is a valid test too, but it's not what Claude Code ships by
default, so it would be a lab result, not a shipping-Opus result.

### Picking a different pair

Your Claude subscription typically exposes four Opus 4 model IDs:

| ID | What it is |
|----|------------|
| `claude-opus-4-6[1m]` | 4.6 at 1M-context tier **← default** |
| `claude-opus-4-7[1m]` | 4.7 at 1M-context tier **← default** |
| `claude-opus-4-6`     | 4.6 at 200k-context tier |
| `claude-opus-4-7`     | 4.7 at 200k-context tier |

Any two of those four can be compared. Useful pairs:

| Command | What you'd learn |
|---------|------------------|
| `/session-metrics compare-run` *(default)* | Realistic "should I stay on 4.6 or switch to 4.7 in my normal Claude Code workflow?" |
| `/session-metrics compare-run claude-opus-4-6 claude-opus-4-7` | Tokenizer delta at the 200k tier (the tier the referenced article studied). |
| `/session-metrics compare-run claude-opus-4-6 claude-opus-4-6[1m]` | Pure context-tier delta — *"what does enabling the 1M window actually cost me on 4.6?"* |
| `/session-metrics compare-run claude-opus-4-7 claude-opus-4-7[1m]` | Same, for 4.7. |

Mixing tiers across sides (e.g. `4-6` vs `4-7[1m]`) is allowed but
the report will flag it with a `context-tier-mismatch` advisory —
the resulting ratio conflates tokenizer shift with tier shift, so
don't read it as a clean tokenizer-only number.

### What you get at the end

A single HTML file with:

- **Summary strip.** Input, output, total, and cost ratios (B vs A).
- **IFEval quality card.** Pass-rate delta — does the more-expensive
  side produce more accurate outputs? Built-in prompts show pass/fail;
  custom prompts you've added show ratio data only (no predicate).
- **Per-turn table.** One row per prompt, A vs B tokens, heatmap tint
  showing where the ratio is worst.
- **Input-token-ratio histogram.** Distribution across the suite.
- **Reproducibility stamp.** Suite version, timestamps, model IDs —
  anyone can re-run and compare.

---

## What if I can't use `compare-run`?

If `claude` isn't on your PATH (CI container, stripped Docker image)
or your plan gates headless differently, there's a manual fallback:

```
/session-metrics compare-prep
```

This prints a 5-step protocol and the 10 prompts you'd paste into two
fresh Claude Code sessions yourself. See the full walkthrough in
[`skills/session-metrics/references/model-compare.md`](skills/session-metrics/references/model-compare.md).

---

## Other things you can ask for

Beyond the two most-used exports above, the skill also surfaces these
through natural language — no slash commands to memorise:

| Ask Claude this… | You get… |
|------------------|----------|
| *"show me my weekly usage"* | Trailing 7 days vs prior 7 days, with percentage deltas on cost, turns, prompts, and cache-hit ratio. |
| *"when do I use Claude most?"* | Hour-of-day + weekday × hour punchcard in your local timezone. |
| *"am I tracking toward the weekly session cap?"* | 5-hour session blocks, trailing 7/14/30-day counters. |
| *"show me cache hit rate"* | Cache-read share, estimated savings vs a hypothetical no-cache run. |
| *"list all sessions in this project"* | Session-ID + user-turn-count + timestamp per session, newest first. |
| *"export this session safely for sharing"* | JSON export with prompts redacted, self-cost suppressed, and files chmod'd to 0600 (`--export-share-safe`). |

The full flag reference for power users is in
[`skills/session-metrics/SKILL.md`](skills/session-metrics/SKILL.md).

---

## Privacy

The skill reads only files under `~/.claude/projects/` on your machine.
No network requests at any point for the default paths. The single
optional mode that calls out to Anthropic — `--count-tokens-only` —
requires an `ANTHROPIC_API_KEY` environment variable (separate from
subscription auth) and prints a confirmation gate first. It's there
for API-billed users who want a cheap tokenizer smoke test, not for
subscription users (who should use `--compare-run`).

**Sharing exports safely.** Pass `--export-share-safe` when handing a
JSON export to a colleague — it redacts freeform prompt and response text,
suppresses the self-cost meta-metric, and chmods every written file to
`0600`. The underlying flags are also available individually:
`--redact-user-prompts` (JSON exports only) and `--no-self-cost`.

A parse cache lives at `~/.cache/session-metrics/parse/`. The cache is
self-managed: stale blobs are pruned on write and in a daily background
sweep. Pass `--no-cache` to skip the cache entirely.

---

## Custom prompts (`v1.10.0+`)

Add your own prompts to the comparison suite — no file format knowledge required:

```
/session-metrics compare-add-prompt "Write a haiku about Python programming."
```

The prompt is saved to `~/.session-metrics/prompts/` and runs automatically on every future `--compare-run` alongside the built-in 10. You can also drop any plain `.md` file into that directory and it will be picked up automatically.

**Manage your suite:**

| Command | What it does |
|---------|-------------|
| `compare-add-prompt "text"` | Add a new user prompt (plain text, no YAML needed) |
| `compare-remove-prompt NAME` | Remove a prompt by name |
| `compare-list-prompts` | Preview the full active suite + call count before running |

Custom prompts capture cost and token ratios just like the built-in 10. They show "no predicate" in the pass/fail column — you get the ratio data without needing to write a compliance check.

For the step-by-step guide (including advanced predicates and replacing the entire suite): [`skills/session-metrics/references/custom-prompts.md`](skills/session-metrics/references/custom-prompts.md).

---

## Cost of running `--compare-run`

One full run = **2 models × N prompts** headless inference calls
against your Claude subscription quota, where N = 10 built-in prompts
(plus any you've added — see below). On Pro and Max this is a minor hit
— the prompts are short-to-medium and finish in under 20 minutes wall-clock. On Team/Enterprise it's
negligible. The plugin's confirmation gate is explicit about this
before any work begins; pass `--yes` to bypass when scripting.

If you only want the *tokenizer* delta (no inference, no output
measurement) and you have an `ANTHROPIC_API_KEY`, use
`--count-tokens-only` instead — 20 free metadata-only calls.

---

## Learn more

- **Full skill reference:** [`skills/session-metrics/SKILL.md`](skills/session-metrics/SKILL.md)
- **Compare deep-dive:** [`skills/session-metrics/references/model-compare.md`](skills/session-metrics/references/model-compare.md)
- **Custom prompts guide:** [`skills/session-metrics/references/custom-prompts.md`](skills/session-metrics/references/custom-prompts.md)
- **Source of truth + changelog:** [`centminmod/session-metrics`](https://github.com/centminmod/session-metrics) (the dev repo this plugin is built from)
- **Background articles:** [*"I built a token-cost analyzer skill for Claude Code"*](https://ai.georgeliu.com/p/i-built-a-token-cost-analyzer-skill) · [*"I ran two Claude Opus 4.7 5-hour sessions"*](https://ai.georgeliu.com/p/i-ran-two-claude-opus-47-5hr-sessions)
