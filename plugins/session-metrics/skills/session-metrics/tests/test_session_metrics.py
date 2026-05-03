"""Unit + integration tests for session-metrics.py.

Run with: uv run python -m pytest tests/ -v
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


# Load main first — the compare module looks up helpers from
# sys.modules["session_metrics"] at call time, so it must already
# be registered when any compare test runs.
#
# Reuse instances that another split test module (e.g. test_pricing.py)
# already registered. ``_load_module`` re-execs unconditionally — without
# this guard, the cross-file ``sm`` reference would point to a different
# module object than the one leaf modules see via ``_sm()``, and
# monkeypatch writes would silently miss.
sm  = sys.modules.get("session_metrics")          or _load_module("session_metrics",         _SCRIPT)
smc = sys.modules.get("session_metrics_compare")  or _load_module("session_metrics_compare", _COMPARE)


# --- Global test-isolation guard ---------------------------------------------
#
# Several helpers in the script (``_touch_compare_state_marker``,
# ``_find_jsonl_files``, ``_resolve_session``) reach into ``_projects_dir()``
# directly. If a test exercises a code path that writes into the projects
# dir — most notably ``_touch_compare_state_marker`` which ``mkdir``s the
# slug directory as a side effect — and does NOT monkeypatch
# ``_PROJECTS_DIR_OVERRIDE``, the write lands in the *real*
# ``~/.claude/projects/``. Over many test runs that leaves hundreds of
# pytest-named leftover directories that pollute the user's actual projects
# catalogue.
#
# This autouse fixture redirects ``_projects_dir()`` to a per-test tmp dir
# for every test in the suite by setting the ``CLAUDE_PROJECTS_DIR`` env
# var (the public override documented for end-users). We deliberately do
# NOT set ``_PROJECTS_DIR_OVERRIDE`` here — that takes *higher* precedence
# than the env var and would clobber the ~30 existing tests that set
# ``CLAUDE_PROJECTS_DIR`` themselves to point at a fixture tree. Tests
# that later call ``monkeypatch.setenv`` override this safely (pytest's
# monkeypatch stacks values and unwinds them in LIFO order at teardown).
# Tests that use ``_PROJECTS_DIR_OVERRIDE`` directly (e.g. the
# ``instance_env`` fixture) also override this because that var has
# higher precedence inside ``_projects_dir()``.
#
# Tests that legitimately need to read the user's real projects dir can
# opt out with ``@pytest.mark.real_projects_dir``.
@pytest.fixture(autouse=True)
def isolate_projects_dir(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("real_projects_dir"):
        return
    safe = tmp_path / "_autouse_projects"
    safe.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(safe))


# v1.41.0: ``_pricing_for`` is wrapped in ``functools.lru_cache``. Most tests
# don't mind the cache (deterministic input → deterministic output), but the
# unknown-model tests at lines ~7351–7369 monkeypatch ``_UNKNOWN_MODELS_SEEN``
# and rely on the side effect refiring per call. Clearing the cache before
# every test guarantees that contract holds even if a future test reuses an
# unknown model name across cases.
@pytest.fixture(autouse=True)
def _clear_pricing_cache():
    sm._pricing_for.cache_clear()
    yield


# --- Cost math ---------------------------------------------------------------

def test_cost_opus_all_buckets():
    usage = {
        "input_tokens": 120,
        "output_tokens": 80,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 1000,
    }
    # Opus 4.7 new tier: 120*5 + 80*25 + 500*0.5 + 1000*6.25
    #                  = 600 + 2000 + 250 + 6250 = 9100 per M
    # Legacy fallback path: no nested cache_creation, so the full 1000 tokens
    # price at the 5m rate (6.25/M).
    assert sm._cost(usage, "claude-opus-4-7") == pytest.approx(0.00910, abs=1e-7)


def test_cost_splits_5m_and_1h_when_nested_present():
    """Nested ``cache_creation`` object triggers the split pricing path."""
    usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1000,  # Sum — not used when nested present
        "cache_creation": {
            "ephemeral_5m_input_tokens": 600,
            "ephemeral_1h_input_tokens": 400,
        },
    }
    # Sonnet 4.7: 5m = 3.75, 1h = 6.00
    # 600*3.75 + 400*6.00 = 2250 + 2400 = 4650 per M = 0.00465
    assert sm._cost(usage, "claude-sonnet-4-7") == pytest.approx(0.00465, abs=1e-7)


def test_cache_write_split_fallback_without_nested():
    """Legacy transcripts (no nested cache_creation) charge everything at 5m."""
    usage = {
        "cache_creation_input_tokens": 1000,
    }
    tokens_5m, tokens_1h = sm._cache_write_split(usage)
    assert tokens_5m == 1000
    assert tokens_1h == 0


def test_cache_write_split_reads_nested_when_present():
    usage = {
        "cache_creation_input_tokens": 1000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 200,
            "ephemeral_1h_input_tokens": 800,
        },
    }
    tokens_5m, tokens_1h = sm._cache_write_split(usage)
    assert tokens_5m == 200
    assert tokens_1h == 800


def test_no_cache_cost_folds_cache_tokens():
    usage = {
        "input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500,
    }
    # hypothetical: (10 + 1000 + 500) * 5/M + 5 * 25/M  (Opus 4.7 new tier)
    expected = (1510 * 5 + 5 * 25) / 1_000_000
    assert sm._no_cache_cost(usage, "claude-opus-4-7") == pytest.approx(expected)


# --- User-prompt filter ------------------------------------------------------

def test_is_user_prompt_text_list():
    assert sm._is_user_prompt({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}})


def test_is_user_prompt_image_list():
    assert sm._is_user_prompt({"type": "user", "message": {"content": [{"type": "image"}]}})


def test_is_user_prompt_plain_string():
    assert sm._is_user_prompt({"type": "user", "message": {"content": "hello"}})


def test_is_user_prompt_excludes_tool_result():
    assert not sm._is_user_prompt({"type": "user", "message": {"content": [{"type": "tool_result"}]}})


def test_is_user_prompt_excludes_meta():
    assert not sm._is_user_prompt({"type": "user", "isMeta": True,
                                    "message": {"content": [{"type": "text", "text": "x"}]}})


def test_is_user_prompt_excludes_empty_string():
    assert not sm._is_user_prompt({"type": "user", "message": {"content": ""}})


def test_is_user_prompt_excludes_assistant_type():
    assert not sm._is_user_prompt({"type": "assistant", "message": {"content": "hi"}})


# --- Dedup + timestamp extraction on the fixture ----------------------------

def test_fixture_dedup_keeps_last_write():
    entries = sm._parse_jsonl(_FIXTURE)
    turns = sm._extract_turns(entries)
    # msg_A..D (legacy flat cache writes) + msg_E (pure 1h) + msg_F (mix) = 6
    assert len(turns) == 6
    by_id = {t["message"]["id"]: t["message"]["usage"] for t in turns}
    # msg_A appears twice; the last write (120/80/500/1000) must win
    assert by_id["msg_A"]["input_tokens"] == 120
    assert by_id["msg_A"]["cache_read_input_tokens"] == 500


def test_fixture_user_timestamps_default_excludes_sidechain_and_tool_results():
    entries = sm._parse_jsonl(_FIXTURE)
    ts = sm._extract_user_timestamps(entries)
    # u2, u6, u8, u9 are text prompts.  u1 meta, u3/u4/u7 tool_result,
    # u5 sidechain — all excluded.
    assert len(ts) == 4


def test_fixture_user_timestamps_include_sidechain_adds_one():
    entries = sm._parse_jsonl(_FIXTURE)
    ts = sm._extract_user_timestamps(entries, include_sidechain=True)
    # adds u5 sidechain text
    assert len(ts) == 5


# --- End-to-end totals on the fixture ---------------------------------------

def _build_fixture_report():
    sid, turns, user_ts = sm._load_session(_FIXTURE, include_subagents=False)
    return sm._build_report("session", "test-slug", [(sid, turns, user_ts)])


def test_fixture_total_cost_exact():
    r = _build_fixture_report()
    # Opus 4.7 ($5/$25/$0.50/$6.25/$10.00), Sonnet 4.7 ($3/$15/$0.30/$3.75/$6.00).
    # msg_A (opus, deduped, legacy flat cwr):   120*5 + 80*25 + 500*0.5 + 1000*6.25       = 0.00910
    # msg_B (sonnet):                            10*3 + 20*15 + 2000*0.3 + 0              = 0.00093
    # msg_C (opus):                               5*5 + 15*25 + 3000*0.5 + 0              = 0.00190
    # msg_D (sonnet):                           200*3 + 300*15 + 1500*0.3 + 0             = 0.00555
    # msg_E (opus, pure 1h):                     10*5 + 20*25 + 0 + 500*10.00             = 0.00555
    # msg_F (sonnet, mix 5m=600 + 1h=400):        5*3 + 10*15 + 0 + 600*3.75 + 400*6.00   = 0.004815
    # Total = 0.027845
    assert r["totals"]["cost"] == pytest.approx(0.027845, abs=1e-7)


def test_fixture_turns_count_and_models():
    r = _build_fixture_report()
    assert r["totals"]["turns"] == 6
    assert r["models"]["claude-opus-4-7"]["turns"] == 3       # msg_A, msg_C, msg_E
    assert r["models"]["claude-sonnet-4-7"]["turns"] == 3     # msg_B, msg_D, msg_F
    # v1.34.0: per-model cost is now surfaced alongside turn count (P2.1)
    assert r["models"]["claude-opus-4-7"]["cost_usd"] >= 0.0
    assert r["models"]["claude-sonnet-4-7"]["cost_usd"] >= 0.0


def test_fixture_time_of_day_total_is_user_prompt_count():
    r = _build_fixture_report()
    # 4 real user prompts — must NOT equal the user-type entry count in the file
    assert r["time_of_day"]["message_count"] == 4


# --- Cache TTL drilldown (Proposal A) ---------------------------------------

def test_fixture_ttl_classification_per_turn():
    """Each turn carries a correct `cache_write_ttl` derived from its split."""
    r = _build_fixture_report()
    # Index 1 = msg_A (legacy flat → classified as 5m via fallback)
    assert r["sessions"][0]["turns"][0]["cache_write_ttl"] == "5m"
    # Index 5 = msg_E (pure 1h)
    msg_E = r["sessions"][0]["turns"][4]
    assert msg_E["cache_write_ttl"] == "1h"
    assert msg_E["cache_write_5m_tokens"] == 0
    assert msg_E["cache_write_1h_tokens"] == 500
    # Index 6 = msg_F (mix)
    msg_F = r["sessions"][0]["turns"][5]
    assert msg_F["cache_write_ttl"] == "mix"
    assert msg_F["cache_write_5m_tokens"] == 600
    assert msg_F["cache_write_1h_tokens"] == 400


def test_fixture_totals_ttl_aggregation():
    r = _build_fixture_report()
    t = r["totals"]
    # 5m buckets: msg_A 1000 (flat fallback) + msg_F 600 = 1600
    assert t["cache_write_5m"] == 1600
    # 1h buckets: msg_E 500 + msg_F 400 = 900
    assert t["cache_write_1h"] == 900
    # Extra cost paid for the 1h tier (delta vs. 5m rate):
    #   msg_E: 500 * (10.00 - 6.25)/M = 0.001875 (opus)
    #   msg_F: 400 * (6.00 - 3.75)/M  = 0.000900 (sonnet)
    # Total = 0.002775
    assert t["extra_1h_cost"] == pytest.approx(0.002775, abs=1e-7)


def test_has_1h_cache_detects_fixture():
    r = _build_fixture_report()
    assert sm._has_1h_cache(r) is True


def test_has_1h_cache_false_on_legacy_only():
    """A report built from only legacy flat-cache turns reports False."""
    legacy_entries = sm._parse_jsonl(_FIXTURE)
    # Strip msg_E, msg_F and their paired users from the raw entries
    keep = [e for e in legacy_entries
            if e.get("uuid") not in {"u8", "a5", "u9", "a6"}]
    # Extract turns from the trimmed set and build a synthetic report
    trimmed_turns = sm._extract_turns(keep)
    user_ts = sm._extract_user_timestamps(keep)
    r = sm._build_report("session", "test-slug", [("s1", trimmed_turns, user_ts)])
    assert sm._has_1h_cache(r) is False


def test_csv_has_ttl_columns():
    r = _build_fixture_report()
    csv_out = sm.render_csv(r)
    header = csv_out.splitlines()[1]  # [0] is the skill-version comment row
    assert "cache_write_5m_tokens" in header
    assert "cache_write_1h_tokens" in header
    assert "cache_write_ttl" in header


def test_json_has_ttl_totals_keys():
    r = _build_fixture_report()
    import json as _json
    data = _json.loads(sm.render_json(r))
    assert "cache_write_5m" in data["totals"]
    assert "cache_write_1h" in data["totals"]
    assert "extra_1h_cost" in data["totals"]
    # Per-turn nested fields
    t = data["sessions"][0]["turns"][-1]  # msg_F
    assert t["cache_write_ttl"] == "mix"
    assert t["cache_write_5m_tokens"] == 600
    assert t["cache_write_1h_tokens"] == 400


def test_text_render_includes_legend_and_1h_annotation():
    r = _build_fixture_report()
    text = sm.render_text(r)
    # Legend header block present
    assert "Columns:" in text
    assert "CacheRd" in text
    # 1h-tier annotation surfaces the `*` suffix and footer explanation
    assert "*" in text
    assert "Extra cost paid for 1h cache tier" in text


def test_md_render_includes_legend_and_annotation():
    r = _build_fixture_report()
    md = sm.render_md(r)
    assert "## Column legend" in md
    assert "1-hour TTL tier" in md
    # Summary card line for the extra 1h cost
    assert "Extra cost paid for 1h cache tier" in md


def test_html_render_includes_legend_and_badge():
    r = _build_fixture_report()
    html = sm.render_html(r, variant="single")
    assert 'class="legend-block"' in html
    # TTL badge renders on the 1h and mix rows
    assert 'class="badge-ttl ttl-1h"' in html
    assert 'class="badge-ttl ttl-mix"' in html
    # Cache TTL mix dashboard card
    assert "Cache TTL mix" in html


def test_html_escapes_synthetic_model_name():
    # CC writes `model: "<synthetic>"` for no-op assistant entries (local-command
    # caveats, API errors). Without escaping, `<synthetic>` renders as an unknown
    # HTML tag and the model cell appears blank. This test guards both the
    # timeline row and the Models summary row.
    r = _build_fixture_report()
    # Inject a synthetic turn into the first session + models dict
    syn_turn = dict(r["sessions"][0]["turns"][0])
    syn_turn["model"] = "<synthetic>"
    r["sessions"][0]["turns"].append(syn_turn)
    # v1.34.0: ``models`` is now ``{name: {turns, cost_usd}}`` (P2.1)
    r["models"]["<synthetic>"] = {"turns": 1, "cost_usd": 0.0}

    html = sm.render_html(r, variant="single")
    # Literal `<synthetic>` must NOT appear outside harmless contexts. Check the
    # two specific rendering sites are escaped.
    assert '<td class="model"><synthetic></td>' not in html
    assert '<td><code><synthetic></code></td>' not in html
    assert '<td class="model">&lt;synthetic&gt;</td>' in html
    assert '<td><code>&lt;synthetic&gt;</code></td>' in html


# --- Resume detection (Phase 3) ---------------------------------------------

_EXIT_USER_ENTRY = {
    "type": "user",
    "message": {"content": "<command-name>/exit</command-name>\n"},
}
_EXIT_STDOUT_ENTRY = {
    "type": "user",
    "message": {"content": "<local-command-stdout>See ya!</local-command-stdout>"},
}
_CLEAR_USER_ENTRY = {
    "type": "user",
    "message": {"content": "<command-name>/clear</command-name>"},
}
_CONTINUE_AUTO_RESUME_USER_ENTRY = {
    "type": "user",
    "isMeta": True,
    "message": {
        "role": "user",
        "content": [{"type": "text",
                     "text": "Continue from where you left off."}],
    },
}


def _synthetic_assistant_entry(msg_id: str, timestamp: str = "2026-04-19T08:29:07Z"):
    """Build a synthetic no-op assistant entry as CC writes it."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": msg_id,
            "model": "<synthetic>",
            "role": "assistant",
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                          "ephemeral_5m_input_tokens": 0}},
            "content": [{"type": "text", "text": "No response requested."}],
        },
    }


