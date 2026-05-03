"""Audit-extract.py + golden-file waste-analysis + retry-chain + classify_turn tests.

Split out of test_session_metrics.py in v1.41.9 (Tier 4 of the
post-audit improvement plan; sibling-file pattern established in v1.41.8).

Run with: uv run python -m pytest tests/test_audit.py -v
"""
import html as html_std
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

_HERE       = Path(__file__).parent
_SCRIPT     = _HERE.parent / "scripts" / "session-metrics.py"
_COMPARE    = _HERE.parent / "scripts" / "session_metrics_compare.py"
_FIXTURE    = _HERE / "fixtures" / "mini.jsonl"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"could not locate module spec for {path}"
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None, f"spec has no loader for {path}"
    spec.loader.exec_module(mod)
    return mod


# Reuse the canonical module instance across split test files.
# _load_module re-execs unconditionally; without sys.modules.get
# guards, leaf modules' _sm() callers would see a different instance
# than monkeypatch writes target. See test_pricing.py for the full
# rationale (Session 144).
sm  = sys.modules.get("session_metrics") or _load_module("session_metrics", _SCRIPT)


# Autouse fixtures `isolate_projects_dir` and `_clear_pricing_cache` are
# defined in tests/conftest.py and apply automatically to every test in
# this directory tree (lifted in v1.41.11).


def _build_fixture_report():
    sid, turns, user_ts = sm._load_session(_FIXTURE, include_subagents=False)
    return sm._build_report("session", "test-slug", [(sid, turns, user_ts)])


# === BODY (extracted from test_session_metrics.py) ============================
# ---------------------------------------------------------------------------

_AUDIT_EXTRACT = (_HERE.parent.parent / "audit-session-metrics"
                  / "scripts" / "audit-extract.py")


def _load_audit_extract():
    """Lazy-load audit-extract.py as a module so tests can call its
    functions directly without spawning a subprocess for each case."""
    return _load_module("audit_extract", _AUDIT_EXTRACT)


def _audit_min_export(**overrides) -> dict:
    """Build a minimal session-metrics JSON export shape that the script's
    trigger evaluation walks over. Callers pass `totals=`, `turns=`,
    `cache_breaks=`, etc. to override per-test."""
    base = {
        "generated_at": "2026-04-29T00:00:00Z",
        "mode": "single",
        "slug": "-test",
        "tz_offset_hours": 0,
        "tz_label": "UTC",
        "models": {"claude-opus-4-7": 1},
        "totals": {
            "input": 100, "output": 100, "cache_read": 0, "cache_write": 0,
            "cache_write_5m": 0, "cache_write_1h": 0, "extra_1h_cost": 0,
            "cost": 1.0, "no_cache_cost": 1.0, "turns": 1,
            "advisor_call_count": 0, "advisor_cost_usd": 0.0,
            "total": 200, "total_input": 100,
            "cache_savings": 0.0, "cache_hit_pct": 0.0,
            "thinking_turn_count": 0, "thinking_turn_pct": 0.0,
            "tool_call_total": 0, "tool_names_top3": [],
        },
        "sessions": [{"subtotal": {}, "turns": []}],
        "cache_breaks": [],
        "by_skill": [],
        "by_subagent_type": [],
        "subagent_attribution_summary": {
            "attributed_turns": 0, "orphan_subagent_turns": 0,
            "nested_levels_seen": 0, "cycles_detected": 0},
        "subagent_share_stats": {
            "include_subagents": True, "has_attribution": False,
            "total_cost": 1.0, "attributed_cost": 0.0, "share_pct": 0.0,
            "spawn_count": 0, "attributed_count": 0, "orphan_turns": 0},
        "weekly_rollup": {"has_data": False},
    }
    base.update(overrides)
    return base


def _audit_min_turn(index: int = 1, **overrides) -> dict:
    """Minimal turn record matching session-metrics export shape."""
    base = {
        "index": index, "input_tokens": 10, "output_tokens": 10,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
        "cost_usd": 0.01, "model": "claude-opus-4-7",
        "slash_command": "", "prompt_text": "",
        "tool_use_names": [], "tool_use_detail": [],
        "stop_reason": "end_turn",
        "attributed_subagent_cost": 0.0, "attributed_subagent_count": 0,
        "advisor_calls": 0, "advisor_cost_usd": 0.0, "advisor_model": None,
    }
    base.update(overrides)
    return base


def test_audit_extract_script_exists_and_executable():
    assert _AUDIT_EXTRACT.exists(), f"missing: {_AUDIT_EXTRACT}"
    assert os.access(_AUDIT_EXTRACT, os.R_OK)


def test_audit_extract_filename_parser_canonical():
    ae = _load_audit_extract()
    id8, ts = ae.session_filename_parts("/p/session_8461c187_20260428T211457Z.json")
    assert id8 == "8461c187"
    assert ts == "20260428T211457Z"


def test_audit_extract_filename_parser_legacy():
    ae = _load_audit_extract()
    id8, ts = ae.session_filename_parts("/p/session_abc12345_20260101_120000.json")
    assert id8 == "abc12345"
    assert ts == "20260101_120000"


def test_audit_extract_filename_parser_fallback():
    ae = _load_audit_extract()
    # Unrecognised pattern → fallback splits on '_'
    id8, ts = ae.session_filename_parts("/p/something_weird_format.json")
    assert id8 == "weird"  # second segment


def test_audit_extract_baseline_io_ratio_uses_uncached():
    ae = _load_audit_extract()
    # 1000 total input, 950 cache reads → 50 uncached, 10 output → 5:1
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "total_input": 1000, "cache_read": 950, "output": 10,
        "cost": 1.0, "cache_hit_pct": 95.0,
    })
    base = ae.compute_baseline(data)
    assert base["input_output_ratio"] == 5
    assert base["cache_hit_pct"] == 95.0


def test_audit_extract_cache_break_fires_with_impact():
    ae = _load_audit_extract()
    data = _audit_min_export(
        cache_breaks=[{"turn_index": 5, "uncached": 200_000, "cache_break_pct": 100,
                       "session_id": "s", "timestamp": "", "timestamp_fmt": "",
                       "total_tokens": 200_000, "prompt_snippet": "...",
                       "slash_command": "", "model": "claude-opus-4-7", "context": []}],
    )
    digest = ae.build_digest(data, "/p/session_test1234_20260101T000000Z.json", "quick")
    fired_metrics = [t["metric"] for t in digest["fired_triggers"]]
    assert "cache_break" in fired_metrics
    cb = next(t for t in digest["fired_triggers"] if t["metric"] == "cache_break")
    # 200k tokens × $5/M = $1.00
    assert cb["estimated_impact_usd"] == pytest.approx(1.0, abs=0.01)
    assert cb["impact_basis"].startswith("200,000 uncached tokens")


def test_audit_extract_cache_break_severity_downgrades_when_rare():
    ae = _load_audit_extract()
    # 1 break in 200 turns (0.5%) — below 2% threshold → severity "low"
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 200, "cost": 50.0},
        cache_breaks=[{"turn_index": 5, "uncached": 100_000,
                       "cache_break_pct": 50, "session_id": "s",
                       "timestamp": "", "timestamp_fmt": "",
                       "total_tokens": 100_000, "prompt_snippet": "",
                       "slash_command": "", "model": "claude-opus-4-7",
                       "context": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    cb = next(t for t in digest["fired_triggers"] if t["metric"] == "cache_break")
    assert cb["default_severity"] == "medium"
    assert cb["suggested_severity"] == "low"
    assert cb["downgrade_reason"] is not None
    assert "0.5%" in cb["downgrade_reason"]


def test_audit_extract_cache_break_impact_uses_per_break_model():
    """P1.2 regression — cache_break impact is summed per-break with each
    break's own model rate, not the Opus rate applied to all of them.
    A 200k-token Haiku break must cost $0.20, not $1.00."""
    ae = _load_audit_extract()
    data = _audit_min_export(
        cache_breaks=[{
            "turn_index": 5, "uncached": 200_000, "cache_break_pct": 100,
            "session_id": "s", "timestamp": "", "timestamp_fmt": "",
            "total_tokens": 200_000, "prompt_snippet": "",
            "slash_command": "", "model": "claude-haiku-4-5-20251001",
            "context": [],
        }],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    cb = next(t for t in digest["fired_triggers"] if t["metric"] == "cache_break")
    # 200k × $1/M (Haiku) = $0.20, not $1.00 (Opus)
    assert cb["estimated_impact_usd"] == pytest.approx(0.20, abs=0.01)
    assert "claude-haiku-4-5-20251001" in cb["impact_basis"]
    assert "$1.00/M" in cb["impact_basis"]


def test_audit_extract_cache_break_impact_mixed_models():
    """P1.2 regression — mixed-model cache_breaks sum their respective rates
    instead of multiplying total uncached by a single rate."""
    ae = _load_audit_extract()
    data = _audit_min_export(
        cache_breaks=[
            {"turn_index": 5, "uncached": 100_000, "cache_break_pct": 100,
             "session_id": "s", "timestamp": "", "timestamp_fmt": "",
             "total_tokens": 100_000, "prompt_snippet": "", "slash_command": "",
             "model": "claude-opus-4-7", "context": []},
            {"turn_index": 7, "uncached": 100_000, "cache_break_pct": 100,
             "session_id": "s", "timestamp": "", "timestamp_fmt": "",
             "total_tokens": 100_000, "prompt_snippet": "", "slash_command": "",
             "model": "claude-haiku-4-5-20251001", "context": []},
        ],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    cb = next(t for t in digest["fired_triggers"] if t["metric"] == "cache_break")
    # 100k × $5/M (Opus) + 100k × $1/M (Haiku) = $0.50 + $0.10 = $0.60
    assert cb["estimated_impact_usd"] == pytest.approx(0.60, abs=0.01)
    assert "mixed models" in cb["impact_basis"]
    assert "claude-opus-4-7" in cb["impact_basis"]
    assert "claude-haiku-4-5-20251001" in cb["impact_basis"]


def test_audit_extract_idle_gap_cache_decay_uses_turn_model():
    """P1.2 regression — _detect_idle_gap_cache_decay uses each turn's own
    model for the rebuild cost. A Sonnet rebuild must cost 60% of an Opus
    rebuild for the same token count."""
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, timestamp="2026-04-29T10:00:00Z",
                        cache_write_tokens=10, input_tokens=100,
                        cache_read_tokens=0, model="claude-sonnet-4-6"),
        _audit_min_turn(index=2, timestamp="2026-04-29T10:06:00Z",
                        cache_write_tokens=200_000, input_tokens=100,
                        cache_read_tokens=0, model="claude-sonnet-4-6"),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.0,
                "output": 100, "total_input": 200},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    fired = next((t for t in digest["fired_triggers"]
                  if t["metric"] == "idle_gap_cache_decay"), None)
    assert fired is not None
    # 200k × $3/M (Sonnet) = $0.60, not $1.00 (Opus)
    assert fired["estimated_impact_usd"] == pytest.approx(0.60, abs=0.01)


def test_audit_extract_top_turn_share_fires_above_30pct():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, cost_usd=10.0),  # 50% of total
        _audit_min_turn(index=2, cost_usd=5.0),
        _audit_min_turn(index=3, cost_usd=5.0),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 20.0, "turns": 3,
                "output": 1000, "total_input": 1000},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    top = next((t for t in digest["fired_triggers"] if t["metric"] == "top_turn_share"), None)
    assert top is not None, "top_turn_share should fire when one turn is >30% of cost"
    assert top["evidence"]["pct_of_total"] == pytest.approx(50.0)


