"""Report-builder tests — Phase A (cache_breaks/by_skill/by_subagent_type), Phase B (subagent attribution), Advisor feature.

Split out of test_session_metrics.py in v1.41.9 (Tier 4 of the
post-audit improvement plan; sibling-file pattern established in v1.41.8).

Run with: uv run python -m pytest tests/test_report.py -v
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


# Autouse fixtures duplicated from test_session_metrics.py — autouse only
# fires for tests in the module they're declared in, so each split file
# needs its own copy. A future slice may lift these into tests/conftest.py.
@pytest.fixture(autouse=True)
def isolate_projects_dir(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("real_projects_dir"):
        return
    safe = tmp_path / "_autouse_projects"
    safe.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(safe))


@pytest.fixture(autouse=True)
def _clear_pricing_cache():
    sm._pricing_for.cache_clear()
    yield


def _build_fixture_report():
    sid, turns, user_ts = sm._load_session(_FIXTURE, include_subagents=False)
    return sm._build_report("session", "test-slug", [(sid, turns, user_ts)])


# === BODY (extracted from test_session_metrics.py) ============================
# ===========================================================================
# T1.5 — argparse mutually-exclusive mode group (F5, v2 audit)
# ===========================================================================

def test_argparse_mutex_compare_and_compare_run_conflict():
    """--compare and --compare-run together must raise SystemExit(2)."""
    with pytest.raises(SystemExit) as excinfo:
        sm._build_parser().parse_args(
            ["--compare", "a.jsonl", "b.jsonl", "--compare-run"]
        )
    assert excinfo.value.code != 0


def test_argparse_mutex_compare_and_count_tokens_only_conflict():
    """--compare and --count-tokens-only together must raise SystemExit."""
    with pytest.raises(SystemExit) as excinfo:
        sm._build_parser().parse_args(
            ["--compare", "a.jsonl", "b.jsonl", "--count-tokens-only"]
        )
    assert excinfo.value.code != 0


def test_argparse_mutex_compare_prep_and_compare_run_conflict():
    """--compare-prep and --compare-run together must raise SystemExit."""
    with pytest.raises(SystemExit) as excinfo:
        sm._build_parser().parse_args(["--compare-prep", "--compare-run"])
    assert excinfo.value.code != 0


def test_argparse_compare_alone_accepted():
    """--compare with two args and no conflicting flags must parse successfully."""
    args = sm._build_parser().parse_args(["--compare", "a.jsonl", "b.jsonl"])
    assert args.compare == ["a.jsonl", "b.jsonl"]
    assert args.compare_run is None
    assert args.compare_prep is None


def test_argparse_compare_run_alone_accepted():
    """--compare-run with no conflicting flags must parse successfully."""
    args = sm._build_parser().parse_args(["--compare-run"])
    assert args.compare_run == []  # nargs="*" with no values gives []
    assert args.compare is None


# ---------------------------------------------------------------------------
# Phase-A aggregators (v1.6.0) — cache_breaks / by_skill / by_subagent_type
# and UUID global dedup. See CLAUDE-session-metrics-development-history.md
# Session 63 for context.
# ---------------------------------------------------------------------------

_PHASE_A_FIXTURE = _HERE / "fixtures" / "phase_a_session.jsonl"


def _build_phase_a_report():
    entries = sm._parse_jsonl(_PHASE_A_FIXTURE)
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    return sm._build_report("session", "pa", [("pa", turns, user_ts)])


def test_phase_a_cache_break_detection_threshold():
    r = _build_phase_a_report()
    breaks = r.get("cache_breaks") or []
    assert len(breaks) == 1
    cb = breaks[0]
    # input 150_000 + cache_creation 2_000 = 152_000 uncached.
    assert cb["uncached"] == 152_000
    assert cb["turn_index"] == 2
    assert any(c.get("here") for c in cb["context"])


def test_phase_a_cache_break_threshold_override():
    entries = sm._parse_jsonl(_PHASE_A_FIXTURE)
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    r = sm._build_report("session", "pa", [("pa", turns, user_ts)],
                          cache_break_threshold=1_000_000)
    assert (r.get("cache_breaks") or []) == []


def test_phase_a_by_skill_table():
    r = _build_phase_a_report()
    rows = {row["name"]: row for row in (r.get("by_skill") or [])}
    assert "compact" in rows
    assert rows["compact"]["invocations"] >= 1
    assert rows["compact"]["turns_attributed"] >= 1
    assert "session-metrics" in rows
    assert rows["session-metrics"]["invocations"] >= 2
    # Plain no-signal turn is not attributed.
    total_attributed_turns = sum(r["turns_attributed"] for r in rows.values())
    assert total_attributed_turns <= 4


def test_phase_a_by_subagent_type_spawn_count():
    r = _build_phase_a_report()
    rows = {row["name"]: row for row in (r.get("by_subagent_type") or [])}
    assert "Explore" in rows
    assert rows["Explore"]["spawn_count"] == 1


def test_phase_a_turn_record_fields_present():
    r = _build_phase_a_report()
    turns = r["sessions"][0]["turns"]
    spawn_turns = [t for t in turns if t.get("spawned_subagents")]
    skill_turns = [t for t in turns if t.get("skill_invocations")]
    assert len(spawn_turns) == 1
    assert spawn_turns[0]["spawned_subagents"] == ["Explore"]
    assert len(skill_turns) == 1
    assert skill_turns[0]["skill_invocations"] == ["session-metrics"]
    assert all(t.get("subagent_type", "") == "" for t in turns)


def test_phase_a_uuid_global_dedup(tmp_path):
    original = _PHASE_A_FIXTURE.read_text(encoding="utf-8")
    a_path = tmp_path / "pa-original.jsonl"
    b_path = tmp_path / "pa-resumed.jsonl"
    a_path.write_text(original, encoding="utf-8")
    b_path.write_text(original, encoding="utf-8")
    _, turns_no_dedup_a, _ = sm._load_session(
        a_path, include_subagents=False, use_cache=False)
    _, turns_no_dedup_b, _ = sm._load_session(
        b_path, include_subagents=False, use_cache=False, seen_uuids=None)
    assert len(turns_no_dedup_a) == len(turns_no_dedup_b) > 0
    shared: set[str] = set()
    _, turns_shared_a, _ = sm._load_session(
        a_path, include_subagents=False, use_cache=False, seen_uuids=shared)
    _, turns_shared_b, _ = sm._load_session(
        b_path, include_subagents=False, use_cache=False, seen_uuids=shared)
    assert len(turns_shared_a) == len(turns_no_dedup_a)
    assert len(turns_shared_b) == 0  # every uuid already seen


def test_phase_a_resolve_subagent_type_filename_fallback(tmp_path):
    sub = tmp_path / "agent-aExplore-abc123def.jsonl"
    sub.write_text("", encoding="utf-8")
    assert sm._resolve_subagent_type(sub) == "Explore"


def test_phase_a_resolve_subagent_type_meta_wins(tmp_path):
    sub = tmp_path / "agent-aExplore-abc123def.jsonl"
    meta = tmp_path / "agent-aExplore-abc123def.meta.json"
    sub.write_text("", encoding="utf-8")
    meta.write_text('{"agentType": "code-searcher"}', encoding="utf-8")
    assert sm._resolve_subagent_type(sub) == "code-searcher"


def test_phase_a_resolve_subagent_type_fork_fallback(tmp_path):
    sub = tmp_path / "random-name.jsonl"
    sub.write_text("", encoding="utf-8")
    assert sm._resolve_subagent_type(sub) == "fork"


def test_phase_a_html_sections_auto_hide_when_empty():
    entries = sm._parse_jsonl(_FIXTURE)
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    r = sm._build_report("session", "mini", [("mini", turns, user_ts)])
    html = sm.render_html(r, variant="single", chart_lib="none")
    # Auto-hide check: the *rendered element* must not be present.
    # The CSS class names themselves live inside the embedded <style>
    # block regardless of whether the section renders, so we look for
    # the opening tag pattern that only the actual section emits.
    assert '<details class="cache-break-row">' not in html
    assert ">Skills &amp; slash commands<" not in html


# ---------------------------------------------------------------------------
# Self-cost meta-metric (v1.27.0). Filters by_skill rows for session-metrics'
# own attribution and exposes them as a top-level report key, an HTML KPI
# card, and a `[self-cost]` stderr line. The current invocation is *not*
# yet in the JSONL when the script reads it — the figure intentionally
# reflects only prior session-metrics turns this session.
# ---------------------------------------------------------------------------

def test_self_cost_summarizer_picks_session_metrics_rows():
    by_skill = [
        {"name": "session-metrics", "turns_attributed": 2,
         "input": 100, "output": 50, "cache_read": 1000,
         "cache_write": 200, "total_tokens": 1350, "cost_usd": 0.0123},
        {"name": "compact", "turns_attributed": 1,
         "input": 10, "output": 5, "cache_read": 0, "cache_write": 0,
         "total_tokens": 15, "cost_usd": 0.0001},
    ]
    sc = sm._summarize_self_cost(by_skill)
    assert sc["turns"] == 2
    assert sc["cost_usd"] == 0.0123
    assert sc["total_tokens"] == 1350
    assert sc["input"] == 100
    assert sc["output"] == 50
    assert sc["cache_read"] == 1000
    assert sc["cache_write"] == 200
    assert sc["matched_skill_names"] == ["session-metrics"]
    assert "current invocation" in sc["note"]


def test_self_cost_summarizer_handles_namespaced_alias():
    """Plugin marketplace surfaces ``session-metrics:session-metrics``;
    the summary must aggregate it alongside the bare slash-command name."""
    by_skill = [
        {"name": "session-metrics", "turns_attributed": 1,
         "input": 10, "output": 5, "cache_read": 0, "cache_write": 0,
         "total_tokens": 15, "cost_usd": 0.0010},
        {"name": "session-metrics:session-metrics", "turns_attributed": 1,
         "input": 20, "output": 10, "cache_read": 0, "cache_write": 0,
         "total_tokens": 30, "cost_usd": 0.0020},
    ]
    sc = sm._summarize_self_cost(by_skill)
    assert sc["turns"] == 2
    assert sc["cost_usd"] == 0.003
    assert sorted(sc["matched_skill_names"]) == [
        "session-metrics", "session-metrics:session-metrics",
    ]


def test_self_cost_summarizer_zero_when_no_session_metrics_rows():
    """First-ever invocation in a session: by_skill has no session-metrics
    row yet (current run not logged), so the meta-metric correctly
    reports $0 / 0 turns."""
    sc = sm._summarize_self_cost([
        {"name": "compact", "turns_attributed": 1, "input": 1, "output": 1,
         "cache_read": 0, "cache_write": 0, "total_tokens": 2,
         "cost_usd": 0.0001},
    ])
    assert sc["turns"] == 0
    assert sc["cost_usd"] == 0.0
    assert sc["total_tokens"] == 0
    assert sc["matched_skill_names"] == []


def test_self_cost_attached_to_phase_a_report():
    r = _build_phase_a_report()
    sc = r.get("self_cost")
    assert isinstance(sc, dict)
    rows = {row["name"]: row for row in (r.get("by_skill") or [])}
    sm_row = rows.get("session-metrics") or {}
    assert sc["turns"] == sm_row.get("turns_attributed", 0)
    assert sc["cost_usd"] == round(sm_row.get("cost_usd", 0.0), 6)
    assert "session-metrics" in sc["matched_skill_names"]


def test_self_cost_html_card_renders_when_nonzero():
    r = _build_phase_a_report()
    html = sm.render_html(r, variant="single", chart_lib="none")
    # Card has the labelled header and the tooltip caveat.
    assert "Skill self-cost" in html
    assert "prior runs" in html


def test_self_cost_html_card_hidden_when_zero():
    """mini fixture has no session-metrics turns; the card must not render."""
    entries = sm._parse_jsonl(_FIXTURE)
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    r = sm._build_report("session", "mini", [("mini", turns, user_ts)])
    html = sm.render_html(r, variant="single", chart_lib="none")
    assert "Skill self-cost" not in html


def test_self_cost_json_export_carries_field():
    r = _build_phase_a_report()
    payload = sm._RENDERERS["json"](r)
    import json as _json
    parsed = _json.loads(payload)
    assert "self_cost" in parsed
    assert parsed["self_cost"]["turns"] >= 1
    assert "current invocation" in parsed["self_cost"]["note"]


def test_self_cost_print_summary(capsys):
    sm._print_self_cost_summary({
        "turns": 3, "cost_usd": 0.0250, "total_tokens": 12345,
    })
    captured = capsys.readouterr()
    assert "[self-cost]" in captured.err
    assert "3 prior turns" in captured.err
    assert "$0.0250" in captured.err
    assert "12,345 tokens" in captured.err


def test_self_cost_print_summary_handles_none(capsys):
    sm._print_self_cost_summary(None)
    captured = capsys.readouterr()
    assert captured.err == ""


# ---------------------------------------------------------------------------
# audit-session-metrics skill — playbook smoke tests. We can't golden-test
# free-form Haiku output, but the reference files must exist with the
# structural anchors the SKILL.md body relies on.
# ---------------------------------------------------------------------------

def test_audit_skill_files_present():
    audit_dir = _HERE.parent.parent / "audit-session-metrics"
    assert (audit_dir / "SKILL.md").exists()
    assert (audit_dir / "references" / "quick-audit.md").exists()
    assert (audit_dir / "references" / "detailed-audit.md").exists()


def test_audit_skill_frontmatter_pins_haiku():
    audit_skill = (_HERE.parent.parent / "audit-session-metrics"
                   / "SKILL.md").read_text(encoding="utf-8")
    # Frontmatter must declare model: haiku, otherwise the cost-saving
    # split is undermined (audit would inherit the parent session's model).
    assert "name: audit-session-metrics" in audit_skill
    assert "model: haiku" in audit_skill


def test_audit_quick_playbook_anchors():
    quick = (_HERE.parent.parent / "audit-session-metrics" / "references"
             / "quick-audit.md").read_text(encoding="utf-8")
    # Anchor strings the playbook contract relies on. Changing these
    # without updating SKILL.md or downstream tooling is a regression.
    # Three-artefact contract:
    assert "Output contract" in quick
    assert "JSON sidecar" in quick
    assert "Markdown copy" in quick
    assert "Inline chat output" in quick
    # Schema and render contract:
    assert "JSON schema" in quick
    assert "audit_schema_version" in quick
    assert "Metric enum" in quick
    assert "Finding object" in quick
    assert "Top expensive turn object" in quick
    assert "Markdown render template" in quick
    # Markdown sections the render template emits:
    assert "Findings" in quick
    assert "Top 3 expensive turns" in quick
    assert "What to fix first" in quick
    # Sample of metric enum names — these are part of the public schema:
    assert "cache_break" in quick
    assert "top_turn_share" in quick
    assert "advisor_share" in quick
    # Tier-1 additions (v1.27.0):
    assert "session_warmup_overhead" in quick
    assert "tool_result_bloat" in quick
    # Optional-impact contract: estimated_impact_usd must explicitly
    # allow null so the model is told never to guess.
    assert "estimated_impact_usd" in quick
    assert "null" in quick
    # v1.29.0: per-array caps — 7 negative, 3 positive — no floor, no padding.
    assert "capped at **7**" in quick or "capped at 7" in quick
    assert "Finding cap" in quick
    assert "no floor" in quick.lower()
    # Helper-script workflow is now the entry path:
    assert "audit-extract.py" in quick
    # Impact-formula reference table (v1.28.0):
    assert "Impact formula" in quick
    # LLM division of labor section:
    assert "LLM division of labor" in quick
    # v1.29.0 additions: positive findings + idle_gap_cache_decay:
    assert "positive_findings" in quick
    assert "cache_savings_high" in quick
    assert "cache_health_excellent" in quick
    assert "idle_gap_cache_decay" in quick
    assert "schema_version" in quick and "1.1" in quick


def test_audit_detailed_playbook_anchors():
    detailed = (_HERE.parent.parent / "audit-session-metrics" / "references"
                / "detailed-audit.md").read_text(encoding="utf-8")
    assert "Context-budget rules" in detailed
    assert "Phase 2" in detailed
    assert "Phase 3" in detailed
    # Three-artefact contract carries forward:
    assert "Output contract" in detailed
    assert "JSON sidecar" in detailed
    assert "audit_schema_version" in detailed
    # Detailed-only schema fields:
    assert "quick_wins" in detailed
    assert "structural_fixes" in detailed
    assert "estimated_savings" in detailed
    # Detailed-only metric enum entries:
    assert "claudemd_oversize" in detailed
    assert "missing_claudeignore" in detailed
    assert "mcp_unused" in detailed
    assert "file_re_read" in detailed
    assert "paste_bomb" in detailed
    # Tier-1 detailed additions (v1.27.0):
    assert "verbose_response" in detailed
    assert "weekly_rollup_regression" in detailed
    assert "peak_hour_concentration" in detailed
    assert "subagent_attribution_orphan" in detailed
    # v1.28.0: cap relaxed from "exactly N" to "up to N", no floor.
    assert "up to 16 finding" in detailed or "up to 16 findings" in detailed
    assert "Sixteen-finding cap" in detailed
    assert "no floor" in detailed.lower()
    # v1.29.0 additions: positive_findings carries forward to detailed.
    assert "positive_findings" in detailed
    assert "idle_gap_cache_decay" in detailed
    # Helper-script workflow:
    assert "audit-extract.py" in detailed
    assert "detailed_candidates" in detailed
    # LLM division of labor section:
    assert "LLM division of labor" in detailed
    # Markdown render template section names:
    assert "Quick wins" in detailed
    assert "Structural fixes" in detailed
    # The playbook MUST tell Haiku not to read the source JSONL, only the
    # structured JSON export — guards the context-budget invariant.
    assert "Do not read the raw" in detailed


def test_audit_playbooks_share_schema_version():
    """quick-audit and detailed-audit must use the same audit_schema_version
    so downstream tooling sees a consistent schema across modes."""
    import re
    quick = (_HERE.parent.parent / "audit-session-metrics" / "references"
             / "quick-audit.md").read_text(encoding="utf-8")
    detailed = (_HERE.parent.parent / "audit-session-metrics" / "references"
                / "detailed-audit.md").read_text(encoding="utf-8")
    # Pull the version string from the JSON-schema header in each file.
    quick_ver = re.search(r"JSON schema \(v([\d.]+)\)", quick)
    detailed_ver = re.search(r"JSON schema \(v([\d.]+)\)", detailed)
    assert quick_ver is not None, "quick-audit.md missing 'JSON schema (vX.Y)' header"
    assert detailed_ver is not None, "detailed-audit.md missing 'JSON schema (vX.Y)' header"
    assert quick_ver.group(1) == detailed_ver.group(1), (
        f"schema version drift: quick=v{quick_ver.group(1)}, "
        f"detailed=v{detailed_ver.group(1)}")


def test_phase_a_html_sections_render_when_present():
    r = _build_phase_a_report()
    html = sm.render_html(r, variant="single", chart_lib="none")
    assert "cache-break-row" in html
    assert ">Skills &amp; slash commands<" in html
    assert ">Subagent types<" in html


def test_phase_a_markdown_sections_render_when_present():
    r = _build_phase_a_report()
    md = sm.render_md(r)
    assert "## Skills & slash commands" in md
    assert "## Subagent types" in md
    assert "## Cache breaks" in md


def test_phase_a_csv_sections_render_when_present():
    r = _build_phase_a_report()
    csv = sm.render_csv(r)
    assert "# SKILLS / SLASH COMMANDS" in csv
    assert "# SUBAGENT TYPES" in csv
    assert "# CACHE BREAKS" in csv


def test_phase_a_cli_flag_parsed():
    args = sm._build_parser().parse_args(["--cache-break-threshold", "50000"])
    assert args.cache_break_threshold == 50000
    # Default is the module constant.
    args_default = sm._build_parser().parse_args([])
    assert args_default.cache_break_threshold == sm._CACHE_BREAK_DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Phase-B subagent → parent-prompt token attribution (v1.7.0).
# Fixture in fixtures/phase_b_attribution.jsonl (+ subagents/ dir) covers:
#   - parent → Explore subagent (direct attribution)
#   - Explore → nested Plan subagent (transitive root attribution)
#   - parent → spawn whose subagent file is missing (orphan path)
# ---------------------------------------------------------------------------

_PHASE_B_FIXTURE = _HERE / "fixtures" / "phase_b_attribution.jsonl"


def _build_phase_b_report(subagent_attribution=True, sort_prompts_by=None):
    """Load Phase-B fixture with subagents enabled and attribution applied."""
    sid, turns, user_ts = sm._load_session(
        _PHASE_B_FIXTURE, include_subagents=True, use_cache=False)
    return sm._build_report(
        "session", "pb", [(sid, turns, user_ts)],
        subagent_attribution=subagent_attribution,
        sort_prompts_by=sort_prompts_by,
        # Propagate the loader flag so the report's ``include_subagents``
        # field is True — needed by v1.26.0 renderers (warm-up columns,
        # headline share branching) that otherwise treat the absence as
        # "attribution disabled".
        include_subagents=True,
    )


def test_phase_b_extract_turns_captures_agent_links():
    entries = sm._parse_jsonl(_PHASE_B_FIXTURE)
    turns = sm._extract_turns(entries)
    # The 2nd assistant turn (pb-a2) is paired with the user entry that
    # carries toolUseResult.agentId=aphasebA1 + tool_use_id=tu-explore.
    a2 = next(t for t in turns if t["message"]["id"] == "pb_msg_2")
    links = a2.get("_preceding_user_agent_links") or []
    assert ("tu-explore", "aphasebA1") in links


def test_extract_turns_accumulates_parallel_task_agent_links():
    # When the assistant emits multiple Task tool_uses in parallel, Anthropic's
    # wire format returns each tool_result in its own user JSONL entry, each
    # carrying its own toolUseResult.agentId. The next assistant turn's
    # _preceding_user_agent_links must contain ALL pairs, not just the last one.
    # Pre-fix: last_user_agent_links was overwritten on each user entry, so
    # only the final (tuid, agentId) pair survived → N-1 of N spawns orphaned.
    base_usage = {"input_tokens": 5, "output_tokens": 10,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    entries = [
        {"type": "user", "timestamp": "2026-04-28T10:00:00Z",
         "message": {"role": "user", "content": "spawn two tasks"}},
        {"type": "assistant", "timestamp": "2026-04-28T10:00:01Z",
         "message": {"id": "spawn_msg", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [
                         {"type": "tool_use", "id": "tu_alpha", "name": "Task", "input": {}},
                         {"type": "tool_use", "id": "tu_beta",  "name": "Task", "input": {}},
                     ]}},
        {"type": "user", "timestamp": "2026-04-28T10:00:10Z",
         "toolUseResult": {"agentId": "aid_alpha"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_alpha", "content": "alpha done"}]}},
        {"type": "user", "timestamp": "2026-04-28T10:00:11Z",
         "toolUseResult": {"agentId": "aid_beta"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_beta", "content": "beta done"}]}},
        {"type": "assistant", "timestamp": "2026-04-28T10:00:12Z",
         "message": {"id": "next_msg", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [{"type": "text", "text": "got both"}]}},
    ]
    turns = sm._extract_turns(entries)
    next_turn = next(t for t in turns if t["message"]["id"] == "next_msg")
    links = next_turn.get("_preceding_user_agent_links") or []
    assert ("tu_alpha", "aid_alpha") in links, f"alpha pair missing: {links}"
    assert ("tu_beta", "aid_beta") in links, f"beta pair missing: {links}"


def test_extract_turns_accumulates_parallel_tool_result_blocks():
    # Sibling fix to the agent_links accumulator: when N tool_result entries
    # land in N separate user messages between two assistant turns, content
    # block counts on the next assistant turn must include every tool_result,
    # not just the last user entry's. Pre-fix: last_user_content was
    # overwritten on each user entry → tool_result count was always 1.
    base_usage = {"input_tokens": 5, "output_tokens": 10,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    entries = [
        {"type": "user", "timestamp": "2026-04-28T12:00:00Z",
         "message": {"role": "user", "content": "spawn 3"}},
        {"type": "assistant", "timestamp": "2026-04-28T12:00:01Z",
         "message": {"id": "spawn3", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [
                         {"type": "tool_use", "id": "tu_a", "name": "Task", "input": {}},
                         {"type": "tool_use", "id": "tu_b", "name": "Task", "input": {}},
                         {"type": "tool_use", "id": "tu_c", "name": "Task", "input": {}},
                     ]}},
        {"type": "user", "timestamp": "2026-04-28T12:00:10Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_a", "content": "a"}]}},
        {"type": "user", "timestamp": "2026-04-28T12:00:11Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_b", "content": "b"}]}},
        {"type": "user", "timestamp": "2026-04-28T12:00:12Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_c", "content": "c"}]}},
        {"type": "assistant", "timestamp": "2026-04-28T12:00:13Z",
         "message": {"id": "after_3", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [{"type": "text", "text": "got 3"}]}},
    ]
    turns = sm._extract_turns(entries)
    after = next(t for t in turns if t["message"]["id"] == "after_3")
    user_raw = after.get("_preceding_user_content")
    assert isinstance(user_raw, list), f"expected list snapshot, got {type(user_raw)}"
    tool_result_blocks = [b for b in user_raw
                          if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(tool_result_blocks) == 3, (
        f"expected 3 tool_result blocks across the gap, got {len(tool_result_blocks)}")


def test_extract_turns_resets_agent_links_after_assistant_first_occurrence():
    # The accumulator must reset after the assistant first-occurrence captures
    # its snapshot, otherwise pairs from one gap leak into the next assistant's
    # _preceding_user_agent_links.
    base_usage = {"input_tokens": 5, "output_tokens": 10,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    entries = [
        {"type": "user", "timestamp": "2026-04-28T11:00:00Z",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "timestamp": "2026-04-28T11:00:01Z",
         "message": {"id": "spawn_msg", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [{"type": "tool_use", "id": "tu_x", "name": "Task", "input": {}}]}},
        {"type": "user", "timestamp": "2026-04-28T11:00:05Z",
         "toolUseResult": {"agentId": "aid_x"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_x", "content": "x done"}]}},
        {"type": "assistant", "timestamp": "2026-04-28T11:00:06Z",
         "message": {"id": "first_resp", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "timestamp": "2026-04-28T11:00:10Z",
         "message": {"role": "user", "content": "plain follow-up, no spawn"}},
        {"type": "assistant", "timestamp": "2026-04-28T11:00:11Z",
         "message": {"id": "second_resp", "role": "assistant", "model": "claude-opus-4-7",
                     "usage": base_usage,
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    turns = sm._extract_turns(entries)
    second = next(t for t in turns if t["message"]["id"] == "second_resp")
    leaked = second.get("_preceding_user_agent_links") or []
    assert leaked == [], f"agent_links leaked into a later turn: {leaked}"


def test_phase_b_load_session_tags_subagent_agent_id():
    sid, turns, _ = sm._load_session(
        _PHASE_B_FIXTURE, include_subagents=True, use_cache=False)
    sub_turns = [t for t in turns if t.get("_subagent_agent_id")]
    # Two subagent files: aphasebA1 (2 assistant turns) + aphasebB1 (1).
    assert {t.get("_subagent_agent_id") for t in sub_turns} == {
        "aphasebA1", "aphasebB1"}


def test_phase_b_simple_attribution():
    r = _build_phase_b_report()
    turns = r["sessions"][0]["turns"]
    parent = next(t for t in turns
                  if t.get("prompt_text", "").startswith("do an Explore"))
    # A1 has 2 assistant turns: 1500+400+3000 = 4900 + 2500+500+4000 = 7000
    # = 11_900 total tokens. Plus B1's 700+250+1500 = 2450 nested.
    assert parent["attributed_subagent_count"] >= 3
    assert parent["attributed_subagent_tokens"] == 11_900 + 2_450
    assert parent["attributed_subagent_cost"] > 0


def test_phase_b_orphan_subagent_counted():
    r = _build_phase_b_report()
    summary = r["subagent_attribution_summary"]
    # The "orphan" parent spawn references aphasebMissing for which no
    # subagent file exists, so no rollup happens — but the attribution
    # pass also doesn't crash. orphan_subagent_turns is non-zero only if
    # a subagent JSONL was loaded but its parent linkage was missing —
    # in this fixture that is also the case for B1 chain (its parent A1
    # IS resolvable, so B1 isn't orphaned). So orphan_subagent_turns = 0
    # here. We assert no crash and that nested_levels_seen >= 1.
    assert summary["nested_levels_seen"] >= 1


def test_phase_b_nested_chain_rolls_to_root():
    """B1's 1 turn (970 tokens) should be on the root prompt, not on A1."""
    r = _build_phase_b_report()
    turns = r["sessions"][0]["turns"]
    parent = next(t for t in turns
                  if t.get("prompt_text", "").startswith("do an Explore"))
    a1_turns = [t for t in turns if t.get("subagent_agent_id") == "aphasebA1"]
    b1_turns = [t for t in turns if t.get("subagent_agent_id") == "aphasebB1"]
    # The Explore subagent file (A1) has 2 assistant turns. Its own
    # ``attributed_subagent_*`` should be 0 — nested tokens roll up to
    # the original root, not the intermediate subagent.
    assert all(t["attributed_subagent_count"] == 0 for t in a1_turns)
    assert all(t["attributed_subagent_count"] == 0 for t in b1_turns)
    # Root parent count >= 3 (2 from A1 + 1 from B1).
    assert parent["attributed_subagent_count"] == 3