def test_extract_turns_flags_resume_marker_after_exit():
    # /exit triplet (caveat, /exit, stdout) → synthetic assistant → marker flagged
    entries = [
        {"type": "user", "message": {"content": "<local-command-caveat>...",
                                      "isMeta": True}},
        _EXIT_USER_ENTRY,
        _EXIT_STDOUT_ENTRY,
        _synthetic_assistant_entry("msg_syn_1"),
    ]
    turns = sm._extract_turns(entries)
    assert len(turns) == 1
    assert turns[0]["_is_resume_marker"] is True


def test_extract_turns_does_not_flag_synthetic_without_exit():
    # Synthetic preceded by /clear (not /exit) — no resume marker
    entries = [
        _CLEAR_USER_ENTRY,
        _synthetic_assistant_entry("msg_syn_2"),
    ]
    turns = sm._extract_turns(entries)
    assert len(turns) == 1
    assert turns[0]["_is_resume_marker"] is False


def test_extract_turns_flags_resume_marker_after_continue_automessage():
    # Session 34 fingerprint: desktop-injected `"Continue from where you left
    # off."` isMeta user entry + <synthetic> "No response requested." reply.
    # Seen when a five-hour rate-limit window lapses and auto-continue can't
    # reach the backend. Matches even without a `/exit` triplet in the window.
    entries = [
        _CONTINUE_AUTO_RESUME_USER_ENTRY,
        _synthetic_assistant_entry("msg_syn_cont"),
    ]
    turns = sm._extract_turns(entries)
    assert len(turns) == 1
    assert turns[0]["_is_resume_marker"] is True


