"""Render-tier tests — chart-library dispatch + vendoring, uPlot / Chart.js renderers, Usage Insights section.

Split out of test_session_metrics.py in v1.41.9 (Tier 4 of the
post-audit improvement plan; sibling-file pattern established in v1.41.8).

Run with: uv run python -m pytest tests/test_render.py -v
"""
import html as html_std
import importlib.util
import json
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


# === BODY (extracted from test_session_metrics.py) ============================

# --- Chart library dispatch / vendoring --------------------------------------

def test_chart_renderers_registry_has_all_four_renderers():
    for key in ("highcharts", "uplot", "chartjs", "none"):
        assert key in sm.CHART_RENDERERS, f"{key} missing from CHART_RENDERERS"
        assert callable(sm.CHART_RENDERERS[key]), f"{key} not callable"


def test_render_chart_none_empty_payload():
    body, head = sm._render_chart_none([])
    assert body == ""
    assert head == ""


def test_render_chart_highcharts_empty_turns_is_empty():
    # No turns -> no chart and no JS (the dashboard-only variant relies on
    # this so it can skip inlining the entire vendored bundle).
    body, head = sm._render_chart_highcharts([])
    assert body == ""
    assert head == ""


def test_vendor_manifest_loads_with_expected_schema():
    m = sm._load_chart_manifest()
    libs = m.get("libraries", {})
    assert "highcharts" in libs
    hc = libs["highcharts"]
    assert hc["license"].startswith("non-commercial")
    assert len(hc["files"]) >= 4
    for f in hc["files"]:
        assert {"name", "path", "sha256"} <= f.keys()
        assert len(f["sha256"]) == 64


def test_read_vendor_js_returns_real_payload_and_hash_matches():
    # Sanity check: the bundled Highcharts files exist and their SHA-256
    # matches what the manifest claims. The function returns a non-empty
    # JS blob when verification passes.
    payload = sm._read_vendor_js("highcharts")
    assert len(payload) > 100_000   # ~360 KB when all 4 files verify


def test_read_vendor_js_unknown_library_raises(monkeypatch):
    """Post-H6 fail-closed: an unknown library raises rather than degrading."""
    monkeypatch.setattr(sm, "_ALLOW_UNVERIFIED_CHARTS", False)
    with pytest.raises(sm.VendorChartVerificationError, match="not in vendor manifest"):
        sm._read_vendor_js("not-a-library")


def test_read_vendor_js_unknown_library_warn_with_override(monkeypatch, capsys):
    """With --allow-unverified-charts the failure degrades to a stderr warning."""
    monkeypatch.setattr(sm, "_ALLOW_UNVERIFIED_CHARTS", True)
    payload = sm._read_vendor_js("not-a-library")
    assert payload == ""
    err = capsys.readouterr().err
    assert "not in vendor manifest" in err
    assert "--allow-unverified-charts" in err


def test_read_vendor_js_sha_mismatch_raises(tmp_path, monkeypatch):
    """Tampered vendor JS must fail verification by default."""
    # Build a fake vendor tree whose manifest entry's SHA doesn't match the
    # on-disk file. Clear the lru_cache first so our synthetic manifest loads.
    fake_root = tmp_path / "vendor_charts"
    (fake_root / "mylib" / "v1").mkdir(parents=True)
    js_path = fake_root / "mylib" / "v1" / "mylib.js"
    js_path.write_text("console.log('tampered');\n", encoding="utf-8")
    manifest = {
        "libraries": {
            "mylib": {
                "version": "1", "license": "MIT",
                "files": [{
                    "name": "mylib.js", "path": "mylib/v1/mylib.js",
                    "sha256": "0" * 64,  # deliberately wrong
                }],
            },
        },
    }
    (fake_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sm, "_VENDOR_CHARTS_DIR", fake_root)
    monkeypatch.setattr(sm, "_ALLOW_UNVERIFIED_CHARTS", False)
    sm._load_chart_manifest.cache_clear()
    try:
        with pytest.raises(sm.VendorChartVerificationError, match="SHA-256 mismatch"):
            sm._read_vendor_js("mylib")
    finally:
        sm._load_chart_manifest.cache_clear()


