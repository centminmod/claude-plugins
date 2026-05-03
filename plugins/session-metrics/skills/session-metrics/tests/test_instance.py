"""Instance-mode (--all-projects) tests — _list_all_projects discovery, _build_instance_report aggregation, _run_all_projects orchestration, instance-branch renderers.

Split out of test_session_metrics.py in v1.41.9 (Tier 4 of the
post-audit improvement plan; sibling-file pattern established in v1.41.8).

Run with: uv run python -m pytest tests/test_instance.py -v
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


# Autouse fixtures `isolate_projects_dir` and `_clear_pricing_cache` live
# in tests/conftest.py (lifted in v1.41.11).


# === BODY (extracted from test_session_metrics.py) ============================
# --- Instance mode (--all-projects) ------------------------------------------
#
# Covers _list_all_projects discovery, _build_instance_report aggregation,
# _run_all_projects orchestration, and the five instance-branch renderers.

def _write_turn(fh, *, session_id: str, uuid: str, ts: str, model: str,
                input_tokens: int, output_tokens: int,
                cache_read: int = 0, cache_write: int = 0,
                msg_id: str | None = None) -> None:
    """Append one assistant turn + its preceding user prompt to an open fh."""
    import json as _json
    # User prompt (so _load_session picks up user_ts for time-of-day)
    fh.write(_json.dumps({
        "type": "user",
        "uuid": f"u{uuid}",
        "timestamp": ts,
        "sessionId": session_id,
        "message": {"role": "user",
                    "content": [{"type": "text", "text": "hi"}]},
    }) + "\n")
    fh.write(_json.dumps({
        "type": "assistant",
        "uuid": f"a{uuid}",
        "timestamp": ts,
        "sessionId": session_id,
        "message": {
            "id": msg_id or f"m{uuid}",
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        },
    }) + "\n")


def _make_instance_fixture(root: Path, specs: dict[str, list[dict]]) -> Path:
    """Materialise a fake ``~/.claude/projects/`` tree under ``root``.

    ``specs`` is ``{slug: [session_spec, ...]}`` where each session_spec is
    ``{"id": str, "turns": [{model, in, out, cache_read?, cache_write?}]}``.

    Returns the projects dir path (``root``) to be passed as the
    ``_PROJECTS_DIR_OVERRIDE``.
    """
    root.mkdir(parents=True, exist_ok=True)
    base_ts = 1_700_000_000
    for slug, sessions in specs.items():
        proj = root / slug
        proj.mkdir(parents=True, exist_ok=True)
        for si, sess in enumerate(sessions):
            sid = sess["id"]
            path = proj / f"{sid}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for ti, t in enumerate(sess["turns"]):
                    ts_epoch = base_ts + si * 3600 + ti * 60
                    ts_iso = (
                        time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.gmtime(ts_epoch)) + "Z"
                    )
                    _write_turn(
                        fh, session_id=sid, uuid=f"{slug}_{si}_{ti}",
                        ts=ts_iso, model=t["model"],
                        input_tokens=t.get("in", 0),
                        output_tokens=t.get("out", 0),
                        cache_read=t.get("cache_read", 0),
                        cache_write=t.get("cache_write", 0),
                        msg_id=f"{slug}-{si}-{ti}",
                    )
    return root


@pytest.fixture
def instance_env(tmp_path, monkeypatch):
    """Isolate instance-mode side effects (projects dir, parse cache, cwd)."""
    projects_dir = tmp_path / "projects"
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", projects_dir)
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    monkeypatch.chdir(tmp_path)  # so _export_dir() lands under tmp_path
    return tmp_path, projects_dir


def test_list_all_projects_skips_subagents_and_hidden(instance_env):
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "sess-alpha-1",
                              "turns": [{"model": "claude-opus-4-7",
                                         "in": 100, "out": 50}]}],
        "-home-user-beta":  [{"id": "sess-beta-1",
                              "turns": [{"model": "claude-sonnet-4-7",
                                         "in": 200, "out": 80}]}],
    })
    # Junk that should be skipped
    (projects_dir / ".hidden").mkdir()
    (projects_dir / ".hidden" / "fake.jsonl").write_text("{}\n")
    (projects_dir / "subagents").mkdir()
    (projects_dir / "not a slug!").mkdir()
    (projects_dir / "empty-shell").mkdir()  # no JSONLs → skipped
    # Directory that passes _SLUG_RE but has no JSONLs
    (projects_dir / "-home-user-empty").mkdir()

    discovered = sm._list_all_projects()
    slugs = {s for s, _ in discovered}
    assert slugs == {"-home-user-alpha", "-home-user-beta"}
    # Every returned entry is a directory under projects_dir
    for slug, path in discovered:
        assert path.parent == projects_dir
        assert path.is_dir()


def test_run_all_projects_aggregates_correctly(instance_env, capsys):
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })

    sm._run_all_projects(
        formats=["html", "md", "csv", "json"],
        tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )

    # Dated subfolder under exports/session-metrics/instance/
    runs = list((tmp_path / "exports" / "session-metrics"
                 / "instance").iterdir())
    assert len(runs) == 1
    run = runs[0]
    for fmt in ("html", "md", "csv", "json"):
        assert (run / f"index.{fmt}").exists(), f"missing index.{fmt}"
    # No drilldown folder
    assert not (run / "projects").exists()


def test_instance_report_shape(instance_env):
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })
    # Drive _run_all_projects with no formats so only in-memory assertions matter
    captured = {}
    real_dispatch = sm._dispatch_instance

    def spy(instance_report, project_reports, formats, **kw):
        captured["ir"] = instance_report
        captured["prs"] = project_reports
        return real_dispatch(instance_report, project_reports, formats, **kw)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "_dispatch_instance", spy)
        sm._run_all_projects(
            formats=[], tz_offset=0.0, tz_label="UTC",
            use_cache=False, chart_lib="none",
            include_subagents=False, drilldown=False,
        )
    ir = captured["ir"]
    assert ir["mode"] == "instance"
    assert ir["slug"] == "all-projects"
    assert ir["project_count"] == 2
    assert ir["session_count"] == 2
    assert isinstance(ir["projects"], list) and len(ir["projects"]) == 2
    # No per-turn leakage: session summaries have turn_count but no "turns" key
    for proj in ir["projects"]:
        for s in proj.get("sessions", []):
            assert "turns" not in s
            assert "turn_count" in s
    # Totals actually aggregated
    assert ir["totals"]["input"] == 1000 + 2000
    assert ir["totals"]["output"] == 500 + 1000
    # Models merged
    assert set(ir["models"].keys()) == {
        "claude-opus-4-7", "claude-sonnet-4-7"}


def test_instance_breakdown_sorted_by_cost_desc(instance_env):
    tmp_path, projects_dir = instance_env
    # Make three projects with markedly different costs (use Opus to amplify)
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [  # small
            {"model": "claude-opus-4-7", "in": 100, "out": 50}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [  # huge
            {"model": "claude-opus-4-7", "in": 100_000, "out": 50_000}]}],
        "-home-user-gamma": [{"id": "c1", "turns": [  # medium
            {"model": "claude-opus-4-7", "in": 10_000, "out": 5_000}]}],
    })
    captured = {}

    def spy(instance_report, project_reports, formats, **kw):
        captured["ir"] = instance_report

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "_dispatch_instance", spy)
        sm._run_all_projects(
            formats=[], tz_offset=0.0, tz_label="UTC",
            use_cache=False, chart_lib="none",
            include_subagents=False, drilldown=False,
        )
    slugs_in_order = [p["slug"] for p in captured["ir"]["projects"]]
    assert slugs_in_order == [
        "-home-user-beta", "-home-user-gamma", "-home-user-alpha"
    ], f"unsorted: {slugs_in_order}"
    costs = [p["cost_usd"] for p in captured["ir"]["projects"]]
    assert costs == sorted(costs, reverse=True)


def _run_instance_capture_html(instance_env, *, drilldown: bool) -> tuple[str, Path]:
    """Helper: run instance mode, return (html_text, run_dir)."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })
    sm._run_all_projects(
        formats=["html"], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=drilldown,
    )
    runs = list((tmp_path / "exports" / "session-metrics"
                 / "instance").iterdir())
    assert len(runs) == 1
    run = runs[0]
    html_path = run / "index.html"
    return html_path.read_text(encoding="utf-8"), run