def test_phase_b_no_double_counting_in_totals():
    """Session total cost is the sum of every turn's cost_usd. The
    attribution pass adds attributed_subagent_* fields without modifying
    cost_usd or total_tokens, so totals must match before/after."""
    r_with = _build_phase_b_report(subagent_attribution=True)
    r_without = _build_phase_b_report(subagent_attribution=False)
    assert r_with["totals"]["cost"] == r_without["totals"]["cost"]
    assert r_with["totals"]["input"] == r_without["totals"]["input"]


def test_phase_b_disabled_zeroes_attribution():
    r = _build_phase_b_report(subagent_attribution=False)
    turns = r["sessions"][0]["turns"]
    assert all(t["attributed_subagent_count"] == 0 for t in turns)
    assert all(t["attributed_subagent_tokens"] == 0 for t in turns)


def test_phase_b_prompt_anchor_index_skips_empty_prompts():
    """A turn with empty prompt_text (e.g., the second turn of an Agent
    spawn chain) inherits the prompt_anchor_index of the most recent
    non-empty user prompt, so attribution lands where it's renderable."""
    r = _build_phase_b_report()
    turns = r["sessions"][0]["turns"]
    parent_idx = next(t["index"] for t in turns
                      if t.get("prompt_text", "").startswith("do an Explore"))
    spawn_turn = next(t for t in turns if "tu-explore" in (t.get("tool_use_ids") or []))
    # The spawn turn itself often has prompt_text=="" because the user
    # entry is a tool_result; the anchor must point to the *user* prompt.
    assert spawn_turn["prompt_anchor_index"] == parent_idx