def test_read_vendor_js_sha_mismatch_warns_with_override(tmp_path, monkeypatch, capsys):
    """With --allow-unverified-charts, SHA mismatches warn instead of raise."""
    fake_root = tmp_path / "vendor_charts"
    (fake_root / "mylib" / "v1").mkdir(parents=True)
    js_path = fake_root / "mylib" / "v1" / "mylib.js"
    js_path.write_text("console.log('tampered');\n", encoding="utf-8")
    manifest = {
        "libraries": {
            "mylib": {
                "version": "1", "license": "MIT",
                "files": [{
                    "name": "mylib.js", "path": "mylib/v1/mylib.js",
                    "sha256": "0" * 64,
                }],
            },
        },
    }
    (fake_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sm, "_VENDOR_CHARTS_DIR", fake_root)
    monkeypatch.setattr(sm, "_ALLOW_UNVERIFIED_CHARTS", True)
    sm._load_chart_manifest.cache_clear()
    try:
        payload = sm._read_vendor_js("mylib")
        # File was skipped, so payload is empty (no verified content).
        assert payload == ""
        err = capsys.readouterr().err
        assert "SHA-256 mismatch" in err
    finally:
        sm._load_chart_manifest.cache_clear()


def _mini_report():
    turns   = sm._extract_turns(sm._parse_jsonl(_FIXTURE))
    user_ts = sm._extract_user_timestamps(sm._parse_jsonl(_FIXTURE))
    return sm._build_report(
        "session", "test-slug", [("mini", turns, user_ts)],
        tz_offset_hours=0.0, tz_label="UTC",
    )


def test_render_html_chart_lib_none_omits_highcharts_bundle():
    html = sm.render_html(_mini_report(), variant="single", chart_lib="none")
    assert "Highcharts.chart" not in html
    assert 'id="chart-container"' not in html


def test_render_html_chart_lib_highcharts_inlines_bundle():
    html = sm.render_html(_mini_report(), variant="single", chart_lib="highcharts")
    # Inlined library + chart container both present; no CDN reference.
    assert "Highcharts.chart" in html
    assert 'id="chart-container"' in html
    assert "cdn.jsdelivr.net" not in html


def test_maybe_warn_chart_license_silent_for_none(capsys):
    sm._maybe_warn_chart_license("none", ["html"])
    err = capsys.readouterr().err
    assert "license" not in err.lower()


def test_maybe_warn_chart_license_warns_for_highcharts(capsys):
    sm._maybe_warn_chart_license("highcharts", ["html"])
    err = capsys.readouterr().err
    assert "non-commercial" in err


def test_maybe_warn_chart_license_silent_when_html_not_exported(capsys):
    sm._maybe_warn_chart_license("highcharts", ["text", "json"])
    err = capsys.readouterr().err
    assert err == ""


# --- uPlot + Chart.js renderers ----------------------------------------------

def test_render_chart_uplot_empty_turns_is_empty():
    body, head = sm._render_chart_uplot([])
    assert body == ""
    assert head == ""


def test_render_chart_chartjs_empty_turns_is_empty():
    body, head = sm._render_chart_chartjs([])
    assert body == ""
    assert head == ""


def test_vendor_manifest_loads_uplot_and_chartjs():
    libs = sm._load_chart_manifest().get("libraries", {})
    for name in ("uplot", "chartjs"):
        assert name in libs, f"{name} missing from manifest"
        assert libs[name]["license"] == "MIT"
        for f in libs[name]["files"]:
            assert {"name", "path", "sha256"} <= f.keys()
            assert len(f["sha256"]) == 64


def test_read_vendor_js_returns_real_payload_uplot():
    payload = sm._read_vendor_js("uplot")
    assert len(payload) > 30_000   # ~50 KB minified IIFE bundle
    assert "uPlot" in payload      # global namespace marker


def test_read_vendor_css_returns_real_payload_uplot():
    css = sm._read_vendor_css("uplot")
    assert len(css) > 500          # ~1.8 KB stylesheet
    assert ".uplot" in css         # uPlot's own class prefix


def test_read_vendor_css_chartjs_is_empty():
    # Chart.js ships no CSS — confirm the helper handles that cleanly.
    assert sm._read_vendor_css("chartjs") == ""


def test_read_vendor_js_returns_real_payload_chartjs():
    payload = sm._read_vendor_js("chartjs")
    assert len(payload) > 100_000  # ~204 KB UMD bundle
    assert "Chart" in payload


