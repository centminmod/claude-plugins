# Changelog — session-metrics

All notable changes to the session-metrics skill.
Versions match the `plugin.json` / `marketplace.json` version field.

## v1.43.0 — 2026-05-29

### Recognise Claude Opus 4.8 + its 1M-context variant as a first-class model

**Added — explicit `claude-opus-4-8` pricing key.** Opus 4.8 ships at the same new tier as 4.7 ($5 input / $25 output / $0.50 cache read / $6.25 5m-write / $10 1h-write). It is now a first-class entry in `_PRICING` (above `claude-opus-4-7`), so the exact-match path prices it silently and the prefix sweep covers date-suffixed forms (`claude-opus-4-8-YYYYMMDD`).

**Fixed — 1M-context variant mispricing.** Before this release, `claude-opus-4-8[1m]` (the 1M-context tag Claude Code writes into `message.model`) evaded the family-fallback regex — its `(?:-|$)` boundary doesn't match the trailing `[` — and fell through to `_DEFAULT_PRICING` (Sonnet $3/$15), a ~40% under-count. The explicit key catches it via the prefix sweep at the correct $5/$25 tier. Mirrors how `claude-opus-4-7[1m]` already resolves.

**Fixed — spurious unknown-model warning for 4.8.** `claude-opus-4-8` previously routed through the family fallback: correct rate, but flagged into `_UNKNOWN_MODELS_SEEN`, emitting the at-exit `[warn] Unknown model(s)…` advisory on every run. The explicit key silences it.

**Parity — `audit-extract.py`.** Added an explicit `("claude-opus-4-8", 5.00)` row to the sibling skill's `_INPUT_RATE_PER_M_BY_MODEL` for lockstep with `_PRICING` (it was already correct via the bare `claude-opus` substring needle; this is traceability, not a behaviour change).

**Note — >200K-context premium still not modelled.** Consistent with the existing 4-7[1m] treatment; out of scope.

## v1.42.0 — 2026-05-04

### Partial-hit rate + /clear detection + sparkline event markers

**Added — partial-hit rate surfacing.** New `partial_hit_turns`, `total_cache_turns`, and `partial_hit_rate` fields track turns where `cache_read > 0 AND cache_write > 0` simultaneously (prefix extension). Surfaces in: text footer, HTML KPI card, compare-view row, JSON export, and the multi-window 7d/30d/90d/all-time comparison ribbon.

**Added — `/clear` event detection.** Detects `<command-name>/clear</command-name>` entries in the JSONL and marks the next assistant turn with `is_clear_event: true`. Lets users distinguish cache-hit drops caused by context resets (`/clear`) from those caused by context bloat — different root causes, different fixes.

**Added — sparkline event markers.** The per-session cache-trend sparkline now overlays thin vertical lines at context-reset positions: amber for `/clear` events, purple for session resumes. Correlating these markers with cache-hit-rate drops shows whether degradation is from invalidation or bloat.

**Added — `/clear` timeline dividers.** "Context cleared" divider rows appear in the HTML timeline before the first post-clear turn, styled with an amber pill (paralleling the existing resume-marker dividers).

**Tests**: 81 passed / 16 skipped.

## v1.41.11 — 2026-05-03

### Tier 6 close-out — conftest extraction + vendor-charts upgrade docs

Final slice of the upstream Session 142 audit triage plan. Three of the originally-listed Tier 6 items were verified **already shipped** (Proposal A cache-TTL drilldown lives in v1.2.0, all 16 leaf modules already carry top-of-file docstrings, and `tests/browser/conftest.py` already gates browser tests behind `SESSION_METRICS_RUN_BROWSER_TESTS=1`). The remaining items in this skill payload land here as a single bundled patch.

**Added — `tests/conftest.py`.** Lifted the duplicated `isolate_projects_dir` and `_clear_pricing_cache` autouse fixtures out of every `tests/test_*.py` (8 split files since v1.41.9) into a new shared conftest. Pytest auto-discovers conftest.py and applies its autouse fixtures to every test in the directory tree, replacing 8× ~5-line copies with a single canonical declaration. The `sm` reference inside `_clear_pricing_cache` is fetched lazily via `sys.modules.get("session_metrics")` rather than captured at conftest import time, so the canonical module instance loaded by the test files' existing `sys.modules.get(...) or _load_module(...)` dedup pattern is the one whose `lru_cache` gets cleared (no hidden coupling on conftest-vs-test-file collection ordering).

**Removed.** The `@pytest.fixture(autouse=True)` block (decorator + body + comment header) was deleted from each of the 8 split test files (`test_session_metrics.py`, `test_audit.py`, `test_compare.py`, `test_instance.py`, `test_pricing.py`, `test_render.py`, `test_report.py`, `test_time.py`) and replaced with a one-line `# Autouse fixtures live in tests/conftest.py` pointer.

**Changed — `scripts/vendor/charts/README.md`.** Extended with a new *Upgrade procedure* section covering when to bump (patch / minor / major / licence-change / CVE matrix), step-by-step refresh (fetch → regen SHA-256 → verify locally via `_read_vendor_files` → run tests → bump `_SKILL_VERSION`), the `_charts.py` verifier flow (fail-closed at the call site, manifest-as-source-of-truth, `--allow-unverified-charts` is operator emergency-recovery only), and licence-renewal awareness for Highcharts (non-commercial-free, watch upstream-text on each major bump, MIT alternatives `--chart-lib uplot|chartjs` are the documented fall-back).

**Tests**: 703 passed / 1 skipped (unchanged — pure refactor + docs). Verified across 3 consecutive clean runs after one initial flake attributable to a pre-existing test-ordering variance in `test_parallel_dispatch_matches_sequential_output` (added v1.41.10), NOT introduced by the fixture move.