def test_extract_turns_flags_multiple_resumes_in_one_session():
    # Two /exit → synthetic pairs separated by real work = two resumes
    entries = [
        # Real assistant turn in between
        {"type": "user", "message": {"content": "first prompt"}},
        {"type": "assistant", "timestamp": "2026-04-19T08:28:24Z",
         "message": {"id": "msg_real_1", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 6, "output_tokens": 10,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": []}},
        _EXIT_USER_ENTRY, _EXIT_STDOUT_ENTRY,
        _synthetic_assistant_entry("msg_syn_a", "2026-04-19T08:29:07Z"),
        # Another real turn
        {"type": "user", "message": {"content": "second prompt"}},
        {"type": "assistant", "timestamp": "2026-04-19T08:30:00Z",
         "message": {"id": "msg_real_2", "model": "claude-opus-4-7",
                     "role": "assistant",
                     "usage": {"input_tokens": 5, "output_tokens": 8,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0},
                     "content": []}},
        _EXIT_USER_ENTRY, _EXIT_STDOUT_ENTRY,
        _synthetic_assistant_entry("msg_syn_b", "2026-04-19T08:31:00Z"),
    ]
    turns = sm._extract_turns(entries)
    resume_flags = [t["_is_resume_marker"] for t in turns]
    # Sorted by timestamp: real_1, syn_a, real_2, syn_b
    assert resume_flags == [False, True, False, True]


def test_build_resumes_computes_gap_and_terminal_flag():
    # Construct turn records with one mid-session and one terminal marker
    turns = [
        {"index": 1, "timestamp": "2026-04-19T08:28:24Z",
         "timestamp_fmt": "2026-04-19 08:28:24", "is_resume_marker": False},
        {"index": 2, "timestamp": "2026-04-19T08:29:00Z",
         "timestamp_fmt": "2026-04-19 08:29:00", "is_resume_marker": True},   # mid
        {"index": 3, "timestamp": "2026-04-19T08:30:00Z",
         "timestamp_fmt": "2026-04-19 08:30:00", "is_resume_marker": False},
        {"index": 4, "timestamp": "2026-04-19T08:31:00Z",
         "timestamp_fmt": "2026-04-19 08:31:00", "is_resume_marker": True},   # terminal
    ]
    resumes = sm._build_resumes(turns)
    assert len(resumes) == 2
    # First marker — mid-session — gap = 36s (08:29:00 - 08:28:24)
    assert resumes[0]["turn_index"] == 2
    assert resumes[0]["gap_seconds"] == 36.0
    assert resumes[0]["terminal"] is False
    # Second marker — terminal — gap = 60s; terminal=True
    assert resumes[1]["turn_index"] == 4
    assert resumes[1]["gap_seconds"] == 60.0
    assert resumes[1]["terminal"] is True


def test_html_renders_resume_marker_divider_and_card():
    # Inject a marker into the fixture report; HTML should include the
    # dashboard card and timeline divider row. Baseline fixture carries no
    # markers — without injection, neither the row nor the card appears.
    # CSS rules for .resume-marker-row live in <style> regardless, so tests
    # look for the row-level `<tr class=...>` and the card's `<div class="lbl">`
    # label to avoid false hits on stylesheet-only presence.
    r = _build_fixture_report()

    # Baseline: no resumes in bundled fixture
    assert r.get("resumes") == []
    baseline_html = sm.render_html(r, variant="single")
    assert '<tr class="resume-marker-row"' not in baseline_html
    assert '>Session resumes' not in baseline_html

    # Inject a resume marker on the first turn and rebuild the resumes list
    r["sessions"][0]["turns"][0]["is_resume_marker"] = True
    r["sessions"][0]["resumes"] = sm._build_resumes(r["sessions"][0]["turns"])
    r["resumes"] = [m for s in r["sessions"] for m in s["resumes"]]

    html = sm.render_html(r, variant="single")
    assert '<tr class="resume-marker-row"' in html
    assert 'class="resume-marker-pill"' in html
    assert "Session resumed" in html
    # Dashboard card label (not the CSS selector)
    assert '>Session resumes' in html


def test_resumes_card_absent_when_no_markers():
    # Guard against the card rendering on a report with an empty resumes list.
    r = _build_fixture_report()
    assert r.get("resumes") == []
    html = sm.render_html(r, variant="single")
    # Row-level emission (not CSS rule) must not appear
    assert '<tr class="resume-marker-row"' not in html
    # Card label (not CSS selector) must not appear
    assert '>Session resumes' not in html


def test_html_distinguishes_terminal_exit_marker_from_resume_marker():
    # Two markers in one session: a mid-session resume (followed by more turns)
    # and a terminal exit (last turn in the session). The dashboard card
    # already breaks these out as "N detected · M terminal exit"; the timeline
    # divider must use the same distinction so the two surfaces stay
    # internally consistent.
    r = _build_fixture_report()
    turns = r["sessions"][0]["turns"]
    # Mid marker on first turn (followed by 5 more — so non-terminal)
    turns[0]["is_resume_marker"] = True
    turns[0]["is_terminal_exit_marker"] = False
    # Terminal marker on last turn (no subsequent turns in session)
    turns[-1]["is_resume_marker"] = True
    turns[-1]["is_terminal_exit_marker"] = True
    r["sessions"][0]["resumes"] = sm._build_resumes(turns)
    r["resumes"] = [m for s in r["sessions"] for m in s["resumes"]]

    html = sm.render_html(r, variant="single")
    # Resume pill: blue, "Session resumed" label
    assert 'class="resume-marker-pill"' in html
    assert "Session resumed" in html
    # Terminal pill: amber via .terminal modifier, "Session exited" label
    assert 'class="resume-marker-pill terminal"' in html
    assert "Session exited" in html


# --- Content-block distribution (Proposal B) --------------------------------

def test_count_content_blocks_mixed_list():
    content = [
        {"type": "thinking", "thinking": ""},
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {}},
        {"type": "tool_use", "id": "u", "name": "Read", "input": {}},
        {"type": "text", "text": "hi"},
    ]
    counts, names = sm._count_content_blocks(content)
    assert counts["thinking"] == 1
    assert counts["tool_use"] == 2
    assert counts["text"] == 1
    assert counts["tool_result"] == 0
    assert counts["image"] == 0
    assert names == ["Bash", "Read"]


def test_count_content_blocks_non_list_returns_zeros():
    # Plain string content (older user prompts) has no structured blocks.
    counts, names = sm._count_content_blocks("hello")
    assert sum(counts.values()) == 0
    assert names == []
    counts, names = sm._count_content_blocks(None)
    assert sum(counts.values()) == 0
    assert names == []


def test_fixture_content_block_counts_per_turn():
    r = _build_fixture_report()
    turns = r["sessions"][0]["turns"]
    # msg_A: thinking + tool_use Bash + text (u2 text → no tool_result/image)
    assert turns[0]["content_blocks"] == {
        "thinking": 1, "tool_use": 1, "text": 1, "tool_result": 0, "image": 0,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }
    # msg_B: text only; preceded by u3 (tool_result)
    assert turns[1]["content_blocks"] == {
        "thinking": 0, "tool_use": 0, "text": 1, "tool_result": 1, "image": 0,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }
    # msg_C: text only; gap before it contains both u4 (tool_result) and u5
    # (sidechain text). Pre-v1.26.2 the parser overwrote last_user_content on
    # each user entry so only u5's text block survived; the v1.26.2 fix
    # accumulates blocks across the gap so u4's tool_result is also counted.
    assert turns[2]["content_blocks"] == {
        "thinking": 0, "tool_use": 0, "text": 1, "tool_result": 1, "image": 0,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }
    # msg_D: tool_use WebFetch + text; preceded by u7 (tool_result)
    assert turns[3]["content_blocks"] == {
        "thinking": 0, "tool_use": 1, "text": 1, "tool_result": 1, "image": 0,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }
    # msg_E: thinking + text (pure 1h turn, preceded by u8 text)
    assert turns[4]["content_blocks"] == {
        "thinking": 1, "tool_use": 0, "text": 1, "tool_result": 0, "image": 0,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }
    # msg_F: 2 tool_use + text; preceded by u9 (text + image)
    assert turns[5]["content_blocks"] == {
        "thinking": 0, "tool_use": 2, "text": 1, "tool_result": 0, "image": 1,
        "server_tool_use": 0, "advisor_tool_result": 0,
    }


def test_fixture_tool_names_top3_ranked_by_count_then_name():
    r = _build_fixture_report()
    totals = r["totals"]
    # Across the fixture: Bash=2 (msg_A + msg_F), Read=1, WebFetch=1.
    # Ties by name ascending → Read < WebFetch.
    assert totals["tool_names_top3"] == ["Bash", "Read", "WebFetch"]


def test_html_escapes_malicious_tool_name_in_top3_card():
    """Regression for H4: tool names originate from the JSONL and must be
    HTML-escaped before interpolation into the tool_calls_card on the single
    and dashboard variants. A compromised transcript with a tool name like
    ``</script><img src=x onerror=1>`` must not emit that raw string."""
    r = _build_fixture_report()
    payload = "</script><img src=x onerror=alert(1)>"
    r["totals"]["tool_names_top3"] = [payload, "Read", "WebFetch"]
    # Ensure the tool_calls_card is actually emitted (guarded by total > 0).
    assert r["totals"].get("tool_call_total", 0) > 0
    html = sm.render_html(r, variant="single")
    assert payload not in html
    assert html_std.escape(payload) in html


def test_fixture_thinking_turn_pct():
    r = _build_fixture_report()
    t = r["totals"]
    # 2 turns carry thinking out of 6 → 33.33%
    assert t["thinking_turn_count"] == 2
    assert t["thinking_turn_pct"] == pytest.approx(200 / 6, abs=1e-6)


def test_fixture_totals_content_blocks_aggregate():
    r = _build_fixture_report()
    cb = r["totals"]["content_blocks"]
    # tool_result=3 post-v1.26.2: msg_B picks up u3, msg_C picks up u4
    # (previously dropped by overwrite), msg_D picks up u7.
    assert cb == {"thinking": 2, "tool_use": 4, "text": 6,
                  "tool_result": 3, "image": 1,
                  "server_tool_use": 0, "advisor_tool_result": 0}
    assert r["totals"]["tool_call_total"] == 4
    assert r["totals"]["tool_call_avg_per_turn"] == pytest.approx(4 / 6, abs=1e-6)


def test_has_content_blocks_helpers_detect_fixture():
    r = _build_fixture_report()
    assert sm._has_content_blocks(r) is True
    assert sm._has_thinking(r) is True
    assert sm._has_tool_use(r) is True


def test_csv_has_content_block_columns():
    r = _build_fixture_report()
    csv_out = sm.render_csv(r)
    header = csv_out.splitlines()[1]  # [0] is the skill-version comment row
    for col in ("thinking_blocks", "tool_use_blocks", "text_blocks",
                 "tool_result_blocks", "image_blocks"):
        assert col in header


def test_json_has_content_blocks_nested():
    r = _build_fixture_report()
    import json as _json
    data = _json.loads(sm.render_json(r))
    # Per-turn nested `content_blocks`
    t = data["sessions"][0]["turns"][5]  # msg_F
    assert t["content_blocks"]["tool_use"] == 2
    assert t["content_blocks"]["image"] == 1
    assert t["tool_use_names"] == ["Read", "Bash"]
    # Totals nested `content_blocks` + scalar aggregates
    assert data["totals"]["content_blocks"]["tool_use"] == 4
    assert data["totals"]["thinking_turn_count"] == 2
    assert data["totals"]["tool_names_top3"] == ["Bash", "Read", "WebFetch"]


def test_text_render_includes_content_column_and_legend():
    r = _build_fixture_report()
    text = sm.render_text(r)
    # Legend row for Content present (only emitted when any blocks exist)
    assert "Content" in text
    # Per-turn content cell uses letter encoding
    assert "T1 u1 x1" in text  # msg_A pattern
    # Tool calls footer summary visible
    assert "Tool calls" in text
    assert "Extended thinking turns" in text


def test_md_render_includes_content_column_and_legend():
    r = _build_fixture_report()
    md = sm.render_md(r)
    assert "**Content**" in md
    assert "Extended thinking turns" in md
    assert "Tool calls" in md


def test_html_render_includes_content_column_and_cards():
    r = _build_fixture_report()
    html = sm.render_html(r, variant="single")
    # Column header + a per-turn content cell rendered with tooltip
    assert 'class="content-blocks"' in html
    assert "Extended thinking engagement" in html
    assert "Tool calls" in html
    # Top-3 tool list surfaces in the Tool calls card
    assert "Bash" in html


def test_extract_turns_merges_streaming_content_blocks():
    """Claude Code emits a single assistant message across N JSONL entries
    (one per content block) that share the same msg_id and usage. Dedup
    must UNION the content arrays, not keep-last-only, or Proposal B
    counters silently drop thinking + earlier tool_use + text blocks.
    """
    entries = [
        {"type": "user", "uuid": "u_pre", "timestamp": "2026-04-15T22:30:00Z",
         "sessionId": "s", "message": {"role": "user", "content": "hi"}},
        # Streaming occurrence 1: thinking only
        {"type": "assistant", "uuid": "a1",
         "timestamp": "2026-04-15T22:31:05.100Z", "sessionId": "s",
         "message": {"id": "msg_stream", "model": "claude-opus-4-7",
                      "role": "assistant",
                      "content": [{"type": "thinking", "thinking": "",
                                    "signature": "sig1"}],
                      "usage": {"input_tokens": 10, "output_tokens": 50,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}}},
        # Streaming occurrence 2: tool_use only
        {"type": "assistant", "uuid": "a2",
         "timestamp": "2026-04-15T22:31:05.500Z", "sessionId": "s",
         "message": {"id": "msg_stream", "model": "claude-opus-4-7",
                      "role": "assistant",
                      "content": [{"type": "tool_use", "id": "t_x",
                                    "name": "Bash", "input": {}}],
                      "usage": {"input_tokens": 10, "output_tokens": 50,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}}},
        # Streaming occurrence 3: second tool_use only
        {"type": "assistant", "uuid": "a3",
         "timestamp": "2026-04-15T22:31:05.900Z", "sessionId": "s",
         "message": {"id": "msg_stream", "model": "claude-opus-4-7",
                      "role": "assistant",
                      "content": [{"type": "tool_use", "id": "t_y",
                                    "name": "Read", "input": {}}],
                      "usage": {"input_tokens": 10, "output_tokens": 50,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}}},
    ]
    turns = sm._extract_turns(entries)
    assert len(turns) == 1
    content = turns[0]["message"]["content"]
    types = [b["type"] for b in content]
    # Union of all three streaming entries: 1 thinking + 2 tool_use.
    assert types.count("thinking") == 1
    assert types.count("tool_use") == 2
    # Usage (cost math) correctly taken from last occurrence — identical across all.
    assert turns[0]["message"]["usage"]["output_tokens"] == 50


def test_no_content_blocks_means_column_omitted_in_text():
    """Synthesize a minimal fixture with no content blocks and verify the
    text report preserves its pre-v1.3.0 shape (no Content column/legend row).
    """
    entries = [
        {"type": "user", "uuid": "uU", "timestamp": "2026-04-15T22:31:00.000Z",
         "sessionId": "synth",
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "uuid": "aA",
         "timestamp": "2026-04-15T22:31:05.000Z", "sessionId": "synth",
         "message": {"id": "msg_S1", "model": "claude-sonnet-4-7",
                      "role": "assistant",
                      "usage": {"input_tokens": 10, "output_tokens": 20,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}}},
    ]
    turns = sm._extract_turns(entries)
    user_ts = sm._extract_user_timestamps(entries)
    r = sm._build_report("session", "synth", [("synth", turns, user_ts)])
    assert sm._has_content_blocks(r) is False
    text = sm.render_text(r)
    # Column legend lists the standard columns only — no `Content` row.
    legend_block = text.split("\n\n", 1)[0]
    assert "Content" not in legend_block


# --- Input validation --------------------------------------------------------

def test_validate_session_id_accepts_uuid_and_hex():
    assert sm._validate_session_id("ca4ecd6c-93c2-4b60-9fc3-37d20120e306")
    assert sm._validate_session_id("abc123")


def test_validate_session_id_rejects_traversal():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        sm._validate_session_id("../etc/passwd")
    with pytest.raises(argparse.ArgumentTypeError):
        sm._validate_session_id("a/b")


def test_validate_slug_preserves_leading_dash():
    assert sm._validate_slug("-Volumes-foo-bar")


def test_validate_slug_rejects_slashes_and_traversal():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        sm._validate_slug("foo/bar")
    with pytest.raises(argparse.ArgumentTypeError):
        sm._validate_slug("foo/../bar")


# --- _cwd_to_slug ------------------------------------------------------------
# The slug must match Claude Code's own cwd → project-dir rule exactly, since
# it drives every session lookup (including compare-run extras, which look up
# per-side JSONLs by building the slug-path themselves). The evidence-based
# rule from observed ~/.claude/projects/<slug>/ entries is: replace every
# non-alphanumeric char (except `-`) with `-`; preserve runs as consecutive
# dashes. Previous behaviour replaced only `/`, which silently drifted when
# cwd contained `_`, `.`, spaces, or apostrophes — and that broke compare-run
# extras under $TMPDIR paths like /private/var/folders/.../xxx_yyy/zzz.


def test_cwd_to_slug_replaces_slashes():
    assert sm._cwd_to_slug("/Volumes/AMZ3/session-metrics") == \
        "-Volumes-AMZ3-session-metrics"


def test_cwd_to_slug_replaces_underscores():
    # Regression: $TMPDIR paths on macOS (/private/var/folders/cv/xxx_yyy/T/...)
    # caused compare-run extras to build the wrong lookup path.
    assert sm._cwd_to_slug("/tmp/foo_bar/baz") == "-tmp-foo-bar-baz"


def test_cwd_to_slug_replaces_dots_preserving_runs():
    # `/Users/x/.claude-mem` → `-Users-x--claude-mem`: the `/` and `.`
    # each become `-`, yielding a consecutive `--`. This matches
    # entries observed in live ~/.claude/projects/.
    assert sm._cwd_to_slug("/Users/x/.claude-mem") == "-Users-x--claude-mem"


def test_cwd_to_slug_replaces_spaces_and_apostrophes():
    assert sm._cwd_to_slug("/Users/george/Liu's Project") == \
        "-Users-george-Liu-s-Project"


def test_cwd_to_slug_preserves_digits_and_dashes():
    assert sm._cwd_to_slug("/a/b-c/d1-2") == "-a-b-c-d1-2"


# ---- Perf regression guard (opt-in) ----------------------------------------
# Gated behind SESSION_METRICS_RUN_PERF_TESTS=1 so the default CI / uv run
# path stays fast. Baselines live under exports/perf-baselines/ and are
# updated by scripts/benchmark.py.

@pytest.mark.perf
@pytest.mark.skipif(
    not os.environ.get("SESSION_METRICS_RUN_PERF_TESTS"),
    reason="set SESSION_METRICS_RUN_PERF_TESTS=1 to enable",
)
def test_parse_jsonl_under_perf_budget(tmp_path):
    """Cold-parse a 5k-turn synthetic fixture in under 2 seconds.

    Deliberately generous slack (the benchmark shows ~60ms at N=1k on a
    laptop). This guards against accidental 10x regressions in parse
    throughput — it's a safety net, not a precision benchmark.
    """
    # Inline fixture generator: keeps test self-contained, no committed
    # binary fixture needed.
    import json as _json
    import random as _random
    fx = tmp_path / "perf-5k.jsonl"
    rng = _random.Random(42)
    with fx.open("w", encoding="utf-8") as fh:
        for i in range(5_000):
            fh.write(_json.dumps({
                "type": "user", "uuid": f"u{i}",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "perf",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": f"p{i}"}]},
            }) + "\n")
            fh.write(_json.dumps({
                "type": "assistant", "uuid": f"a{i}",
                "timestamp": "2026-01-01T00:00:01Z",
                "sessionId": "perf",
                "message": {
                    "id": f"m{i}", "model": "claude-opus-4-7",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "x" * 50}],
                    "usage": {
                        "input_tokens": rng.randint(100, 2000),
                        "output_tokens": rng.randint(50, 500),
                        "cache_read_input_tokens": rng.randint(0, 500),
                        "cache_creation_input_tokens": rng.randint(0, 100),
                    },
                },
            }) + "\n")
    t0 = time.perf_counter()
    entries = sm._parse_jsonl(fx)
    elapsed = time.perf_counter() - t0
    assert len(entries) == 10_000
    assert elapsed < 2.0, f"parse took {elapsed:.2f}s (budget 2.0s)"