def _real_turns():
    """Real enriched turns from the fixture (with ``timestamp_fmt`` and
    other report-level keys the chart renderers rely on)."""
    rep = _mini_report()
    return [t for s in rep["sessions"] for t in s["turns"]]


def test_render_chart_uplot_emits_canvas_and_data():
    turns = _real_turns()
    assert turns, "fixture must have at least one turn"
    body, head = sm._render_chart_uplot(turns)
    assert 'id="chart-container"' in body
    assert 'class="chart-lazy"' in body
    assert "new uPlot(" in body                # init call
    assert "<style>" in head and "<script>" in head  # both blocks present
    assert ".uplot" in head                    # uPlot CSS inlined
    assert "uPlot" in head                     # uPlot JS inlined


def test_render_chart_chartjs_emits_canvas_and_data():
    turns = _real_turns()
    body, head = sm._render_chart_chartjs(turns)
    assert 'id="chart-container"' in body
    assert 'class="chart-lazy"' in body
    assert "new Chart(" in body                # init call
    assert "createElement('canvas')" in body   # canvas-based renderer
    assert "<script>" in head and "Chart" in head


def test_render_html_chart_lib_uplot_inlines_bundle():
    html = sm.render_html(_mini_report(), variant="single", chart_lib="uplot")
    assert "new uPlot(" in html
    assert ".uplot" in html
    assert 'id="chart-container"' in html
    assert "Highcharts.chart" not in html
    assert "cdn.jsdelivr.net" not in html


def test_render_html_chart_lib_chartjs_inlines_bundle():
    html = sm.render_html(_mini_report(), variant="single", chart_lib="chartjs")
    assert "new Chart(" in html
    assert 'id="chart-container"' in html
    assert "Highcharts.chart" not in html
    assert "cdn.jsdelivr.net" not in html


def test_maybe_warn_chart_license_silent_for_mit_libs(capsys):
    for lib in ("uplot", "chartjs"):
        sm._maybe_warn_chart_license(lib, ["html"])
    err = capsys.readouterr().err
    assert "non-commercial" not in err
    assert "license" not in err.lower()


# --- Usage Insights ----------------------------------------------------------
#
# Synthetic reports give us tight control over each threshold; the
# JSONL-fixture path is exercised separately via the existing tests.

def _synthetic_turn(cost=0.10, model="claude-sonnet-4-7", inp=100,
                    cread=0, cwrite=0, tools=None, ts="2026-04-01T12:00:00Z"):
    return {
        "cost_usd":           cost,
        "model":              model,
        "input_tokens":       inp,
        "cache_read_tokens":  cread,
        "cache_write_tokens": cwrite,
        "tool_use_names":     tools or [],
        "timestamp":          ts,
        "content_blocks":     {"thinking": 0, "tool_use": len(tools or []),
                                "text": 1, "tool_result": 0, "image": 0},
    }


def _synthetic_report(sessions, blocks=None, tz_offset_hours=10.0):
    """Build the minimum report shape `_compute_usage_insights` consumes."""
    total_cost = sum(t.get("cost_usd", 0.0)
                     for s in sessions for t in s.get("turns", []))
    return {
        "totals":          {"cost": total_cost, "turns":
                            sum(len(s.get("turns", [])) for s in sessions)},
        "sessions":        sessions,
        "session_blocks":  blocks or [],
        "tz_offset_hours": tz_offset_hours,
    }


def _by_id(insights, iid):
    for i in insights:
        if i["id"] == iid:
            return i
    raise KeyError(iid)


def test_usage_insights_zero_cost_short_circuits():
    """Empty / $0 reports return no candidates — avoids percentage divide-by-zero."""
    rep = _synthetic_report([{"turns": [], "subtotal": {"cost": 0.0},
                              "duration_seconds": 0}], blocks=[])
    assert sm._compute_usage_insights(rep) == []