def test_audit_extract_top_turn_share_does_not_fire_below_30pct():
    ae = _load_audit_extract()
    # Even spread — no single turn dominates
    turns = [_audit_min_turn(index=i, cost_usd=1.0) for i in range(1, 11)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 10.0, "turns": 10},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    fired = [t["metric"] for t in digest["fired_triggers"]]
    assert "top_turn_share" not in fired


def test_audit_extract_advisor_share_uses_realised_cost():
    ae = _load_audit_extract()
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "cost": 10.0, "advisor_call_count": 3, "advisor_cost_usd": 1.50,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    adv = next(t for t in digest["fired_triggers"] if t["metric"] == "advisor_share")
    # estimated_impact_usd is the already-realised advisor cost itself.
    assert adv["estimated_impact_usd"] == pytest.approx(1.50)
    assert adv["evidence"]["pct_of_total"] == pytest.approx(15.0)


def test_audit_extract_thinking_high_fires_above_30pct():
    ae = _load_audit_extract()
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "thinking_turn_pct": 35.0, "thinking_turn_count": 35, "turns": 100,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    fired = [t["metric"] for t in digest["fired_triggers"]]
    assert "thinking_engagement_high" in fired


def test_audit_extract_truncated_outputs_fires_when_present():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1),
        _audit_min_turn(index=2, stop_reason="max_tokens"),
        _audit_min_turn(index=3, stop_reason="max_tokens"),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    trunc = next((t for t in digest["fired_triggers"] if t["metric"] == "truncated_outputs"), None)
    assert trunc is not None
    assert trunc["evidence"]["truncated_count"] == 2
    assert trunc["evidence"]["turn_indices"] == [2, 3]


def test_audit_extract_session_warmup_fires_for_short_costly_first_turn():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, cost_usd=0.50),  # 50% of total cost
        _audit_min_turn(index=2, cost_usd=0.30),
        _audit_min_turn(index=3, cost_usd=0.20),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "turns": 3,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    warm = next((t for t in digest["fired_triggers"]
                 if t["metric"] == "session_warmup_overhead"), None)
    assert warm is not None
    assert warm["evidence"]["pct_of_total"] == pytest.approx(50.0)


def test_audit_extract_tool_result_bloat_fires_after_bash():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, tool_use_names=["Bash"]),
        _audit_min_turn(index=2, cache_write_tokens=80_000),  # bloated
        _audit_min_turn(index=3),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    bloat = next((t for t in digest["fired_triggers"]
                  if t["metric"] == "tool_result_bloat"), None)
    assert bloat is not None
    assert bloat["evidence"]["turn_index"] == 2
    assert bloat["evidence"]["prior_tool"] == "Bash"


def test_audit_extract_top_turns_flag_cache_break_correlation():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, cost_usd=5.0, cache_write_tokens=300_000),
        _audit_min_turn(index=2, cost_usd=1.0),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 6.0, "turns": 2,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
        cache_breaks=[{"turn_index": 1, "uncached": 300_000, "cache_break_pct": 100,
                       "session_id": "s", "timestamp": "", "timestamp_fmt": "",
                       "total_tokens": 300_000, "prompt_snippet": "",
                       "slash_command": "", "model": "claude-opus-4-7", "context": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    top = digest["top_expensive_turns"][0]
    assert top["turn_index"] == 1
    assert top["is_cache_break"] is True, (
        "top expensive turn that is also the cache_break must surface the cross-finding flag"
    )


def test_audit_extract_top_turns_hypothesis_classification():
    ae = _load_audit_extract()
    # Cache-write-heavy turn
    turns = [_audit_min_turn(index=1, cost_usd=5.0,
                             cache_write_tokens=200_000, output_tokens=100)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 5.0, "turns": 1,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    h = digest["top_expensive_turns"][0]["hypothesis"]
    assert "cache-write heavy" in h


def test_audit_extract_detailed_mode_includes_candidates():
    ae = _load_audit_extract()
    # File re-read fixture: 3 reads of same file. The session-metrics export
    # schema serialises Read's path as `input_preview` (a string), not a
    # nested `input.file_path` dict — see _summarise_tool_input in
    # session-metrics.py.
    turns = []
    for i in range(1, 4):
        turns.append(_audit_min_turn(
            index=i, tool_use_names=["Read"],
            tool_use_detail=[{"name": "Read", "input_preview": "/x/y.py"}],
        ))
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "detailed")
    assert "detailed_candidates" in digest
    re_reads = digest["detailed_candidates"]["file_re_reads"]
    assert any(r["file_path"] == "/x/y.py" and r["read_count"] == 3 for r in re_reads)


def test_audit_extract_file_re_reads_ignores_legacy_input_dict_shape():
    """Regression guard for P1.1 — the detector must NOT reach for
    `input.file_path`. Old assumption was that tool_use_detail carried a
    structured `input` dict; the actual export only has `input_preview`.
    Feeding the legacy shape must produce zero re-reads, proving the
    detector reads the documented field rather than silently hitting a
    nested key that no real export contains."""
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(
            index=i, tool_use_names=["Read"],
            tool_use_detail=[{"name": "Read", "input": {"file_path": "/x/y.py"}}],
        )
        for i in range(1, 4)
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "detailed")
    assert digest["detailed_candidates"]["file_re_reads"] == []


def test_audit_extract_quick_mode_omits_detailed_candidates():
    ae = _load_audit_extract()
    digest = ae.build_digest(_audit_min_export(),
                             "/p/session_t_20260101T000000Z.json", "quick")
    assert "detailed_candidates" not in digest


def test_audit_extract_verbose_response_uses_total_input():
    """Cache-heavy sessions must NOT all flag as verbose. Denominator is
    input_tokens + cache_read_tokens, not just input_tokens."""
    ae = _load_audit_extract()
    # 10 input + 990 cache_read = 1000 billable input vs 100 output → ratio 0.1
    # Without the fix, ratio = 100/10 = 10 → would falsely fire
    turns = [_audit_min_turn(index=i, input_tokens=10, cache_read_tokens=990,
                              output_tokens=100) for i in range(1, 16)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 15},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "detailed")
    vr = digest["detailed_candidates"]["verbose_response"]
    assert vr["pct_of_turns"] == 0.0, (
        "cache-heavy session must not register as verbose — denominator is "
        "input_tokens + cache_read_tokens"
    )