def test_render_html_instance_suppresses_drawer(instance_env):
    html, _ = _run_instance_capture_html(instance_env, drilldown=False)
    # The per-turn drawer is a detail-page artefact — it must not appear
    # in the instance index. Same for the turn-level data blob.
    assert 'id="drawer"' not in html
    assert 'id="turn-data-json"' not in html


def test_render_html_instance_hyperlinks_to_project_drilldowns(instance_env):
    html, run = _run_instance_capture_html(instance_env, drilldown=True)
    # Instance index links to the dashboard half of each drilldown pair
    assert 'href="projects/-home-user-alpha_dashboard.html"' in html
    assert 'href="projects/-home-user-beta_dashboard.html"' in html
    # Both halves of each drilldown pair exist on disk
    assert (run / "projects" / "-home-user-alpha_dashboard.html").exists()
    assert (run / "projects" / "-home-user-alpha_detail.html").exists()
    assert (run / "projects" / "-home-user-beta_dashboard.html").exists()
    assert (run / "projects" / "-home-user-beta_detail.html").exists()


def test_render_html_instance_no_drilldown_flag(instance_env):
    html, run = _run_instance_capture_html(instance_env, drilldown=False)
    assert 'href="projects/' not in html
    assert not (run / "projects").exists()


def test_render_csv_instance_has_project_slug_column(instance_env):
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })
    sm._run_all_projects(
        formats=["csv"], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )
    run = next(iter(
        (tmp_path / "exports" / "session-metrics" / "instance").iterdir()))
    content = (run / "index.csv").read_text(encoding="utf-8")
    header = content.splitlines()[1]  # [0] is the skill-version comment row
    assert header.startswith("project_slug,")
    # Two per-session data rows — filter to rows whose first column looks
    # like a project slug (starts with "-"), which skips the "# TOTALS" /
    # "# PROJECTS" sub-section headers that follow.
    data_rows = [ln for ln in content.splitlines()
                 if ln and ln[0] == "-" and "," in ln]
    assert len(data_rows) >= 2, f"expected >=2 slug rows; got {data_rows}"
    # Drop the "projects breakdown" rollup (one row per project) so only
    # per-session rows remain — they have the session_id in column 1.
    per_session = [row for row in data_rows
                   if row.split(",")[1] in {"a1", "b1"}]
    assert len(per_session) == 2
    slugs_seen = {row.split(",")[0] for row in per_session}
    assert slugs_seen == {"-home-user-alpha", "-home-user-beta"}