def test_parallel_sessions_passes_threshold():
    """Two sessions overlapping in one 5h block → high parallel %."""
    sessions = [
        {"session_id": "s1", "duration_seconds": 1800,
         "subtotal": {"cost": 5.0},
         "turns": [_synthetic_turn(cost=5.0)]},
        {"session_id": "s2", "duration_seconds": 1800,
         "subtotal": {"cost": 5.0},
         "turns": [_synthetic_turn(cost=5.0)]},
    ]
    blocks = [{"cost_usd": 10.0, "sessions_touched": ["s1", "s2"]}]
    rep = _synthetic_report(sessions, blocks=blocks)
    res = sm._compute_usage_insights(rep)
    par = _by_id(res, "parallel_sessions")
    assert par["value"] == 100.0
    assert par["shown"] is True


def test_parallel_sessions_below_threshold_hidden():
    """Single session, single block → 0% parallel, hidden."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0},
                 "turns": [_synthetic_turn(cost=1.0)]}]
    blocks = [{"cost_usd": 1.0, "sessions_touched": ["s1"]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions, blocks=blocks))
    assert _by_id(res, "parallel_sessions")["shown"] is False


def test_long_sessions_threshold_8h():
    """One 9h session contributes 100% to long-session cost share."""
    sessions = [{"session_id": "s1", "duration_seconds": 9 * 3600,
                 "subtotal": {"cost": 10.0},
                 "turns": [_synthetic_turn(cost=10.0)]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    long_i = _by_id(res, "long_sessions")
    assert long_i["value"] == 100.0
    assert long_i["shown"] is True


def test_big_context_turns_only_counted_above_150k():
    """Two turns, one with 200k context, one with 50k — only the big one counts."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 2.0}, "turns": [
        _synthetic_turn(cost=1.0, inp=200_000, cread=0, cwrite=0),
        _synthetic_turn(cost=1.0, inp=50_000,  cread=0, cwrite=0),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    bc = _by_id(res, "big_context_turns")
    assert bc["value"] == 50.0  # 1 of 2 turns by cost
    assert bc["shown"] is True


def test_subagent_heavy_counts_task_tool_invocations():
    """Session with 4 Task tool calls trips the 3+ threshold."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 5.0}, "turns": [
        _synthetic_turn(cost=1.25, tools=["Task"]),
        _synthetic_turn(cost=1.25, tools=["Task", "Task"]),
        _synthetic_turn(cost=1.25, tools=["Task"]),
        _synthetic_turn(cost=1.25, tools=["Read"]),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    sa = _by_id(res, "subagent_heavy")
    assert sa["value"] == 100.0
    assert sa["shown"] is True


def test_top3_tools_min_calls_gate():
    """Below the 10-call gate, the insight stays hidden even if math works."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=1.0, tools=["Read", "Bash", "Edit"]),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    assert _by_id(res, "top3_tools")["shown"] is False


def test_top3_tools_passes_when_volume_high():
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=0.1, tools=["Read"] * 5 + ["Bash"] * 5 + ["Edit"] * 2),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    top3 = _by_id(res, "top3_tools")
    assert top3["shown"] is True
    assert top3["value"] == 100.0  # only 3 distinct tools


def test_off_peak_calibration_heavy_off_peak():
    """All turns at 03:00 local (UTC+10 → 17:00 UTC) → 100% off-peak, shown."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=0.5, ts="2026-04-01T17:00:00Z"),  # 03:00 local
        _synthetic_turn(cost=0.5, ts="2026-04-02T17:00:00Z"),  # 03:00 local
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions, tz_offset_hours=10.0))
    op = _by_id(res, "off_peak_share")
    assert op["value"] == 100.0
    assert op["shown"] is True


def test_off_peak_calibration_office_hours_hidden():
    """All turns at 13:00 local Mon — unremarkable, stays hidden."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=1.0, ts="2026-04-06T03:00:00Z"),  # Mon 13:00 +10
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions, tz_offset_hours=10.0))
    assert _by_id(res, "off_peak_share")["shown"] is False


def test_cost_concentration_min_turns_gate():
    """Only 5 turns → top-5 = 100% trivially; gate hides this case."""
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=0.2) for _ in range(5)
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    assert _by_id(res, "cost_concentration")["shown"] is False


def test_cost_concentration_shown_when_concentrated():
    """20 turns, top-5 dominate 90% of cost → fires."""
    turns = [_synthetic_turn(cost=1.0) for _ in range(5)] + \
            [_synthetic_turn(cost=0.01) for _ in range(15)]
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": sum(t["cost_usd"] for t in turns)},
                 "turns": turns}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    cc = _by_id(res, "cost_concentration")
    assert cc["shown"] is True
    assert cc["value"] > 90