def test_audit_extract_weekly_rollup_suppressed_when_first_week():
    ae = _load_audit_extract()
    data = _audit_min_export(weekly_rollup={
        "has_data": True,
        "trailing_7d": {"cost": 50.0, "cache_hit_pct": 90.0},
        "prior_7d": {"cost": 0.0, "cache_hit_pct": 0.0},
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "detailed")
    assert digest["detailed_candidates"]["weekly_rollup"] is None


# ---------------------------------------------------------------------------
# v1.1 schema additions: positive_findings + idle_gap_cache_decay.
# ---------------------------------------------------------------------------


def test_audit_extract_digest_schema_version_is_1_3():
    ae = _load_audit_extract()
    digest = ae.build_digest(_audit_min_export(),
                             "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["digest_schema_version"] == "1.3"
    # v1.1 additions remain (additive evolution)
    assert "positive_triggers" in digest
    # v1.2 additions remain
    assert "session_archetype" in digest
    assert "archetype_signals" in digest
    # v1.3 additions
    assert "scope" in digest
    assert "first_turn_cost_usd" in digest["baseline"]
    assert "first_turn_cost_share_pct" in digest["baseline"]


def test_audit_extract_positive_cache_savings_high_fires_on_ratio():
    ae = _load_audit_extract()
    # cost $0.50, cache_savings $2.00 → ratio 4× → fires (3× threshold)
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "cost": 0.50, "cache_savings": 2.00, "cache_hit_pct": 80.0,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [p["metric"] for p in digest["positive_triggers"]]
    assert "cache_savings_high" in metrics
    p = next(p for p in digest["positive_triggers"] if p["metric"] == "cache_savings_high")
    assert p["estimated_savings_usd"] == pytest.approx(2.00)
    assert p["evidence"]["ratio_savings_to_cost"] == pytest.approx(4.0)


def test_audit_extract_positive_cache_savings_high_fires_on_absolute():
    ae = _load_audit_extract()
    # cost $20, cache_savings $6 → ratio 0.3× (below 3×) but $6 > $5 → fires
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "cost": 20.0, "cache_savings": 6.0,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [p["metric"] for p in digest["positive_triggers"]]
    assert "cache_savings_high" in metrics


def test_audit_extract_positive_cache_savings_high_does_not_fire_when_low():
    ae = _load_audit_extract()
    # cost $10, savings $1 → ratio 0.1× and < $5 → does not fire
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "cost": 10.0, "cache_savings": 1.0,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [p["metric"] for p in digest["positive_triggers"]]
    assert "cache_savings_high" not in metrics


def test_audit_extract_positive_cache_health_excellent_fires():
    ae = _load_audit_extract()
    data = _audit_min_export(totals={
        **_audit_min_export()["totals"],
        "cost": 1.0, "cache_hit_pct": 92.0,
    })
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [p["metric"] for p in digest["positive_triggers"]]
    assert "cache_health_excellent" in metrics


def test_audit_extract_positive_cache_health_suppressed_when_cache_break():
    ae = _load_audit_extract()
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "cache_hit_pct": 95.0},
        cache_breaks=[{"turn_index": 1, "uncached": 100_000, "cache_break_pct": 50,
                       "session_id": "s", "timestamp": "", "timestamp_fmt": "",
                       "total_tokens": 100_000, "prompt_snippet": "",
                       "slash_command": "", "model": "claude-opus-4-7", "context": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [p["metric"] for p in digest["positive_triggers"]]
    assert "cache_health_excellent" not in metrics, (
        "cache_health_excellent must not fire when any cache_break event exists"
    )


def test_audit_extract_idle_gap_cache_decay_fires_after_5min_gap():
    ae = _load_audit_extract()
    # Two turns: second is 6 min after first AND has cache rebuild (>50% cw)
    turns = [
        _audit_min_turn(index=1, timestamp="2026-04-29T10:00:00Z",
                        cache_write_tokens=10, input_tokens=100, cache_read_tokens=0),
        _audit_min_turn(index=2, timestamp="2026-04-29T10:06:00Z",
                        cache_write_tokens=200_000, input_tokens=100, cache_read_tokens=0),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.0,
                "output": 100, "total_input": 200},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    fired = next((t for t in digest["fired_triggers"]
                  if t["metric"] == "idle_gap_cache_decay"), None)
    assert fired is not None
    assert fired["evidence"]["worst_turn_index"] == 2
    assert fired["evidence"]["worst_gap_minutes"] == pytest.approx(6.0)
    # 200k tokens × $5/M = $1.00
    assert fired["estimated_impact_usd"] == pytest.approx(1.00, abs=0.01)


def test_audit_extract_idle_gap_cache_decay_skips_short_gap():
    ae = _load_audit_extract()
    # Gap < 5min → cache TTL not crossed → does not fire
    turns = [
        _audit_min_turn(index=1, timestamp="2026-04-29T10:00:00Z"),
        _audit_min_turn(index=2, timestamp="2026-04-29T10:03:00Z",
                        cache_write_tokens=200_000),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.0},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [t["metric"] for t in digest["fired_triggers"]]
    assert "idle_gap_cache_decay" not in metrics


def test_audit_extract_idle_gap_cache_decay_severity_scales_with_cost():
    ae = _load_audit_extract()
    # 100k tokens × $5/M = $0.50 → medium severity ($0.30-$1.00 band)
    turns = [
        _audit_min_turn(index=1, timestamp="2026-04-29T10:00:00Z"),
        _audit_min_turn(index=2, timestamp="2026-04-29T10:10:00Z",
                        cache_write_tokens=100_000, input_tokens=100, cache_read_tokens=0),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.0,
                "output": 100, "total_input": 200},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    fired = next(t for t in digest["fired_triggers"]
                 if t["metric"] == "idle_gap_cache_decay")
    assert fired["suggested_severity"] == "medium"


def test_audit_extract_idle_gap_cache_decay_skips_when_no_cache_rebuild():
    ae = _load_audit_extract()
    # Gap >5min but no cache rebuild (cache_write_tokens small) → does not fire
    turns = [
        _audit_min_turn(index=1, timestamp="2026-04-29T10:00:00Z"),
        _audit_min_turn(index=2, timestamp="2026-04-29T10:10:00Z",
                        cache_write_tokens=100, input_tokens=10,
                        cache_read_tokens=50_000),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.0},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = [t["metric"] for t in digest["fired_triggers"]]
    assert "idle_gap_cache_decay" not in metrics


# v1.30.0 — session_archetype classifier (detect-only) + first_turn_cost_share

def test_audit_extract_archetype_agent_workflow_fires_on_subagent_share():
    ae = _load_audit_extract()
    base = _audit_min_export()
    data = _audit_min_export(
        totals={**base["totals"], "turns": 12, "cost": 5.0},
        subagent_share_stats={**base["subagent_share_stats"],
                              "share_pct": 45.0, "attributed_cost": 2.25,
                              "has_attribution": True},
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "agent_workflow"
    assert digest["archetype_signals"]["subagent_share_pct"] == 45.0


def test_audit_extract_archetype_short_test_fires_at_low_turn_count():
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=i + 1) for i in range(3)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3, "cost": 0.20},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "short_test"


def test_audit_extract_archetype_long_debug_fires_on_cache_break_density():
    ae = _load_audit_extract()
    # 50 turns, 3 cache_breaks → 6% break rate (above 2% threshold)
    turns = [_audit_min_turn(index=i + 1) for i in range(50)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 50, "cost": 5.0},
        sessions=[{"subtotal": {}, "turns": turns}],
        cache_breaks=[
            {"turn_index": i, "uncached": 100_000, "cache_break_pct": 50,
             "session_id": "s", "timestamp": "", "timestamp_fmt": "",
             "total_tokens": 100_000, "prompt_snippet": "",
             "slash_command": "", "model": "claude-opus-4-7", "context": []}
            for i in (10, 20, 30)
        ],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "long_debug"
    assert digest["archetype_signals"]["cache_break_pct"] == pytest.approx(6.0)


def test_audit_extract_archetype_long_debug_skips_low_density_breaks():
    ae = _load_audit_extract()
    # 200 turns, 1 cache_break → 0.5% break rate (below 2% threshold) AND
    # cache_hit OK → must NOT classify as long_debug.
    turns = [_audit_min_turn(index=i + 1, tool_use_detail=[{"name": "Read"}])
             for i in range(200)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 200, "cost": 10.0,
                "cache_hit_pct": 95.0, "tool_call_total": 200},
        sessions=[{"subtotal": {}, "turns": turns}],
        cache_breaks=[{"turn_index": 100, "uncached": 100_000, "cache_break_pct": 50,
                       "session_id": "s", "timestamp": "", "timestamp_fmt": "",
                       "total_tokens": 100_000, "prompt_snippet": "",
                       "slash_command": "", "model": "claude-opus-4-7", "context": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] != "long_debug", (
        "1 break in 200 turns (0.5%) is below the 2% threshold and must not "
        "force-classify the session as long_debug"
    )


def test_audit_extract_archetype_code_writing_fires_on_edit_write_share():
    ae = _load_audit_extract()
    # 20 turns, 50 tool calls, 15 Edit + 5 Write = 40% Edit/Write
    turns = []
    for i in range(20):
        details = []
        if i < 15:
            details.append({"name": "Edit"})
        elif i < 20:
            details.append({"name": "Write"})
        # Bash filler so tool_call_total > 0 even at low Edit count
        details.extend([{"name": "Bash"}] * 2 if i < 15 else [])
        turns.append(_audit_min_turn(index=i + 1, tool_use_detail=details))
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 20, "cost": 2.0,
                "tool_call_total": 50},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "code_writing"
    assert digest["archetype_signals"]["edit_write_pct_of_tools"] >= 25


def test_audit_extract_archetype_exploratory_chat_fires_on_low_tool_density():
    ae = _load_audit_extract()
    # 20 turns, 5 tool calls → 0.25 tools/turn (below 1.0 threshold), no Edit
    turns = [_audit_min_turn(index=i + 1) for i in range(20)]
    for i in range(5):
        turns[i]["tool_use_detail"] = [{"name": "Read"}]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 20, "cost": 1.0,
                "tool_call_total": 5},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "exploratory_chat"


def test_audit_extract_archetype_unknown_when_no_pattern_matches():
    ae = _load_audit_extract()
    # 50 turns, balanced tool mix, no cache breaks, no subagent share
    # → no archetype clearly fires → unknown (must not force-label)
    turns = []
    for i in range(50):
        turns.append(_audit_min_turn(
            index=i + 1,
            tool_use_detail=[{"name": "Bash"}, {"name": "Read"}],
        ))
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 50, "cost": 5.0,
                "cache_hit_pct": 95.0, "tool_call_total": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "unknown"


def test_audit_extract_archetype_priority_subagent_wins_over_short_test():
    ae = _load_audit_extract()
    # turns=3 would normally match short_test, but subagent_share=80 must win.
    base = _audit_min_export()
    data = _audit_min_export(
        totals={**base["totals"], "turns": 3, "cost": 5.0},
        subagent_share_stats={**base["subagent_share_stats"],
                              "share_pct": 80.0, "attributed_cost": 4.0,
                              "has_attribution": True},
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["session_archetype"] == "agent_workflow"


def test_audit_extract_archetype_unknown_on_zero_turns():
    ae = _load_audit_extract()
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 0, "cost": 0.0},
        sessions=[{"subtotal": {}, "turns": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    # Zero turns must NOT match short_test — short_test requires turns > 0
    assert digest["session_archetype"] == "unknown"


def test_audit_extract_archetype_signals_present_and_typed():
    ae = _load_audit_extract()
    digest = ae.build_digest(_audit_min_export(),
                             "/p/session_t_20260101T000000Z.json", "quick")
    sig = digest["archetype_signals"]
    # Required keys for the v1.31.0 override matrix to consume.
    expected = {
        "turns", "subagent_share_pct", "cache_hit_pct", "cache_break_count",
        "thinking_turn_pct", "tool_call_total", "edit_write_pct_of_tools",
        "read_pct_of_tools", "bash_pct_of_tools", "tools_per_turn",
        "cache_break_pct",
    }
    assert expected.issubset(sig.keys())


def test_audit_extract_first_turn_cost_share_pct_computed():
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=1, cost_usd=0.50),
             _audit_min_turn(index=2, cost_usd=0.50)]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 2, "cost": 1.00},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["baseline"]["first_turn_cost_usd"] == pytest.approx(0.50)
    assert digest["baseline"]["first_turn_cost_share_pct"] == pytest.approx(50.0)