# ===========================================================================
# T1.3 — Unknown-model advisory (F4, v2 audit)
# ===========================================================================

def test_print_advisories_warns_unknown_models(monkeypatch, capsys):
    """_print_run_advisories emits a [warn] line to stderr listing unknown models.

    v1.41.2: phrasing changed from "Sonnet rates" to "fallback rates" because
    family-fallback hits route to the family's tier (Opus NEW / Haiku /
    Sonnet) rather than always landing on _DEFAULT_PRICING.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", {"claude-fake-model-x"})
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [0])
    sm._print_run_advisories()
    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "claude-fake-model-x" in err
    assert "fallback rates" in err


def test_print_advisories_silent_when_nothing_to_warn(monkeypatch, capsys):
    """_print_run_advisories is silent when both advisory sets are empty/zero."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [0])
    sm._print_run_advisories()
    assert capsys.readouterr().err == ""


# ===========================================================================
# T1.4 — Fast-mode advisory (F1, v2 audit)
# ===========================================================================

def _make_fast_turn_entry(speed: str = "fast") -> dict:
    return {
        "message": {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "speed": speed,
            },
            "model": "claude-opus-4-7",
        },
        "timestamp": "2025-01-01T00:00:00.000Z",
    }


def test_fast_mode_turn_increments_counter(monkeypatch):
    """_build_turn_record increments _FAST_MODE_TURNS[0] for speed==\"fast\" turns."""
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [0])
    sm._build_turn_record(0, _make_fast_turn_entry("fast"))
    assert sm._FAST_MODE_TURNS[0] == 1