def test_model_mix_single_family_hidden():
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0}, "turns": [
        _synthetic_turn(cost=1.0, model="claude-sonnet-4-7"),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    assert _by_id(res, "model_mix")["shown"] is False


def test_model_mix_multi_family_shown():
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 2.0}, "turns": [
        _synthetic_turn(cost=1.0, model="claude-opus-4-7"),
        _synthetic_turn(cost=1.0, model="claude-sonnet-4-7"),
    ]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    mm = _by_id(res, "model_mix")
    assert mm["shown"] is True
    assert "Opus" in mm["body"] or "Sonnet" in mm["body"]


def test_session_pacing_requires_two_sessions():
    sessions = [{"session_id": "s1", "duration_seconds": 600,
                 "subtotal": {"cost": 1.0},
                 "turns": [_synthetic_turn(cost=1.0)]}]
    res = sm._compute_usage_insights(_synthetic_report(sessions))
    assert _by_id(res, "session_pacing")["shown"] is False


def test_session_duration_seconds_stamped_on_real_fixture():
    """Regression guard for the new pre-computation in _build_report."""
    rep = _mini_report()
    for s in rep["sessions"]:
        assert "duration_seconds" in s
        assert isinstance(s["duration_seconds"], int)
        assert s["duration_seconds"] >= 0


def test_build_report_includes_usage_insights_key():
    rep = _mini_report()
    assert "usage_insights" in rep
    assert isinstance(rep["usage_insights"], list)
    for i in rep["usage_insights"]:
        for key in ("id", "headline", "body", "value",
                    "threshold", "shown", "always_on"):
            assert key in i, f"insight missing key {key}: {i}"


def test_html_dashboard_includes_panel_when_insights_present():
    """Dashboard variant should render the panel when at least one insight is shown.
    Uses the real fixture report and force-injects a heavy long_sessions
    insight (the threshold-bearing kind that drives the above-fold slot)."""
    rep = _mini_report()
    # Stamp a long duration on one session so `long_sessions` definitely fires.
    rep["sessions"][0]["duration_seconds"] = 12 * 3600
    rep["usage_insights"] = sm._compute_usage_insights(rep)
    html = sm.render_html(rep, variant="dashboard", chart_lib="none")
    assert 'class="usage-insights"' in html
    assert any(i["shown"] and not i.get("always_on") for i in rep["usage_insights"])


def test_html_detail_excludes_panel():
    """Detail variant must NOT render the panel — gated by include_insights."""
    rep = _mini_report()
    html = sm.render_html(rep, variant="detail", chart_lib="none")
    assert 'class="usage-insights"' not in html


def test_build_usage_insights_html_empty_returns_empty_string():
    assert sm._build_usage_insights_html([]) == ""
    assert sm._build_usage_insights_html([{"shown": False, "headline": "x",
                                            "body": "y", "value": 0,
                                            "always_on": False}]) == ""


def test_build_usage_insights_html_single_insight_skips_accordion():
    one = [{"id": "a", "shown": True, "headline": "100%",
            "body": " of cost is in one bucket.", "value": 100.0,
            "threshold": 1.0, "always_on": False}]
    out = sm._build_usage_insights_html(one)
    assert 'class="usage-insights"' in out
    assert "<details>" not in out
    assert "<strong>100%</strong>" in out


def test_build_usage_insights_html_escapes_dynamic_strings():
    """Belt-and-braces HTML escaping — guards against future regressions where
    a tool/model name might carry an angle bracket (e.g. <synthetic>)."""
    bad = [{"id": "a", "shown": True, "headline": "100%",
            "body": " of cost from <script>alert(1)</script>.", "value": 100.0,
            "threshold": 1.0, "always_on": False}]
    out = sm._build_usage_insights_html(bad)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_build_usage_insights_md_empty_returns_empty_string():
    assert sm._build_usage_insights_md([]) == ""


def test_build_usage_insights_md_emits_section_header():
    one = [{"id": "a", "shown": True, "headline": "100%",
            "body": " of cost is in one bucket.", "value": 100.0,
            "threshold": 1.0, "always_on": False}]
    out = sm._build_usage_insights_md(one)
    assert out.startswith("## Usage Insights")
    assert "- **100%**" in out