def test_render_md_instance_has_projects_breakdown(instance_env):
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })
    sm._run_all_projects(
        formats=["md"], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )
    run = next(iter(
        (tmp_path / "exports" / "session-metrics" / "instance").iterdir()))
    md = (run / "index.md").read_text(encoding="utf-8")
    # Contains the instance dashboard header + both project rows
    assert "all-projects" in md.lower() or "instance" in md.lower()
    assert "-home-user-alpha" in md
    assert "-home-user-beta" in md


# --- v1.14.1: instance daily token breakdown + chart axis label -------------
#
# Pre-v1.14.1 the daily buckets only tracked cost + total_tokens, so the
# instance HTML chart flatlined the four stacked-bar series (input, output,
# cache_read, cache_write) at 0 and the x-axis was labelled "Turn" even
# though each data point was a calendar day. These tests pin the fix.

def test_build_instance_daily_tracks_per_token_breakdowns(instance_env):
    """_build_instance_daily must accumulate input/output/cache_read/
    cache_write totals per day so the instance chart can render real
    stacked-bar data rather than hardcoded zeros."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500,
             "cache_read": 400, "cache_write": 200}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000,
             "cache_read": 800, "cache_write": 300}]}],
    })
    captured = {}

    def spy(instance_report, project_reports, formats, **kw):
        captured["ir"] = instance_report

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "_dispatch_instance", spy)
        sm._run_all_projects(
            formats=[], tz_offset=0.0, tz_label="UTC",
            use_cache=False, chart_lib="none",
            include_subagents=False, drilldown=False,
        )
    daily = captured["ir"]["daily"]
    assert daily, "expected at least one daily bucket"
    # Every bucket carries the four per-token subcategories
    required_keys = {"date", "cost", "tokens",
                     "input", "output", "cache_read", "cache_write"}
    for b in daily:
        assert required_keys.issubset(b.keys()), \
            f"missing keys in bucket {b}"
        # And they're integers, non-negative
        for k in ("input", "output", "cache_read", "cache_write"):
            assert isinstance(b[k], int) and b[k] >= 0

    # Summing across days must reproduce the grand totals
    assert sum(b["input"] for b in daily)       == 3000   # 1000 + 2000
    assert sum(b["output"] for b in daily)      == 1500   # 500  + 1000
    assert sum(b["cache_read"] for b in daily)  == 1200   # 400  + 800
    assert sum(b["cache_write"] for b in daily) == 500    # 200  + 300


@pytest.mark.parametrize("chart_lib", ["highcharts", "uplot", "chartjs"])
def test_render_chart_accepts_x_title_override(chart_lib):
    """All three JS chart renderers must accept an x_title kwarg so the
    instance dashboard can label the axis 'Day' instead of the default
    'Turn'. The 'none' renderer accepts x_title for API parity but is
    exercised trivially below.

    We generate >_CHART_PAGE (60) points so the pagination header renders —
    Chart.js only surfaces x_title in the pagination label, whereas
    Highcharts and uPlot also embed it directly in the chart JS.
    """
    # 61 synthetic turns → forces multi-page pagination so every renderer
    # has an opportunity to emit the x_title string somewhere in its body.
    turns = [
        {"timestamp":     f"2026-04-{((i % 28) + 1):02d}T12:00:00Z",
         "timestamp_fmt": f"2026-04-{((i % 28) + 1):02d}",
         "input_tokens": 100 + i, "output_tokens": 50 + i,
         "cache_read_tokens": 40, "cache_write_tokens": 20,
         "total_tokens": 210 + 2 * i, "cost_usd": 0.001 * (i + 1),
         "model": "claude-opus-4-7"}
        for i in range(sm._CHART_PAGE + 1)  # 61 points → 2 pages
    ]
    renderer = sm.CHART_RENDERERS[chart_lib]
    body_turn, _ = renderer(turns)              # default "Turn"
    body_day,  _ = renderer(turns, x_title="Day")
    # The default body references "Turn" in its pagination header / labels
    assert "Turn" in body_turn, (
        f"{chart_lib}: default x_title 'Turn' missing from body"
    )
    # The overridden body must reference "Day" and not bleed through to 'Turn'
    assert "Day" in body_day, (
        f"{chart_lib}: x_title override 'Day' absent from body"
    )
    assert body_turn != body_day, (
        f"{chart_lib}: body unchanged when x_title overridden"
    )


def test_render_chart_none_accepts_x_title():
    """The 'none' renderer ignores x_title but must not raise on it."""
    out = sm._render_chart_none([], x_title="Day")
    assert out == ("", "")


def test_render_html_instance_chart_uses_day_axis_label(instance_env):
    """End-to-end: instance HTML uses the daily cost rail (not Highcharts 3D).
    One bar per calendar day showing daily cost, horizontally scrollable."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500,
             "cache_read": 400, "cache_write": 200}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000,
             "cache_read": 800, "cache_write": 300}]}],
    })
    sm._run_all_projects(
        formats=["html"], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="highcharts",
        include_subagents=False, drilldown=False,
    )
    run = next(iter(
        (tmp_path / "exports" / "session-metrics" / "instance").iterdir()))
    html = (run / "index.html").read_text(encoding="utf-8")
    # Instance page uses the daily cost rail, not Highcharts 3D.
    assert "Highcharts.chart(" not in html, (
        "instance HTML must not contain Highcharts 3D chart"
    )
    assert 'id="costail-data"' in html, (
        "instance HTML must contain the daily cost rail JSON data blob"
    )
    assert 'id="costail-scroll"' in html, (
        "instance HTML must contain the daily cost rail scroll container"
    )
    # The per-session chartrail must not appear on the instance page.
    assert 'id="chartrail-data"' not in html, (
        "instance HTML must not contain the per-session chartrail"
    )