def test_phase_b_html_attribution_column_renders():
    r = _build_phase_b_report()
    html = sm.render_html(r, variant="single", chart_lib="none")
    assert ">Subagents +$<" in html
    # And the row badge appears for the parent prompt.
    assert "+3 subagents" in html or "+3 subagent" in html


def test_phase_b_html_no_column_when_attribution_off():
    r = _build_phase_b_report(subagent_attribution=False)
    html = sm.render_html(r, variant="single", chart_lib="none")
    assert ">Subagents +$<" not in html


def test_phase_b_csv_includes_attributed_columns():
    r = _build_phase_b_report()
    csv = sm.render_csv(r)
    assert "attributed_subagent_tokens" in csv
    assert "attributed_subagent_cost" in csv
    assert "attributed_subagent_count" in csv


def test_phase_b_sort_prompts_by_total_default_html():
    # In the fixture the parent-Explore prompt has small parent cost but
    # large attributed cost — under default 'total' sort it should appear
    # before the orphan-spawn prompt (which has higher parent cost on
    # this fixture is similar).
    r = _build_phase_b_report(sort_prompts_by=None)
    html = sm.render_html(r, variant="single", chart_lib="none")
    # The Explore parent prompt snippet appears before the orphan
    # parent's snippet in the rendered table when sorted by total.
    explore_pos = html.find("do an Explore for foo")
    orphan_pos = html.find("orphan-spawn")
    plain_pos = html.find("plain prompt no spawn")
    # All three snippets should be in the prompts table.
    assert explore_pos > 0 and orphan_pos > 0 and plain_pos > 0
    # Explore precedes plain (much bigger total).
    assert explore_pos < plain_pos