def test_standard_turn_does_not_increment_fast_mode_counter(monkeypatch):
    """_build_turn_record leaves _FAST_MODE_TURNS[0] unchanged for non-fast turns."""
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [0])
    sm._build_turn_record(0, _make_fast_turn_entry(""))
    assert sm._FAST_MODE_TURNS[0] == 0


def test_print_advisories_warns_fast_mode_plural(monkeypatch, capsys):
    """_print_run_advisories emits a [note] advisory when fast-mode turns exist (plural)."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [3])
    sm._print_run_advisories()
    err = capsys.readouterr().err
    assert "[note]" in err
    assert "3 fast-mode turns" in err
    assert "~6×" in err


def test_print_advisories_warns_fast_mode_singular(monkeypatch, capsys):
    """_print_run_advisories uses singular 'turn' for exactly one fast-mode turn."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    monkeypatch.setattr(sm, "_FAST_MODE_TURNS", [1])
    sm._print_run_advisories()
    err = capsys.readouterr().err
    assert "1 fast-mode turn " in err  # singular — no trailing 's'


# ---------------------------------------------------------------------------
# audit-session-metrics — audit-extract.py helper-script tests.
#
# The helper script is the trigger-evaluation engine. A bug here is silent —
# every downstream audit picks it up unchallenged. Tests build minimal
# synthetic JSON exports that contain just enough shape for one trigger to
# fire and assert the script produces the right digest fields.
# ===========================================================================
# v1.41.0 — Audit-driven fixes (P0-B regex, P1-A parse_jsonl, P1-B/C dir overrides)
# ===========================================================================