**Why patch bump for what looks like a refactor + docs change.** Both `tests/` and `scripts/vendor/charts/README.md` ship downstream as part of the skill payload — file bytes change in both mirrors, `_SKILL_VERSION` is embedded in every export. Same boring-bump rule as v1.41.8 / v1.41.9 / v1.41.10.

## v1.41.10 — 2026-05-03

### Test-suite — ThreadPoolExecutor parallel-branch coverage

Three new tests appended to `tests/test_instance.py` pin the previously-untested parallel-orchestration path in `scripts/_dispatch.py:_run_all_projects` (L371-376). The `len(project_inputs) > 1` branch was indirectly exercised by existing instance tests but nothing asserted that `ThreadPoolExecutor` was actually used or that its output matched the serial fallback.

**Added** (3 tests, 700/1 → 703/1):

- `test_parallel_branch_uses_thread_pool_when_multiple_projects` — asserts exactly one `ThreadPoolExecutor` is constructed when >1 project is dispatched, with `max_workers ≤ min(8, cpu_count)`.
- `test_single_project_skips_thread_pool` — symmetric assertion: exactly one project takes the `else` branch and never instantiates the pool.
- `test_parallel_dispatch_matches_sequential_output` — runs `_run_all_projects` twice over 3 synthetic projects (real-pool then fake-serial-pool), spies on `_dispatch_instance`, deep-equals all per-project + instance-level reports except `generated_at`. Pins the load-bearing assumption (documented at L350-358) that `_build_report` is pure over `sessions_raw`.

**Pattern**: a small `_TrackingExec` class (context-manager + `.map()` returning serial list) substitutes via `monkeypatch.setattr(sys.modules["_dispatch"], "ThreadPoolExecutor", _TrackingExec)` so the closure inside `_run_all_projects` (which resolves `ThreadPoolExecutor` from module globals at call-time) picks up the fake.

**Why patch bump for a test-only change.** Same as v1.41.9 — `tests/` is part of the skill payload that rsyncs downstream, so file bytes change in both mirrors. Boring-bump rule applies.

## v1.41.9 — 2026-05-03

### Test-suite restructure — bundle the remaining 6 split slices

Six new sibling test files extracted from `tests/test_session_metrics.py` in one bundled commit. The monolith drops from 10,647 → 1,149 lines (-9,498); the test surface area is now spread across 8 topic-focused modules.

**New files**:

- `tests/test_audit.py` (~1900 lines) — audit-extract.py helper-script tests + golden-file waste-analysis + `_classify_turn` waterfall + retry-chain detection.
- `tests/test_compare.py` (~4720 lines) — all compare-mode phases (1, 2, 3, 4-5, 6, 7, 10, prompt-steering, 8) — the LARGEST single topical block.
- `tests/test_report.py` (~1250 lines) — Phase A (cache_breaks / by_skill / by_subagent_type), Phase B (subagent attribution), Advisor feature.
- `tests/test_time.py` (~880 lines) — time-of-day, hour-of-day, weekday × hour matrix, 5-hour session blocks.
- `tests/test_render.py` (~610 lines) — chart-library dispatch + vendoring, uPlot / Chart.js renderers, Usage Insights section.
- `tests/test_instance.py` (~510 lines) — `--all-projects` discovery, `_build_instance_report` aggregation, `_run_all_projects` orchestration.

**What stays in `test_session_metrics.py`** (1,149 lines): cost-math, prompt-filter, dedup, fixture totals, cache-TTL drilldown (Proposal A), resume detection (Phase 3), content-block distribution (Proposal B), input validation, `_cwd_to_slug`, parse-jsonl perf-regression guard, T1.3-T1.5 advisory tests, v1.41.0 audit-driven fixes (parse_jsonl, dir overrides).

**Pattern**: each new file uses the cross-file module-aliasing dedup (`sys.modules.get(...) or _load_module(...)`) proven in v1.41.8. Two autouse fixtures duplicated per split file because pytest autouse only fires for tests in the declaring module. `_build_fixture_report` (3-line helper) copied into the four slice files that need it.

**Tests**: 700 passed, 1 skipped — perfect parity with the pre-slice baseline. No behaviour change; pure refactor.

## v1.41.8 — 2026-05-03

### Test-suite restructure — pricing tests split into a sibling module

First slice of a multi-step split of the 10,942-line `tests/test_session_metrics.py` monolith. 21 pricing-domain tests move to a new sibling `tests/test_pricing.py` (~370 lines); the source file shrinks to 10,647 lines.

**Tests moved:**

- 13 `test_pricing_*` tests (Pricing block, lines 84–231 of the original)
- 3 `test_pricing_unknown_model_*` tests
- 1 parametrized `test_pricing_regex_boundaries_v1_41_0` (21 cases)
- 4 audit-extract pricing-table tests (`_input_rate_for_model_table`, `_pricing_parity_forward`, `_pricing_parity_reverse`, `_bare_prefix_needles_match_documented_set`)

**Module-aliasing fix.** Both files end with `_load_module("session_metrics", _SCRIPT)`, which re-execs unconditionally — whichever file pytest collects last wins the `sys.modules["session_metrics"]` slot, and leaf modules under `scripts/_*.py` use `_sm()` to fetch the canonical instance from `sys.modules` at call time. Without dedup, the loser file's `sm` reference points to a stale module and `monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())` writes silently miss. Both files now check `sys.modules.get(...)` first; whichever file pytest loads first creates the canonical instance, the other reuses it.