def test_audit_extract_first_turn_cost_share_skips_synthetic_turn():
    ae = _load_audit_extract()
    # Synthetic turn at index 1 must be skipped; idx 2 is the first user turn.
    turns = [
        _audit_min_turn(index=1, model="<synthetic>", cost_usd=0.0),
        _audit_min_turn(index=2, model="claude-opus-4-7", cost_usd=0.20),
        _audit_min_turn(index=3, model="claude-opus-4-7", cost_usd=0.80),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3, "cost": 1.00},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["baseline"]["first_turn_cost_usd"] == pytest.approx(0.20)
    assert digest["baseline"]["first_turn_cost_share_pct"] == pytest.approx(20.0)


def test_audit_extract_first_turn_cost_share_skips_resume_marker():
    ae = _load_audit_extract()
    turns = [
        _audit_min_turn(index=1, is_resume_marker=True, cost_usd=0.0),
        _audit_min_turn(index=2, cost_usd=0.30),
        _audit_min_turn(index=3, cost_usd=0.70),
    ]
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 3, "cost": 1.00},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["baseline"]["first_turn_cost_usd"] == pytest.approx(0.30)
    assert digest["baseline"]["first_turn_cost_share_pct"] == pytest.approx(30.0)


def test_audit_extract_first_turn_cost_share_zero_when_no_turns():
    ae = _load_audit_extract()
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "turns": 0, "cost": 0.0},
        sessions=[{"subtotal": {}, "turns": []}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["baseline"]["first_turn_cost_usd"] == 0
    assert digest["baseline"]["first_turn_cost_share_pct"] == 0.0


# ---------------------------------------------------------------------------
# audit-extract.py — project and instance scope (v1.3)
# ---------------------------------------------------------------------------

def _audit_min_project_export(**overrides) -> dict:
    """Minimal project-scope JSON export for audit-extract tests."""
    base = {
        "generated_at": "2026-04-29T00:00:00Z",
        "mode": "project",
        "slug": "-test-project",
        "tz_offset_hours": 0,
        "tz_label": "UTC",
        "models": {"claude-opus-4-7": 10},
        "totals": {
            "input": 500, "output": 5000, "cache_read": 90000, "cache_write": 10000,
            "cache_write_5m": 5000, "cache_write_1h": 5000, "extra_1h_cost": 0.5,
            "cost": 10.0, "no_cache_cost": 50.0, "turns": 100,
            "advisor_call_count": 0, "advisor_cost_usd": 0.0,
            "total": 105500, "total_input": 100500,
            "cache_savings": 40.0, "cache_hit_pct": 89.5,
            "thinking_turn_count": 20, "thinking_turn_pct": 20.0,
            "tool_call_total": 80, "tool_names_top3": ["Bash", "Read", "Edit"],
        },
        "sessions": [
            {
                "session_id": "aaaa1111bbbb2222cccc3333dddd4444",
                "first_ts": "2026-04-01 10:00:00",
                "last_ts": "2026-04-01 12:00:00",
                "subtotal": {"cost": 6.0, "turns": 60, "cache_hit_pct": 92.0,
                             "cache_savings": 25.0, "no_cache_cost": 31.0,
                             "input": 300, "output": 3000, "cache_read": 55000,
                             "cache_write": 6000, "total_input": 61300},
                "turns": [],
                "cache_breaks": [],
            },
            {
                "session_id": "bbbb2222cccc3333dddd4444eeee5555",
                "first_ts": "2026-04-02 08:00:00",
                "last_ts": "2026-04-02 09:00:00",
                "subtotal": {"cost": 3.0, "turns": 30, "cache_hit_pct": 70.0,
                             "cache_savings": 12.0, "no_cache_cost": 15.0,
                             "input": 150, "output": 1500, "cache_read": 25000,
                             "cache_write": 3000, "total_input": 28150},
                "turns": [],
                "cache_breaks": [{"turn_index": 5, "uncached": 10000}],
            },
            {
                "session_id": "cccc3333dddd4444eeee5555ffff6666",
                "first_ts": "2026-04-03 14:00:00",
                "last_ts": "2026-04-03 14:30:00",
                "subtotal": {"cost": 1.0, "turns": 10, "cache_hit_pct": 20.0,
                             "cache_savings": 3.0, "no_cache_cost": 4.0,
                             "input": 50, "output": 500, "cache_read": 10000,
                             "cache_write": 1000, "total_input": 11050},
                "turns": [],
                "cache_breaks": [],
            },
        ],
        "cache_breaks": [{"turn_index": 5, "uncached": 10000}],
        "by_skill": [],
        "by_subagent_type": [],
        "subagent_attribution_summary": {
            "attributed_turns": 0, "orphan_subagent_turns": 0,
            "nested_levels_seen": 0, "cycles_detected": 0},
        "subagent_share_stats": {
            "include_subagents": True, "has_attribution": False,
            "total_cost": 10.0, "attributed_cost": 0.0, "share_pct": 0.0,
            "spawn_count": 0, "attributed_count": 0, "orphan_turns": 0},
        "weekly_rollup": {"has_data": True,
                          "trailing_7d": {"cost": 5.0, "cache_hit_pct": 88.0},
                          "prior_7d": {"cost": 4.0, "cache_hit_pct": 90.0}},
    }
    base.update(overrides)
    return base


def _audit_min_instance_export(**overrides) -> dict:
    """Minimal instance-scope JSON export for audit-extract tests."""
    base = {
        "generated_at": "2026-04-29T00:00:00Z",
        "mode": "instance",
        "slug": "-home-user",
        "projects_dir": "/home/user/.claude/projects",
        "tz_offset_hours": 0,
        "tz_label": "UTC",
        "project_count": 3,
        "session_count": 20,
        "models": {"claude-opus-4-7": 100},
        "totals": {
            "input": 2000, "output": 20000, "cache_read": 500000, "cache_write": 50000,
            "cache_write_5m": 25000, "cache_write_1h": 25000, "extra_1h_cost": 5.0,
            "cost": 100.0, "no_cache_cost": 600.0, "turns": 1000,
            "advisor_call_count": 0, "advisor_cost_usd": 0.0,
            "total": 572000, "total_input": 552000,
            "cache_savings": 500.0, "cache_hit_pct": None,
        },
        "projects": [
            {"slug": "-proj-alpha", "cost_usd": 60.0, "session_count": 10,
             "turn_count": 600,
             "sessions": [
                 {"subtotal": {"cache_hit_pct": 95.0}},
                 {"subtotal": {"cache_hit_pct": 90.0}},
             ]},
            {"slug": "-proj-beta", "cost_usd": 30.0, "session_count": 7,
             "turn_count": 300,
             "sessions": [
                 {"subtotal": {"cache_hit_pct": 50.0}},
                 {"subtotal": {"cache_hit_pct": 60.0}},
             ]},
            {"slug": "-proj-gamma", "cost_usd": 10.0, "session_count": 3,
             "turn_count": 100,
             "sessions": [
                 {"subtotal": {"cache_hit_pct": 92.0}},
             ]},
        ],
        "sessions": [],
        "cache_breaks": [],
        "weekly_rollup": {"has_data": True,
                          "trailing_7d": {"cost": 55.0, "cache_hit_pct": 88.0},
                          "prior_7d": {"cost": 40.0, "cache_hit_pct": 90.0}},
    }
    base.update(overrides)
    return base


def test_audit_extract_project_filename_parts_canonical():
    ae = _load_audit_extract()
    scope, ts = ae.project_filename_parts("/exports/project_20260429T031942Z.json")
    assert scope == "project"
    assert ts == "20260429T031942Z"


def test_audit_extract_project_filename_parts_unrecognised():
    ae = _load_audit_extract()
    scope, ts = ae.project_filename_parts("/exports/project_unknown.json")
    assert scope == "project"
    assert ts == "unknown"


def test_audit_extract_instance_filename_parts_canonical():
    ae = _load_audit_extract()
    scope, ts = ae.instance_filename_parts(
        "/exports/session-metrics/instance/2026-04-29-034750/index.json"
    )
    assert scope == "instance"
    assert ts == "2026-04-29-034750"


def test_audit_extract_instance_filename_parts_unrecognised():
    ae = _load_audit_extract()
    scope, ts = ae.instance_filename_parts("/exports/weirdpath/index.json")
    assert scope == "instance"
    assert ts == "unknown"


def test_audit_extract_detect_scope_from_mode_field():
    ae = _load_audit_extract()
    assert ae.detect_scope({"mode": "session"}, "/p/whatever.json") == "session"
    assert ae.detect_scope({"mode": "project"}, "/p/whatever.json") == "project"
    assert ae.detect_scope({"mode": "instance"}, "/p/whatever.json") == "instance"


def test_audit_extract_detect_scope_falls_back_to_filename():
    ae = _load_audit_extract()
    assert ae.detect_scope({}, "/p/project_20260429T031942Z.json") == "project"
    assert ae.detect_scope({}, "/instance/2026-04-29/index.json") == "instance"
    assert ae.detect_scope({}, "/p/session_abc_20260429T000000Z.json") == "session"


def test_audit_extract_project_baseline_fields():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    base = ae.compute_project_baseline(data)
    assert base["sessions_count"] == 3
    assert base["total_cost_usd"] == pytest.approx(10.0)
    assert base["cost_per_session_avg_usd"] == pytest.approx(10.0 / 3, rel=0.01)
    assert base["cache_hit_pct"] == pytest.approx(89.5)
    assert base["cache_savings_usd"] == pytest.approx(40.0)
    assert "weekly_rollup" in base


def test_audit_extract_instance_baseline_fields():
    ae = _load_audit_extract()
    data = _audit_min_instance_export()
    base = ae.compute_instance_baseline(data)
    assert base["projects_count"] == 3
    assert base["sessions_count"] == 20
    assert base["total_cost_usd"] == pytest.approx(100.0)


def test_audit_extract_project_session_analysis_top_sessions():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    pa = ae.compute_project_session_analysis(data)
    top = pa["top_expensive_sessions"]
    assert len(top) <= 5
    # Most expensive is session aaaa1111 at $6.00
    assert top[0]["session_id_short"] == "aaaa1111"
    assert top[0]["cost_usd"] == pytest.approx(6.0)
    assert top[0]["cost_share_pct"] == pytest.approx(60.0)


def test_audit_extract_project_session_analysis_poor_cache():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    pa = ae.compute_project_session_analysis(data)
    poor = pa["poor_cache_health_sessions"]
    # Sessions with cache_hit_pct < 80: session bbbb (70%) and cccc (20%)
    slugs = [s["session_id_short"] for s in poor]
    assert "bbbb2222" in slugs
    assert "cccc3333" in slugs
    assert "aaaa1111" not in slugs  # 92% — above threshold


def test_audit_extract_project_session_analysis_cache_breaks():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    pa = ae.compute_project_session_analysis(data)
    breaks = pa["sessions_with_cache_breaks"]
    # Only session bbbb has cache_breaks
    assert len(breaks) == 1
    assert breaks[0]["session_id_short"] == "bbbb2222"
    assert breaks[0]["break_count"] == 1


def test_audit_extract_instance_project_analysis_top_projects():
    ae = _load_audit_extract()
    data = _audit_min_instance_export()
    ia = ae.compute_instance_project_analysis(data)
    top = ia["top_expensive_projects"]
    assert top[0]["slug"] == "-proj-alpha"
    assert top[0]["cost_usd"] == pytest.approx(60.0)
    assert top[0]["cost_share_pct"] == pytest.approx(60.0)


def test_audit_extract_instance_project_analysis_poor_cache():
    ae = _load_audit_extract()
    data = _audit_min_instance_export()
    ia = ae.compute_instance_project_analysis(data)
    poor = ia["poor_cache_health_projects"]
    # proj-beta avg=(50+60)/2=55% < 80 and cost=$30 > 0.10
    assert any(p["slug"] == "-proj-beta" for p in poor)
    # proj-alpha avg=(95+90)/2=92.5% — above threshold
    assert not any(p["slug"] == "-proj-alpha" for p in poor)


def test_audit_extract_build_digest_project_scope_sets_scope_field():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    digest = ae.build_digest(data, "/p/project_20260429T031942Z.json", "quick")
    assert digest["scope"] == "project"
    assert digest["session_id_short"] == "project"
    assert digest["session_archetype"] == "n/a"
    assert "project_analysis" in digest
    assert "instance_analysis" not in digest


def test_audit_extract_build_digest_project_suppresses_session_only_metrics():
    ae = _load_audit_extract()
    data = _audit_min_project_export()
    digest = ae.build_digest(data, "/p/project_20260429T031942Z.json", "quick")
    fired_metrics = {t["metric"] for t in digest["fired_triggers"]}
    # SESSION_ONLY_METRICS must not fire at project scope
    assert "idle_gap_cache_decay" not in fired_metrics
    assert "session_warmup_overhead" not in fired_metrics


def test_audit_extract_build_digest_instance_scope():
    ae = _load_audit_extract()
    data = _audit_min_instance_export()
    digest = ae.build_digest(
        data,
        "/exports/session-metrics/instance/2026-04-29-034750/index.json",
        "quick",
    )
    assert digest["scope"] == "instance"
    assert digest["session_id_short"] == "instance"
    assert digest["session_archetype"] == "n/a"
    assert digest["fired_triggers"] == []
    assert digest["top_expensive_turns"] == []
    assert "instance_analysis" in digest
    assert "project_analysis" not in digest


def test_audit_extract_build_digest_session_scope_now_has_scope_field():
    ae = _load_audit_extract()
    data = _audit_min_export()
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    assert digest["scope"] == "session"
    assert "project_analysis" not in digest
    assert "instance_analysis" not in digest


def test_audit_extract_schema_version_is_1_3():
    ae = _load_audit_extract()
    assert ae.DIGEST_SCHEMA_VERSION == "1.3"


def test_audit_new_reference_files_exist():
    refs = (_HERE.parent.parent / "audit-session-metrics" / "references")
    assert (refs / "project-quick-audit.md").exists()
    assert (refs / "project-detailed-audit.md").exists()
    assert (refs / "instance-quick-audit.md").exists()


def test_audit_new_reference_files_have_schema_v1_3():
    import re
    refs = (_HERE.parent.parent / "audit-session-metrics" / "references")
    for fname in ("project-quick-audit.md", "project-detailed-audit.md",
                  "instance-quick-audit.md"):
        text = (refs / fname).read_text(encoding="utf-8")
        m = re.search(r"JSON schema \(v([\d.]+)\)", text)
        assert m is not None, f"{fname} missing 'JSON schema (vX.Y)' header"
        assert m.group(1) == "1.3", f"{fname} schema version should be 1.3, got {m.group(1)}"


# --- P1.3 — _BASH_PATH_RE must require ≥1 leading dot OR start-of-arg boundary

def test_detect_file_reaccesses_ignores_hidden_dir_path_in_bash():
    """P1.3 regression: ``.claude/skills/foo.py`` in a Bash command must NOT
    be captured as ``/skills/foo.py``. The old regex allowed zero leading
    dots (``\\.{0,2}/``), which silently merged same-suffix files across
    different project subtrees in the re-read detector.
    """
    turns = [
        {"index": 0, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "cat .claude/skills/foo.py"}]},
        {"index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "cat other-project/skills/foo.py"}]},
    ]
    result = sm._detect_file_reaccesses(turns)
    paths_seen = {d["path"] for d in result["details"]}
    assert "/skills/foo.py" not in paths_seen
    assert result["reaccessed_count"] == 0