def test_parse_jsonl_skips_non_dict_lines(tmp_path, capsys):
    """P1-A: a JSONL line that parses successfully but isn't an object
    is dropped via the same skip path as malformed JSON. Without this
    guard, downstream `_extract_turns` would AttributeError on
    ``entry.get("type")``."""
    bad_jsonl = tmp_path / "mixed.jsonl"
    bad_jsonl.write_text(
        '{"type": "user", "message": {"content": "hi"}}\n'
        '[1, 2, 3]\n'
        '"a stray string"\n'
        '42\n'
        '{"type": "user", "message": {"content": "bye"}}\n',
        encoding="utf-8",
    )
    entries = sm._parse_jsonl(bad_jsonl)
    # Only the two object-typed lines survive
    assert len(entries) == 2
    assert all(isinstance(e, dict) for e in entries)
    err = capsys.readouterr().err
    assert "3 malformed lines skipped" in err
    assert "top-level value is" in err


def test_parse_jsonl_happy_path_unaffected(tmp_path, capsys):
    """P1-A regression: well-formed JSONL with only object lines still
    parses to the full list with no warnings."""
    good = tmp_path / "good.jsonl"
    good.write_text(
        '{"type": "user", "message": {"content": "hi"}}\n'
        '{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    entries = sm._parse_jsonl(good)
    assert len(entries) == 2
    err = capsys.readouterr().err
    assert "skipped" not in err


def test_parse_cache_dir_honors_override(monkeypatch, tmp_path):
    """P1-B: ``_CACHE_DIR_OVERRIDE`` (set by --cache-dir) takes highest
    precedence."""
    custom = tmp_path / "custom-cache"
    monkeypatch.setattr(sm, "_CACHE_DIR_OVERRIDE", custom)
    monkeypatch.delenv("CLAUDE_SESSION_METRICS_CACHE_DIR", raising=False)
    assert sm._parse_cache_dir() == custom


def test_parse_cache_dir_honors_env_var(monkeypatch, tmp_path):
    """P1-B: ``CLAUDE_SESSION_METRICS_CACHE_DIR`` env var fires when the
    override attribute is unset."""
    custom = tmp_path / "env-cache"
    monkeypatch.setattr(sm, "_CACHE_DIR_OVERRIDE", None)
    monkeypatch.setenv("CLAUDE_SESSION_METRICS_CACHE_DIR", str(custom))
    assert sm._parse_cache_dir() == custom


def test_parse_cache_dir_override_beats_env(monkeypatch, tmp_path):
    """P1-B: --flag (override attr) wins when both override and env set."""
    flag_dir = tmp_path / "flag-cache"
    env_dir  = tmp_path / "env-cache"
    monkeypatch.setattr(sm, "_CACHE_DIR_OVERRIDE", flag_dir)
    monkeypatch.setenv("CLAUDE_SESSION_METRICS_CACHE_DIR", str(env_dir))
    assert sm._parse_cache_dir() == flag_dir


def test_parse_cache_dir_default_when_unset(monkeypatch):
    """P1-B: with neither override nor env var, the historical default
    (~/.cache/session-metrics/parse) is used."""
    monkeypatch.setattr(sm, "_CACHE_DIR_OVERRIDE", None)
    monkeypatch.delenv("CLAUDE_SESSION_METRICS_CACHE_DIR", raising=False)
    assert sm._parse_cache_dir() == Path.home() / ".cache" / "session-metrics" / "parse"


def test_export_dir_honors_override(monkeypatch, tmp_path):
    """P1-C: ``_EXPORT_DIR_OVERRIDE`` (set by --export-dir) takes highest
    precedence."""
    custom = tmp_path / "custom-exports"
    monkeypatch.setattr(sm, "_EXPORT_DIR_OVERRIDE", custom)
    monkeypatch.delenv("CLAUDE_SESSION_METRICS_EXPORT_DIR", raising=False)
    assert sm._export_dir() == custom


def test_export_dir_honors_env_var(monkeypatch, tmp_path):
    """P1-C: ``CLAUDE_SESSION_METRICS_EXPORT_DIR`` env var fires when the
    override attribute is unset."""
    custom = tmp_path / "env-exports"
    monkeypatch.setattr(sm, "_EXPORT_DIR_OVERRIDE", None)
    monkeypatch.setenv("CLAUDE_SESSION_METRICS_EXPORT_DIR", str(custom))
    assert sm._export_dir() == custom


def test_export_dir_override_beats_env(monkeypatch, tmp_path):
    """P1-C: --flag (override attr) wins when both override and env set."""
    flag_dir = tmp_path / "flag-exports"
    env_dir  = tmp_path / "env-exports"
    monkeypatch.setattr(sm, "_EXPORT_DIR_OVERRIDE", flag_dir)
    monkeypatch.setenv("CLAUDE_SESSION_METRICS_EXPORT_DIR", str(env_dir))
    assert sm._export_dir() == flag_dir


def test_export_dir_default_when_unset(monkeypatch, tmp_path):
    """P1-C: default lands under <cwd>/exports/session-metrics."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sm, "_EXPORT_DIR_OVERRIDE", None)
    monkeypatch.delenv("CLAUDE_SESSION_METRICS_EXPORT_DIR", raising=False)
    assert sm._export_dir() == tmp_path / "exports" / "session-metrics"