**Cost / cache-write tests stayed put.** Per literal reading of the plan, only tests whose names contain `pricing` (plus the four audit-extract pricing-table tests) move in this slice; `test_cost_*`, `test_cache_write_split_*`, `test_no_cache_cost_*`, and the audit-extract `cache_break_*` tests stay in `test_session_metrics.py` for a future cost-domain slice.

**Tests:** 705 passed, 16 skipped (unchanged — pure file-split, no count delta). Patch bump because the skill payload's `tests/` directory bytes change and `_SKILL_VERSION` is embedded in every export.

## v1.41.7 — 2026-05-03

### `_no_cache_cost` symmetry — read cache-creation tokens via the same helper as `_cost`

`_no_cache_cost` (`_turn_parser.py:535`) previously read the FLAT `cache_creation_input_tokens` field directly while `_cost` (line 467) routed the same data through `_cache_write_split` (which prefers nested `cache_creation.ephemeral_*` fields with a flat fallback). Empirically equal on every real-world transcript checked, but a silent future-drift risk: if Anthropic ever stops populating the flat field while keeping the nested ones, `_no_cache_cost` would silently undercount the cache-creation token portion, biasing the "savings from caching" delta downward on every turn. Routed `_no_cache_cost` through `_cache_write_split` and summed the buckets so both functions read the same source of truth.

Behaviour-preserving on real-world transcripts. Existing cost suite stays green.

**Tests**: 705 passed, 16 skipped (unchanged).

Patch bump for export traceability — `_SKILL_VERSION` is embedded in every export so byte-level changes bump the version even when behaviour is unchanged on real-world transcripts.

## v1.41.6 — 2026-05-03

### Tier 1 doc/lint sweep — README cache-format, active-context leaf count, audit-extract bare-prefix removal

Three small fixes from a triple-AI repo audit (Opus 4.7 + Codex GPT-5.5 + DeepSeek V4 Pro). All three correct stale or risky state without changing user-visible behaviour on real-world Anthropic models.

**README cache-format wording**: said "gzipped JSON dump" but the parse-cache format switched to pickle protocol 5 in `_SCRIPT_VERSION = "1.1.0"` (2026-04-30). Replaced with "pickle protocol 5 dump … keyed on file mtime and `_SCRIPT_VERSION`".

**Active-context doc leaf count**: said "All 13 Graphify-derived sub-modules extracted" but the working tree now has 15 leaf modules (13 Graphify-derived + `_constants.py` + `_time_of_day.py`). Updated to current count without rewriting the historical Session 131 audit reference.

**`audit-extract.py:_INPUT_RATE_PER_M_BY_MODEL` bare-prefix entry**: removed `("claude-opus-4", 15.00)`. The bare prefix substring-matched any future Opus 4 minor (e.g. hypothetical `claude-opus-4-2`) at the OLD $15/M tier — a 3× over-charge on audit impact estimates if Anthropic ever ships such a model. Mirrors the same removal made in `session-metrics.py:_PRICING` in v1.41.2. Audit-extract's substring matcher now falls through to `claude-opus` at $5/M for any future Opus 4 minor — conservative under-charge rather than the prior 3× over-charge. Real Opus 4.0 IDs (`claude-opus-4`, `claude-opus-4-YYYYMMDD`) are an inherent main-vs-audit asymmetry: the main script's anchored regex prices them at $15; audit-extract now prices them at $5. Audit impact estimates are approximate by design and the under-direction is the safer drift mode.

**Tests**: 700 passed, 1 skipped (unchanged). Pricing parity tests stay green because the removed entry was never an exact key in `_PRICING` (the family-fallback regex covered it) and was never in the documented bare-prefix sentinel set `{"claude-sonnet", "claude-haiku", "claude-opus"}`.

Patch bump for export traceability — `_SKILL_VERSION` is embedded in every export so byte-level changes bump the version even when behaviour is unchanged on real-world transcripts.

## v1.41.5 — 2026-05-03

### Retry-chain detection perf — pre-tokenize prompts once

Single perf micro-optimisation in `_detect_retry_chains`. The inner loop tokenized each prompt's text twice via `re.findall(r"\w+", text)` (once as the `SequenceMatcher` argument, once when reassigning the carry-over after a match). On a session with `N` consecutive prompts that meant up to `2N(N-1)/2` regex calls — quadratic in the prompt count.

The fix pre-tokenizes all eligible prompts once into a parallel `pre_toks: list[list[str]]` and reads both sides of each comparison by index. Combined with hoisting `set(chain)` above the cost-summing generator (so `if t["index"] in chain_set` builds the set once per chain rather than once per turn iteration), the function is now linear in prompt count for tokenization and constant per chain for the cost lookup.

Behaviour-preserving change. The 700/1 test suite — including the `_detect_retry_chains` direct unit tests and the golden-fixture `test_build_waste_analysis_golden_retry_chains_match_fixture` — passes unchanged.

## v1.41.4 — 2026-05-03

### Advisor cost edge cases, atomic-replace cache invalidation, coverage gaps

Three correctness-adjacent fixes plus regression tests. The advisor and cache-key fixes are dormant under normal transcripts — real-world iteration records always carry a model, and normal upgrade paths bump mtime — so there is no observable behaviour change. Only the defensive fallback paths shifted to the more accurate behaviour.

**Advisor cost edge cases** (`_turn_parser.py:467-560`):

- `_cost`: `it.get("model", model)` returned `""` when the key was present but empty (default arg of `dict.get` only fires on missing keys). Empty model fell through `_pricing_for("")` → `_DEFAULT_PRICING` ($3/$15) instead of the parent turn's tier. Replaced with `it.get("model") or model` — collapses both missing-key and empty-string to the parent rate. On Opus 4.7 the divergence was 60%+ on advisor cost.
- `_advisor_info`: same defect. Now takes the parent `model` parameter and uses `_pricing_for(adv_model or model)`. The displayed advisor model name still goes `None` when the iteration carries no model — only the rate fallback changed.
- `_no_cache_cost`: skipped the advisor iteration loop entirely, so the "savings from caching" delta was biased downward on advisor-using turns. Mirrored the iterations loop from `_cost` for a symmetric comparison.