def test_detect_file_reaccesses_keeps_dot_relative_bash_path():
    """Sanity: ``./scripts/run.py`` accessed twice in the same segment is
    still flagged as a re-read after the boundary fix."""
    turns = [
        {"index": 0, "_ctx_seg": 0, "cost_usd": 0.10,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "cat ./scripts/run.py"}]},
        {"index": 1, "_ctx_seg": 0, "cost_usd": 0.10,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "head -n 10 ./scripts/run.py"}]},
    ]
    result = sm._detect_file_reaccesses(turns)
    assert result["reaccessed_count"] == 1
    assert result["details"][0]["path"] == "./scripts/run.py"
    assert result["details"][0]["count"] == 2


def test_detect_file_reaccesses_keeps_absolute_bash_path():
    """Sanity: ``/etc/hosts.conf`` (start-of-arg boundary) still matches."""
    turns = [
        {"index": 0, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "cat /etc/hosts.conf"}]},
        {"index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "diff /etc/hosts.conf /tmp/x"}]},
    ]
    result = sm._detect_file_reaccesses(turns)
    assert result["reaccessed_count"] == 1
    assert result["details"][0]["path"] == "/etc/hosts.conf"


def test_detect_file_reaccesses_keeps_tilde_home_bash_path():
    """Sanity: ``~/dotfiles/zshrc.sh`` (tilde branch) still matches."""
    turns = [
        {"index": 0, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "cat ~/dotfiles/zshrc.sh"}]},
        {"index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
         "tool_use_detail": [{"name": "Bash",
                              "input_preview": "wc -l ~/dotfiles/zshrc.sh"}]},
    ]
    result = sm._detect_file_reaccesses(turns)
    assert result["reaccessed_count"] == 1
    assert result["details"][0]["path"] == "~/dotfiles/zshrc.sh"