# ---------------------------------------------------------------------------
# Tier 5 — ThreadPoolExecutor parallel-branch coverage (_dispatch.py:371-376).
# The "len(project_inputs) > 1" branch was previously exercised only as a
# side effect of other instance tests; nothing asserted that the pool was
# actually used or that its output matches the serial fallback. These three
# tests pin both behaviours explicitly so a refactor can't silently regress
# to single-threaded execution or drift parallel/serial output.
# ---------------------------------------------------------------------------

class _TrackingExec:
    """Fake ThreadPoolExecutor that records construction and runs jobs serially."""
    instances: list["_TrackingExec"] = []

    def __init__(self, max_workers=None):
        self.max_workers = max_workers
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, items):
        return [fn(i) for i in items]


def _patch_executor(monkeypatch, fake_cls):
    _TrackingExec.instances = []
    dispatch_mod = sys.modules["_dispatch"]
    monkeypatch.setattr(dispatch_mod, "ThreadPoolExecutor", fake_cls)


def test_parallel_branch_uses_thread_pool_when_multiple_projects(
    instance_env, monkeypatch
):
    """With >1 project, ThreadPoolExecutor must be instantiated exactly once
    with max_workers ≤ min(8, cpu_count)."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
    })
    _patch_executor(monkeypatch, _TrackingExec)

    sm._run_all_projects(
        formats=[], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )

    assert len(_TrackingExec.instances) == 1, (
        f"expected exactly one ThreadPoolExecutor for >1 project, "
        f"got {len(_TrackingExec.instances)}"
    )
    workers = _TrackingExec.instances[0].max_workers
    assert isinstance(workers, int) and 1 <= workers <= 8, (
        f"max_workers must be a positive int ≤ 8, got {workers!r}"
    )


def test_single_project_skips_thread_pool(instance_env, monkeypatch):
    """With exactly one project, the parallel branch is skipped — the
    `else` clause must be taken and ThreadPoolExecutor never instantiated."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7", "in": 1000, "out": 500}]}],
    })
    _patch_executor(monkeypatch, _TrackingExec)

    sm._run_all_projects(
        formats=[], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )

    assert _TrackingExec.instances == [], (
        f"single-project run must not construct ThreadPoolExecutor, "
        f"got {len(_TrackingExec.instances)}"
    )