**`_parse_cache_key` includes `st_size`** (`_data.py:113-148`):

The cache key was `path_hash + mtime_ns + _SCRIPT_VERSION`. Atomic-replace tools (`cp -p`, `rsync --inplace`, restore-from-backup) preserve `mtime_ns` while changing content — the cache would silently serve the stale pickled blob. Added `st_size` to the key. Existing on-disk blobs are invalidated by the format change; cold rebuild runs automatically on first invocation. The prune-on-write logic is unaffected.

**Five new tests** in `tests/test_session_metrics.py`:

1. `test_cached_parse_invalidates_on_size` — same mtime, different size → fresh blob.
2. `test_cached_parse_invalidates_on_script_version` — bumping `_SCRIPT_VERSION` mints a new key.
3. `test_weekly_rollup_boundary_inclusivity` — half-open `[start, end)` math at the `now-7d` and `now-14d` cutoffs.
4. `test_advisor_empty_model_falls_back_to_parent_rate` — the empty-model fallback invariant.
5. `test_no_cache_cost_includes_advisor_iterations` — the symmetry invariant.

`test_parse_cache_key_includes_path_hash` was updated for the new signature.

**Tests**: 5 added, 1 updated. **700 passed, 1 skipped** (was 695).

Patch bump for export traceability — `_SKILL_VERSION` is embedded in every export, so byte-level changes bump the version even when behaviour is unchanged.

## v1.41.3 — 2026-05-03

### Pricing-table parity guard between session-metrics and audit-extract

Test-only patch. The audit-session-metrics sibling skill carries a hand-maintained `_INPUT_RATE_PER_M_BY_MODEL` table used to estimate cache_break and idle_gap_cache_decay impact. With no automated check, a future Anthropic price change could land in `session-metrics.py:_PRICING` while the audit-extract table silently kept the stale rate — quietly mis-estimating impact on every audit until someone noticed.

**Fix**: three new tests in `tests/test_session_metrics.py`:

1. **Forward parity** (`test_audit_extract_pricing_parity_forward`) — every Anthropic-prefixed key in `_PRICING` must resolve to the same input rate via `audit_extract._input_rate_for_model`.
2. **Reverse parity** (`test_audit_extract_pricing_parity_reverse`) — every entry in `_INPUT_RATE_PER_M_BY_MODEL` (excluding three documented bare-prefix catchalls) must resolve to the same input rate via `session_metrics._pricing_for`.
3. **Bare-prefix sentinel** (`test_audit_extract_bare_prefix_needles_match_documented_set`) — keeps the documented exemption set in sync with the table; adding or removing a bare prefix without updating the exemption would silently weaken the parity guard.