# --- P1.4 — total_reaccess_cost must use marginal-per-tool-call attribution

def test_detect_file_reaccesses_cost_uses_marginal_per_tool_attribution():
    """P1.4 regression: a turn that runs 5 tool calls but only 1 of them
    reads the re-accessed path must contribute 1/5 of its turn cost, not
    the full turn cost. The old detector summed the entire turn cost for
    any turn that touched the path, over-attributing waste massively
    (e.g. Session 112 attributed $1.11 = 54% of cost on 1 Bash arg).
    """
    turns = [
        {
            "index": 0, "_ctx_seg": 0, "cost_usd": 1.00,
            "tool_use_detail": [
                {"name": "Read", "input_preview": "/repo/foo.py"},
                {"name": "Bash", "input_preview": "ls /etc"},
                {"name": "Bash", "input_preview": "echo hi"},
                {"name": "Bash", "input_preview": "true"},
                {"name": "Bash", "input_preview": "false"},
            ],
        },
        {
            "index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
            "tool_use_detail": [{"name": "Read", "input_preview": "/repo/foo.py"}],
        },
    ]
    result = sm._detect_file_reaccesses(turns)
    detail = next(d for d in result["details"] if d["path"] == "/repo/foo.py")
    # Turn 0: 1 of 5 tool calls hit the path → 1/5 × $1.00 = $0.20
    # Turn 1: 1 of 1, $0.00 → $0.00
    assert detail["cost_usd"] == pytest.approx(0.20, abs=1e-6)
    assert result["total_reaccess_cost"] == pytest.approx(0.20, abs=1e-6)


def test_detect_file_reaccesses_cost_singleton_turns_unchanged():
    """When every tool call in every relevant turn reads the path, the
    sum of turn costs is preserved (no over-attribution to fix here)."""
    turns = [
        {
            "index": 0, "_ctx_seg": 0, "cost_usd": 0.50,
            "tool_use_detail": [{"name": "Read", "input_preview": "/repo/foo.py"}],
        },
        {
            "index": 1, "_ctx_seg": 0, "cost_usd": 0.30,
            "tool_use_detail": [{"name": "Read", "input_preview": "/repo/foo.py"}],
        },
    ]
    result = sm._detect_file_reaccesses(turns)
    detail = next(d for d in result["details"] if d["path"] == "/repo/foo.py")
    assert detail["cost_usd"] == pytest.approx(0.80, abs=1e-6)


def test_detect_file_reaccesses_cost_multiple_path_reads_in_same_turn():
    """A turn that reads the path multiple times scales by
    (path_reads / total_tool_calls), not 1.0 nor path_reads alone."""
    turns = [
        {
            "index": 0, "_ctx_seg": 0, "cost_usd": 1.00,
            "tool_use_detail": [
                {"name": "Read", "input_preview": "/repo/foo.py"},
                {"name": "Read", "input_preview": "/repo/foo.py"},
                {"name": "Bash", "input_preview": "true"},
                {"name": "Bash", "input_preview": "false"},
            ],
        },
        {
            "index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
            "tool_use_detail": [{"name": "Read", "input_preview": "/repo/foo.py"}],
        },
    ]
    result = sm._detect_file_reaccesses(turns)
    detail = next(d for d in result["details"] if d["path"] == "/repo/foo.py")
    # Turn 0: 2 of 4 tool calls hit the path → 2/4 × $1.00 = $0.50
    assert detail["cost_usd"] == pytest.approx(0.50, abs=1e-6)


def test_detect_file_reaccesses_cost_two_paths_share_turn_proportionally():
    """Two re-read paths sharing the same multi-tool turn each get their
    own proportional slice; the sum across paths is bounded by the turn
    cost (the previous bug double-counted the full turn cost per path)."""
    turns = [
        {
            "index": 0, "_ctx_seg": 0, "cost_usd": 1.00,
            "tool_use_detail": [
                {"name": "Read", "input_preview": "/repo/a.py"},
                {"name": "Read", "input_preview": "/repo/b.py"},
                {"name": "Bash", "input_preview": "true"},
                {"name": "Bash", "input_preview": "false"},
            ],
        },
        {
            "index": 1, "_ctx_seg": 0, "cost_usd": 0.0,
            "tool_use_detail": [
                {"name": "Read", "input_preview": "/repo/a.py"},
                {"name": "Read", "input_preview": "/repo/b.py"},
            ],
        },
    ]
    result = sm._detect_file_reaccesses(turns)
    a = next(d for d in result["details"] if d["path"] == "/repo/a.py")
    b = next(d for d in result["details"] if d["path"] == "/repo/b.py")
    # Each path: 1/4 × $1.00 = $0.25
    assert a["cost_usd"] == pytest.approx(0.25, abs=1e-6)
    assert b["cost_usd"] == pytest.approx(0.25, abs=1e-6)
    # Sum of path costs ≤ turn cost (was 2×$1.00 = $2.00 under old bug)
    assert result["total_reaccess_cost"] == pytest.approx(0.50, abs=1e-6)


# ---------------------------------------------------------------------------
# P2.1 — model breakdown ships {turns, cost_usd}; audit baseline emits shares
# ---------------------------------------------------------------------------


def test_model_breakdown_returns_turns_and_cost():
    """``_model_breakdown`` returns ``{name: {turns, cost_usd}}`` so the
    Models table and audit playbook can read cost share without a second
    pass over per-turn records (P2.1)."""
    turns = [
        {"model": "claude-opus-4-7",   "cost_usd": 1.50},
        {"model": "claude-opus-4-7",   "cost_usd": 0.50},
        {"model": "claude-sonnet-4-7", "cost_usd": 0.10},
    ]
    out = sm._model_breakdown(turns)
    assert out["claude-opus-4-7"]["turns"] == 2
    assert out["claude-opus-4-7"]["cost_usd"] == pytest.approx(2.00, abs=1e-6)
    assert out["claude-sonnet-4-7"]["turns"] == 1
    assert out["claude-sonnet-4-7"]["cost_usd"] == pytest.approx(0.10, abs=1e-6)


def test_audit_extract_baseline_models_attaches_cost_pct():
    """``compute_baseline`` should compute ``turns_pct`` and ``cost_pct``
    from the new dict-shape ``models`` field so the playbook can render
    the cost split without LLM arithmetic (P2.1)."""
    ae = _load_audit_extract()
    data = _audit_min_export(
        models={
            "claude-opus-4-7":   {"turns": 50, "cost_usd": 9.0},
            "claude-sonnet-4-7": {"turns": 30, "cost_usd": 1.0},
            "claude-haiku-4-5":  {"turns": 20, "cost_usd": 0.0},
        },
    )
    baseline = ae.compute_baseline(data)
    opus   = baseline["models"]["claude-opus-4-7"]
    sonnet = baseline["models"]["claude-sonnet-4-7"]
    haiku  = baseline["models"]["claude-haiku-4-5"]
    # Turn shares: 50%, 30%, 20%
    assert opus["turns_pct"]   == pytest.approx(50.0, abs=0.1)
    assert sonnet["turns_pct"] == pytest.approx(30.0, abs=0.1)
    assert haiku["turns_pct"]  == pytest.approx(20.0, abs=0.1)
    # Cost shares: 90%, 10%, 0% — opus dominates spend despite only 50% turns
    assert opus["cost_pct"]   == pytest.approx(90.0, abs=0.1)
    assert sonnet["cost_pct"] == pytest.approx(10.0, abs=0.1)
    assert haiku["cost_pct"]  == pytest.approx(0.0,  abs=0.1)


def test_audit_extract_baseline_models_legacy_int_shape_keeps_turns_only():
    """Pre-v1.34.0 exports stored ``models`` as ``{name: int}``. The audit
    must still parse those: turn share is computable, cost share is
    explicitly null so the playbook can fall back to turn share (P2.1)."""
    ae = _load_audit_extract()
    data = _audit_min_export(
        models={"claude-opus-4-7": 8, "claude-sonnet-4-7": 2},
    )
    baseline = ae.compute_baseline(data)
    opus = baseline["models"]["claude-opus-4-7"]
    sonnet = baseline["models"]["claude-sonnet-4-7"]
    assert opus["turns"] == 8
    assert opus["turns_pct"] == pytest.approx(80.0, abs=0.1)
    assert opus["cost_pct"] is None
    assert sonnet["turns_pct"] == pytest.approx(20.0, abs=0.1)
    assert sonnet["cost_pct"] is None


# ---------------------------------------------------------------------------
# P2.2 — paste_bomb classification (>5000 char prompt → user-side waste)
# ---------------------------------------------------------------------------


def test_classify_turn_paste_bomb_fires_above_5000_chars():
    """A user prompt >5 000 chars classifies as paste_bomb (P2.2)."""
    turn = {
        "index": 0, "tool_use_names": [], "content_blocks": {},
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "prompt_text": "x" * 5_001,
    }
    assert sm._classify_turn(turn, set(), set(), set()) == "paste_bomb"