def test_phase_b_sort_prompts_by_self_explicit():
    r = _build_phase_b_report(sort_prompts_by="self")
    html = sm.render_html(r, variant="single", chart_lib="none")
    assert "ranked by parent-turn cost only" in html


def test_phase_b_cli_flags_parsed():
    args = sm._build_parser().parse_args(["--no-subagent-attribution",
                                          "--sort-prompts-by", "self"])
    assert args.no_subagent_attribution is True
    assert args.sort_prompts_by == "self"
    args_default = sm._build_parser().parse_args([])
    assert args_default.no_subagent_attribution is False
    assert args_default.sort_prompts_by is None


# ---------------------------------------------------------------------------
# v1.26.0: subagent share + within-session split + warm-up columns.
# Built on the same Phase-B fixture; all derivations are render-time only.
# ---------------------------------------------------------------------------

def test_share_stats_match_attributed_sum_with_attribution():
    """Headline ``share_pct`` matches sum(attributed_subagent_cost)/total."""
    r = _build_phase_b_report()
    stats = r["subagent_share_stats"]
    turns = r["sessions"][0]["turns"]
    expected_attributed = sum(
        t.get("attributed_subagent_cost", 0.0) for t in turns
        if not t.get("subagent_agent_id") and not t.get("is_resume_marker")
    )
    assert stats["has_attribution"] is True
    assert stats["attributed_cost"] == expected_attributed
    expected_pct = 100.0 * expected_attributed / r["totals"]["cost"]
    assert abs(stats["share_pct"] - expected_pct) < 1e-9