def test_parallel_dispatch_matches_sequential_output(instance_env, monkeypatch):
    """The parallel branch must produce byte-identical instance_report and
    per-project reports vs. the serial fallback. Drift here would mean
    `_build_report` is not actually pure over its sessions_raw input."""
    tmp_path, projects_dir = instance_env
    _make_instance_fixture(projects_dir, {
        "-home-user-alpha": [{"id": "a1", "turns": [
            {"model": "claude-opus-4-7",   "in": 1000, "out": 500}]}],
        "-home-user-beta":  [{"id": "b1", "turns": [
            {"model": "claude-sonnet-4-7", "in": 2000, "out": 1000}]}],
        "-home-user-gamma": [{"id": "c1", "turns": [
            {"model": "claude-opus-4-7",   "in": 3000, "out": 1500}]}],
    })

    captured: dict[str, list] = {"runs": []}
    real_dispatch = sm._dispatch_instance

    def spy(instance_report, project_reports, formats, **kw):
        captured["runs"].append({
            "ir": instance_report,
            "prs": project_reports,
        })
        # Skip real dispatch — we only need the in-memory shape.
        return None

    # Run 1: real ThreadPoolExecutor (parallel path).
    monkeypatch.setattr(sm, "_dispatch_instance", spy)
    sm._run_all_projects(
        formats=[], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )

    # Run 2: forced-serial via _TrackingExec (also routes through ex.map).
    _patch_executor(monkeypatch, _TrackingExec)
    sm._run_all_projects(
        formats=[], tz_offset=0.0, tz_label="UTC",
        use_cache=False, chart_lib="none",
        include_subagents=False, drilldown=False,
    )

    assert len(captured["runs"]) == 2
    parallel, serial = captured["runs"]

    # Per-project reports (order-preserved by submit-order in both branches).
    assert len(parallel["prs"]) == len(serial["prs"]) == 3
    for pp, sp in zip(parallel["prs"], serial["prs"]):
        assert pp["slug"] == sp["slug"]
        assert pp["totals"] == sp["totals"], (
            f"per-project totals diverged for {pp['slug']}"
        )
        # Whole-report equality — `_build_report` must be pure over its input.
        # `generated_at` reflects wall-clock time and is the only known
        # field that legitimately differs between two consecutive runs.
        pp_filtered = {k: v for k, v in pp.items() if k != "generated_at"}
        sp_filtered = {k: v for k, v in sp.items() if k != "generated_at"}
        assert pp_filtered == sp_filtered, (
            f"per-project report drift for {pp['slug']}: "
            f"differing keys = "
            f"{set(pp_filtered) ^ set(sp_filtered) or 'same keys, different values'}"
        )

    # Instance-level totals and aggregates must match exactly.
    assert parallel["ir"]["totals"] == serial["ir"]["totals"]
    assert parallel["ir"]["models"] == serial["ir"]["models"]
    assert parallel["ir"]["project_count"] == serial["ir"]["project_count"]
    assert parallel["ir"]["session_count"] == serial["ir"]["session_count"]
    # Project ordering inside instance_report must also be deterministic.
    assert [p["slug"] for p in parallel["ir"]["projects"]] == \
           [p["slug"] for p in serial["ir"]["projects"]]