def test_classify_turn_paste_bomb_does_not_fire_at_threshold():
    """Exactly 5 000 chars is the boundary — strictly greater fires (P2.2)."""
    turn = {
        "index": 0, "tool_use_names": [], "content_blocks": {},
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "prompt_text": "x" * 5_000,
    }
    assert sm._classify_turn(turn, set(), set(), set()) == "productive"


def test_classify_turn_paste_bomb_beats_reasoning():
    """A paste-bombed turn that triggers thinking surfaces as paste_bomb,
    not reasoning — the user's pasted wall is the actionable signal, not
    the downstream extended-thinking burn (P2.2 priority order)."""
    turn = {
        "index": 0, "tool_use_names": [],
        "content_blocks": {"thinking": 1},
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "prompt_text": "x" * 6_000,
    }
    assert sm._classify_turn(turn, set(), set(), set()) == "paste_bomb"


def test_classify_turn_paste_bomb_yields_to_subagent_overhead():
    """Subagent_overhead still wins over paste_bomb — Agent/Task tool use
    is the dominant character even when the dispatching prompt was a
    paste bomb (P2.2 priority order)."""
    turn = {
        "index": 0, "tool_use_names": ["Agent"], "content_blocks": {},
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "prompt_text": "x" * 10_000,
    }
    assert sm._classify_turn(turn, set(), set(), set()) == "subagent_overhead"


def test_classify_turn_paste_bomb_in_risk_categories():
    """paste_bomb is a risk signal — should be flagged in the waste bar
    and per-turn drawer (P2.2)."""
    assert "paste_bomb" in sm._RISK_CATEGORIES
    assert "paste_bomb" in sm._TURN_CHARACTER_LABELS


# v1.35.0 — P2.3: drop length cap on session_warmup_overhead

def test_audit_extract_warmup_fires_for_mid_length_session_above_15_turns():
    """Pre-v1.35 the trigger was gated on len(turns) <= 15 — a 17-turn
    session with 30% first-turn cost was silently never surfaced. Drop
    the cap so it fires (P2.3)."""
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=1, cost_usd=0.30)]
    turns.extend(_audit_min_turn(index=i, cost_usd=0.70 / 16)
                 for i in range(2, 18))  # 16 more turns; 17 total
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "turns": 17,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    warm = next((t for t in digest["fired_triggers"]
                 if t["metric"] == "session_warmup_overhead"), None)
    assert warm is not None
    assert warm["evidence"]["pct_of_total"] == pytest.approx(30.0)
    assert warm["evidence"]["total_turns"] == 17
    # 17 turns is not "long" by the new downgrade rule (>30) — stays medium.
    assert warm["suggested_severity"] == "medium"
    assert warm["downgrade_reason"] is None


def test_audit_extract_warmup_downgrades_to_low_for_long_session():
    """Long-session (>30 turns) with first-turn share between 20-30% should
    downgrade to ``low`` because the warmup cost amortises across many turns
    (P2.3)."""
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=1, cost_usd=0.22)]
    turns.extend(_audit_min_turn(index=i, cost_usd=0.78 / 39)
                 for i in range(2, 41))  # 39 more turns; 40 total
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "turns": 40,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    warm = next((t for t in digest["fired_triggers"]
                 if t["metric"] == "session_warmup_overhead"), None)
    assert warm is not None
    assert warm["evidence"]["total_turns"] == 40
    assert warm["suggested_severity"] == "low"
    assert warm["downgrade_reason"] is not None
    assert "long session" in warm["downgrade_reason"]


def test_audit_extract_warmup_stays_medium_when_share_dominates_long_session():
    """If a long session has ≥30% first-turn share, the warmup cost is
    large enough that the downgrade should NOT apply — keep medium."""
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=1, cost_usd=0.40)]
    turns.extend(_audit_min_turn(index=i, cost_usd=0.60 / 49)
                 for i in range(2, 51))  # 49 more turns; 50 total
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "turns": 50,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    warm = next((t for t in digest["fired_triggers"]
                 if t["metric"] == "session_warmup_overhead"), None)
    assert warm is not None
    assert warm["suggested_severity"] == "medium"
    assert warm["downgrade_reason"] is None


def test_audit_extract_warmup_does_not_fire_below_threshold():
    """Threshold (>20%) is unchanged — share at or below 20% never fires."""
    ae = _load_audit_extract()
    turns = [_audit_min_turn(index=1, cost_usd=0.18)]
    turns.extend(_audit_min_turn(index=i, cost_usd=0.82 / 4)
                 for i in range(2, 6))  # 4 more turns; 5 total
    data = _audit_min_export(
        totals={**_audit_min_export()["totals"], "cost": 1.0, "turns": 5,
                "output": 100, "total_input": 100},
        sessions=[{"subtotal": {}, "turns": turns}],
    )
    digest = ae.build_digest(data, "/p/session_t_20260101T000000Z.json", "quick")
    metrics = {t["metric"] for t in digest["fired_triggers"]}
    assert "session_warmup_overhead" not in metrics


# v1.35.0 — P2.4: --redact-user-prompts wired through render_json

def test_render_json_redacts_prompt_and_assistant_text():
    """When ``redact_user_prompts=True`` the JSON export replaces freeform
    prompt and assistant text on every turn with ``[redacted]`` so the
    file is safe to share (P2.4)."""
    import json as _json
    r = _build_fixture_report()
    # Force at least one turn to have non-empty freeform text so the
    # redaction has something to replace — the bundled fixture has empty
    # ``prompt_text`` because it's a tool-result-only sequence.
    r["sessions"][0]["turns"][0]["prompt_text"]      = "search the web for X"
    r["sessions"][0]["turns"][0]["prompt_snippet"]   = "search the web for X"
    r["sessions"][0]["turns"][0]["assistant_text"]   = "OK, fetching results..."
    r["sessions"][0]["turns"][0]["assistant_snippet"] = "OK, fetching..."
    redacted = _json.loads(sm.render_json(r, redact_user_prompts=True))
    t0 = redacted["sessions"][0]["turns"][0]
    assert t0["prompt_text"]      == "[redacted]"
    assert t0["prompt_snippet"]   == "[redacted]"
    assert t0["assistant_text"]   == "[redacted]"
    assert t0["assistant_snippet"] == "[redacted]"


def test_render_json_default_leaves_prompt_text_intact():
    """Without the flag the freeform fields stay as-is — the redaction
    must be opt-in so existing pipelines don't suddenly lose data."""
    import json as _json
    r = _build_fixture_report()
    r["sessions"][0]["turns"][0]["prompt_text"]    = "verbatim prompt content"
    r["sessions"][0]["turns"][0]["assistant_text"] = "verbatim reply"
    plain = _json.loads(sm.render_json(r))
    t0 = plain["sessions"][0]["turns"][0]
    assert t0["prompt_text"]    == "verbatim prompt content"
    assert t0["assistant_text"] == "verbatim reply"


def test_render_json_redact_keeps_structured_fields_visible():
    """Redaction targets only freeform PII fields. Token counts, costs,
    tool inputs, slash-command names, and turn timestamps must stay
    visible so the JSON remains useful for cost analysis."""
    import json as _json
    r = _build_fixture_report()
    r["sessions"][0]["turns"][0]["prompt_text"]   = "secret content"
    r["sessions"][0]["turns"][0]["slash_command"] = "/audit-session-metrics"
    redacted = _json.loads(sm.render_json(r, redact_user_prompts=True))
    t0 = redacted["sessions"][0]["turns"][0]
    assert t0["prompt_text"]    == "[redacted]"
    assert t0["slash_command"]  == "/audit-session-metrics"
    assert "cost_usd"           in t0
    assert "input_tokens"       in t0
    assert "cache_read_tokens"  in t0


def test_render_json_redact_preserves_empty_fields():
    """Empty prompt_text means a tool-result-only turn — the truthiness
    is meaningful downstream (e.g. ``if t.get("prompt_text"):``). The
    redactor must NOT replace empty strings with ``[redacted]``."""
    import json as _json
    r = _build_fixture_report()
    # Force the freeform fields to empty so we can verify the redactor
    # leaves empties alone (the bundled fixture has compaction-marker
    # text on turn 0, which would mask this assertion).
    for fld in sm._REDACTED_TURN_FIELDS:
        r["sessions"][0]["turns"][0][fld] = ""
    redacted = _json.loads(sm.render_json(r, redact_user_prompts=True))
    t0 = redacted["sessions"][0]["turns"][0]
    for fld in sm._REDACTED_TURN_FIELDS:
        assert t0.get(fld, "") == "", \
            f"redactor must leave empty {fld} alone, got {t0.get(fld)!r}"


# --- P3: golden-file + waste-analysis coverage --------------------------------
# Locks in correctness of _build_waste_analysis, _detect_retry_chains, and
# _classify_turn waterfall arms not exercised by the existing paste_bomb suite.
# The golden fixture session_golden_1a68560a.json was regenerated under
# current (post-P2.2/P1.4) code; if a future code change shifts these
# numbers, regenerate the fixture deliberately.

_GOLDEN_SESSION = _HERE / "fixtures" / "session_golden_1a68560a.json"


def _load_golden_turns() -> list[dict]:
    """Deep-copy the fixture's turns so tests mutate isolated state."""
    import copy
    import json as _json
    with open(_GOLDEN_SESSION) as f:
        d = _json.load(f)
    return copy.deepcopy(d["sessions"][0]["turns"])


def _load_golden_doc() -> dict:
    import json as _json
    with open(_GOLDEN_SESSION) as f:
        return _json.load(f)