def test_share_stats_md_renders_share_line_with_attribution():
    """``Subagent share of cost`` row appears in the MD summary table
    when attribution found something to roll up."""
    r = _build_phase_b_report()
    md = sm.render_md(r)
    assert "| Subagent share of cost |" in md
    # The line uses the neutral "share" framing, never "overhead".
    assert "overhead" not in md.lower() or "subagent overhead" not in md.lower()


def test_share_card_disabled_label_when_subagents_not_loaded():
    """When ``--include-subagents`` was not passed, the headline KPI
    branches to the 'attribution disabled' message rather than a
    deceptive 0% reading."""
    stats_disabled = {
        "include_subagents": False,
        "has_attribution":   False,
        "total_cost":        9.05,
        "attributed_cost":   0.0,
        "share_pct":         0.0,
        "spawn_count":       3,
        "attributed_count":  0,
        "orphan_turns":      0,
        "cycles_detected":   0,
        "nested_levels_seen": 0,
    }
    card = sm._build_subagent_share_card_html(stats_disabled)
    assert "attribution disabled" in card
    # Important: the card must NOT render "0%" when subagents weren't
    # loaded — that would falsely imply zero subagent activity.
    assert ">0%<" not in card


def test_within_session_split_renders_when_qualifies():
    """A session with ≥3 spawning + ≥3 non-spawning turns produces a
    qualifying row from ``_compute_within_session_split``."""
    sessions = [{
        "session_id": "abc123def",
        "subtotal":   {"cost": 1.0},
        "turns": [
            # 4 spawning turns
            {"index": 1, "cost_usd": 0.10, "tool_use_ids": ["t1"],
             "spawned_subagents": ["Explore"], "attributed_subagent_cost": 0.30},
            {"index": 2, "cost_usd": 0.05, "tool_use_ids": ["t2"],
             "spawned_subagents": ["Explore"], "attributed_subagent_cost": 0.20},
            {"index": 3, "cost_usd": 0.08, "tool_use_ids": ["t3"],
             "spawned_subagents": ["Explore"], "attributed_subagent_cost": 0.25},
            {"index": 4, "cost_usd": 0.06, "tool_use_ids": ["t4"],
             "spawned_subagents": ["Explore"], "attributed_subagent_cost": 0.15},
            # 4 non-spawning turns
            {"index": 5, "cost_usd": 0.02},
            {"index": 6, "cost_usd": 0.01},
            {"index": 7, "cost_usd": 0.03},
            {"index": 8, "cost_usd": 0.02},
        ],
    }]
    rows = sm._compute_within_session_split(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert row["spawn_n"] == 4
    assert row["no_spawn_n"] == 4
    # Median of {0.40, 0.25, 0.33, 0.21} (combined) > median of
    # {0.02, 0.01, 0.03, 0.02} (no-spawn).
    assert row["median_spawn"] > row["median_no_spawn"]
    assert row["delta"] > 0


def test_within_session_split_omits_when_too_few_turns():
    """Sessions with <3 spawning OR <3 non-spawning turns are filtered."""
    sessions = [{
        "session_id": "low_spawn",
        "subtotal":   {"cost": 0.5},
        "turns": [
            {"index": 1, "cost_usd": 0.10, "tool_use_ids": ["t1"],
             "spawned_subagents": ["Explore"], "attributed_subagent_cost": 0.30},
            {"index": 2, "cost_usd": 0.05},
            {"index": 3, "cost_usd": 0.05},
            {"index": 4, "cost_usd": 0.05},
        ],
    }]
    assert sm._compute_within_session_split(sessions) == []


def test_per_turn_badge_uses_combined_cost_label_not_turn():
    """Per-turn subagent badge in the prompts table uses the
    confounder-resistant '(NN% of combined cost)' label, not the
    misleading 'of turn' wording.
    """
    r = _build_phase_b_report()
    html = sm.render_html(r, variant="single", chart_lib="none")
    # The fixture parent prompt has both direct and attributed cost,
    # so the badge must render the percentage variant.
    assert "of combined cost" in html
    # The misleading old wording must not appear in the badge.
    assert "% of turn" not in html


def test_warmup_columns_render_when_invocations_observed():
    """First-turn % and SP amortised % columns render in the by-subagent
    type HTML when at least one row has invocation_count > 0."""
    r = _build_phase_b_report()
    html = sm.render_html(r, variant="single", chart_lib="none")
    # The Phase-B fixture has multi-turn subagent invocations
    # (aphasebA1 has 2 turns) — column headers should appear.
    assert "First-turn %" in html
    assert "SP amortised %" in html


def test_share_card_shows_lower_bound_when_orphans_exist():
    """If ``orphan_turns`` is non-zero, the headline card explicitly
    discloses the share is a lower bound."""
    stats_with_orphans = {
        "include_subagents": True,
        "has_attribution":   True,
        "total_cost":        10.0,
        "attributed_cost":   4.0,
        "share_pct":         40.0,
        "spawn_count":       5,
        "attributed_count":  8,
        "orphan_turns":      2,
        "cycles_detected":   0,
        "nested_levels_seen": 1,
    }
    card = sm._build_subagent_share_card_html(stats_with_orphans)
    assert "lower bound" in card
    assert "2 orphan turns" in card


# ---------------------------------------------------------------------------
# Wall-clock + per-turn latency surfacing (Stage 1a — feeds the benchmark
# orchestrators, which extract Wall clock from the markdown Summary table)
# ---------------------------------------------------------------------------

def _latency_entries(prompt_ts: str, asst_ts: str, msg_id: str = "msg_lat") -> list[dict]:
    """Build a minimal user-prompt → assistant pair for latency tests."""
    return [
        {"type": "user", "timestamp": prompt_ts,
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": asst_ts,
         "message": {"id": msg_id, "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "text", "text": "hello"}]}},
    ]


def test_extract_turns_attaches_preceding_user_timestamp():
    # The user-prompt timestamp immediately before each assistant turn must
    # round-trip onto the turn entry as ``_preceding_user_timestamp`` so
    # downstream layers can compute per-turn latency.
    entries = _latency_entries("2026-04-26T10:00:00Z", "2026-04-26T10:00:05Z")
    turns = sm._extract_turns(entries)
    assert len(turns) == 1
    assert turns[0]["_preceding_user_timestamp"] == "2026-04-26T10:00:00Z"


def test_build_turn_record_computes_latency_seconds():
    # Per-turn ``latency_seconds`` = assistant_ts - preceding_user_ts.
    entries = _latency_entries("2026-04-26T10:00:00Z", "2026-04-26T10:00:05.250Z")
    turns = sm._extract_turns(entries)
    rec = sm._build_turn_record(1, turns[0], tz_offset_hours=0.0)
    assert rec["latency_seconds"] == 5.25


def test_build_turn_record_latency_none_when_predecessor_missing():
    # A bare assistant entry (no preceding user) leaves latency_seconds None
    # rather than fabricating a fake gap.
    entries = [
        {"type": "assistant", "timestamp": "2026-04-26T10:00:05Z",
         "message": {"id": "msg_orphan", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": []}},
    ]
    turns = sm._extract_turns(entries)
    rec = sm._build_turn_record(1, turns[0], tz_offset_hours=0.0)
    assert rec["latency_seconds"] is None


def test_md_summary_includes_wall_clock_and_mean_latency():
    # End-to-end: a two-turn fixture session yields a session.md whose
    # Summary table carries both Wall clock and Mean turn latency rows —
    # the two anchors the benchmark orchestrators parse for headline
    # latency. Uses ``_build_report`` directly to avoid touching disk.
    entries = [
        {"type": "user", "timestamp": "2026-04-26T10:00:00Z",
         "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "timestamp": "2026-04-26T10:00:04Z",
         "message": {"id": "msg_a", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "timestamp": "2026-04-26T10:00:10Z",
         "message": {"role": "user", "content": "second"}},
        {"type": "assistant", "timestamp": "2026-04-26T10:00:18Z",
         "message": {"id": "msg_b", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "text", "text": "ok"}]}},
    ]
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    report = sm._build_report("session", "test-slug",
                                [("session-uuid", turns, user_ts)])
    md = sm.render_md(report)
    assert "| Wall clock |" in md
    # 10:00:00 → 10:00:18 = 18s. Formatted as "18s".
    assert "| Wall clock | 18s |" in md
    # Two turns with latency 4s and 8s → mean 6.00s.
    assert "| Mean turn latency | 6.00s (2 turns) |" in md
    # Per-session record must carry the new wall_clock_seconds field.
    assert report["sessions"][0]["wall_clock_seconds"] == 18


def test_extract_turns_does_not_inherit_prior_user_timestamp_when_blank():
    # Regression: a user entry whose timestamp is empty/missing must NOT
    # inherit the previous user's timestamp — otherwise the next assistant
    # turn will compute latency_seconds against an unrelated earlier
    # predecessor instead of None. Pre-fix code at session-metrics.py:421
    # used ``... or last_user_timestamp`` which silently fabricated a gap.
    entries = [
        {"type": "user", "timestamp": "2026-04-26T10:00:00Z",
         "message": {"role": "user", "content": "first"}},
        {"type": "assistant", "timestamp": "2026-04-26T10:00:04Z",
         "message": {"id": "msg_a", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "text", "text": "ok"}]}},
        # Second user entry has no timestamp; downstream assistant must
        # treat this as "no usable predecessor" rather than inheriting
        # the 10:00:00Z from the first user.
        {"type": "user",
         "message": {"role": "user", "content": "second"}},
        {"type": "assistant", "timestamp": "2026-04-26T10:00:30Z",
         "message": {"id": "msg_b", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": [{"type": "text", "text": "ok"}]}},
    ]
    turns = sm._extract_turns(entries)
    assert len(turns) == 2
    # First turn has the legitimate 10:00:00Z predecessor.
    assert turns[0]["_preceding_user_timestamp"] == "2026-04-26T10:00:00Z"
    # Second turn must not inherit it.
    assert turns[1]["_preceding_user_timestamp"] == ""
    rec_b = sm._build_turn_record(2, turns[1], tz_offset_hours=0.0)
    assert rec_b["latency_seconds"] is None


# --- Advisor feature (v1.25.0) -----------------------------------------------

_ADV_FIXTURE = _HERE / "fixtures" / "advisor_session.jsonl"

# Sonnet 4.6 rates: $3/$15/$0.30/$3.75 per 1M
# Opus 4.7 rates:   $5/$25 per 1M (advisor, no caching)
#
# Turn 2 (advisor turn) costs:
#   primary: 200*3 + 80*15 + 1000*0.30 + 500*3.75  = 3975   → $0.003975
#   advisor: 10000*5 + 500*25                        = 62500  → $0.062500
#   total:   $0.066475
_ADV_PRIMARY_COST  = (200*3 + 80*15 + 1000*0.30 + 500*3.75) / 1_000_000   # $0.003975
_ADV_ADVISOR_COST  = (10_000*5 + 500*25) / 1_000_000                        # $0.062500
_ADV_TURN2_TOTAL   = _ADV_PRIMARY_COST + _ADV_ADVISOR_COST                  # $0.066475


def _build_advisor_report():
    sid, turns, user_ts = sm._load_session(_ADV_FIXTURE, include_subagents=False)
    return sm._build_report("session", "adv-test-slug", [(sid, turns, user_ts)])


def test_advisor_cost_includes_iterations():
    """Turn-level cost_usd includes advisor iteration tokens at Opus 4.7 rates."""
    r = _build_advisor_report()
    turns = r["sessions"][0]["turns"]
    assert turns[1]["cost_usd"] == pytest.approx(_ADV_TURN2_TOTAL, abs=1e-7)


def test_advisor_call_count_per_turn():
    """advisor_calls == 1 on the advisor turn, 0 on all others."""
    r = _build_advisor_report()
    turns = r["sessions"][0]["turns"]
    assert turns[0]["advisor_calls"] == 0
    assert turns[1]["advisor_calls"] == 1
    assert turns[2]["advisor_calls"] == 0


def test_advisor_model_field_per_turn():
    """advisor_model is set on the advisor turn and None on others."""
    r = _build_advisor_report()
    turns = r["sessions"][0]["turns"]
    assert turns[0]["advisor_model"] is None
    assert turns[1]["advisor_model"] == "claude-opus-4-7"
    assert turns[2]["advisor_model"] is None


def test_advisor_tool_in_tool_names():
    """server_tool_use blocks with name 'advisor' appear in tool_use_names."""
    r = _build_advisor_report()
    turns = r["sessions"][0]["turns"]
    assert "advisor" in turns[1]["tool_use_names"]


def test_advisor_content_blocks_classified():
    """server_tool_use and advisor_tool_result content blocks are counted."""
    r = _build_advisor_report()
    turns = r["sessions"][0]["turns"]
    cb = turns[1]["content_blocks"]
    assert cb["server_tool_use"] == 1
    assert cb["advisor_tool_result"] == 1


def test_session_summary_advisor_fields():
    """Session subtotals aggregate advisor_call_count, advisor_cost_usd, advisor_configured_model."""
    r = _build_advisor_report()
    s = r["sessions"][0]
    st = s["subtotal"]
    assert st["advisor_call_count"] == 1
    assert st["advisor_cost_usd"] == pytest.approx(_ADV_ADVISOR_COST, abs=1e-7)
    assert s["advisor_configured_model"] == "claude-opus-4-7"


def test_no_advisor_session_unchanged():
    """Sessions without advisor activity produce all-zero advisor fields; cost_usd unaffected."""
    r = _build_fixture_report()
    st = r["sessions"][0]["subtotal"]
    assert st["advisor_call_count"] == 0
    assert st["advisor_cost_usd"] == 0.0
    for t in r["sessions"][0]["turns"]:
        assert t["advisor_calls"] == 0
        assert t["advisor_cost_usd"] == 0.0
        assert t["advisor_model"] is None
    # The well-known fixture total is unchanged.
    assert r["totals"]["cost"] == pytest.approx(0.027845, abs=1e-7)


def test_advisor_empty_model_falls_back_to_parent_rate():
    """Empty ``iterations[i].model`` must charge at the parent turn's rate.

    Regression for the silent ``it.get("model", "")`` divergence: the
    default arg of ``dict.get`` only fires on a missing key, so a key
    present but empty (``"model": ""``) used to fall through
    ``_pricing_for("")`` to ``_DEFAULT_PRICING`` instead of the parent
    model's tier. After the v1.41.4 fix both ``_cost`` and
    ``_advisor_info`` collapse missing-key and empty-string to the
    parent model — the more accurate fallback when an iteration record
    is partial.

    Parent model: ``claude-opus-4-7`` (input/output $5/$25 per 1M).
    Default tier: ``$3/$15`` per 1M. The two diverge by 60%+ on cost,
    so a wrong fallback is loud — not just an edge case.
    """
    sm._pricing_for.cache_clear()
    parent_model = "claude-opus-4-7"
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "iterations": [
            {"type": "advisor_message",
             "model": "",
             "input_tokens":  10_000,
             "output_tokens": 500},
        ],
    }
    expected_advisor = (10_000 * 5 + 500 * 25) / 1_000_000
    cost = sm._cost(usage, parent_model)
    assert cost == pytest.approx(expected_advisor, abs=1e-9)
    calls, adv_cost, adv_model, _, _ = sm._advisor_info(usage, parent_model)
    assert calls == 1
    assert adv_cost == pytest.approx(expected_advisor, abs=1e-9)
    # adv_model stays None because the iteration's model field was empty
    # — the rate fallback fixed the cost, but the displayed model name
    # is still unknown for that record.
    assert adv_model is None


def test_no_cache_cost_includes_advisor_iterations():
    """``_no_cache_cost`` must mirror the advisor loop in ``_cost``.

    Regression for the asymmetry that biased the cache-savings delta:
    on advisor-using turns ``_cost`` charged the advisor portion but
    ``_no_cache_cost`` skipped it, so ``cost_usd - no_cache_cost``
    appeared smaller than the real saving from prompt caching. Advisor
    iterations have no cache fields, so the no-cache and cached forms
    are identical for that portion — the no-cache function must add it
    back to keep the comparison honest.
    """
    sm._pricing_for.cache_clear()
    parent_model = "claude-opus-4-7"
    usage = {
        # Primary turn: only cache_read tokens, no fresh input — so the
        # primary _cost is small and any non-zero result must come from
        # the advisor block.
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "iterations": [
            {"type": "advisor_message",
             "model": "claude-opus-4-7",
             "input_tokens":  10_000,
             "output_tokens": 500},
        ],
    }
    nc = sm._no_cache_cost(usage, parent_model)
    expected_primary = 1000 * 5 / 1_000_000      # cache_read recharged at full input rate
    expected_advisor = (10_000 * 5 + 500 * 25) / 1_000_000
    assert nc == pytest.approx(expected_primary + expected_advisor, abs=1e-9)


