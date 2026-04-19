# Model comparison (`--compare`)

Compare two Claude Code sessions — or two sets of sessions — across tokens, cost, cache behaviour, tool-call fan-out, and IFEval-style instruction compliance. Designed to answer:

- "Is the newer model really using more tokens on my content?"
- "How much of my cost delta is tokenizer-driven vs workload-driven?"
- "Does the compliance delta justify the cost delta?"

This doc is the long-form companion to the `--compare*` flags. Start at [When to use](#when-to-use), then follow one of the two workflows.

---

## When to use

You want a controlled comparison across two models on a fixed, reproducible prompt suite. Common cases:

- A new Claude model shipped; you want to know the cost impact on your specific content shape before switching.
- You already use both models in different contexts and want to attribute a spend swing.
- You need a reproducible "how much does my CLAUDE.md cost to summarise" number, not a vibes-based guess.

If you just want per-session metrics for today's work, don't use this — run `session-metrics` with no flags.

---

## Two modes

| Mode | Input | Output | Validity |
|------|-------|--------|----------|
| **Controlled** (Mode 1) | Two single-session JSONLs that both ran the canonical suite | Per-turn paired table, IFEval column, tokenizer-ratio summary | Clean attribution. Same prompts → ratio = tokenizer + output-length delta only. |
| **Observational** (Mode 2) | `all-<family>` or two project-level specifiers | Aggregate-only cards, no per-turn pairing | Drift summary. Conflates tokenizer shift with prompt-distribution shift. |

`auto` scope (default) picks Mode 1 for session pairs, Mode 2 for any `all-<family>` arg. Force with `--compare-scope session|project`.

---

## Workflow A — Controlled comparison (recommended)

### 1. Print the capture protocol + prompts

```bash
session-metrics --compare-prep > /tmp/compare-prompts.md
```

Defaults to `claude-opus-4-6` vs `claude-opus-4-7`. Override with positional model IDs:

```bash
session-metrics --compare-prep claude-opus-4-7 claude-opus-4-8
```

### 2. Capture side A

1. Start a fresh Claude Code session in an **empty scratch directory** (no large CLAUDE.md, no pre-existing memory).
2. Run `/model claude-opus-4-6` (or whichever side A).
3. Verify: run `/model` with no args — should echo the model ID.
4. Paste each of the 10 prompts from the suite in order. Let each complete before pasting the next.
5. Exit.

### 3. Capture side B

Repeat in a **new** fresh session with `/model claude-opus-4-7` (or your side B). Paste the same 10 prompts in the same order.

### 4. Generate the report

```bash
session-metrics --compare last-opus-4-6 last-opus-4-7 --output md
```

Or pass explicit JSONL paths / session UUIDs.

### Output

The report surfaces:

- **Summary strip** — input/output/total/cost ratios (B vs A), IFEval pass rate per side, pass-rate delta.
- **Per-turn table** — paired rows with A and B tokens, ratios, and a `prompt` column naming the suite prompt. The `A✓` / `B✓` columns show IFEval pass/fail.
- **Advisories banner** — flags context-tier mismatch, cache-warmth drift (>10 pp), suite-version mismatch, empty sides.

### Interpretation

- **Cost ratio ≫ 1.0 with identical pricing** → tokenizer or output-length driven. Pricing is identical between `claude-opus-4-6` and `claude-opus-4-7` at the time of writing, so the full cost delta is tokenizer. See [`references/pricing.md`](pricing.md).
- **IFEval pass rate up + cost up** → classic quality/cost trade-off. Read together.
- **Near-1.0× on CJK prose, large ratio on code / CLAUDE.md** → expected; Claude 4.7's tokenizer compresses code/prose differently than CJK.

---

## Workflow B — Observational drift summary

Use when you already have a pile of historical sessions and want a spend summary across models, even though the prompts differ.

```bash
session-metrics --compare all-opus-4-6 all-opus-4-7 --yes
```

- `--yes` skips the confirmation gate (the CLI otherwise asks before rolling up N sessions per side).
- Output has no per-turn table and no IFEval column (predicates can't pair to unknown prompts). It has aggregate ratios, per-side averages (avg input per prompt, avg output per turn, tool-calls per turn), and cache-read share.
- The banner tells you this is a drift summary, not a benchmark.

---

## Workflow C — Inference-free tokenizer check (`--count-tokens-only`)

The fastest "am I affected?" check. Hits `POST /v1/messages/count_tokens` once per prompt × model — no inference runs, no output/cost data, just the input-token delta the article is about.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
session-metrics --count-tokens-only --yes     # defaults: opus-4-6 vs opus-4-7
session-metrics --count-tokens-only \
    --compare-models claude-sonnet-4-6 claude-sonnet-4-7 --yes
```

- **Input only.** Output length and total cost are NOT measured — those columns don't exist in this mode. For a full comparison run Workflow A against two real sessions.
- **Confirmation gate.** Prints the total API-call count (`N prompts × 2 models`) and waits for `y`. Bypass with `--yes`. Non-TTY stdin without `--yes` is a hard refusal to avoid surprise rate-limit burn in scripts.
- **Probe fallback.** On startup, calls the first model with the first prompt as a probe. If that call fails (e.g. the API key no longer has access to the baseline model), the mode collapses to counting the second model only and prints a friendly explanation. Ratios are not computable from that collapsed state — the run still gives you absolute input-token counts, which is useful for "how many tokens does my prompt suite consume on model X" questions.
- **Rate limits.** count_tokens requests don't incur per-token charges but they do count against the account's request rate limit. A 10-prompt × 2-model suite is 20 calls — negligible on any real account.
- **Custom suite directory.** Pairs with `--compare-prompts DIR` to point at an alternative prompt set (same YAML-frontmatter-plus-body format as the packaged suite; predicates are ignored in this mode since no inference runs).

---

## Prompt suite (v1)

Located at `.claude/skills/session-metrics/references/model-compare/prompts/`. Each file is a Markdown document with YAML frontmatter, a prompt body, and an optional Python predicate.

Every prompt body starts with a sentinel:

```
[session-metrics:compare-suite:v1:prompt=<name>]
```

The skill detects this sentinel in user prompts to (a) identify which suite prompt a turn corresponds to, (b) run the IFEval predicate against the assistant's text output, and (c) refuse when the two compared sessions carry different suite versions.

| # | Name | Content shape | Predicate |
|--:|------|---------------|-----------|
| 1 | `claudemd_summarise` | prose-dense CLAUDE.md | exactly 120 words |
| 2 | `english_prose` | English prose | zero commas |
| 3 | `code_review` | Python diff | exactly 3 bullet items |
| 4 | `stack_trace_debug` | Python stack trace | ≤ ~200 output tokens |
| 5 | `tool_heavy_task` | agentic tool-use | *(none — ratio only)* |
| 6 | `cjk_prose` | Japanese prose | no CJK codepoints remaining |
| 7 | `json_reshape` | structured JSON | valid JSON with required shape |
| 8 | `csv_transform` | structured CSV | valid CSV, no prose preamble |
| 9 | `typescript_refactor` | TypeScript code | word "refactor" appears exactly twice |
| 10 | `instruction_stress` | stacked constraints | 50 words, no commas, "foo" ×2, lowercase |

Add your own prompts by dropping a file into the suite dir that matches the same format. Point `--compare-prompts <dir>` at an override directory to swap the suite.

---

## Methodology caveats

- **Single-run variance.** Each prompt runs once per side. One-offs can swing ±10% on tokenizer ratios. The article this feature is based on acknowledges the same limitation. Multi-trial support (`--compare-trials N`) is on the roadmap but not in this release.
- **Cache warmth.** Running B immediately after A means B's CLAUDE.md cache is in a different state than A's was on first turn. The skill emits a `cache-share-drift` advisory when the two sides' cache-read share differs by >10 pp. When you see it, read the cache column with skepticism.
- **Context-tier confound.** Claude Code's default Opus 4.7 arrives tagged `claude-opus-4-7[1m]` (1M-context tier). If side A is on the default tier and side B is `[1m]`, the `context-tier-mismatch` advisory fires — the ratio then conflates tokenizer + window tier + cache-hit-rate. Run both sides on the same tier when practical.
- **System-prompt drift.** Claude Code's system prompt evolves over time. Compares across months can drift for that reason alone; Mode 2 is especially exposed. Protocol encourages same-day capture.
- **Prompt-suite representativeness.** The canonical 10 prompts cover the content shapes the referenced article measured. Your workload may be skewed. Add your own prompts and re-run.

---

## "Should I switch?" decision framework

| Cost ratio | IFEval Δ | Recommendation |
|------------|----------|----------------|
| ≤ 1.05× | any | Switch. Minimal cost impact. |
| 1.05–1.20× | +5 pp or more | Switch if quality matters. |
| 1.05–1.20× | ±2 pp | Suite-agnostic — depends on workload. Test with your own content. |
| 1.20–1.45× | +10 pp or more | Trade-off call. Model your spend at the new ratio. |
| ≥ 1.45× | any | Stay, or use the newer model selectively (e.g. code review only). |

IFEval Δ is side B minus side A in percentage points.

---

## Reference ratios (observed)

| Pair | Suite | Avg cost ratio | IFEval Δ | Source |
|------|-------|----------------|----------|--------|
| `claude-opus-4-6` → `claude-opus-4-7` | v1 | 1.21–1.45× *(content-shape-dependent)* | ≈ +5 pp | [Tokenizer article][article-url] |

When you run the suite on a new pair, you can PR your observed ratio into this table — it grows into a community registry.

[article-url]: https://www.claudecodecamp.com/p/i-measured-claude-4-7-s-new-tokenizer-here-s-what-it-costs-you

---

## Troubleshooting

- **"compare-suite versions differ"** — you ran the suite at v1 on one side and v2 on the other. Re-run both sides with the same suite, or pass `--allow-suite-mismatch` to proceed (ratios will be misleading).
- **"aggregate compare requires --yes when stdin is not a TTY"** — Mode 2 guards against accidental large rollups in scripts. Add `--yes` or run interactively.
- **Predicate says ✗ but the text looks right** — check the predicate in the prompt file. Predicates are strict by design (IFEval-style); near-misses still fail.
- **`last-opus-4-7` resolves to nothing** — the default threshold is 5 user turns. Short or crashed sessions are filtered out. Override with `--compare-min-turns 1`.
- **`--count-tokens-only requires ANTHROPIC_API_KEY`** — set the env var and re-run. The endpoint is lightweight (no inference) but still requires authentication.
- **`probe failed: HTTP 403` in count-tokens mode** — the first model is not accessible to the API key. The mode auto-falls-back to counting the second model only and tells you in stderr. Use Workflow A (sessions) if you need a true A-vs-B comparison.