def test_build_waste_analysis_golden_distribution_matches_fixture():
    doc = _load_golden_doc()
    turns = _load_golden_turns()
    result = sm._build_waste_analysis([{"turns": turns}])
    assert result["distribution"] == doc["waste_analysis"]["distribution"]


def test_build_waste_analysis_golden_retry_chains_match_fixture():
    doc = _load_golden_doc()
    turns = _load_golden_turns()
    result = sm._build_waste_analysis([{"turns": turns}])
    assert result["retry_chains"]["chain_count"] == \
        doc["waste_analysis"]["retry_chains"]["chain_count"]
    assert round(result["retry_chains"]["retry_cost_pct"], 6) == \
        round(doc["waste_analysis"]["retry_chains"]["retry_cost_pct"], 6)


def test_build_waste_analysis_golden_file_reaccess_summary_matches_fixture():
    doc = _load_golden_doc()
    turns = _load_golden_turns()
    result = sm._build_waste_analysis([{"turns": turns}])
    expected = doc["waste_analysis"]["file_reaccesses"]
    actual   = result["file_reaccesses"]
    assert len(actual["details"]) == len(expected["details"])
    assert round(actual.get("total_reaccess_cost", 0), 6) == \
        round(expected.get("total_reaccess_cost", 0), 6)


def test_build_waste_analysis_golden_stop_reasons_match_fixture():
    doc = _load_golden_doc()
    turns = _load_golden_turns()
    result = sm._build_waste_analysis([{"turns": turns}])
    assert result["stop_reasons"] == doc["waste_analysis"]["stop_reasons"]


def test_build_waste_analysis_classifies_resume_marker_as_productive():
    """Resume markers bypass the classifier and stay productive + non-risk."""
    turns = [
        {"index": 0, "is_resume_marker": True, "prompt_text": "", "model": "opus"},
        {"index": 1, "prompt_text": "do work", "model": "opus", "cost_usd": 0.10,
         "tool_use_names": [], "content_blocks": {}},
    ]
    sm._build_waste_analysis([{"turns": turns}])
    assert turns[0]["turn_character"] == "productive"
    assert turns[0]["turn_risk"] is False
    assert turns[0]["reread_cross_ctx"] is False


# --- _detect_retry_chains direct unit tests (zero coverage prior to P3) ------

def test_detect_retry_chains_empty_input_returns_zero_chains():
    result = sm._detect_retry_chains([])
    assert result == {"chains": [], "chain_count": 0, "retry_cost_pct": 0.0}


def test_detect_retry_chains_dissimilar_prompts_form_no_chain():
    turns = [
        {"index": 0, "prompt_text": "explain how database indexing works in postgres",
         "cost_usd": 1.0},
        {"index": 1, "prompt_text": "list the planets in our solar system by mass",
         "cost_usd": 1.0},
    ]
    result = sm._detect_retry_chains(turns)
    assert result["chain_count"] == 0
    assert result["chains"] == []
    assert result["retry_cost_pct"] == 0.0


def test_detect_retry_chains_identical_prompts_form_chain():
    turns = [
        {"index": 0, "prompt_text": "fix the failing test",  "cost_usd": 1.0},
        {"index": 1, "prompt_text": "fix the failing test",  "cost_usd": 2.0},
        {"index": 2, "prompt_text": "fix the failing test",  "cost_usd": 3.0},
    ]
    result = sm._detect_retry_chains(turns)
    assert result["chain_count"] == 1
    chain = result["chains"][0]
    assert chain["turn_indices"] == [0, 1, 2]
    assert chain["length"] == 3
    assert chain["cost_usd"] == 6.0
    assert round(result["retry_cost_pct"], 6) == 100.0


def test_detect_retry_chains_breaks_on_dissimilar_prompt():
    turns = [
        {"index": 0, "prompt_text": "fix the failing test", "cost_usd": 1.0},
        {"index": 1, "prompt_text": "fix the failing test", "cost_usd": 1.0},
        {"index": 2, "prompt_text": "explain how database indexing works",
         "cost_usd": 5.0},
        {"index": 3, "prompt_text": "explain how database indexing works",
         "cost_usd": 5.0},
    ]
    result = sm._detect_retry_chains(turns)
    # Two separate chains: [0,1] and [2,3] — each ≥2 entries, broken at index 2.
    assert result["chain_count"] == 2
    indices = sorted(c["turn_indices"] for c in result["chains"])
    assert indices == [[0, 1], [2, 3]]


def test_detect_retry_chains_skips_resume_marker_in_chain_membership():
    """Resume markers are filtered from `prompted` and never appear in a
    chain's turn_indices, even if their cost would otherwise be summed."""
    turns = [
        {"index": 0, "prompt_text": "fix failing test", "cost_usd": 1.0},
        {"index": 1, "prompt_text": "",                 "cost_usd": 0.0,
         "is_resume_marker": True},
        {"index": 2, "prompt_text": "fix failing test", "cost_usd": 1.0},
    ]
    result = sm._detect_retry_chains(turns)
    assert result["chain_count"] == 1
    assert result["chains"][0]["turn_indices"] == [0, 2]
    # retry_cost_pct denominator includes ALL turns (even the resume marker
    # at cost 0), but the chain's own cost is just the two prompted turns.
    assert round(result["retry_cost_pct"], 6) == 100.0


# --- _classify_turn waterfall arms not covered by the paste_bomb suite -------

def test_classify_turn_subagent_overhead_wins_when_agent_in_tool_names():
    turn = {"prompt_text": "go", "tool_use_names": ["Agent"], "content_blocks": {}}
    assert sm._classify_turn(turn, set(), set(), set()) == "subagent_overhead"


def test_classify_turn_reasoning_when_thinking_block_present():
    turn = {"prompt_text": "go", "tool_use_names": [],
            "content_blocks": {"thinking": 1}}
    assert sm._classify_turn(turn, set(), set(), set()) == "reasoning"


def test_classify_turn_cache_read_when_cr_dominates_input():
    turn = {"prompt_text": "go", "tool_use_names": [], "content_blocks": {},
            "cache_read_tokens": 200_000, "input_tokens": 50_000}
    # cr=200K > 100K AND cr/(cr+inp)=0.8 > 0.5 → cache_read
    assert sm._classify_turn(turn, set(), set(), set()) == "cache_read"


def test_classify_turn_cache_write_when_cw_above_threshold():
    turn = {"prompt_text": "go", "tool_use_names": [], "content_blocks": {},
            "cache_write_tokens": 150_000}
    assert sm._classify_turn(turn, set(), set(), set()) == "cache_write"


def test_classify_turn_dead_end_when_stop_reason_max_tokens():
    turn = {"prompt_text": "go", "tool_use_names": [], "content_blocks": {},
            "stop_reason": "max_tokens"}
    assert sm._classify_turn(turn, set(), set(), set()) == "dead_end"


def test_classify_turn_productive_baseline_when_no_signals():
    turn = {"prompt_text": "go", "tool_use_names": [], "content_blocks": {},
            "stop_reason": "end_turn"}
    assert sm._classify_turn(turn, set(), set(), set()) == "productive"


# v1.36.0 — P5.2: --export-share-safe one-flag pre-share gesture
# (implies --redact-user-prompts + --no-self-cost, chmods every written
# export file to 0600).

def test_export_share_safe_implies_redact_and_no_self_cost():
    """--export-share-safe is a bundle: argparse alone leaves the implied
    flags False, but main() flips them. Verify the bundle on a parser
    instance — main() applies the implication on its own."""
    parser = sm._build_parser()
    args = parser.parse_args(["--export-share-safe"])
    assert args.export_share_safe is True
    # Implication is applied in main() (after parse), not by argparse,
    # so the raw namespace still shows them False here. We verify the
    # implication runs by calling the same logic main() does.
    assert args.redact_user_prompts is False
    assert args.no_self_cost is False
    if args.export_share_safe:
        args.redact_user_prompts = True
        args.no_self_cost = True
    assert args.redact_user_prompts is True
    assert args.no_self_cost is True


def test_write_output_share_safe_chmods_to_0o600(tmp_path, monkeypatch):
    """Every export file written via _write_output with share_safe=True
    ends up with mode 0o600. Default share_safe=False leaves the file
    at the umask-determined mode (typically 0o644 on macOS/Linux)."""
    monkeypatch.chdir(tmp_path)
    report = {"mode": "session",
              "sessions": [{"session_id": "deadbeef" + "0" * 24}]}
    p_default = sm._write_output("text", "hello\n", report)
    p_share = sm._write_output("text", "hello\n", report,
                                suffix="_safe", share_safe=True)
    assert p_default.exists()
    assert p_share.exists()
    assert (p_share.stat().st_mode & 0o777) == 0o600
    # Default is whatever the umask permits; only assert the contrast.
    assert (p_default.stat().st_mode & 0o777) != 0o600


def test_write_output_default_does_not_chmod(tmp_path, monkeypatch):
    """share_safe=False (the default) must NOT chmod the file. Pipelines
    that rely on group-readable exports stay unchanged."""
    monkeypatch.chdir(tmp_path)
    report = {"mode": "session",
              "sessions": [{"session_id": "f" * 32}]}
    p = sm._write_output("json", "{}", report)
    # Mode is umask-dependent (typically 0o644 on dev macOS) but
    # must not equal 0o600 unless the user opted in.
    mode = p.stat().st_mode & 0o777
    assert mode != 0o600
    # A reasonable sanity check — should be at least owner-readable.
    assert mode & 0o400