The bare-prefix entries (`claude-sonnet`, `claude-haiku`, `claude-opus`) are family-tier substring fallbacks for hypothetical un-versioned Anthropic IDs. Real transcripts always carry a version, so the divergence from `_DEFAULT_PRICING` is dormant; the sentinel test pins the exemption so future changes get reviewed. Non-Anthropic models (glm-*, openai/*, deepseek/*) are intentionally out of scope: cache_break / idle_gap_cache_decay never fire on them because those models lack prompt caching.

**Tests**: 3 added. **695 passed, 1 skipped** (was 692 in v1.41.2).

No production code changed. No behaviour, interface, or export-format change. Patch bump for export traceability — `_SKILL_VERSION` is embedded in every export, so leaving it unchanged after byte-level skill changes would make exports indistinguishable from the prior set.

## v1.41.2 — 2026-05-03

### Wrong-model rate fallback for unknown sub-variants

Latent silent-overcharge bugs in `_pricing_for` for hypothetical future Anthropic releases. Two cases collapsed into one fix:

- **`claude-opus-4-N` for N ≥ 8** prefix-matched the bare `claude-opus-4` entry in `_PRICING` and silently 3×-overcharged at OLD-tier $15/$75 instead of NEW-tier $5/$25 (the rate that has held across 4-5/4-6/4-7).
- **`claude-haiku-4-6` / `claude-haiku-5+`** had no Haiku prefix entry at all and fell through to `_DEFAULT_PRICING` (Sonnet $3/$15) — also a 3× overcharge over the correct Haiku $1/$5.

**The fix** has three parts:

1. **Anchored regex for Opus 4.0** in `_PRICING_PATTERNS`: `^claude-opus-4(?:-\d{8})?$` matches the bare ID and an 8-digit-date-suffixed form. The bare `claude-opus-4` key was removed from `_PRICING` so the prefix sweep no longer silently catches future minors.
2. **New `_PRICING_FAMILY_FALLBACKS` list** in `session-metrics.py`, consulted by `_pricing_for` AFTER the prefix sweep miss but BEFORE the `_DEFAULT_PRICING` fallback. Patterns: future Opus 4 minors → NEW tier; future Opus 5+ → NEW tier; future Haiku 4 minors → Haiku tier; future Haiku 5+ → Haiku tier. Each match adds the model to `_UNKNOWN_MODELS_SEEN` so the at-exit advisory tells the user to refresh `references/pricing.md`.
3. **Advisory wording** updated from "priced at Sonnet rates ($3/$15 per 1M tokens)" to "priced at fallback rates (verify in references/pricing.md)" — the family-fallback path lands on the family's tier, not always Sonnet.

**Sonnet intentionally NOT given a family fallback.** `claude-sonnet-4` is a bare prefix entry and Sonnet 4.x has held one rate tier across all minors, so the silent prefix-sweep behavior is correct for Sonnet. A future `claude-sonnet-5` would need an explicit `_PRICING` row regardless.

**Tests**: 692 passed, 1 skipped — eight new tests cover the bare ID, date-suffixed Opus 4.0, opus-4-8, opus-4-8 with date, opus-5, haiku-4-6, haiku-9, the silent date-suffixed known-model path, and opus-4-1-with-date silent path.

## v1.41.1 — 2026-05-02

### Internal: ruff hygiene cleanup (no behaviour change)

Silenced the 22 pre-existing ruff errors across the four v1.41.0-modified files. No behaviour change, no interface change, no export-format change. Patch bump is for **export traceability** — `_SKILL_VERSION` is embedded in every export, so leaving the version unchanged after byte-level skill changes would make exports indistinguishable from the prior set.

**4 documented re-imports pinned with `# noqa: F401` + `# noqa: I001`** at `session-metrics.py:30,33,36`. Tests patch them via `sm.secrets`, `sm.ZoneInfo`, `sm.ZoneInfoNotFoundError`; `ruff check --fix` would delete them and break 7+ tests.

**15 cosmetic auto-fixes**: 8 × `timezone.utc` → `datetime.UTC` (UP017) and 7 × redundant `f` prefix removal (F541).

**3 SIM105 — split treatment**: 2 × `try/except: pass` → `with contextlib.suppress(...)` in `_cli.py` (where comments live above the try); 1 × `# noqa: SIM105` in `_data.py:192` where the on-`pass` comment is locality-dependent.

**Tests**: 684 passed, 1 skipped (unchanged from v1.41.0 baseline).

## v1.41.0 — 2026-05-02

### Audit-driven correctness + ergonomic batch

DeepSeek V4 Pro audit (re-validated by Codex GPT-5.5 + code-searcher) surfaced six actionable findings; six rejections were already correct.

**P0-A — `assert` → explicit error/exit at `session-metrics.py:_load_leaf`.** Under `python -O` the assert was stripped and a missing leaf module crashed cryptically. Now mirrors `_cli.py:_load_compare_module`'s explicit `if/print/sys.exit(1)` pattern.

**P0-B — `_PRICING_PATTERNS` regex hardening (behaviour change).** Three fixes:
- Numeric-suffix families (`gpt-5.5`, `qwen3.6`, `mimo-v2.5`, `kimi-k2.6`, `minimax-m2.7`) carry `(?!\d)` — extra-digit IDs (`gpt-5.55`) fall through to default rates.
- Provider/model separators switched from bare `.` to `[-_/.]` — `deepseekXv4Yflash` no longer satisfies `deepseek.v4.*flash`.
- Suffix tokens (`pro`, `flash`, `plus`) anchored with `\b`.

**Behaviour-impact note**: model names that previously over-matched the looser regex now route to default Sonnet rates. Re-run historical reports for accurate before/after.

**P1-A — `_parse_jsonl` defensive `isinstance(dict)` filter.** A stray non-dict line that parsed as valid JSON would `AttributeError` at `_extract_turns`. Now skipped via the same warn path as malformed JSON.

**P1-B — `--cache-dir` flag + `CLAUDE_SESSION_METRICS_CACHE_DIR` env var.** Parse-cache directory gains operator override; same precedence shape as `--projects-dir` (flag > env > default).

**P1-C — `--export-dir` flag + `CLAUDE_SESSION_METRICS_EXPORT_DIR` env var.** Export directory gains the same override shape; `_instance_export_root` flows through automatically.

**P2-A — `@functools.lru_cache(maxsize=128)` on `_pricing_for`.** Removes the redundant three-tier resolution `_cost`/`_no_cache_cost`/`_advisor_info` each performed per turn. Idempotent set side-effect preserved.

`SKILL.md` and `references/pricing.md` updated for parity with the new regex + flags. **684 passed, 1 skipped.**

---

## v1.40.2 — 2026-05-01

### Post-split audit-2 cleanup (P1 + P1b + P2)

Second-pass audit caught three follow-ups missed by the v1.40.1 sweep.

**P1 — `_PROJECTS_DIR_OVERRIDE` dead seed.** `_cli.py:49` defined the constant even though every read/write inside the leaf already routed through `_sm()._PROJECTS_DIR_OVERRIDE`. Same shape as the v1.40.1 Bug 2/3 fix. Leaf seed deleted; canonical attr now defined directly on the orchestrator beside `_VENDOR_CHARTS_DIR` / `_ALLOW_UNVERIFIED_CHARTS`.

**P1b — drop two unused imports.** `_data.py` collapsed `from datetime import datetime, timedelta, timezone` (timedelta unused). `_cli.py` dropped a redundant function-local `import importlib.util` shadowing the top-level import.

**P2 — collapse the `_CACHE_BREAK_DEFAULT_THRESHOLD = 100_000` triplicate.** New `_constants.py` zero-dep sibling leaf holds the single canonical literal. Orchestrator loads it first via `_load_leaf("_constants")` and keeps the module-level alias for runtime reads + tests.

No behaviour change. 653 tests pass, 1 skipped.

---

## v1.40.1 — 2026-05-01

### Post-split bug fixes and import cleanup

Three bugs corrected in the 13-module monolith split.

**Bug 1 — `_dispatch.py` cache-break threshold fallback.** `_CACHE_BREAK_DEFAULT_THRESHOLD` in the HTML-render path was a locally-defined constant that would have drifted if the default ever changed. Fixed to route through `_sm()`.

**Bug 2/3 — `_charts.py` vendor-dir and allow-unverified constants.** `_VENDOR_CHARTS_DIR` and `_ALLOW_UNVERIFIED_CHARTS` were unnecessary duplicates in `_charts.py`. All reads now route through `_sm()`.

**Import cleanup.** Nine imports moved entirely into leaf modules during the split removed from `session-metrics.py`. Three retained as module-level attributes for test monkeypatching: `secrets`, `ZoneInfo`, `ZoneInfoNotFoundError`.

**Audit-skill playbook reinforcement.** `SKILL.md` and all five playbook references in `audit-session-metrics` now carry an explicit guard prohibiting intermediate Python synthesis scripts.

653 tests pass, 1 skipped.

---

## v1.40.0 — 2026-04-30

### Skill version embedded in all exports

`_SKILL_VERSION = "1.40.0"` added to `session-metrics.py`. Every export now surfaces the skill version that generated it: HTML meta line appends `· skill v1.40.0`; Markdown `Generated:` line appends `|  Skill: v1.40.0`; JSON export gains a top-level `"skill_version"` field; CSV exports prepend a comment row `# Session Metrics skill v1.40.0, <generated_at>, <mode>`. `_SKILL_VERSION` must match `plugin.json` / `marketplace.json` and is bumped whenever those bump.

---

## v1.39.0 — 2026-04-30

### Cache hygiene — daily lazy global prune

`_prune_cache_global` runs at most once per 24 hours (sentinel file in cache dir) on every normal invocation. It deletes three categories of blobs: (1) **orphaned** — the UUID stem matches no JSONL under `_projects_dir()` (deleted project, renamed slug); (2) **inactive session** — source JSONL mtime > 60 days AND blob mtime > 30 days (session long closed); (3) **stale blob** — blob mtime > 30 days even for a semi-active session. The 30 d / 60 d split protects blobs that are being served on warm hits for an ongoing project. Subagent JSONLs (`*/subagents/*.jsonl`) are included in the live-session index so their blobs are not incorrectly treated as orphaned. No new CLI flags; honours `--no-cache`. 647 → 653 passed, 1 skipped.

---

## v1.38.0 — 2026-04-30

### Cache hygiene — self-pruning parse cache (option a)

`_cached_parse_jsonl` now deletes stranded blobs for the same source file on every cache write. Each JSONL has a unique `{stem}__{path_hash}__` prefix in the cache filename; any blob sharing that prefix but not matching the just-written filename was stranded by a previous `mtime_ns` bump or `_SCRIPT_VERSION` change. The prune glob runs only on cache miss (post-successful write) — zero latency on warm hits. Failures are non-fatal; the parse result is always returned. No `_SCRIPT_VERSION` bump (cache schema unchanged), no new CLI flags.

**Motivation.** v1.37.0 switched to pickle (no compression), which is ~2× larger per blob (~9 MB → ~19 MB per typical session). Live sessions receive a new `mtime_ns` on every appended turn, stranding the prior blob each time. At project scale the orphaned files accumulate silently. The per-file prune targets the structural cause (same-source stranding) without a whole-cache scan.

**Stdlib-only, cross-platform, single-user-local trust model.** No new dependencies or CLI surface.

### Tests

Four new tests; one existing test updated to match the new prune behaviour. 643 → 647 passed, 1 skipped.

---

## v1.37.0 — 2026-04-30

### Performance — pickle parse cache (-67% cold / -18% warm / -17% project)

Switched the parse cache at `~/.cache/session-metrics/parse/` from gzip+JSON to `pickle` protocol 5 (stdlib, no compression). Single-file change with broad wins across cold parse, warm cache hits, and `--project-cost` fanout on a 158-session corpus.

**Implementation.** `_cached_parse_jsonl` swaps `gzip.open` + `json.load`/`json.dump` for `open` + `pickle.load`/`pickle.dump(protocol=5)`. Filename suffix `.json.gz` → `.pkl`. Read-side exception catch updated from `(OSError, json.JSONDecodeError)` → `(OSError, pickle.UnpicklingError, EOFError)`. Atomic write via random-suffix tmp + `os.replace` is unchanged (POSIX + Windows safe since Py 3.3). `import gzip` removed (no longer used anywhere); `import pickle` added.

**Cache schema bump.** `_SCRIPT_VERSION` 1.0-rc.5 → 1.1.0 invalidates every existing cache blob exactly once. First run after upgrade rebuilds the cache transparently — slower than a warm hit but identical cold-path cost. No data loss; the JSONL transcripts under `~/.claude/projects/` are the source of truth.

**Disk trade-off.** Pickle (no compression) is ~2× larger on disk than the prior gzip+JSON: ~9 MB → ~19 MB for a typical 28 MB JSONL session. At project scale (158 sessions): ~1.4 GB → ~3 GB cache footprint. Acceptable for a developer-tool cache living in `~/.cache/`. Stale cache management (no GC today; mtime_ns and version bumps strand prior blobs) is a known follow-up.

**Stdlib-only invariant preserved.** No new dependencies; the shipped skill remains stdlib-only per `plugin.json` `strict: true`. Cross-platform: identical behaviour on macOS, Linux, Windows. Trust model is single-user-local; pickle of the script's own writes is safe.

### Tests

Four test sites updated to match the new cache extension (`*.json.gz` → `*.pkl`) and docstring ("gzip+JSON" → "pickle"). 643 passed, 1 skipped — same count as v1.36.0.

---

## v1.36.0 — 2026-04-30

### Sharing-time hygiene — `--export-share-safe` one-flag pre-share gesture

Adds a single CLI flag for the common "I'm about to publish or paste this somewhere" workflow, plus README / SKILL.md guidance documenting which export surfaces redact and which only chmod.

**Documentation — `--redact-user-prompts` + `--export-share-safe`.**
The README gains a *Sharing exports safely* subsection under *Privacy* with an at-a-glance table of redacted (JSON + compare HTML) versus chmod-only (HTML / MD / CSV / text) surfaces. Most-used-commands block gains an `--export-share-safe` example. SKILL.md *Other useful flags* table documents both `--redact-user-prompts` and `--export-share-safe`.

**`--export-share-safe` flag.**
One-flag bundle that implies `--redact-user-prompts` and `--no-self-cost`, and chmods every written export file to `0o600` (`rw-------`) immediately after the write. Wired through every export write site — single-session, project, instance JSON / HTML / MD / CSV, the split-HTML / per-project drilldown writers, plus the compare-mode and compare-run-extras writers. Verified end-to-end against a real 361-turn session: 198 turns redacted, 0 verbatim, `self_cost` absent, both files chmod'd to `-rw-------`. Help text explicitly documents the JSON-only redaction caveat (HTML / MD / CSV / text are chmod'd but contain verbatim prompts) so users pair `--export-share-safe` with `--output json` for full redaction.

### Tests

3 new regression tests (argparse implication; chmod 0o600 on a `share_safe=True` write; default-off does NOT chmod). 643 total tests pass (1 skipped).

---

## v1.35.0 — 2026-04-29

### Insight + sharing — P2 batch (warmup-trigger length cap + JSON redaction)

Two follow-up fixes from the user-facing audit. Schema-additive: existing tooling keeps working; new behaviour is opt-in.

**P2.3 — `session_warmup_overhead` now length-agnostic.**
The trigger was previously gated on `len(turns) <= 15`, which silenced mid-length sessions where the first turn still dominated cost. A 17-turn session with 30% first-turn cost never fired. The cap is dropped: any session with `first_turn_cost / total_cost > 20%` now surfaces. To keep the signal honest on long sessions, the suggested severity downgrades to `low` (with a `downgrade_reason`) when `total_turns > 30 AND first_pct < 30` — the warmup cost amortises across many turns. Default severity stays `medium`. The `quick-audit.md` playbook row was rewritten to match.

**P2.4 — `--redact-user-prompts` wired through JSON exports.**
The `--redact-user-prompts` flag was silently ineffective on JSON exports — the redact path only ran in compare HTML, while `render_json` wrote full `prompt_text` and `assistant_text` verbatim. The flag now also masks `prompt_text` / `prompt_snippet` and `assistant_text` / `assistant_snippet` on every turn of single-session and project JSON exports with `[redacted]`. Tool inputs, slash-command names, and structured cost / token fields stay visible so the redacted JSON is still useful for cost analysis. Empty fields stay empty (truthiness preserved). No-op for instance-scope JSON, which carries no per-turn records. Help text updated to document JSON coverage explicitly.

### Tests

8 new regression tests (4 for P2.3 length cap + downgrade matrix, 4 for P2.4 redaction including default-off, structured-field visibility, and empty-field preservation). 624 total tests pass (1 skipped).

---

## v1.34.0 — 2026-04-29

### Insight — P2 batch (cost-share + paste-bomb classification)

Two user-visible insight gaps closed. Schema-additive: existing tooling that only reads turn counts keeps working; new fields are extra.

**P2.1 — Cost share alongside turn share in Models table + audit playbook.**
The `models` field in JSON exports gained `cost_usd` per model (was turn count only) and Markdown / HTML / text Models tables now show `Turn %` and `Cost %` columns side by side. The audit playbook's `model_split_clause` is now rendered by cost share — turn share alone hides the long-tail expensive model (e.g. one model with 22% of turns can be 37% of cost). The audit helper still parses pre-v1.34 exports where `models` was `{name: int}`; in that case `cost_pct` is `null` and the playbook falls back to turn share. A turn-share aside is appended when the gap on the dominant model is ≥10pp.

**P2.2 — `paste_bomb` waste category.**
The waste classifier now flags any turn where the user prompt is >5 000 characters as `paste_bomb` (matching the threshold the audit detailed scan already used for its `paste_bombs` finding). Fires above `reasoning` in the priority waterfall — pasting a wall of text is the actionable user behaviour, the downstream thinking burn is just an effect. Subagent dispatch still wins. The category is risk-flagged in the waste-distribution bar (bright red, between `oververbose_edit` and `dead_end`) and surfaces in the per-turn drawer; previously these turns silently classified as `productive`.

### Tests

8 new regression tests (3 for P2.1, 5 for P2.2). 616 total tests pass (1 skipped).

---

## v1.33.0 — 2026-04-29

### Correctness — P1 audit-pipeline fixes

Four correctness fixes in the `audit-session-metrics` and `session-metrics` audit pipelines, batched together so users get a single coherent release rather than four point-bumps. No schema change. No flag change. The HTML/JSON exports look the same; the underlying numbers are now right.

**P1.1 — `file_re_reads` detector reads `input_preview`.**
`audit-extract.py` previously looked up `tool_use_detail` entries via `d["input"]["file_path"]`, but the export schema only carries `input_preview` (a string from `_summarise_tool_input`). The dict access silently returned `None` on every `Read`, so `detailed_candidates.file_re_reads` was always `[]` regardless of how many times the same path was re-read. Detector now reads `input_preview` directly. Verified end-to-end against a 328-turn export — surfaced 9 real re-read paths where the old detector returned `[]`.

**P1.2 — Per-model input rate for `cache_break` and `idle_gap_cache_decay` impact.**
The audit pipeline used a hardcoded `OPUS_INPUT_RATE_PER_M = 5.00` to convert uncached / cache-write tokens into dollars regardless of which model the turn actually ran on, overstating Sonnet by 67% and Haiku by 400%. Replaced with `_INPUT_RATE_PER_M_BY_MODEL` (substring-priority table covering Opus 4.5/4.6/4.7 / Opus 4.0/4.1 / Sonnet 3.x/4.x / Haiku 4.5 / Haiku 3.5) plus an `_input_rate_for_model` helper. `_detect_idle_gap_cache_decay` now reads the turn's `model`; the cache_break trigger sums per-break impact at each break's own model rate and emits a model-aware `impact_basis` (mixed-model variant when breaks span models). Verified end-to-end on a mixed Opus + Haiku export: cache_break impact $1.28 → $0.86 (33% overstatement removed).

**P1.3 — `_BASH_PATH_RE` requires leading-dot or start-of-arg boundary.**
The bash-branch path regex previously allowed *zero* leading dots (`\.{0,2}/`), so a longer string like `cat .claude/skills/foo.py` would yield the substring `/skills/foo.py`. In the re-read detector, that fragment formed its own bucket separate from the legitimate full-path access, silently merging same-suffix files across different project subtrees. Anchored the regex to a start-of-arg boundary via leading `(?<![\w.])` and split the previously combined `\.{0,2}/` alternative into two explicit branches: `\.{1,2}/...` (dot-relative) and `/...` (absolute). Trade-off: `.name/...` style hidden-dir paths in Bash commands no longer contribute to the bash-branch detector at all; net coverage preserved on realistic workloads because the same files are also accessed through Read/Edit/Write (absolute paths, separate detector branch). Verified end-to-end: reaccessed-path count dropped 29 (polluted) → 23 (clean), top-10 free of phantom fragments.

**P1.4 — Marginal-cost attribution in `_detect_file_reaccesses`.**
The detector previously summed each path's `cost_usd` as the entire turn cost for every turn that touched the path — a single Bash arg in a 10-tool turn would charge that path 100% of the turn cost, and two re-read paths sharing one turn would each be charged 100%, so `total_reaccess_cost` could exceed the underlying session cost. Fix: weight each turn's contribution by `path_reads_in_turn / total_tool_calls_in_turn`. Total contribution per turn is now bounded by the turn cost. Verified end-to-end on a 328-turn export: `total_reaccess_cost` $24.70 → $22.80 (46.3% → 42.7%), largest per-path drop $4.81 → $3.51.

### Tests

12 new regression tests covering the four P1 fixes (4 for P1.1+P1.2 in `audit_extract`, 8 for P1.3+P1.4 in `_detect_file_reaccesses`). 612 total tests pass (1 skipped, pre-existing).

---

## v1.32.0 — 2026-04-29

### Feature — project-scope and instance-scope audit support in `audit-session-metrics`

`audit-extract.py` now auto-detects the JSON scope from `data["mode"]` and branches into three code paths:

**Project scope** (`project_*.json`): per-session cost ranking, poor-cache-health sessions (avg cache-hit < 80%), sessions with cache breaks, weekly cost and cache delta. Intra-session-only triggers (`idle_gap_cache_decay`, `session_warmup_overhead`) are suppressed via a `SESSION_ONLY_METRICS` frozenset.

**Instance scope** (`instance/*/index.json`): cross-project cost ranking with cost-share %, poor-cache projects, instance-wide cache-hit average, weekly trend. `fired_triggers` and `top_expensive_turns` are always `[]` (no per-turn data at instance scope). `None`-safe evaluation for `cache_hit_pct` and `cache_savings` (present-but-`null` in instance JSON).

**Schema**: `DIGEST_SCHEMA_VERSION` bumped `1.2 → 1.3` (additive — adds `scope`, `project_analysis`, `instance_analysis` fields).

**New reference playbooks**: `project-quick-audit.md`, `project-detailed-audit.md`, `instance-quick-audit.md`.

**SKILL.md routing**: `audit-session-metrics/SKILL.md` gains a scope routing dispatch matrix. `session-metrics/SKILL.md` now shows scope-aware post-export audit suggestions (project → per-session audit, instance → cross-project audit).

---

## v1.31.0 — 2026-04-29

### Feature — natural-language export dispatch keywords

`SKILL.md` gains an `export` dispatch keyword and a new `## Export shortcuts` section so users can invoke the skill with natural-language phrases documented in the plugin marketplace article.

**New dispatch keywords** (matched on `$ARGUMENTS[0]`):

- `export` — routes to `## Export shortcuts`; scans the full argument string to determine scope and output formats
- `project` — runs `--project-cost`, also picking up `--output` format flags from remaining args
- `project-cost` — alias for `project`

**Export shortcuts routing** (priority-ordered, first match wins):

1. Arg string contains `all-projects` → `--all-projects --output <formats>`
2. Arg string contains `project` → `--project-cost --output <formats>`
3. Otherwise → single session `--session <id> --output <formats>`

Format flags (`html`, `csv`, `md`/`markdown`) are inferred from the argument text; `json` is always appended per the post-export audit convention. Bare invocations without a format word default to `--output json`.

**Bug fixed:** `export all-projects` would have matched the `project` substring check and silently routed to `--project-cost`; priority ordering now prevents this.

---

## v1.30.1 — 2026-04-29

### Fix — audit suggestion shown after every export

When the session-metrics skill is invoked with `--output html` (or `csv` or `md`), it now automatically appends `json` to the format list if absent. This ensures the JSON sidecar is always written and the `/audit-session-metrics quick <path>` suggestion is always shown — previously the hint was suppressed on html-only exports, requiring a redundant second `/session-metrics --output json` invocation before the audit could be run.

The Haiku model-pinning on the audit skill is unaffected — the skill still prints the slash-command suggestion rather than invoking the audit programmatically.

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
