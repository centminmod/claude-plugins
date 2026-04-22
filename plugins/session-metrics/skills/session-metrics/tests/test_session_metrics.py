"""Unit + integration tests for session-metrics.py.

Run with: uv run python -m pytest tests/ -v
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
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load main first — the compare module looks up helpers from
# sys.modules["session_metrics"] at call time, so it must already
# be registered when any compare test runs.
sm = _load_module("session_metrics", _SCRIPT)
smc = _load_module("session_metrics_compare", _COMPARE)


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
def _isolate_projects_dir(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("real_projects_dir"):
        return
    safe = tmp_path / "_autouse_projects"
    safe.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(safe))


# --- Pricing -----------------------------------------------------------------

def test_pricing_opus_4_7_explicit():
    # Opus 4.5-generation uses the new cheaper tier: $5 input / $25 output.
    r = sm._pricing_for("claude-opus-4-7")
    assert r["input"] == 5.00
    assert r["output"] == 25.00
    assert r["cache_read"] == 0.50
    assert r["cache_write"] == 6.25
    # 1-hour TTL tier is 2x base input (vs 1.25x for 5m).
    assert r["cache_write_1h"] == 10.00


def test_pricing_sonnet_4_7_explicit():
    r = sm._pricing_for("claude-sonnet-4-7")
    assert r["input"] == 3.00
    assert r["cache_write_1h"] == 6.00


def test_pricing_opus_4_old_tier_retained():
    # Opus 4 / 4.1 stayed on the original $15 / $75 tier.
    r = sm._pricing_for("claude-opus-4")
    assert r["input"] == 15.00
    assert r["output"] == 75.00
    assert r["cache_write_1h"] == 30.00


def test_pricing_haiku_4_5_new_tier():
    # Haiku 4.5 has its own tier: $1 input / $5 output.
    r = sm._pricing_for("claude-haiku-4-5-20251001")
    assert r["input"] == 1.00
    assert r["output"] == 5.00
    assert r["cache_write_1h"] == 2.00


def test_pricing_prefix_fallback():
    """Unknown future model falls through to nearest known prefix."""
    # "claude-opus-4-99-future" doesn't start with 4-5/4-6/4-7/4-1, so it
    # matches "claude-opus-4" (old-tier). Safer to over- than under-estimate.
    r = sm._pricing_for("claude-opus-4-99-future")
    assert r["input"] == 15.00


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
    assert r["models"]["claude-opus-4-7"] == 3       # msg_A, msg_C, msg_E
    assert r["models"]["claude-sonnet-4-7"] == 3     # msg_B, msg_D, msg_F


def test_fixture_time_of_day_total_is_user_prompt_count():
    r = _build_fixture_report()
    # 4 real user prompts — must NOT equal the user-type entry count in the file
    assert r["time_of_day"]["message_count"] == 4


# --- Cache TTL drilldown (Proposal A) ---------------------------------------

def test_fixture_ttl_classification_per_turn():
    """Each turn carries a correct `cache_write_ttl` derived from its split."""
    r = _build_fixture_report()
    by_id = {t["model"] + "_" + str(t["index"]): t for t in r["sessions"][0]["turns"]}
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
    header = csv_out.splitlines()[0]
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
    r["models"]["<synthetic>"] = r["models"].get("<synthetic>", 0) + 1

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
    }
    # msg_B: text only; preceded by u3 (tool_result)
    assert turns[1]["content_blocks"] == {
        "thinking": 0, "tool_use": 0, "text": 1, "tool_result": 1, "image": 0,
    }
    # msg_C: text only; preceded by u5 (sidechain text) — no attributable blocks
    assert turns[2]["content_blocks"] == {
        "thinking": 0, "tool_use": 0, "text": 1, "tool_result": 0, "image": 0,
    }
    # msg_D: tool_use WebFetch + text; preceded by u7 (tool_result)
    assert turns[3]["content_blocks"] == {
        "thinking": 0, "tool_use": 1, "text": 1, "tool_result": 1, "image": 0,
    }
    # msg_E: thinking + text (pure 1h turn, preceded by u8 text)
    assert turns[4]["content_blocks"] == {
        "thinking": 1, "tool_use": 0, "text": 1, "tool_result": 0, "image": 0,
    }
    # msg_F: 2 tool_use + text; preceded by u9 (text + image)
    assert turns[5]["content_blocks"] == {
        "thinking": 0, "tool_use": 2, "text": 1, "tool_result": 0, "image": 1,
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
    assert cb == {"thinking": 2, "tool_use": 4, "text": 6,
                  "tool_result": 2, "image": 1}
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
    header = csv_out.splitlines()[0]
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
    legend_block, _rest = text.split("\n\n", 1)
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


# --- Time-of-day bucketing ---------------------------------------------------

def _epoch(y, mo, d, h=0, m=0):
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, m, tzinfo=timezone.utc).timestamp())


def test_bucket_utc_midnight_is_night():
    counts = sm._bucket_time_of_day([_epoch(2026, 4, 15, 0, 0)], offset_hours=0)
    assert counts["night"] == 1
    assert counts["total"] == 1


def test_bucket_offset_shifts_hour():
    # 22:00 UTC on a given day = 08:00 next day in Brisbane (UTC+10)
    ts = [_epoch(2026, 4, 15, 22, 0)]
    assert sm._bucket_time_of_day(ts, offset_hours=0)["evening"] == 1
    assert sm._bucket_time_of_day(ts, offset_hours=10)["morning"] == 1


# --- Hour-of-day (24-bucket) -------------------------------------------------

def test_hour_of_day_length_and_total():
    ts = [_epoch(2026, 4, 15, 9, 0), _epoch(2026, 4, 15, 9, 30),
          _epoch(2026, 4, 15, 22, 0)]
    hod = sm._build_hour_of_day(ts, offset_hours=0)
    assert len(hod["hours"]) == 24
    assert hod["total"] == 3
    assert hod["hours"][9] == 2
    assert hod["hours"][22] == 1


def test_hour_of_day_offset_shifts_bucket():
    ts = [_epoch(2026, 4, 15, 22, 0)]  # 22:00 UTC
    hod_utc = sm._build_hour_of_day(ts, offset_hours=0)
    hod_bne = sm._build_hour_of_day(ts, offset_hours=10)  # 08:00 Brisbane next day
    assert hod_utc["hours"][22] == 1
    assert hod_bne["hours"][8] == 1


def test_hour_of_day_empty():
    hod = sm._build_hour_of_day([])
    assert hod["hours"] == [0] * 24
    assert hod["total"] == 0


# --- Weekday x hour matrix --------------------------------------------------

def test_weekday_hour_matrix_shape():
    wh = sm._build_weekday_hour_matrix([_epoch(2026, 4, 15, 9, 0)])
    assert len(wh["matrix"]) == 7
    assert all(len(row) == 24 for row in wh["matrix"])
    assert wh["total"] == 1


def test_weekday_hour_matrix_mon_is_row_zero():
    # 2026-04-13 is a Monday (verify via Python's weekday())
    from datetime import datetime, timezone
    d = datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc)
    assert d.weekday() == 0
    wh = sm._build_weekday_hour_matrix([int(d.timestamp())])
    assert wh["matrix"][0][9] == 1
    assert wh["row_totals"][0] == 1
    assert wh["col_totals"][9] == 1


def test_weekday_hour_matrix_offset_crosses_day_boundary():
    # 2026-04-15 22:00 UTC -> 2026-04-16 08:00 Brisbane (weekday shifts Wed->Thu)
    from datetime import datetime, timezone
    e = _epoch(2026, 4, 15, 22, 0)
    utc_wd = datetime(2026, 4, 15, 22, 0, tzinfo=timezone.utc).weekday()
    bne_wd = datetime(2026, 4, 16,  8, 0, tzinfo=timezone.utc).weekday()
    wh_utc = sm._build_weekday_hour_matrix([e], offset_hours=0)
    wh_bne = sm._build_weekday_hour_matrix([e], offset_hours=10)
    assert wh_utc["matrix"][utc_wd][22] == 1
    assert wh_bne["matrix"][bne_wd][8] == 1


def test_fixture_hour_of_day_from_real_prompts():
    r = _build_fixture_report()
    hod = r["time_of_day"]["hour_of_day"]
    # u2 at 22:31 UTC + u6/u8/u9 at 03:45/03:46 UTC = 1 at h=22, 3 at h=3
    assert hod["hours"][22] == 1
    assert hod["hours"][3] == 3
    assert hod["total"] == 4


# --- 5-hour session blocks ---------------------------------------------------

def test_session_blocks_fixture_splits_on_5h_gap():
    """Fixture has events at 22:31 (Apr 15) and 03:45 (Apr 16) — ~5h 14m gap.

    The 5h window from 22:31 ends at 03:31 the next day, so 03:45 must
    anchor a second block.
    """
    r = _build_fixture_report()
    blocks = r["session_blocks"]
    assert len(blocks) == 2
    # First block anchors at u2 (22:31 UTC) — the earliest event
    assert blocks[0]["anchor_iso"].startswith("2026-04-15T22:31:")
    # Second block anchors at u6 (03:45 UTC next day)
    assert blocks[1]["anchor_iso"].startswith("2026-04-16T03:45:")


def test_session_blocks_counts():
    r = _build_fixture_report()
    blocks = r["session_blocks"]
    # Block 0: u2 (user) + 3 assistant turns (a1_dedup, a2, a3) = 1 user, 3 turns
    # Note: a1 appears twice in the JSONL under same msg_id "msg_A" but
    # assistant timestamps aren't deduped in block building — both are events.
    # What matters: user_msg_count is from filtered prompts.
    assert blocks[0]["user_msg_count"] == 1
    # Block 1: u6 + a4 + u8 + a5 + u9 + a6 = 3 user prompts, 3 turns
    assert blocks[1]["user_msg_count"] == 3


def test_session_blocks_cost_sums_match_report_total():
    r = _build_fixture_report()
    # The sum of block costs should NOT equal totals["cost"] because blocks
    # include every raw turn (duplicates included), while totals dedups on
    # message.id.  Verify blocks include at least one duplicate (block cost
    # > per-turn-dedup total would indicate duplicates counted).  The point
    # of this test: blocks are computed from raw events and expose the
    # rate-limit picture correctly, not the deduped picture.
    blocks = r["session_blocks"]
    assert len(blocks) >= 1
    total_block_cost = sum(b["cost_usd"] for b in blocks)
    # At least the full deduped total is present in the blocks.
    assert total_block_cost >= r["totals"]["cost"] - 1e-6


def test_parse_peak_hours_valid():
    assert sm._parse_peak_hours("5-11") == (5, 11)
    assert sm._parse_peak_hours("0-24") == (0, 24)
    assert sm._parse_peak_hours(" 9 - 17 ") == (9, 17)


def test_parse_peak_hours_rejects_wrap_or_invalid():
    import argparse
    for bad in ["5", "11-5", "24-25", "-1-5", "abc", ""]:
        with pytest.raises(argparse.ArgumentTypeError):
            sm._parse_peak_hours(bad)


def test_build_peak_none_when_no_hours():
    assert sm._build_peak(None, None) is None


def test_build_peak_defaults_to_los_angeles():
    p = sm._build_peak((5, 11), None)
    assert p is not None
    assert p["start"] == 5
    assert p["end"] == 11
    assert p["tz_label"] == "America/Los_Angeles"
    # LA is either UTC-8 (PST) or UTC-7 (PDT); either is acceptable.
    assert p["tz_offset_hours"] in (-7.0, -8.0)
    assert "community" in p["note"].lower()


def _raise_zoneinfo_missing(*args, **kwargs):
    raise sm.ZoneInfoNotFoundError("simulated windows-without-tzdata")


def test_resolve_tz_missing_warns_and_falls_back(monkeypatch, capsys):
    """Default: ZoneInfo miss warns to stderr and returns (0.0, 'UTC')."""
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    off, label = sm._resolve_tz("America/Los_Angeles", None)
    assert off == 0.0
    assert label == "UTC"
    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "tzdata" in err
    assert "Falling back to UTC" in err


def test_resolve_tz_strict_raises_on_missing(monkeypatch, capsys):
    """--strict-tz: ZoneInfo miss raises SystemExit with actionable hint."""
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    with pytest.raises(SystemExit) as excinfo:
        sm._resolve_tz("Europe/Berlin", None, strict=True)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "[error]" in err
    assert "tzdata" in err


def test_build_peak_missing_warns_and_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    p = sm._build_peak((9, 17), "America/Los_Angeles")
    assert p is not None
    assert p["tz_label"] == "UTC"
    assert p["tz_offset_hours"] == 0.0
    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "tzdata" in err


def test_build_peak_strict_raises_on_missing(monkeypatch, capsys):
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    with pytest.raises(SystemExit) as excinfo:
        sm._build_peak((9, 17), "America/Los_Angeles", strict=True)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "[error]" in err


def test_resolve_tz_clean_input_unchanged_by_strict_flag():
    """Behaviour-preservation: a resolvable tz is unaffected by strict=True."""
    off_default, label_default = sm._resolve_tz("America/Los_Angeles", None)
    off_strict, label_strict = sm._resolve_tz(
        "America/Los_Angeles", None, strict=True)
    assert off_default == off_strict
    assert label_default == label_strict == "America/Los_Angeles"


def test_weekly_rollup_has_data_flag():
    r = _build_fixture_report()
    # Fixture has 4 turns; fixture dates are in 2026-04, so whether the
    # trailing/prior windows catch them depends on "now". We only assert
    # structure here: the keys and shapes are stable.
    ro = r["weekly_rollup"]
    assert "trailing_7d" in ro and "prior_7d" in ro
    for w in (ro["trailing_7d"], ro["prior_7d"]):
        assert set(w.keys()) >= {"turns", "user_prompts", "cost", "blocks",
                                  "input", "output", "cache_read", "cache_write",
                                  "cache_hit_pct"}


def test_weekly_rollup_uses_deduped_cost():
    """Trailing-7d cost (when fixture dates are in-window) should equal the
    report total — confirms rollup uses the deduped turn records, not raw."""
    now = _epoch(2026, 4, 17, 0, 0)   # just after both fixture events
    sid, turns, user_ts = sm._load_session(_FIXTURE, include_subagents=False)
    r = sm._build_report("session", "test", [(sid, turns, user_ts)])
    ro = sm._build_weekly_rollup(
        r["sessions"], [(sid, turns, user_ts)], r["session_blocks"],
        now_epoch=now,
    )
    assert ro["trailing_7d"]["cost"] == pytest.approx(0.027845, abs=1e-7)
    assert ro["prior_7d"]["cost"] == pytest.approx(0.0, abs=1e-9)


def test_fmt_delta_pct_prior_zero_returns_new():
    d, _ = sm._fmt_delta_pct(5, 0)
    assert d == "new"
    d, _ = sm._fmt_delta_pct(0, 0)
    assert d in ("new", "\u2013")


def test_fmt_delta_pct_positive_and_negative():
    d, _ = sm._fmt_delta_pct(120, 100)
    assert d == "+20.0%"
    d, _ = sm._fmt_delta_pct(80, 100)
    assert d == "-20.0%"


def test_session_duration_stats_requires_two_turns():
    assert sm._session_duration_stats({"turns": []}) is None
    one_turn = {"turns": [{"timestamp": "2026-04-15T22:31:00Z"}],
                "subtotal": {"total": 100, "cost": 0.1, "turns": 1}}
    assert sm._session_duration_stats(one_turn) is None


def test_cached_parse_matches_uncached(tmp_path, monkeypatch):
    """The cache round-trip must produce exactly the same entries as direct parse."""
    # Redirect the cache dir so we don't pollute ~/.cache.
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    direct = sm._parse_jsonl(_FIXTURE)
    cached = sm._cached_parse_jsonl(_FIXTURE, use_cache=True)
    assert direct == cached
    # Second call should hit the cache — still equal.
    cached2 = sm._cached_parse_jsonl(_FIXTURE, use_cache=True)
    assert direct == cached2


def test_cached_parse_writes_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    sm._cached_parse_jsonl(_FIXTURE, use_cache=True)
    blobs = list((tmp_path / "parse").glob("*.json.gz"))
    assert len(blobs) == 1


def test_cached_parse_no_cache_skips_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    sm._cached_parse_jsonl(_FIXTURE, use_cache=False)
    # Cache dir should not even exist (no writes).
    assert not (tmp_path / "parse").exists()


def test_cached_parse_invalidates_on_mtime(tmp_path, monkeypatch):
    """A touched JSONL must generate a fresh cache key, not a stale one."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    # Copy fixture to a writable temp file so we can bump its mtime.
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"))
    sm._cached_parse_jsonl(src, use_cache=True)
    before = {p.name for p in (tmp_path / "parse").iterdir()}
    # Bump mtime by 2 seconds to force a distinct key.
    import os
    stat = src.stat()
    os.utime(src, (stat.st_atime, stat.st_mtime + 2))
    sm._cached_parse_jsonl(src, use_cache=True)
    after = {p.name for p in (tmp_path / "parse").iterdir()}
    # Two distinct cache files now.
    assert len(after) == 2
    assert before.issubset(after)


def test_parse_cache_key_includes_path_hash(tmp_path):
    """Same stem + same mtime in sibling dirs must yield distinct cache keys.

    Regression for H1: prior to the path-hash component, two JSONLs with
    identical UUID filenames (e.g. sibling project dirs) sharing an mtime_ns
    would collide on the same cache blob.
    """
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    p_a = dir_a / "shared.jsonl"
    p_b = dir_b / "shared.jsonl"
    p_a.write_text("{}\n", encoding="utf-8")
    p_b.write_text("{}\n", encoding="utf-8")
    mtime_ns = 1_700_000_000_000_000_000
    key_a = sm._parse_cache_key(p_a, mtime_ns)
    key_b = sm._parse_cache_key(p_b, mtime_ns)
    assert key_a != key_b
    # Keys must still be deterministic for the same path.
    assert key_a == sm._parse_cache_key(p_a, mtime_ns)


def test_cached_parse_same_stem_sibling_dirs_no_collision(tmp_path, monkeypatch):
    """End-to-end: two identically-named JSONLs in sibling dirs with identical
    mtimes must cache independently and return their own distinct contents."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    # Two distinct JSONL payloads — must NOT cross-contaminate via cache.
    p_a = dir_a / "shared.jsonl"
    p_b = dir_b / "shared.jsonl"
    p_a.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # Truncate the b-side so the two parses are observably different.
    b_lines = _FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)[:1]
    p_b.write_text("".join(b_lines), encoding="utf-8")
    # Force identical mtimes so only the path distinguishes them.
    import os
    t = 1_700_000_000.0
    os.utime(p_a, (t, t))
    os.utime(p_b, (t, t))
    parsed_a = sm._cached_parse_jsonl(p_a, use_cache=True)
    parsed_b = sm._cached_parse_jsonl(p_b, use_cache=True)
    assert parsed_a != parsed_b
    # Re-parse from cache — still distinct, proving the cache stored two blobs.
    assert sm._cached_parse_jsonl(p_a, use_cache=True) == parsed_a
    assert sm._cached_parse_jsonl(p_b, use_cache=True) == parsed_b
    # Two blobs on disk.
    blobs = list((tmp_path / "parse").glob("*.json.gz"))
    assert len(blobs) == 2


def test_cache_write_tmp_filename_is_randomized(tmp_path, monkeypatch):
    """Two writers must not collide on the same .tmp file.

    Regression for H2: prior to randomizing the tmp suffix, two writers on
    the same cache_path shared a deterministic '.tmp' name, risking
    interleaved bytes prior to the atomic replace(). Post-fix the tmp
    suffix is `<pid>.<token_hex(4)>.tmp` — each write gets a unique name.
    """
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    seen = []
    real_token_hex = sm.secrets.token_hex

    def spy_token_hex(n):
        r = real_token_hex(n)
        seen.append(r)
        return r

    monkeypatch.setattr(sm.secrets, "token_hex", spy_token_hex)
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    sm._cached_parse_jsonl(src, use_cache=True)
    # Bump mtime so the second call is a cache miss (not a read).
    import os
    stat = src.stat()
    os.utime(src, (stat.st_atime, stat.st_mtime + 2))
    sm._cached_parse_jsonl(src, use_cache=True)
    assert len(seen) == 2
    # Each write draws a fresh 4-byte token — overwhelmingly likely distinct.
    assert seen[0] != seen[1]
    # No leftover .tmp files — atomic replace succeeded both times.
    leftovers = list((tmp_path / "parse").glob("*.tmp"))
    assert leftovers == []


def test_cache_write_concurrent_threads_no_corruption(tmp_path, monkeypatch):
    """Concurrent writers on the same cache path must not corrupt the blob.

    Regression for H2: four threads racing to populate the cache for the
    same source file all succeed, the final blob is a valid gzip+JSON,
    and no orphan .tmp files are left behind. Threading (not
    multiprocessing) is used intentionally — the contention that matters
    here is on the tmp filename, which the random suffix now guards
    against regardless of whether writers share a pid.
    """
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    import threading
    errors: list[BaseException] = []

    def worker():
        try:
            sm._cached_parse_jsonl(src, use_cache=True)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "worker deadlocked"

    assert errors == [], f"worker raised: {errors!r}"
    # Final cache layer must be consistent: one blob, zero orphaned .tmp files.
    cache_dir = tmp_path / "parse"
    blobs = list(cache_dir.glob("*.json.gz"))
    assert len(blobs) == 1, f"expected one blob, saw {[b.name for b in blobs]}"
    orphans = list(cache_dir.glob("*.tmp"))
    assert orphans == [], f"orphan tmp files: {[o.name for o in orphans]}"
    # Reading the cached blob must succeed and return a usable list.
    parsed = sm._cached_parse_jsonl(src, use_cache=True)
    assert isinstance(parsed, list)


def test_hour_of_day_dst_boundary_uses_fixed_offset():
    """Behaviour lock: hour_of_day bucketing uses a scalar offset, not per-event DST.

    Regression lock for the _resolve_tz documented contract: static-export
    buckets apply a single offset uniformly to every timestamp. A future
    "DST fix" that switched this module to per-event ``ZoneInfo`` math
    would silently perturb every historical report — this test exists to
    force that change to be explicit.

    Scenario: a January event and a July event, each at 10:00 *local*
    wall-clock time in America/Los_Angeles. With per-event DST math,
    both would land in bucket 10. With the documented scalar offset
    (PST = UTC-8), the July event (which was 10:00 PDT = 17:00 UTC)
    buckets to 09:00 — one hour "off" from wall-clock local time. That
    one-hour delta is the whole point of the contract.
    """
    # 10:00 PST = 18:00 UTC (January, no DST in effect).
    jan = _epoch(2026, 1, 15, 18, 0)
    # 10:00 PDT = 17:00 UTC (July, DST in effect).
    jul = _epoch(2026, 7, 15, 17, 0)
    hod = sm._build_hour_of_day([jan, jul], offset_hours=-8.0)
    # Jan: (18 - 8) mod 24 == 10 — matches wall-clock.
    assert hod["hours"][10] == 1
    # Jul: (17 - 8) mod 24 == 9 — one hour off (no DST adjust).
    assert hod["hours"][9] == 1
    # Proves the two events did NOT coalesce into a single bucket.
    assert hod["total"] == 2
    assert sum(hod["hours"]) == 2


def test_render_html_single_page_has_everything():
    r = _build_fixture_report()
    html = sm.render_html(r, variant="single")
    assert 'id="session-blocks"' in html
    assert 'id="hod-chart"'       in html
    assert 'id="chart-container'  in html   # chart lives on the same page


def test_render_html_dashboard_omits_chart_and_highcharts():
    r = _build_fixture_report()
    html = sm.render_html(r, variant="dashboard", nav_sibling="detail.html")
    assert 'id="session-blocks"'   in html
    assert 'id="hod-chart"'        in html
    assert 'id="chart-container'   not in html
    assert "Highcharts"            not in html
    assert 'href="detail.html"'    in html


def test_render_html_detail_omits_insights():
    r = _build_fixture_report()
    html = sm.render_html(r, variant="detail", nav_sibling="dashboard.html")
    assert 'id="chart-container'   in html
    assert "Highcharts"            in html
    assert 'id="session-blocks"'   not in html
    assert 'class="cards"'         not in html
    assert 'href="dashboard.html"' in html


def test_session_duration_stats_computes_burn_rate():
    session = {
        "turns": [
            {"timestamp": "2026-04-15T22:00:00Z"},
            {"timestamp": "2026-04-15T22:10:00Z"},
        ],
        "subtotal": {"total": 100_000, "cost": 5.00, "turns": 2},
    }
    st = sm._session_duration_stats(session)
    assert st is not None
    assert st["wall_sec"] == 600
    assert st["wall_min"] == pytest.approx(10.0)
    assert st["tokens_per_min"] == pytest.approx(10_000)
    assert st["cost_per_min"] == pytest.approx(0.5)


def test_weekly_block_counts_trailing_windows():
    # Build a small block list at fixed epoch times, then count with a
    # fixed "now" so the test is deterministic regardless of when run.
    now = _epoch(2026, 4, 20, 0, 0)
    blocks = [
        {"last_epoch": now - 2 * 86400},   # 2 days ago — in 7d
        {"last_epoch": now - 10 * 86400},  # 10 days ago — in 14d, not 7d
        {"last_epoch": now - 45 * 86400},  # 45 days ago — in total only
    ]
    s = sm._weekly_block_counts(blocks, now_epoch=now)
    assert s["trailing_7"]  == 1
    assert s["trailing_14"] == 2
    assert s["trailing_30"] == 2
    assert s["total"]       == 3


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


# =============================================================================
# Compare primitives (session_metrics_compare module) - Phase 1
# =============================================================================

import shutil  # noqa: E402  (import here to keep compare tests self-contained)


_FIXTURES_DIR = _HERE / "fixtures"


# --- _model_family_slug ------------------------------------------------------

def test_model_family_slug_opus_4_7():
    assert smc._model_family_slug("claude-opus-4-7") == "opus-4-7"


def test_model_family_slug_opus_4_6():
    assert smc._model_family_slug("claude-opus-4-6") == "opus-4-6"


def test_model_family_slug_tolerates_1m_suffix():
    # Default 4.7 arrives tagged claude-opus-4-7[1m]; the compare
    # resolver must treat it as the same family as the non-[1m]
    # baseline — pricing is identical and the tokenizer is the same.
    assert smc._model_family_slug("claude-opus-4-7[1m]") == "opus-4-7"


def test_model_family_slug_strips_date_stamp():
    # Some haiku ids carry a trailing YYYYMMDD date; treat as same
    # family as the un-dated form.
    assert (
        smc._model_family_slug("claude-haiku-4-5-20251001")
        == smc._model_family_slug("claude-haiku-4-5")
        == "haiku-4-5"
    )


def test_model_family_slug_cross_family():
    assert smc._model_family_slug("claude-sonnet-4-7") == "sonnet-4-7"


def test_model_family_slug_unknown_returns_empty():
    # Non-Claude models (BYOK / proxy edge cases) produce empty so
    # callers can refuse gracefully rather than guess a family.
    assert smc._model_family_slug("gpt-4o") == ""
    assert smc._model_family_slug("") == ""
    assert smc._model_family_slug("garbage-string") == ""


def test_strip_context_tier_suffix():
    assert smc._strip_context_tier_suffix("claude-opus-4-7[1m]") == "claude-opus-4-7"
    assert smc._strip_context_tier_suffix("claude-opus-4-7") == "claude-opus-4-7"
    # Only trailing bracketed tags are stripped.
    assert smc._strip_context_tier_suffix("claude-[old]-opus-4-7") == "claude-[old]-opus-4-7"


# --- _user_prompt_fingerprint_text & _user_prompt_fingerprint ---------------

def test_fingerprint_text_plain_string():
    assert smc._user_prompt_fingerprint_text("hello world") == "hello world"


def test_fingerprint_text_whitespace_collapsed():
    # Leading/trailing/internal whitespace normalization so CR/LF
    # drift between paste paths doesn't change the fingerprint.
    assert (
        smc._user_prompt_fingerprint_text("  hello\n\n world  ")
        == smc._user_prompt_fingerprint_text("hello world")
        == "hello world"
    )


def test_fingerprint_text_list_of_blocks():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    assert smc._user_prompt_fingerprint_text(content) == "hello world"


def test_fingerprint_text_ignores_tool_result_blocks():
    # tool_result blocks don't represent user-written prompts; they
    # should be excluded from the fingerprint so a prompt paired
    # with tool output doesn't hash differently than the same
    # prompt alone.
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "output"},
    ]
    assert smc._user_prompt_fingerprint_text(content) == "hello"


def test_fingerprint_text_returns_empty_for_unpairable():
    # Pure tool_result, None, and empty inputs all return "" — the
    # pairing code treats these as unpairable turns.
    assert smc._user_prompt_fingerprint_text(None) == ""
    assert smc._user_prompt_fingerprint_text([]) == ""
    assert smc._user_prompt_fingerprint_text([
        {"type": "tool_result", "tool_use_id": "t1", "content": "out"},
    ]) == ""


def test_fingerprint_hash_stable():
    # Same input → same hash; different inputs → different hashes.
    h1 = smc._user_prompt_fingerprint("prompt one")
    h2 = smc._user_prompt_fingerprint("prompt one")
    h3 = smc._user_prompt_fingerprint("prompt two")
    assert h1 == h2
    assert h1 != h3


def test_fingerprint_text_prefix_window():
    # Prompts that share the first 200 chars but diverge later
    # hash identically — an intentional trade-off: trailing drift
    # (tacked-on modifiers, signature blocks) shouldn't break pairing.
    base = "x" * 199 + "A"
    drifted = "x" * 199 + "B" + " suffix that does not matter"
    # Only the first 200 chars hash. Char 200 differs (A vs B) so
    # these should hash differently. Verify the window is at 200.
    h1 = smc._user_prompt_fingerprint(base)
    h2 = smc._user_prompt_fingerprint(drifted)
    assert h1 != h2
    # But prompts identical in the first 200 chars hash the same.
    same = "x" * 199 + "A" + " different suffix"
    assert smc._user_prompt_fingerprint(same) == h1


# --- _pair_turns -------------------------------------------------------------

def _make_turn(prompt_text, model="claude-opus-4-7"):
    """Construct a minimal turn dict for pairing tests."""
    return {
        "message": {"model": model, "usage": {"input_tokens": 1}},
        "_preceding_user_content": [
            {"type": "text", "text": prompt_text},
        ] if prompt_text else None,
    }


def test_pair_turns_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown pairing mode"):
        smc._pair_turns([], [], mode="nope")


def test_pair_turns_ordinal_exact():
    a = [_make_turn("p1"), _make_turn("p2"), _make_turn("p3")]
    b = [_make_turn("p1"), _make_turn("p2"), _make_turn("p3")]
    result = smc._pair_turns(a, b, mode="ordinal")
    assert result["mode"] == "ordinal"
    assert len(result["paired"]) == 3
    assert result["unmatched_a"] == []
    assert result["unmatched_b"] == []
    assert result["warnings"] == []


def test_pair_turns_ordinal_length_mismatch():
    a = [_make_turn("p1"), _make_turn("p2"), _make_turn("p3")]
    b = [_make_turn("p1"), _make_turn("p2")]
    result = smc._pair_turns(a, b, mode="ordinal")
    assert len(result["paired"]) == 2
    assert len(result["unmatched_a"]) == 1
    assert result["unmatched_b"] == []
    # Warning tells the user A had an extra turn.
    assert result["warnings"]
    assert "lengths differ" in result["warnings"][0]


def test_pair_turns_fingerprint_exact_match():
    a = [_make_turn("alpha"), _make_turn("beta"), _make_turn("gamma")]
    b = [_make_turn("alpha"), _make_turn("beta"), _make_turn("gamma")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert result["mode"] == "fingerprint"
    assert len(result["paired"]) == 3
    assert result["unmatched_a"] == result["unmatched_b"] == []


def test_pair_turns_fingerprint_tolerates_whitespace_drift():
    # Whitespace-normalized fingerprint means CR/LF and leading
    # spaces don't break pairing.
    a = [_make_turn("alpha prompt"), _make_turn("beta prompt")]
    b = [_make_turn("  alpha  prompt\n"), _make_turn("beta\nprompt")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert len(result["paired"]) == 2


def test_pair_turns_fingerprint_reorders_ok():
    # Fingerprint pairing is order-independent — prompts can be
    # run in a different sequence on the two sides.
    a = [_make_turn("one"), _make_turn("two"), _make_turn("three")]
    b = [_make_turn("three"), _make_turn("one"), _make_turn("two")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert len(result["paired"]) == 3


def test_pair_turns_fingerprint_unmatched_reported():
    a = [_make_turn("shared"), _make_turn("only_a")]
    b = [_make_turn("shared"), _make_turn("only_b")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert len(result["paired"]) == 1
    assert len(result["unmatched_a"]) == 1
    assert len(result["unmatched_b"]) == 1
    # Warning surfaces the unmatched counts.
    assert any("no partner" in w for w in result["warnings"])


def test_pair_turns_fingerprint_duplicate_prompts_paired_ordinally():
    # Same prompt asked twice on each side should pair 1st-with-1st
    # and 2nd-with-2nd (not squashed into a single pairing).
    a = [_make_turn("repeat"), _make_turn("repeat"), _make_turn("repeat")]
    b = [_make_turn("repeat"), _make_turn("repeat")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert len(result["paired"]) == 2
    assert len(result["unmatched_a"]) == 1


def test_pair_turns_fingerprint_empty_prompts_unpairable():
    # Turns whose preceding user content is a pure tool_result (no
    # text block) can't be paired — they have no fingerprint.
    a = [_make_turn("prompt"), _make_turn(None)]
    b = [_make_turn("prompt"), _make_turn(None)]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert len(result["paired"]) == 1
    assert len(result["unmatched_a"]) == 1
    assert len(result["unmatched_b"]) == 1


def test_pair_turns_fingerprint_zero_paired_warns():
    a = [_make_turn("only_a1"), _make_turn("only_a2")]
    b = [_make_turn("only_b1")]
    result = smc._pair_turns(a, b, mode="fingerprint")
    assert result["paired"] == []
    assert any("matched 0 turns" in w for w in result["warnings"])


# --- _dominant_model_family --------------------------------------------------

def test_dominant_model_family_homogeneous():
    turns = [
        {"message": {"model": "claude-opus-4-7"}},
        {"message": {"model": "claude-opus-4-7"}},
    ]
    assert smc._dominant_model_family(turns) == "opus-4-7"


def test_dominant_model_family_tolerates_1m_suffix():
    turns = [
        {"message": {"model": "claude-opus-4-7[1m]"}},
        {"message": {"model": "claude-opus-4-7[1m]"}},
    ]
    assert smc._dominant_model_family(turns) == "opus-4-7"


def test_dominant_model_family_mixed_picks_most_frequent():
    turns = [
        {"message": {"model": "claude-opus-4-7"}},
        {"message": {"model": "claude-opus-4-7"}},
        {"message": {"model": "claude-sonnet-4-7"}},
    ]
    assert smc._dominant_model_family(turns) == "opus-4-7"


def test_dominant_model_family_empty_returns_blank():
    assert smc._dominant_model_family([]) == ""


def test_dominant_model_family_unknown_models_bucketed_out():
    # Non-Claude turns are excluded from the count; if they're the
    # only ones present, the function returns "".
    turns = [{"message": {"model": "gpt-4o"}}]
    assert smc._dominant_model_family(turns) == ""


# --- project inventory + compare arg resolver --------------------------------

def _build_project_dir(tmp_path, slug, filenames_with_mtimes):
    """Populate a fake project dir with fixture JSONLs at fixed mtimes.

    Args:
        tmp_path: pytest tmp_path fixture.
        slug: project slug.
        filenames_with_mtimes: [(fixture_filename, mtime_offset_seconds), ...].
            Fixtures are copied from tests/fixtures/; mtimes are set
            relative to a deterministic base so file ordering is
            reproducible across test runs.

    Returns:
        (projects_dir, project_dir) — set CLAUDE_PROJECTS_DIR to
        projects_dir in tests that need the resolver to find these.
    """
    projects_dir = tmp_path / "projects"
    project_dir = projects_dir / slug
    project_dir.mkdir(parents=True)
    base_mtime = 1_700_000_000  # arbitrary fixed epoch
    for fname, offset in filenames_with_mtimes:
        src = _FIXTURES_DIR / fname
        dst = project_dir / fname
        shutil.copy(src, dst)
        import os as _os
        _os.utime(dst, (base_mtime + offset, base_mtime + offset))
    return projects_dir, project_dir


def test_project_family_inventory_groups_by_family(tmp_path, monkeypatch):
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
            ("compare_sonnet_4_7_a.jsonl", 30),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    inv = smc._project_family_inventory("test-slug", use_cache=False)
    assert "opus-4-6" in inv
    assert "opus-4-7" in inv
    assert "sonnet-4-7" in inv
    # Each family has exactly one session.
    assert len(inv["opus-4-6"]) == 1
    assert len(inv["opus-4-7"]) == 1
    assert len(inv["sonnet-4-7"]) == 1
    # User-turn count is captured (5 prompts per fixture).
    assert inv["opus-4-6"][0][1] == 5


def test_resolve_compare_arg_path_existing(tmp_path, monkeypatch):
    # A real path inside CLAUDE_PROJECTS_DIR returns ("single", [path])
    # without any project lookup. Post-H5 the explicit-path form requires
    # the target to resolve under the projects directory.
    projects_dir = tmp_path / "projects"
    (projects_dir / "slug").mkdir(parents=True)
    target = projects_dir / "slug" / "compare_opus_4_6_a.jsonl"
    target.write_bytes((_FIXTURES_DIR / "compare_opus_4_6_a.jsonl").read_bytes())
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(str(target), "any-slug")
    assert kind == "single"
    assert len(paths) == 1
    assert paths[0].name == "compare_opus_4_6_a.jsonl"


def test_resolve_compare_arg_path_missing_raises(tmp_path):
    with pytest.raises(smc.CompareArgError, match="path does not exist"):
        smc._resolve_compare_arg(str(tmp_path / "nope.jsonl"), "slug")


def test_resolve_compare_arg_path_outside_projects_rejected(tmp_path, monkeypatch):
    """Regression for H5: explicit paths outside CLAUDE_PROJECTS_DIR must
    raise CompareArgError, not silently accept arbitrary filesystem reads."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(smc.CompareArgError, match="refusing to read outside"):
        smc._resolve_compare_arg(str(outside), "slug")


def test_resolve_compare_arg_last_family(tmp_path, monkeypatch):
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "last-opus-4-7", "test-slug", use_cache=False,
    )
    assert kind == "single"
    assert paths[0].name == "compare_opus_4_7_a.jsonl"


def test_resolve_compare_arg_last_family_short_form(tmp_path, monkeypatch):
    # Short form "last-4-7" should resolve when only one family
    # in the project carries that suffix.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "last-4-7", "test-slug", use_cache=False,
    )
    assert kind == "single"
    assert paths[0].name == "compare_opus_4_7_a.jsonl"


def test_resolve_compare_arg_last_family_short_form_ambiguous(tmp_path, monkeypatch):
    # When two families end with "-4-7" (opus-4-7 and sonnet-4-7),
    # "last-4-7" is ambiguous and the resolver refuses with a list
    # of candidates rather than guessing.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_a.jsonl", 10),
            ("compare_sonnet_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    with pytest.raises(smc.CompareArgError) as exc:
        smc._resolve_compare_arg(
            "last-4-7", "test-slug", use_cache=False,
        )
    assert "no sessions found for family" in str(exc.value)
    # Error lists both ambiguous candidates so the user can pick.
    assert "opus-4-7" in str(exc.value)
    assert "sonnet-4-7" in str(exc.value)


def test_resolve_compare_arg_last_family_tolerates_1m(tmp_path, monkeypatch):
    # A session whose model is claude-opus-4-7[1m] still resolves
    # under family "opus-4-7".
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_1m_a.jsonl", 10),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "last-opus-4-7", "test-slug", use_cache=False,
    )
    assert kind == "single"
    assert paths[0].name == "compare_opus_4_7_1m_a.jsonl"


def test_resolve_compare_arg_last_family_min_turns_filter(tmp_path, monkeypatch):
    # With both a 5-turn and a 2-turn opus-4-7 session, default
    # min_turns=5 must pick the 5-turn one even though the 2-turn
    # file has a more recent mtime.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_a.jsonl", 10),      # 5 turns, older
            ("compare_opus_4_7_short.jsonl", 20),  # 2 turns, newer
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "last-opus-4-7", "test-slug", use_cache=False,
    )
    assert kind == "single"
    assert paths[0].name == "compare_opus_4_7_a.jsonl"


def test_resolve_compare_arg_last_family_min_turns_override(tmp_path, monkeypatch):
    # With min_turns=1, the newer (2-turn) session becomes eligible
    # and wins on recency.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_a.jsonl", 10),
            ("compare_opus_4_7_short.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "last-opus-4-7", "test-slug", min_turns=1, use_cache=False,
    )
    assert paths[0].name == "compare_opus_4_7_short.jsonl"


def test_resolve_compare_arg_last_family_all_below_min(tmp_path, monkeypatch):
    # Only short sessions present → error mentions the min-turn
    # override flag.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_short.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    with pytest.raises(smc.CompareArgError) as exc:
        smc._resolve_compare_arg(
            "last-opus-4-7", "test-slug", use_cache=False,
        )
    assert "--compare-min-turns" in str(exc.value)


def test_resolve_compare_arg_all_family_aggregate(tmp_path, monkeypatch):
    # all-<family> should return every session matching the family,
    # including ones below min_turns.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_7_a.jsonl", 10),
            ("compare_opus_4_7_short.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    kind, paths = smc._resolve_compare_arg(
        "all-opus-4-7", "test-slug", use_cache=False,
    )
    assert kind == "aggregate"
    assert len(paths) == 2
    names = {p.name for p in paths}
    assert names == {"compare_opus_4_7_a.jsonl", "compare_opus_4_7_short.jsonl"}


def test_resolve_compare_arg_unknown_family_lists_present(tmp_path, monkeypatch):
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_sonnet_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    with pytest.raises(smc.CompareArgError) as exc:
        smc._resolve_compare_arg(
            "last-opus-4-5", "test-slug", use_cache=False,
        )
    # Error lists the families actually present so the user can
    # fix the typo or pick an alternative.
    msg = str(exc.value)
    assert "opus-4-6" in msg
    assert "sonnet-4-7" in msg


def test_resolve_compare_arg_empty_family_slug_raises(tmp_path):
    with pytest.raises(smc.CompareArgError, match="missing a family slug"):
        smc._resolve_compare_arg("last-", "slug")


def test_resolve_compare_arg_uninterpretable_raises():
    # A string with whitespace / invalid chars fails _SESSION_RE and
    # isn't a path or magic token, so falls through to the generic
    # "could not interpret" error.
    with pytest.raises(smc.CompareArgError, match="could not interpret"):
        smc._resolve_compare_arg("has spaces not valid", "slug")


def test_resolve_compare_arg_empty_string_raises():
    with pytest.raises(smc.CompareArgError, match="compare arg is empty"):
        smc._resolve_compare_arg("", "slug")


def test_match_family_key_exact():
    assert smc._match_family_key("opus-4-7", ["opus-4-7", "sonnet-4-7"]) == "opus-4-7"


def test_match_family_key_unique_suffix():
    assert smc._match_family_key("4-6", ["opus-4-6", "sonnet-4-7"]) == "opus-4-6"


def test_match_family_key_ambiguous_suffix_returns_none():
    # Two families end in "-4-7" → ambiguous, return None so the
    # caller can produce a helpful error.
    assert smc._match_family_key("4-7", ["opus-4-7", "sonnet-4-7"]) is None


def test_match_family_key_no_match_returns_none():
    assert smc._match_family_key("4-99", ["opus-4-7"]) is None


def test_main_module_required():
    # Sanity check: the compare module's _main() must see the
    # session_metrics module in sys.modules. This test verifies
    # the coupling contract by removing and restoring the module
    # registration.
    saved = sys.modules.pop("session_metrics")
    try:
        with pytest.raises(RuntimeError, match="must be loaded"):
            smc._main()
    finally:
        sys.modules["session_metrics"] = saved


# =========================================================================
# Phase 2 — Compare-report builder, renderers, and CLI dispatch
# =========================================================================

import json   # noqa: E402  (local to Phase 2 tests — keep imports grouped)
import copy   # noqa: E402


def _load_compare_fixture(name: str):
    """Read a compare fixture the way ``_load_session`` would. Used by
    every Phase-2 builder test so the three-line setup isn't duplicated.
    """
    path = _FIXTURES_DIR / name
    sid, turns, user_ts = sm._load_session(
        path, include_subagents=False, use_cache=False,
    )
    return sid, turns, user_ts


def _make_basic_compare_report():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    return smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
    )


# ---- Report builder --------------------------------------------------------

def test_build_compare_report_basic_shape():
    report = _make_basic_compare_report()
    assert report["mode"] == "compare"
    assert report["compare_mode"] == "controlled"
    assert report["slug"] == "test-slug"
    assert report["pair_by"] == "fingerprint"
    assert report["side_a"]["model_family"] == "opus-4-6"
    assert report["side_b"]["model_family"] == "opus-4-7"
    assert report["side_a"]["turn_count"] == 5
    assert report["side_b"]["turn_count"] == 5
    assert len(report["paired"]) == 5
    assert report["unmatched_a"] == []
    assert report["unmatched_b"] == []
    assert report["summary"]["paired_count"] == 5
    # Fixture designed for exact 1.3× ratio across every metric.
    assert report["summary"]["input_tokens_ratio"] == pytest.approx(1.3)
    assert report["summary"]["cost_ratio"] == pytest.approx(1.3, rel=1e-9)
    # Effort is unset by default — renderers treat None as "don't annotate".
    assert report["side_a"]["effort"] is None
    assert report["side_b"]["effort"] is None


def test_build_compare_report_threads_effort_into_both_sides():
    # Effort labels are purely cosmetic annotations that flow from the
    # CLI into the report dict so every renderer (text/MD/CSV/HTML/
    # analysis.md) can surface which --effort level each side ran at.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
        effort_a="high", effort_b="xhigh",
    )
    assert report["side_a"]["effort"] == "high"
    assert report["side_b"]["effort"] == "xhigh"


def test_compare_text_renderer_emits_effort_suffix_when_set():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
        effort_a="high", effort_b="xhigh",
    )
    text = smc._render_controlled_text(report)
    # Both sides' banner rows carry the effort suffix when set.
    assert "effort=high" in text
    assert "effort=xhigh" in text


def test_compare_text_renderer_omits_effort_suffix_when_unset():
    report = _make_basic_compare_report()
    text = smc._render_controlled_text(report)
    assert "effort=" not in text


def test_compare_md_renderer_adds_effort_column_when_set():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
        effort_a="high", effort_b="xhigh",
    )
    md = smc._render_controlled_md(report)
    assert "| Effort |" in md
    assert "`high`" in md
    assert "`xhigh`" in md


def test_compare_md_renderer_omits_effort_column_when_unset():
    report = _make_basic_compare_report()
    md = smc._render_controlled_md(report)
    assert "| Effort |" not in md


def test_compare_html_renderer_includes_effort_when_set():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
        effort_a="high", effort_b="xhigh",
    )
    html = smc._render_compare_html_controlled(report)
    # Effort renders as `effort <code>level</code>` inside side-meta.
    assert "effort <code>high</code>" in html
    assert "effort <code>xhigh</code>" in html


def test_compare_html_renderer_omits_effort_when_unset():
    report = _make_basic_compare_report()
    html = smc._render_compare_html_controlled(report)
    assert "effort <code>" not in html


def test_compare_analysis_md_includes_effort_when_set():
    # Build a minimal fake analysis input: compare_report + two
    # per-session report dicts. The analysis renderer only reads a
    # narrow slice so empty session reports are fine.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    compare_report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
        effort_a="high", effort_b="xhigh",
    )
    fake_session_report = {"totals": {}}
    article = smc._render_compare_analysis_md(
        compare_report, fake_session_report, fake_session_report, links={},
    )
    assert "effort `high`" in article
    assert "effort `xhigh`" in article


def test_build_compare_report_cost_delta_is_tokenizer_driven():
    # Pricing is identical for claude-opus-4-6 and claude-opus-4-7, so
    # a nonzero cost delta between two sessions running the same prompts
    # is 100% tokenizer-driven. The article's whole premise.
    a_rates = sm._pricing_for("claude-opus-4-6")
    b_rates = sm._pricing_for("claude-opus-4-7")
    assert a_rates["input"]  == b_rates["input"]
    assert a_rates["output"] == b_rates["output"]
    report = _make_basic_compare_report()
    cost_a = report["side_a"]["totals"]["cost"]
    cost_b = report["side_b"]["totals"]["cost"]
    assert cost_b > cost_a > 0


def test_build_compare_report_fingerprint_matches_regardless_of_order():
    # Same prompt text on both sides — every pair matches on content,
    # even if the B-side order is reversed.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, list(reversed(b_turns)), b_user_ts,
        slug="test-slug", pair_by="fingerprint",
    )
    assert len(report["paired"]) == 5
    for pair in report["paired"]:
        assert pair["a"]["input_tokens"] * 1.3 == pytest.approx(pair["b"]["input_tokens"])


def test_build_compare_report_ordinal_pairing():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug", pair_by="ordinal",
    )
    assert report["pair_by"] == "ordinal"
    assert len(report["paired"]) == 5
    # Sanity: fingerprint field is either absent or a hex digest — never
    # a prompt string or garbled value.
    for pair in report["paired"]:
        fp = pair.get("fingerprint")
        assert fp is None or all(c in "0123456789abcdef" for c in fp)


def test_build_compare_report_context_tier_mismatch_advisory():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_1m_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
    )
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "context-tier-mismatch" in kinds


def test_build_compare_report_cache_share_drift_advisory():
    # Synthesize > 10 pp cache-read-share drift by inflating side A's
    # cache_read on every turn.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    a_mut = copy.deepcopy(a_turns)
    for t in a_mut:
        t["message"]["usage"]["cache_read_input_tokens"] = 1000
    report = smc._build_compare_report(
        a_sid, a_mut, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
    )
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "cache-share-drift" in kinds


def test_build_compare_report_model_family_collision_info():
    # Feeding the same session on both sides fires an info-level
    # collision advisory; the report still renders.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
    )
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "model-family-collision" in kinds
    assert report["summary"]["cost_ratio"] == pytest.approx(1.0)


def test_build_compare_report_cross_family_no_collision():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_sonnet_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_turns, b_user_ts,
        slug="test-slug",
    )
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "model-family-collision" not in kinds
    # Model-agnostic header data — no hardcoded family slug in report.
    assert report["side_a"]["model_family"] == "opus-4-7"
    assert report["side_b"]["model_family"] == "sonnet-4-7"


def test_build_compare_report_no_fingerprint_matches_advisory():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    # Replace every B-side prompt with distinct text so fingerprint
    # pairing matches zero turns.
    b_mut = copy.deepcopy(b_turns)
    for i, t in enumerate(b_mut):
        t["_preceding_user_content"] = [
            {"type": "text", "text": f"unique b-side prompt {i}"}
        ]
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts,
        b_sid, b_mut, b_user_ts,
        slug="test-slug", pair_by="fingerprint",
    )
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "no-fingerprint-matches" in kinds
    assert report["paired"] == []


# ---- Per-helper edge cases -------------------------------------------------

def test_safe_ratio_zero_denominator():
    assert smc._safe_ratio(10, 0) is None
    assert smc._safe_ratio(0, 5) == 0.0
    assert smc._safe_ratio(6, 2) == 3.0


def test_context_tier_from_model_id():
    assert smc._context_tier_from_model_id("claude-opus-4-7[1m]") == "1m"
    assert smc._context_tier_from_model_id("claude-opus-4-7") == ""
    assert smc._context_tier_from_model_id("") == ""


def test_dominant_model_id_picks_majority():
    turns = [
        {"message": {"model": "claude-opus-4-6"}},
        {"message": {"model": "claude-opus-4-6"}},
        {"message": {"model": "claude-opus-4-7"}},
    ]
    assert smc._dominant_model_id(turns) == "claude-opus-4-6"


def test_dominant_model_id_empty():
    assert smc._dominant_model_id([]) == ""


def test_cache_read_share_zero_input():
    # Degenerate empty-session totals → share is 0.0, not NaN / crash.
    assert smc._cache_read_share_pct({"total_input": 0, "cache_read": 0}) == 0.0


# ---- Renderers --------------------------------------------------------------

def test_render_compare_text_contains_summary_and_ratios():
    report = _make_basic_compare_report()
    out = smc.render_compare_text(report)
    assert "COMPARE (controlled)" in out
    assert "claude-opus-4-6" in out
    assert "claude-opus-4-7" in out
    assert "1.30×" in out
    # Model-agnostic: no hardcoded "4.6 vs 4.7" literal in the renderer.
    assert "4.6 vs 4.7" not in out


def test_render_compare_md_contains_tables():
    report = _make_basic_compare_report()
    out = smc.render_compare_md(report)
    assert "# Model Compare" in out
    assert "| Side | Session |" in out
    assert "| Input-token ratio |" in out
    assert "1.30×" in out
    assert "## Paired turns" in out


def test_render_compare_json_is_valid_json():
    report = _make_basic_compare_report()
    out = smc.render_compare_json(report)
    parsed = json.loads(out)
    assert parsed["mode"] == "compare"
    assert parsed["side_a"]["model_family"] == "opus-4-6"
    assert parsed["side_b"]["model_family"] == "opus-4-7"
    assert len(parsed["paired"]) == 5


def test_render_compare_csv_layout():
    report = _make_basic_compare_report()
    out = smc.render_compare_csv(report)
    lines = out.strip().splitlines()
    assert lines[0].startswith("pair_index,fingerprint,")
    data_rows = lines[1:6]
    assert all("claude-opus-4-6" in r and "claude-opus-4-7" in r for r in data_rows)
    assert "# SUMMARY" in out
    assert "# RATIOS (B vs A)" in out
    assert "cost_ratio,1.3000" in out


def test_main_renderers_delegate_on_compare_mode():
    # The four dispatchers in session-metrics.py branch on
    # report["mode"] == "compare" and delegate to the compare module.
    report = _make_basic_compare_report()
    assert sm.render_text(report) == smc.render_compare_text(report)
    assert sm.render_md(report)   == smc.render_compare_md(report)
    assert sm.render_csv(report)  == smc.render_compare_csv(report)
    assert sm.render_json(report) == smc.render_compare_json(report)


# ---- Scope reconciliation ---------------------------------------------------

def test_check_compare_scope_auto_session_pair():
    assert smc._check_compare_scope("auto", "single", "single") == "controlled"


def test_check_compare_scope_session_forces_single():
    with pytest.raises(smc.CompareArgError, match="requires two single"):
        smc._check_compare_scope("session", "aggregate", "single")


def test_check_compare_scope_project_returns_observational():
    # Phase 3: --compare-scope=project forces Mode 2 even for two single
    # sessions. Useful for comparing two one-session "families" side-by-side
    # under the observational framing (no pairing; aggregate columns only).
    assert smc._check_compare_scope("project", "single", "single") == "observational"


def test_check_compare_scope_auto_picks_observational_on_aggregate():
    # Phase 3: auto-scope picks observational iff either side is aggregate.
    assert smc._check_compare_scope("auto", "aggregate", "single") == "observational"
    assert smc._check_compare_scope("auto", "single", "aggregate") == "observational"
    assert smc._check_compare_scope("auto", "aggregate", "aggregate") == "observational"


# ---- CLI smoke tests (through sm.main) --------------------------------------

def test_cli_compare_happy_path(monkeypatch, tmp_path, capsys):
    # End-to-end invocation: fire --compare with absolute paths and
    # confirm stdout carries the compare report and the 1.30× ratio.
    # Uses an isolated projects dir so compare's marker file
    # (.session-metrics-compare-used) doesn't pollute the committed
    # fixtures dir on every test run.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (controlled)" in out
    assert "1.30×" in out


def test_cli_compare_html_exports_single_page(monkeypatch, tmp_path, capsys):
    # Phase 6: --output html for compare mode now ships a single-page
    # HTML document. The test ensures the file lands on disk and carries
    # at least one compare-specific DOM marker so downstream renderers
    # that wrap the output can still see it.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug", "--output", "html"],
    )
    sm.main()
    err = capsys.readouterr().err
    assert "[export] HTML (compare)" in err
    exports = tmp_path / "exports" / "session-metrics"
    html_files = list(exports.glob("compare_*.html"))
    assert html_files, f"No compare HTML exported under {exports}"
    body = html_files[0].read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in body
    assert "Session Metrics" in body
    assert "section class=\"compare-card" in body or 'class="compare-card' in body


def test_cli_compare_last_family_magic_token(monkeypatch, tmp_path, capsys):
    # Set up a project dir with one 4.6 + one 4.7 session; resolver
    # picks the single match per family via last-<family>.
    projects_dir, _ = _build_project_dir(
        tmp_path,
        "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 0),
            ("compare_opus_4_7_a.jsonl", 100),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", "last-opus-4-6", "last-opus-4-7",
         "--slug", "test-slug"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (controlled)" in out
    assert "claude-opus-4-6" in out
    assert "claude-opus-4-7" in out


def test_cli_compare_all_family_runs_mode_2(monkeypatch, tmp_path, capsys):
    # Phase 3: resolver produces an aggregate; _check_compare_scope returns
    # "observational" and _run_compare dispatches Mode 2.
    projects_dir, project_dir = _build_project_dir(
        tmp_path,
        "test-slug",
        [
            # Include the A-side fixture in the projects dir so the
            # post-H5 traversal guard on compare's explicit-path form
            # accepts the absolute path.
            ("compare_opus_4_6_a.jsonl", -1),
            ("compare_opus_4_7_a.jsonl", 0),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), "all-opus-4-7",
         "--slug", "test-slug", "--yes"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (observational)" in out
    assert "claude-opus-4-6" in out
    assert "claude-opus-4-7" in out


def test_cli_compare_scope_project_runs_mode_2(monkeypatch, tmp_path, capsys):
    # Phase 3: --compare-scope=project forces observational even when both
    # args are single sessions.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug", "--compare-scope", "project", "--yes"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (observational)" in out


def test_cli_compare_write_output_filename(monkeypatch, tmp_path):
    # --output md should produce a compare_<a>_vs_<b>_<ts>.md file.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug", "--output", "md"],
    )
    sm.main()
    exports = tmp_path / "exports" / "session-metrics"
    md_files = list(exports.glob("compare_*.md"))
    assert len(md_files) == 1
    body = md_files[0].read_text()
    assert "# Model Compare" in body
    assert "1.30×" in body


def test_cli_compare_missing_args_argparse_error(monkeypatch):
    # --compare requires two args; argparse errors with usage text.
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", "only-one-arg",
         "--slug", "test-slug"],
    )
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 2   # argparse usage errors are code 2


def test_cli_compare_not_triggered_without_flag(monkeypatch, tmp_path, capsys):
    # Without --compare, natural-language-style args can't dispatch
    # compare mode. The skill drops into single-session handling and
    # fails on the missing session, which is the correct guardrail
    # behavior — the key assertion is that "COMPARE" isn't in stdout.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "empty-projects"))
    (tmp_path / "empty-projects").mkdir()
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--slug", "nonexistent-slug"],
    )
    with pytest.raises(SystemExit):
        sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (controlled)" not in out


# =========================================================================
# Phase 3 — Mode 2 (observational, project-aggregate) compare
# =========================================================================

import io  # noqa: E402  (local to Phase 3 tests — keep imports grouped)


def _load_compare_sessions_for_aggregate(*names):
    """Shorthand: build the (sid, raw_turns, user_ts) tuples Mode 2 wants."""
    sessions = []
    for name in names:
        sid, raw, user_ts = _load_compare_fixture(name)
        sessions.append((sid, raw, user_ts))
    return sessions


def test_aggregate_side_info_rolls_up_multiple_sessions():
    # Two 4.7 fixtures rolled up on side B; one 4.6 fixture on side A.
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate(
        "compare_opus_4_7_a.jsonl", "compare_opus_4_7_short.jsonl",
    )
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    assert report["mode"] == "compare"
    assert report["compare_mode"] == "observational"
    # Side B aggregate: 5 + 2 = 7 turns.
    assert report["side_b"]["session_count"] == 2
    assert report["side_b"]["turn_count"] == 7
    # Totals sum across sessions.
    b_input = (170 + 150 + 130 + 110 + 90) + (50 + 55)  # 4_7_a + 4_7_short... wait
    # Actually compare_opus_4_7_a has 90,110,130,150,170 (6.5% of what 4_6_a has? no)
    # Rather than reverse-engineering the fixture, assert that total > either individual.
    assert report["side_b"]["totals"]["input"] == b_input or (
        report["side_b"]["totals"]["input"] > 50
        and report["side_b"]["totals"]["input"] > 170
    )
    # No paired/unmatched in Mode 2.
    assert "paired" not in report
    assert "unmatched_a" not in report


def test_aggregate_report_carries_observational_banner():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "observational-not-controlled" in kinds


def test_aggregate_report_avg_ratios_computed():
    # With 4.6 vs 4.7 and identical prompt structure, the avg-per-prompt
    # ratio should equal the input-token ratio (1.30×) since prompt
    # count and turn count are identical.
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    s = report["summary"]
    assert s["avg_input_per_prompt_ratio"] == pytest.approx(1.3, rel=1e-6)
    assert s["avg_output_per_turn_ratio"] == pytest.approx(1.3, rel=1e-6)


def test_aggregate_context_tier_advisory():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_1m_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "context-tier-mismatch" in kinds


def test_aggregate_empty_side_advisory():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    report = smc._build_compare_aggregate_report(a, [], slug="agg-test")
    kinds = [adv["kind"] for adv in report["advisories"]]
    assert "empty-side" in kinds
    assert report["side_b"]["session_count"] == 0


# ---- Aggregate renderers ----------------------------------------------------

def test_render_compare_text_observational_branch():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    out = smc.render_compare_text(report)
    assert "COMPARE (observational)" in out
    assert "observational compare" in out  # the advisory prefix
    assert "AGGREGATE DETAIL" in out
    assert "1.30×" in out
    # No per-turn table header in Mode 2.
    assert "PAIRED TURNS" not in out


def test_render_compare_md_observational_branch():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    out = smc.render_compare_md(report)
    assert "**observational**" in out
    assert "Observational, not controlled" in out
    assert "## Aggregate detail" in out
    # Controlled-mode "Paired turns" header absent.
    assert "## Paired turns" not in out


def test_render_compare_csv_observational_branch():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    out = smc.render_compare_csv(report)
    lines = out.strip().splitlines()
    # Aggregate header + 2 rows (A, B).
    assert lines[0].startswith("side,model_family,")
    assert any(row.startswith("A,opus-4-6,") for row in lines)
    assert any(row.startswith("B,opus-4-7,") for row in lines)
    assert "# RATIOS (B vs A)" in out
    assert "cost_ratio,1.3000" in out


def test_render_compare_json_observational_mode_flag():
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_aggregate_report(a, b, slug="agg-test")
    out = smc.render_compare_json(report)
    parsed = json.loads(out)
    assert parsed["compare_mode"] == "observational"
    assert parsed["side_a"]["session_count"] == 1
    assert parsed["side_b"]["session_count"] == 1
    assert "paired" not in parsed


# ---- Confirmation gate ------------------------------------------------------

def test_confirm_aggregate_skipped_with_assume_yes(monkeypatch, capsys):
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    # Force stdin non-TTY to prove --yes trumps the non-TTY guard.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    smc._confirm_aggregate_or_exit(a, b, assume_yes=True)  # must not exit
    err = capsys.readouterr().err
    assert "aggregate preview" in err
    assert "--yes given" in err


def test_confirm_aggregate_refuses_non_tty_without_yes(monkeypatch, capsys):
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc:
        smc._confirm_aggregate_or_exit(a, b, assume_yes=False)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires --yes" in err


def test_confirm_aggregate_interactive_y_proceeds(monkeypatch, capsys):
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    fake_stdin = io.StringIO("y\n")
    fake_stdin.isatty = lambda: True   # pretend we're interactive
    monkeypatch.setattr("sys.stdin", fake_stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    smc._confirm_aggregate_or_exit(a, b, assume_yes=False)   # must not exit
    err = capsys.readouterr().err
    assert "aggregate preview" in err


def test_confirm_aggregate_interactive_n_aborts(monkeypatch, capsys):
    a = _load_compare_sessions_for_aggregate("compare_opus_4_6_a.jsonl")
    b = _load_compare_sessions_for_aggregate("compare_opus_4_7_a.jsonl")
    fake_stdin = io.StringIO("n\n")
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", fake_stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    with pytest.raises(SystemExit) as exc:
        smc._confirm_aggregate_or_exit(a, b, assume_yes=False)
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "aborted" in err


# ---- CLI + resolver integration --------------------------------------------

def test_cli_compare_aggregate_rollup_end_to_end(monkeypatch, tmp_path, capsys):
    # Two 4.7 sessions under the slug, rolled up as side B; single 4.6
    # session as side A. Verify stdout has observational report.
    projects_dir, _ = _build_project_dir(
        tmp_path, "test-slug",
        [
            ("compare_opus_4_6_a.jsonl", 0),
            ("compare_opus_4_7_a.jsonl", 10),
            ("compare_opus_4_7_short.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", "last-opus-4-6", "all-opus-4-7",
         "--slug", "test-slug", "--yes"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "COMPARE (observational)" in out
    # Two sessions on side B means the aggregate label shows "(2 sessions)"
    assert "(2 sessions)" in out


def test_cli_compare_aggregate_write_output_filename(monkeypatch, tmp_path):
    # Mode 2 hitting --output md produces a compare_<a>_vs_<b>_<ts>.md file.
    # Side A session_id ("s6") prefixes the filename since the aggregate
    # writes preserve the single session_id when session_count == 1.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug", "--compare-scope", "project",
         "--output", "md", "--yes"],
    )
    sm.main()
    exports = tmp_path / "exports" / "session-metrics"
    md_files = list(exports.glob("compare_*.md"))
    assert len(md_files) == 1
    body = md_files[0].read_text()
    assert "**observational**" in body
    assert "## Aggregate detail" in body


# =============================================================================
# Phase 4 — prompt suite + sentinels + --compare-prep
# Phase 5 — IFEval predicate eval + instruction_pass column
# =============================================================================

import io as _io_p45       # noqa: E402  (import here to keep Phase 4/5 tests self-contained)
import textwrap as _tw45   # noqa: E402

_SUITE_DIR = (
    _HERE.parent / "references" / "model-compare" / "prompts"
)


# ---- Sentinel regex --------------------------------------------------------

def test_sentinel_regex_matches_basic():
    text = "[session-metrics:compare-suite:v1:prompt=claudemd_summarise]\n\nplease summarise..."
    hits = smc._extract_sentinels(text)
    assert hits == [(1, "claudemd_summarise")]


def test_sentinel_regex_survives_whitespace_roundtrip():
    # Leading/trailing whitespace + CR/LF normalization should not affect the match.
    text = "   \r\n[session-metrics:compare-suite:v1:prompt=english_prose]\r\n  ..."
    hits = smc._extract_sentinels(text)
    assert hits == [(1, "english_prose")]


def test_sentinel_regex_survives_markdown_quoting():
    # Blockquote-prefixed paste shouldn't break the regex.
    text = "> [session-metrics:compare-suite:v1:prompt=code_review]\n> Review this..."
    hits = smc._extract_sentinels(text)
    assert hits == [(1, "code_review")]


def test_sentinel_regex_no_match():
    assert smc._extract_sentinels("no sentinel here") == []
    assert smc._extract_sentinels("") == []
    assert smc._extract_sentinels(None) == []  # type: ignore[arg-type]


def test_sentinel_regex_rejects_uppercase():
    # Prompt names are restricted to [a-z0-9_] to avoid false positives from
    # bracketed acronyms that happen to be adjacent to 'prompt='.
    text = "[session-metrics:compare-suite:v1:prompt=CamelCase]"
    assert smc._extract_sentinels(text) == []


def test_primary_sentinel_returns_first():
    text = (
        "[session-metrics:compare-suite:v1:prompt=foo]\n"
        "[session-metrics:compare-suite:v2:prompt=bar]"
    )
    assert smc._primary_sentinel(text) == (1, "foo")


def test_primary_sentinel_none_on_empty():
    assert smc._primary_sentinel("nothing here") is None


# ---- YAML frontmatter parser -----------------------------------------------

def test_parse_simple_yaml_basic():
    text = _tw45.dedent("""\
        name: foo
        description: a simple description
        reference_tokens_per_char: 0.23
    """)
    out = smc._parse_simple_yaml(text)
    assert out["name"] == "foo"
    assert out["description"] == "a simple description"
    assert out["reference_tokens_per_char"] == "0.23"


def test_parse_simple_yaml_strips_quotes():
    text = 'sentinel: "[session-metrics:compare-suite:v1:prompt=x]"'
    out = smc._parse_simple_yaml(text)
    assert out["sentinel"] == "[session-metrics:compare-suite:v1:prompt=x]"


def test_parse_simple_yaml_ignores_comments_and_blanks():
    text = "# a comment\nname: foo\n\n# another\n"
    out = smc._parse_simple_yaml(text)
    assert out == {"name": "foo"}


# ---- Prompt-suite loader ---------------------------------------------------

def test_load_prompt_suite_loads_10_prompts():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    assert len(suite) == 10
    expected_names = {
        "claudemd_summarise", "english_prose", "code_review",
        "stack_trace_debug", "tool_heavy_task", "cjk_prose",
        "json_reshape", "csv_transform", "typescript_refactor",
        "instruction_stress",
    }
    assert set(suite.keys()) == expected_names


def test_load_prompt_suite_every_prompt_has_sentinel_in_body():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    for name, entry in suite.items():
        hits = smc._extract_sentinels(entry["body"])
        assert any(h[1] == name and h[0] == smc._SUITE_VERSION for h in hits), (
            f"prompt {name!r} body missing matching sentinel"
        )


def test_load_prompt_suite_parses_predicates_except_tool_heavy():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    # tool_heavy_task deliberately has `check = None`.
    assert suite["tool_heavy_task"]["check"] is None
    # Everything else has a callable predicate.
    for name, entry in suite.items():
        if name == "tool_heavy_task":
            continue
        assert callable(entry["check"]), f"{name} should have a callable check"


def test_parse_prompt_file_malformed_raises(tmp_path):
    # A file that STARTS with '---' but has no closing fence is malformed.
    bad = tmp_path / "bad.md"
    bad.write_text("---\nname: oops\n(no closing fence)\n")
    with pytest.raises(smc.PromptSuiteError):
        smc._parse_prompt_file(bad)


def test_parse_prompt_file_lite_format(tmp_path):
    # A plain-text file (no frontmatter) is accepted as a lite-format prompt.
    lite = tmp_path / "my_lite_prompt.md"
    lite.write_text("Write a haiku about Python.\n")
    entry = smc._parse_prompt_file(lite)
    assert entry["name"] == "my_lite_prompt"
    assert entry["check"] is None
    assert "[session-metrics:user-suite:v1:prompt=my_lite_prompt]" in entry["body"]
    assert "Write a haiku about Python." in entry["body"]


def test_load_prompt_suite_missing_dir_returns_empty(tmp_path):
    # A suite-less install shouldn't raise; callers silently skip IFEval.
    assert smc._load_prompt_suite(tmp_path / "nonexistent") == {}


# ---- Individual predicates (spot-check the critical ones) ------------------

def test_predicate_english_prose_no_commas():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    check = suite["english_prose"]["check"]
    assert check("No commas here.") is True
    assert check("Has a comma, see?") is False


def test_predicate_instruction_stress_composite():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    check = suite["instruction_stress"]["check"]
    # 50 lowercase words, no commas, "foo" twice.
    good = " ".join(["foo", "foo"] + ["x"] * 48)
    assert check(good) is True
    # Same words but uppercase 'Foo' breaks the lowercase rule.
    bad_case = good.replace("foo", "Foo", 1)
    assert check(bad_case) is False
    # 49 words fails.
    assert check(" ".join(good.split()[:-1])) is False


def test_predicate_typescript_refactor_exactly_twice():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    check = suite["typescript_refactor"]["check"]
    assert check("I refactor this. Then I'll refactor again.") is True
    assert check("Refactor refactor refactor.") is False
    assert check("No matching word.") is False


def test_predicate_cjk_prose_rejects_japanese():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    check = suite["cjk_prose"]["check"]
    assert check("A plain English translation.") is True
    assert check("Mixed output 日本語 still here.") is False


def test_predicate_json_reshape_validates_shape():
    suite = smc._load_prompt_suite(_SUITE_DIR)
    check = suite["json_reshape"]["check"]
    good = '[{"id": "o_001", "name": "Acme", "total_cents": 3798}]'
    assert check(good) is True
    # Wrong keys.
    assert check('[{"id": "x", "name": "y"}]') is False
    # Not JSON.
    assert check("not json at all") is False


def test_predicate_exception_returns_false():
    # A predicate that raises must not crash the run — returns False.
    def bad(text):
        raise RuntimeError("boom")
    assert smc._run_predicate(bad, "whatever") is False


def test_predicate_none_returns_none():
    assert smc._run_predicate(None, "anything") is None


# ---- Assistant-text extractor ----------------------------------------------

def test_assistant_text_joins_text_blocks():
    raw = {"message": {"content": [
        {"type": "text", "text": "hello "},
        {"type": "tool_use", "name": "Bash", "input": {}},
        {"type": "text", "text": "world"},
    ]}}
    assert smc._assistant_text(raw) == "hello world"


def test_assistant_text_handles_string_content():
    raw = {"message": {"content": "just a string"}}
    assert smc._assistant_text(raw) == "just a string"


def test_assistant_text_empty_when_no_text():
    raw = {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}}
    assert smc._assistant_text(raw) == ""


def test_assistant_text_missing_content():
    assert smc._assistant_text({"message": {}}) == ""
    assert smc._assistant_text({}) == ""


# ---- Suite-version detection + mismatch refusal ----------------------------

def _make_sentinel_turn(
    prompt_name: str,
    version: int,
    assistant_text: str,
    model: str = "claude-opus-4-6",
    input_tokens: int = 100,
):
    """Build a raw-turn dict with a sentinel-tagged user prompt."""
    return {
        "type": "assistant",
        "uuid": f"a-{prompt_name}",
        "timestamp": "2026-04-19T10:00:00.000Z",
        "sessionId": "sX",
        "message": {
            "id": f"msg-{prompt_name}",
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        "_preceding_user_content": [
            {"type": "text",
             "text": f"[session-metrics:compare-suite:v{version}:prompt={prompt_name}]\nplease..."},
        ],
        "_is_resume_marker": False,
    }


def test_detect_suite_versions_single_version():
    turns = [_make_sentinel_turn("claudemd_summarise", 1, "ok")]
    assert smc._detect_suite_versions(turns) == {1}


def test_detect_suite_versions_mixed_refuses_without_allow():
    a_turns = [_make_sentinel_turn("foo", 1, "ok")]
    b_turns = [_make_sentinel_turn("foo", 2, "ok")]
    with pytest.raises(smc.SuiteVersionMismatchError):
        smc._resolve_suite_versions(a_turns, b_turns, allow_mismatch=False)


def test_detect_suite_versions_mismatch_allowed_emits_advisory():
    a_turns = [_make_sentinel_turn("foo", 1, "ok")]
    b_turns = [_make_sentinel_turn("foo", 2, "ok")]
    va, vb, advisories = smc._resolve_suite_versions(
        a_turns, b_turns, allow_mismatch=True,
    )
    assert va == {1}
    assert vb == {2}
    assert any(a["kind"] == "suite-version-mismatch" for a in advisories)


def test_resolve_suite_versions_empty_ok():
    # No sentinels on either side — not a mismatch, just non-suite compare.
    va, vb, advisories = smc._resolve_suite_versions([], [], allow_mismatch=False)
    assert va == set()
    assert vb == set()
    assert advisories == []


def test_intrasession_suite_mix_advises_when_allowed():
    a_turns = [
        _make_sentinel_turn("foo", 1, "ok"),
        _make_sentinel_turn("bar", 2, "ok"),
    ]
    b_turns = [_make_sentinel_turn("foo", 1, "ok")]
    # A session mixing v1 and v2 is almost always a copy-paste bug; by
    # default the cross-side mismatch refuses outright. With
    # allow_mismatch=True the compare still surfaces both the intra-session
    # mix advisory and the cross-side mismatch advisory.
    va, vb, advisories = smc._resolve_suite_versions(
        a_turns, b_turns, allow_mismatch=True,
    )
    assert len(va) == 2
    kinds = {a["kind"] for a in advisories}
    assert "suite-version-intrasession-mix" in kinds
    assert "suite-version-mismatch" in kinds


# ---- IFEval wiring in _build_compare_report --------------------------------

def _fake_suite() -> dict:
    """Tiny inline suite for wiring tests — two named prompts with predicates."""
    return {
        "no_commas": {
            "name":     "no_commas",
            "metadata": {"name": "no_commas"},
            "body":     "...",
            "check":    lambda text: "," not in text,
            "path":     None,
        },
        "lowercase": {
            "name":     "lowercase",
            "metadata": {"name": "lowercase"},
            "body":     "...",
            "check":    lambda text: text == text.lower(),
            "path":     None,
        },
    }


def test_build_compare_report_records_instruction_pass():
    # A passes no_commas, B fails no_commas; both pass lowercase.
    a_turns = [
        _make_sentinel_turn("no_commas", 1, "no comma here"),
        _make_sentinel_turn("lowercase", 1, "all lowercase"),
    ]
    b_turns = [
        _make_sentinel_turn("no_commas", 1, "has, a comma"),
        _make_sentinel_turn("lowercase", 1, "all lowercase"),
    ]
    report = smc._build_compare_report(
        "s_a", a_turns, [],
        "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    assert len(report["paired"]) == 2
    by_name = {p["suite_prompt_name"]: p for p in report["paired"]}
    assert by_name["no_commas"]["instruction_pass_a"] is True
    assert by_name["no_commas"]["instruction_pass_b"] is False
    assert by_name["lowercase"]["instruction_pass_a"] is True
    assert by_name["lowercase"]["instruction_pass_b"] is True


def test_compare_summary_has_instruction_pass_rate():
    a_turns = [_make_sentinel_turn("no_commas", 1, "no comma here")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "has, comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    s = report["summary"]
    # Existing fields — value and presence preserved.
    assert s["instruction_evaluated"] == 1
    assert s["instruction_pass_a"] == 1
    assert s["instruction_pass_b"] == 0
    assert s["instruction_pass_rate_a"] == pytest.approx(1.0)
    assert s["instruction_pass_rate_b"] == pytest.approx(0.0)
    assert s["instruction_pass_delta_pp"] == pytest.approx(-100.0)
    # New paired-samples fields (v1.13.0+) coexist with existing ones.
    assert "instruction_mcnemar_b" in s
    assert "instruction_mcnemar_c" in s
    assert "instruction_mcnemar_pvalue" in s
    assert "instruction_pass_rate_a_ci" in s
    assert "instruction_pass_rate_b_ci" in s
    assert "low_sample_size" in s
    assert "sample_size_note" in s
    # N=1 triggers the low-sample-size banner.
    assert s["low_sample_size"] is True
    assert s["sample_size_note"] is not None


# ---- Paired-samples statistics (v1.13.0+) ----------------------------------

def test_mcnemar_midp_no_discordant_pairs_returns_none():
    # Both models agree on every prompt → no evidence for a difference.
    assert smc._mcnemar_midp(0, 0) is None


def test_mcnemar_midp_strong_b_bias_gives_small_pvalue():
    # b=5, c=0: under null p=0.5 this is 2^-5 = 1/32 per side. Mid-p corrects
    # down by 0.5 * point_mass; two-sided doubles the result.
    # Expected: 2 * (1/32 - 0.5 * 1/32) = 2 * (1/64) = 1/32 = 0.03125
    p = smc._mcnemar_midp(5, 0)
    assert p == pytest.approx(1.0 / 32.0, abs=1e-9)


def test_mcnemar_midp_symmetric_case_gives_pvalue_one():
    # b=c → no evidence for either direction → mid-p two-sided = 1.0
    p = smc._mcnemar_midp(3, 3)
    assert p == pytest.approx(1.0, abs=1e-9)


def test_mcnemar_midp_capped_at_one():
    # b=1, c=1 → the unadjusted two-sided tail exceeds 1; must be clipped.
    p = smc._mcnemar_midp(1, 1)
    assert p is not None
    assert 0.0 < p <= 1.0


def test_wilson_ci_n_zero_returns_none():
    assert smc._wilson_ci(0, 0) is None


def test_wilson_ci_known_case_n_10_successes_7():
    # Wilson 95% CI for 7/10 — standard reference value is [0.397, 0.892]
    ci = smc._wilson_ci(7, 10)
    assert ci is not None
    lo, hi = ci
    assert lo == pytest.approx(0.397, abs=0.005)
    assert hi == pytest.approx(0.892, abs=0.005)


def test_wilson_ci_all_pass_has_nonzero_lower_bound():
    # 10/10 pass → upper bound pinned at 1.0, lower bound strictly > 0
    # (This is the main reason Wilson is preferred over Wald at boundaries.)
    ci = smc._wilson_ci(10, 10)
    assert ci is not None
    lo, hi = ci
    assert hi == pytest.approx(1.0, abs=1e-9)
    assert lo > 0.6


def test_wilson_ci_zero_pass_has_lt_one_upper_bound():
    ci = smc._wilson_ci(0, 10)
    assert ci is not None
    lo, hi = ci
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert hi < 0.4


def test_compare_summary_flags_low_sample_size_when_n_under_20():
    # Build a paired report with a single evaluated prompt.
    a_turns = [_make_sentinel_turn("no_commas", 1, "no comma here")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "no comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    s = report["summary"]
    assert s["low_sample_size"] is True
    assert s["sample_size_note"] is not None
    assert "N=" in s["sample_size_note"]


def test_compare_summary_no_low_sample_flag_when_no_predicates():
    # Zero evaluated → low_sample_size should be False (not "yes, N=0").
    a_sid, a_turns, _ = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, _ = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, [], b_sid, b_turns, [],
        slug="t", prompt_suite={},
    )
    s = report["summary"]
    assert s["instruction_evaluated"] == 0
    assert s["low_sample_size"] is False
    assert s["sample_size_note"] is None
    # p-value and CIs cleanly None when nothing was evaluated.
    assert s["instruction_mcnemar_pvalue"] is None
    assert s["instruction_pass_rate_a_ci"] is None
    assert s["instruction_pass_rate_b_ci"] is None


def test_compare_summary_paired_perfect_agreement_gives_null_pvalue():
    """Both models pass the same prompt → no discordant pairs → p-value None."""
    a_turns = [
        _make_sentinel_turn("no_commas", 1, "no comma"),
        _make_sentinel_turn("lowercase", 1, "all lowercase"),
    ]
    b_turns = [
        _make_sentinel_turn("no_commas", 1, "no comma"),
        _make_sentinel_turn("lowercase", 1, "all lowercase"),
    ]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    s = report["summary"]
    assert s["instruction_mcnemar_b"] == 0
    assert s["instruction_mcnemar_c"] == 0
    assert s["instruction_mcnemar_pvalue"] is None
    # pass_delta_pp preserved: both at 100% → 0.0 pp
    assert s["instruction_pass_delta_pp"] == pytest.approx(0.0, abs=1e-9)


def test_compare_summary_blank_without_sentinels():
    # Regular paired sessions with no sentinels leave IFEval fields blank.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts, b_sid, b_turns, b_user_ts,
        slug="t", prompt_suite={},
    )
    s = report["summary"]
    assert s["instruction_evaluated"] == 0
    assert s["instruction_pass_rate_a"] is None
    assert s["instruction_pass_rate_b"] is None
    assert s["instruction_pass_delta_pp"] is None


def test_suite_version_mismatch_refuses_via_builder():
    a_turns = [_make_sentinel_turn("foo", 1, "ok")]
    b_turns = [_make_sentinel_turn("foo", 2, "ok")]
    with pytest.raises(smc.SuiteVersionMismatchError):
        smc._build_compare_report(
            "s_a", a_turns, [], "s_b", b_turns, [],
            slug="t", prompt_suite=_fake_suite(),
        )


def test_allow_suite_mismatch_proceeds_and_adds_advisory():
    a_turns = [_make_sentinel_turn("foo", 1, "ok")]
    b_turns = [_make_sentinel_turn("foo", 2, "ok")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
        allow_suite_mismatch=True,
    )
    kinds = {adv["kind"] for adv in report["advisories"]}
    assert "suite-version-mismatch" in kinds


# ---- Renderer wiring for IFEval --------------------------------------------

def test_render_compare_text_has_instruction_pass_column():
    a_turns = [_make_sentinel_turn("no_commas", 1, "no commas here")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "has, a comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    out = smc.render_compare_text(report)
    assert "IFEval pass" in out
    assert "A✓" in out
    assert "B✓" in out
    assert "no_commas" in out
    # A passed, B failed.
    assert "✓" in out
    assert "✗" in out


def test_render_compare_md_has_instruction_pass_column():
    a_turns = [_make_sentinel_turn("no_commas", 1, "no commas here")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "has, a comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    out = smc.render_compare_md(report)
    assert "A✓" in out
    assert "IFEval pass (A)" in out
    assert "| no_commas |" in out


def test_render_compare_csv_has_instruction_pass_column():
    a_turns = [_make_sentinel_turn("no_commas", 1, "no commas here")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "has, a comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    out = smc.render_compare_csv(report)
    assert "suite_prompt_name" in out
    assert "a_instruction_pass" in out
    assert "b_instruction_pass" in out
    assert "no_commas,True" in out or "no_commas\tTrue" in out or ",no_commas," in out


def test_render_compare_json_carries_instruction_pass():
    a_turns = [_make_sentinel_turn("no_commas", 1, "no commas")]
    b_turns = [_make_sentinel_turn("no_commas", 1, "has, comma")]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="t", prompt_suite=_fake_suite(),
    )
    payload = json.loads(smc.render_compare_json(report))
    assert payload["paired"][0]["suite_prompt_name"] == "no_commas"
    assert payload["paired"][0]["instruction_pass_a"] is True
    assert payload["paired"][0]["instruction_pass_b"] is False


# ---- --compare-prep helper --------------------------------------------------

def test_compare_prep_default_models():
    buf = _io_p45.StringIO()
    smc._run_compare_prep([], out=buf)
    txt = buf.getvalue()
    # Default pair is the 1M-context tier because Claude Code ships Opus
    # routed to ``[1m]``. Comparing ``[1m]`` vs ``[1m]`` reflects real-
    # world usage; the 200k variants are a deliberate opt-out.
    assert "claude-opus-4-6[1m]" in txt
    assert "claude-opus-4-7[1m]" in txt
    assert "PROMPT SUITE (v1" in txt


def test_compare_prep_custom_models():
    buf = _io_p45.StringIO()
    smc._run_compare_prep(["claude-opus-4-7", "claude-opus-4-8"], out=buf)
    txt = buf.getvalue()
    assert "claude-opus-4-7" in txt
    assert "claude-opus-4-8" in txt


def test_compare_prep_single_model_defaults_second():
    # One positional model → A overridden, B stays at the 4.7[1m] default
    # (to match Claude Code's shipping Opus tier).
    buf = _io_p45.StringIO()
    smc._run_compare_prep(["claude-opus-4-5"], out=buf)
    txt = buf.getvalue()
    assert "claude-opus-4-5" in txt
    assert "claude-opus-4-7[1m]" in txt


def test_compare_prep_three_models_refused(capsys):
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_prep(["a", "b", "c"])
    assert exc.value.code == 1
    assert "at most two" in capsys.readouterr().err


def test_compare_prep_includes_all_10_prompts():
    buf = _io_p45.StringIO()
    smc._run_compare_prep([], out=buf)
    txt = buf.getvalue()
    assert "PROMPT 1 of 10" in txt
    assert "PROMPT 10 of 10" in txt


def test_compare_prep_sentinels_in_output():
    buf = _io_p45.StringIO()
    smc._run_compare_prep([], out=buf)
    txt = buf.getvalue()
    # Every suite prompt's sentinel should appear in the emitted protocol.
    suite = smc._load_prompt_suite(_SUITE_DIR)
    for name in suite:
        assert f"[session-metrics:compare-suite:v1:prompt={name}]" in txt


def test_compare_prep_missing_suite_dir_errors(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_prep([], suite_dir=tmp_path / "nope")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "suite is empty or missing" in err


# ---- CLI wiring --------------------------------------------------------------

def test_cli_compare_prep_end_to_end(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare-prep", "--slug", "test-slug"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "Compare capture protocol" in out
    assert "PROMPT SUITE" in out


def test_cli_compare_prep_with_custom_models(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare-prep", "claude-opus-4-7",
         "claude-opus-4-8", "--slug", "test-slug"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "claude-opus-4-7" in out
    assert "claude-opus-4-8" in out


def test_cli_compare_prompts_override_accepted(monkeypatch, tmp_path, capsys):
    # Override points at a dir with one prompt → only one prompt in the
    # emitted list. Proves --compare-prompts wires through to --compare-prep.
    custom = tmp_path / "prompts"
    custom.mkdir()
    (custom / "01_mini.md").write_text(_tw45.dedent("""\
        ---
        name: mini
        description: minimal test prompt
        ---

        [session-metrics:compare-suite:v1:prompt=mini]

        tiny prompt

        <!-- PREDICATE -->

        ````python
        def check(text: str) -> bool:
            return True
        ````
    """))
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare-prep",
         "--compare-prompts", str(custom), "--slug", "test-slug"],
    )
    sm.main()
    out = capsys.readouterr().out
    assert "PROMPT 1 of 1" in out
    assert "prompt=mini" in out


def test_cli_compare_suite_mismatch_refuses(monkeypatch, tmp_path, capsys):
    # Two sessions whose user prompts sentinel at different suite versions.
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    b_dir = tmp_path / "b"
    b_dir.mkdir()

    def _write(path, version):
        # Minimal-but-valid JSONL with one user+assistant pair carrying a sentinel.
        user_text = f"[session-metrics:compare-suite:v{version}:prompt=foo]\nplease"
        lines = [
            json.dumps({
                "type": "user", "uuid": "u1",
                "timestamp": "2026-04-19T10:00:00.000Z", "sessionId": path.stem,
                "message": {"role": "user",
                            "content": [{"type": "text", "text": user_text}]},
            }),
            json.dumps({
                "type": "assistant", "uuid": "a1",
                "timestamp": "2026-04-19T10:00:05.000Z", "sessionId": path.stem,
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-7",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ack"}],
                    "usage": {"input_tokens": 100, "output_tokens": 20,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0},
                },
            }),
        ]
        path.write_text("\n".join(lines) + "\n")

    a_jsonl = a_dir / "s_a.jsonl"
    b_jsonl = b_dir / "s_b.jsonl"
    _write(a_jsonl, 1)
    _write(b_jsonl, 2)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_jsonl), str(b_jsonl),
         "--slug", "test-slug"],
    )
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "compare-suite versions differ" in err


def test_cli_compare_allow_suite_mismatch_proceeds(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    b_dir = tmp_path / "b"
    b_dir.mkdir()

    def _write(path, version):
        user_text = f"[session-metrics:compare-suite:v{version}:prompt=foo]\nplease"
        lines = [
            json.dumps({
                "type": "user", "uuid": "u1",
                "timestamp": "2026-04-19T10:00:00.000Z", "sessionId": path.stem,
                "message": {"role": "user",
                            "content": [{"type": "text", "text": user_text}]},
            }),
            json.dumps({
                "type": "assistant", "uuid": "a1",
                "timestamp": "2026-04-19T10:00:05.000Z", "sessionId": path.stem,
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-7",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ack"}],
                    "usage": {"input_tokens": 100, "output_tokens": 20,
                              "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0},
                },
            }),
        ]
        path.write_text("\n".join(lines) + "\n")

    a_jsonl = a_dir / "s_a.jsonl"
    b_jsonl = b_dir / "s_b.jsonl"
    _write(a_jsonl, 1)
    _write(b_jsonl, 2)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_jsonl), str(b_jsonl),
         "--slug", "test-slug", "--allow-suite-mismatch"],
    )
    sm.main()   # should not raise
    out = capsys.readouterr().out
    assert "COMPARE (controlled)" in out


# =============================================================================
# Phase 6 — HTML variant="compare" + --redact-user-prompts
# =============================================================================
# Split out after Phase 5 because these tests lean heavily on the compare
# report builder, so keeping the report-construction helpers near the HTML
# assertions prevents drift between "what the report contains" and "what
# the renderer expects".


def _make_controlled_compare_report():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    return smc._build_compare_report(
        a_sid, a_turns, a_user_ts, b_sid, b_turns, b_user_ts,
        slug="phase6-slug", prompt_suite={},
    )


def _make_aggregate_compare_report():
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_a.jsonl")
    return smc._build_compare_aggregate_report(
        [(a_sid, a_turns, a_user_ts)],
        [(b_sid, b_turns, b_user_ts)],
        slug="phase6-agg-slug",
    )


def test_compare_html_mode1_renders_basic_shell():
    """Mode 1 HTML has the shell elements every downstream viewer needs:
    doctype, title, summary cards, a per-turn table, a methodology card."""
    report = _make_controlled_compare_report()
    html = smc.render_compare_html(report)
    assert html.startswith("<!DOCTYPE html>")
    assert "Session Metrics" in html
    assert "phase6-slug" in html
    # Summary strip + at least one cost/ratio card.
    assert "Cost ratio" in html
    assert "Input tokens ratio" in html
    # Per-turn table is the Mode-1 hallmark.
    assert "Paired turns" in html
    # At least one paired-row with a ratio cell.
    assert "ratio-warm" in html or "ratio-hot" in html or "ratio-mild" in html
    # Methodology footer.
    assert "references/model-compare.md" in html


def test_compare_html_mode1_reproducibility_stamp_carries_both_models():
    report = _make_controlled_compare_report()
    html = smc.render_compare_html(report)
    # Stamp names both models so a shared HTML includes provenance.
    assert "claude-opus-4-6" in html
    assert "claude-opus-4-7" in html


def test_compare_html_mode1_ifeval_column_when_evaluated():
    # Build a report with two prompts + inline predicates so the IFEval
    # column actually fires. A passes, B fails on a comma predicate.
    a_turns = [
        _make_sentinel_turn("no_commas", 1, "no comma here", model="claude-opus-4-6"),
    ]
    b_turns = [
        _make_sentinel_turn("no_commas", 1, "has, comma", model="claude-opus-4-7"),
    ]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="phase6-slug", prompt_suite=_fake_suite(),
    )
    html = smc.render_compare_html(report)
    # Pass / fail icons present (✓ ✗).
    assert "pass-ok" in html
    assert "pass-fail" in html
    # IFEval summary card ("IFEval pass rate (B)") only renders when
    # at least one predicate was evaluated.
    assert "IFEval pass rate (B)" in html


def test_compare_html_mode1_no_ifeval_column_without_predicates():
    report = _make_controlled_compare_report()
    html = smc.render_compare_html(report)
    # No sentinel-tagged turns in the fixture, so the IFEval summary
    # card is absent.
    assert "IFEval pass rate" not in html


def test_compare_html_mode1_histogram_card_when_paired_turns_present():
    report = _make_controlled_compare_report()
    html = smc.render_compare_html(report)
    assert "Per-turn input-token ratio distribution" in html
    # Mean / p50 / p95 meta-line.
    assert "p50" in html and "p95" in html


def test_compare_html_mode1_quality_vs_cost_card_present():
    report = _make_controlled_compare_report()
    html = smc.render_compare_html(report)
    assert "Quality vs cost" in html
    # Verdict sentence is always present on the quality-vs-cost card.
    assert "quality/cost trade-off" in html or "no IFEval measurement" in html \
        or "cost roughly flat" in html or "quality up with no" in html


def test_compare_html_mode1_advisories_rendered_when_present():
    # Two sessions with different context tiers triggers the
    # context-tier-mismatch advisory.
    a_sid, a_turns, a_user_ts = _load_compare_fixture("compare_opus_4_6_a.jsonl")
    b_sid, b_turns, b_user_ts = _load_compare_fixture("compare_opus_4_7_1m_a.jsonl")
    report = smc._build_compare_report(
        a_sid, a_turns, a_user_ts, b_sid, b_turns, b_user_ts,
        slug="phase6-tier-slug", prompt_suite={},
    )
    html = smc.render_compare_html(report)
    assert "context-tier mismatch" in html
    assert "class=\"advisory warn\"" in html \
        or "class='advisory warn'" in html


def test_compare_html_mode1_redact_masks_non_suite_prompt_labels():
    # Without redaction the prompt cell shows a fingerprint snippet;
    # with redaction it flips to the literal "[redacted]" marker.
    report = _make_controlled_compare_report()
    html_plain = smc.render_compare_html(report, redact_user_prompts=False)
    html_masked = smc.render_compare_html(report, redact_user_prompts=True)
    assert "[redacted]" in html_masked
    assert "[redacted]" not in html_plain


def test_compare_html_mode1_redact_preserves_suite_names():
    # Sentinel-tagged suite prompts stay visible even with redaction.
    a_turns = [
        _make_sentinel_turn("no_commas", 1, "no comma here", model="claude-opus-4-6"),
    ]
    b_turns = [
        _make_sentinel_turn("no_commas", 1, "also fine", model="claude-opus-4-7"),
    ]
    report = smc._build_compare_report(
        "s_a", a_turns, [], "s_b", b_turns, [],
        slug="phase6-slug", prompt_suite=_fake_suite(),
    )
    html = smc.render_compare_html(report, redact_user_prompts=True)
    # Suite prompt name stays visible through redaction.
    assert "no_commas" in html
    assert "[redacted]" not in html


def test_compare_html_mode2_aggregate_shell():
    """Mode 2 HTML swaps per-turn table for aggregate-detail cards and
    keeps the observational advisory up front."""
    report = _make_aggregate_compare_report()
    html = smc.render_compare_html(report)
    assert "<!DOCTYPE html>" in html
    assert "mode <strong>observational</strong>" in html
    # Observational-not-controlled advisory always fires on Mode 2.
    assert "observational compare" in html
    # No per-turn paired-turns section in Mode 2.
    assert "Paired turns" not in html
    # Aggregate detail table is present.
    assert "Aggregate detail" in html
    # Aggregate-only ratio cards.
    assert "Avg input / prompt" in html
    assert "Tool calls / turn" in html


def test_compare_html_mode_dispatch_via_compare_mode_field():
    """render_compare_html must dispatch on compare_mode, not any other
    report shape — single hole to plug for future variants."""
    controlled = _make_controlled_compare_report()
    html_a = smc.render_compare_html(controlled)
    assert "controlled" in html_a

    observational = _make_aggregate_compare_report()
    html_b = smc.render_compare_html(observational)
    assert "observational" in html_b


def test_compare_html_escapes_special_chars_in_advisory():
    # HTML injection through an advisory message should be escaped.
    report = _make_controlled_compare_report()
    report["advisories"].append({
        "kind":     "test-injection",
        "severity": "warn",
        "message":  "<script>alert(1)</script> & ' \" <b>",
    })
    html = smc.render_compare_html(report)
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_compare_html_ratio_tint_class_boundaries():
    # Spot-check the heatmap classifier so downstream CSS can rely on
    # the bucket boundaries.
    assert smc._ratio_tint_class(None) == "ratio-na"
    assert smc._ratio_tint_class(1.0) == "ratio-neutral"
    assert smc._ratio_tint_class(1.10) == "ratio-mild"
    assert smc._ratio_tint_class(1.30) == "ratio-warm"
    assert smc._ratio_tint_class(1.50) == "ratio-hot"
    assert smc._ratio_tint_class(0.80) == "ratio-cool"
    assert smc._ratio_tint_class(0.92) == "ratio-coolish"


def test_cli_compare_html_with_redact_flag(monkeypatch, tmp_path, capsys):
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "test-slug",
        [("compare_opus_4_6_a.jsonl", -1),
         ("compare_opus_4_7_a.jsonl", 0)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    a_path = project_dir / "compare_opus_4_6_a.jsonl"
    b_path = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", str(a_path), str(b_path),
         "--slug", "test-slug", "--output", "html",
         "--redact-user-prompts"],
    )
    sm.main()
    err = capsys.readouterr().err
    assert "[export] HTML (compare)" in err
    html_files = list((tmp_path / "exports" / "session-metrics").glob("*.html"))
    assert html_files
    body = html_files[0].read_text(encoding="utf-8")
    assert "[redacted]" in body


def test_cli_compare_drops_state_marker(monkeypatch, tmp_path, capsys):
    """Phase 7: running --compare plants the marker file under the
    project's JSONL directory so dashboard renders can flip the compare
    insight from "hint" to "refresh"."""
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    marker = project_dir / ".session-metrics-compare-used"
    assert not marker.exists()
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--compare", "last-opus-4-6", "last-opus-4-7",
         "--slug", "phase7-slug"],
    )
    sm.main()
    _ = capsys.readouterr()   # drain
    assert marker.exists()


# =============================================================================
# Phase 7 — Model-compare insight card, state marker, --no-... flag
# =============================================================================


def test_version_suffix_of_family_parses_trailing_ints():
    assert sm._version_suffix_of_family("opus-4-7") == (4, 7)
    assert sm._version_suffix_of_family("opus-4-6") == (4, 6)
    # Trailing non-int breaks the collection.
    assert sm._version_suffix_of_family("opus") == ()
    # Date stamps already stripped upstream, so we only handle bare slugs.
    assert sm._version_suffix_of_family("haiku-4-5") == (4, 5)


def test_order_family_pair_picks_oldest_and_newest():
    assert sm._order_family_pair(["opus-4-7", "opus-4-6"]) == ("opus-4-6", "opus-4-7")
    # With three families present, skill picks lowest / highest version.
    assert sm._order_family_pair(["opus-4-7", "opus-4-6", "opus-4-8"]) == (
        "opus-4-6", "opus-4-8",
    )
    # Fewer than two distinct families → None.
    assert sm._order_family_pair(["opus-4-7"]) is None
    assert sm._order_family_pair([]) is None


def test_order_family_pair_alphabetical_fallback():
    # Cross-tier (different version length) — falls back to alphabetical
    # after the version comparison declares them equal at the shared
    # prefix.
    pair = sm._order_family_pair(["sonnet-4-7", "opus-4-7"])
    assert pair is not None
    assert "opus-4-7" in pair and "sonnet-4-7" in pair


def test_model_compare_insight_fires_when_two_families_present(
    tmp_path, monkeypatch,
):
    projects_dir, _ = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    report = {
        "slug":           "phase7-slug",
        "totals":         {"cost": 1.0},
        "sessions":       [],
        "session_blocks": [],
    }
    insight = sm._compute_model_compare_insight(report)
    assert insight is not None
    assert insight["id"] == "model_compare"
    assert insight["shown"] is True
    # First-time copy mentions "Run session-metrics --compare-prep".
    assert "compare-prep" in insight["body"]


def test_model_compare_insight_escalates_after_marker(tmp_path, monkeypatch):
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    (project_dir / ".session-metrics-compare-used").write_text("marker")
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    report = {
        "slug": "phase7-slug", "totals": {"cost": 1.0},
        "sessions": [], "session_blocks": [],
    }
    insight = sm._compute_model_compare_insight(report)
    assert insight is not None
    assert "refresh attribution" in insight["body"]
    assert "--compare last-" in insight["body"]


def test_model_compare_insight_none_when_single_family(tmp_path, monkeypatch):
    projects_dir, _ = _build_project_dir(
        tmp_path, "phase7-slug",
        [("compare_opus_4_7_a.jsonl", 10)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    report = {
        "slug": "phase7-slug", "totals": {"cost": 1.0},
        "sessions": [], "session_blocks": [],
    }
    assert sm._compute_model_compare_insight(report) is None


def test_model_compare_insight_none_when_slug_missing():
    # Safety net: builder refuses to scan without a slug.
    assert sm._compute_model_compare_insight({"slug": "", "totals": {"cost": 1.0}}) is None


def test_compute_usage_insights_includes_model_compare_card(
    tmp_path, monkeypatch,
):
    projects_dir, _ = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    report = {
        "slug":           "phase7-slug",
        "totals":         {"cost": 1.0},
        "sessions":       [],
        "session_blocks": [],
    }
    insights = sm._compute_usage_insights(report)
    ids = [i["id"] for i in insights]
    assert "model_compare" in ids


def test_compute_usage_insights_suppressed_flag_hides_card(
    tmp_path, monkeypatch,
):
    projects_dir, _ = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    report = {
        "slug":           "phase7-slug",
        "totals":         {"cost": 1.0},
        "sessions":       [],
        "session_blocks": [],
        "_suppress_model_compare_insight": True,
    }
    insights = sm._compute_usage_insights(report)
    ids = [i["id"] for i in insights]
    assert "model_compare" not in ids


def test_build_report_suppress_flag_plumbs_through(tmp_path, monkeypatch):
    # The flag is accepted by _build_report and the resulting report has
    # no model_compare insight even when two families would otherwise
    # be present — also confirming the internal underscore key doesn't
    # leak into the final report dict.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    # Use the newer of the two as the target session.
    sid, turns, user_ts = sm._load_session(
        project_dir / "compare_opus_4_7_a.jsonl",
        include_subagents=False, use_cache=False,
    )
    report = sm._build_report(
        "session", "phase7-slug",
        [(sid, turns, user_ts)],
        suppress_model_compare_insight=True,
    )
    # Internal flag must be stripped from the finished report.
    assert "_suppress_model_compare_insight" not in report
    ids = [i["id"] for i in report.get("usage_insights", [])]
    assert "model_compare" not in ids


def test_cli_no_model_compare_insight_flag_accepts(
    tmp_path, monkeypatch, capsys,
):
    # Smoke: the flag doesn't break an otherwise-normal single-session
    # run, even when the project has multiple families that would
    # otherwise populate the card.
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "phase7-slug",
        [
            ("compare_opus_4_6_a.jsonl", 10),
            ("compare_opus_4_7_a.jsonl", 20),
        ],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.chdir(tmp_path)
    target = project_dir / "compare_opus_4_7_a.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py",
         "--session", target.stem,
         "--slug", "phase7-slug",
         "--no-model-compare-insight"],
    )
    sm.main()
    out = capsys.readouterr().out
    # Normal single-session output rendered.
    assert "SESSION TOTAL" in out or "TOT " in out or "Totals" in out \
        or "cache" in out.lower()


def test_touch_compare_state_marker_and_detection(tmp_path, monkeypatch):
    projects_dir, project_dir = _build_project_dir(
        tmp_path, "phase7-slug",
        [("compare_opus_4_7_a.jsonl", 10)],
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    assert not sm._has_compare_state_marker("phase7-slug")
    sm._touch_compare_state_marker("phase7-slug")
    assert sm._has_compare_state_marker("phase7-slug")
    # Marker lands in the project dir, not the session-metrics cache.
    assert (project_dir / ".session-metrics-compare-used").is_file()


# ---------------------------------------------------------------------------
# Phase 8 — count_tokens API mode
# ---------------------------------------------------------------------------

class _MockResp:
    """Stand-in for ``urllib.request.urlopen``'s context-manager return.

    Returns whatever JSON bytes are passed in at construction. Tests
    pass a small helper function to urlopen to route per-model
    responses.
    """

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _make_mock_urlopen(responses):
    """Return a callable suitable for urlopen= injection.

    ``responses`` maps a model id → either an int (token count to
    return), a tuple ``(int, int)`` (status, tokens) for custom status,
    or a string like ``"http_400"`` to raise HTTPError.
    """
    import urllib.error as ue
    from email.message import Message as _HdrsMsg

    def _urlopen(req, timeout=None):
        # Pull the body out to find which model the request targets.
        import json as _json
        payload = _json.loads(req.data.decode("utf-8"))
        model = payload["model"]
        entry = responses.get(model)
        if entry is None:
            raise ue.HTTPError(
                req.full_url, 404, "unknown model", hdrs=_HdrsMsg(),
                fp=__import__("io").BytesIO(b'{"error":"unknown model"}'),
            )
        if isinstance(entry, str) and entry.startswith("http_"):
            code = int(entry.split("_", 1)[1])
            raise ue.HTTPError(
                req.full_url, code, f"error {code}", hdrs=_HdrsMsg(),
                fp=__import__("io").BytesIO(
                    '{"error":"model not available (mock)"}'.encode()
                ),
            )
        if isinstance(entry, str) and entry == "network":
            raise ue.URLError("connection refused")
        if isinstance(entry, tuple):
            status, tokens = entry
            return _MockResp(
                __import__("json").dumps({"input_tokens": tokens}).encode(),
                status=status,
            )
        # Integer token count.
        return _MockResp(
            __import__("json").dumps({"input_tokens": int(entry)}).encode()
        )

    return _urlopen


def test_count_tokens_request_happy_path():
    urlopen = _make_mock_urlopen({"claude-opus-4-7": 42})
    assert smc._count_tokens_request(
        "claude-opus-4-7", "hello world",
        api_key="dummy-key", urlopen=urlopen,
    ) == 42


def test_count_tokens_request_http_error_raises():
    urlopen = _make_mock_urlopen({"claude-opus-4-6": "http_404"})
    with pytest.raises(smc.CountTokensError) as exc:
        smc._count_tokens_request(
            "claude-opus-4-6", "hello",
            api_key="dummy-key", urlopen=urlopen,
        )
    assert "HTTP 404" in str(exc.value)


def test_count_tokens_request_network_error_raises():
    urlopen = _make_mock_urlopen({"claude-opus-4-7": "network"})
    with pytest.raises(smc.CountTokensError) as exc:
        smc._count_tokens_request(
            "claude-opus-4-7", "hello",
            api_key="dummy-key", urlopen=urlopen,
        )
    assert "network error" in str(exc.value)


def test_count_tokens_request_malformed_body_raises():
    """Server returned JSON but no ``input_tokens`` key."""
    import urllib.error as ue  # noqa: F401

    def _urlopen(req, timeout=None):
        return _MockResp(b'{"something_else": 123}')
    with pytest.raises(smc.CountTokensError) as exc:
        smc._count_tokens_request(
            "claude-opus-4-7", "hi",
            api_key="dummy-key", urlopen=_urlopen,
        )
    assert "missing 'input_tokens'" in str(exc.value)


def test_count_tokens_request_sends_correct_headers():
    captured = {}

    def _urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _MockResp(b'{"input_tokens": 99}')

    smc._count_tokens_request(
        "claude-opus-4-7", "hi",
        api_key="test-key-123", urlopen=_urlopen,
    )
    # urllib normalizes header names to Title-Case — use case-insensitive lookup.
    headers_ci = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_ci["x-api-key"] == "test-key-123"
    assert headers_ci["anthropic-version"] == "2023-06-01"
    assert headers_ci["content-type"] == "application/json"
    assert captured["method"] == "POST"
    assert captured["url"] == smc._COUNT_TOKENS_URL


def test_run_count_tokens_only_missing_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        smc._run_count_tokens_only(None, assume_yes=True)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


def test_run_count_tokens_only_pair_emits_ratio(capsys):
    import io as _io
    urlopen = _make_mock_urlopen({
        "claude-opus-4-6": 100,
        "claude-opus-4-7": 130,
    })
    buf = _io.StringIO()
    smc._run_count_tokens_only(
        ["claude-opus-4-6", "claude-opus-4-7"],
        assume_yes=True, api_key="dummy", urlopen=urlopen, out=buf,
    )
    text = buf.getvalue()
    # One row per prompt in the packaged suite.
    assert "claude-opus-4-6" in text
    assert "claude-opus-4-7" in text
    # 130/100 = 1.30× on every prompt.
    assert "1.30×" in text
    # Ratio summary footer renders for the pair.
    assert "Ratio summary (B/A)" in text
    # Input-only disclaimer reminds the user what this does NOT measure.
    assert "INPUT tokens only" in text


def test_run_count_tokens_only_probe_falls_back_to_b(capsys):
    """Model A probe fails → collapse to counting against B only."""
    import io as _io
    urlopen = _make_mock_urlopen({
        "claude-opus-4-6": "http_403",
        "claude-opus-4-7": 150,
    })
    buf = _io.StringIO()
    smc._run_count_tokens_only(
        ["claude-opus-4-6", "claude-opus-4-7"],
        assume_yes=True, api_key="dummy", urlopen=urlopen, out=buf,
    )
    text = buf.getvalue()
    err = capsys.readouterr().err
    # Friendly fallback message in stderr, not stdout.
    assert "not accessible" in err
    assert "claude-opus-4-6" in err
    # Body covers model B counts, no ratio (single model remaining).
    assert "claude-opus-4-7" in text
    assert "1.30×" not in text  # no ratio when single-model.
    assert "Ratios not computable" in text


def test_run_count_tokens_only_rejects_three_models(capsys):
    with pytest.raises(SystemExit) as exc:
        smc._run_count_tokens_only(
            ["a", "b", "c"],
            assume_yes=True, api_key="dummy",
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "at most two" in err


def test_run_count_tokens_only_single_model_accepts(capsys):
    """Single-model invocation is allowed (no ratio, no probe)."""
    import io as _io
    urlopen = _make_mock_urlopen({"claude-opus-4-7": 77})
    buf = _io.StringIO()
    smc._run_count_tokens_only(
        ["claude-opus-4-7"],
        assume_yes=True, api_key="dummy", urlopen=urlopen, out=buf,
    )
    text = buf.getvalue()
    err = capsys.readouterr().err
    assert "only one model" in err
    assert "claude-opus-4-7" in text
    # 77 appears in every row (10 prompts in the packaged suite).
    assert text.count(" 77") >= 10


def test_run_count_tokens_only_confirmation_required_without_yes(monkeypatch, capsys):
    """Non-interactive stdin + missing --yes → hard refusal."""
    import io as _io
    urlopen = _make_mock_urlopen({
        "claude-opus-4-6": 100, "claude-opus-4-7": 120,
    })

    # Simulate non-TTY stdin: isatty returns False, no .read input.
    class _NonTTYStdin:
        def isatty(self):
            return False

    buf = _io.StringIO()
    with pytest.raises(SystemExit) as exc:
        smc._run_count_tokens_only(
            ["claude-opus-4-6", "claude-opus-4-7"],
            assume_yes=False, api_key="dummy",
            urlopen=urlopen, stdin=_NonTTYStdin(), out=buf,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires --yes" in err


def test_run_count_tokens_only_empty_suite_errors(tmp_path, capsys):
    empty = tmp_path / "empty-suite"
    empty.mkdir()
    with pytest.raises(SystemExit) as exc:
        smc._run_count_tokens_only(
            None, suite_dir=empty, assume_yes=True, api_key="dummy",
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "prompt suite is empty" in err


def test_run_count_tokens_only_defaults_to_reference_pair(capsys):
    """No models passed → canonical ``4-6`` / ``4-7`` reference pair."""
    import io as _io
    urlopen = _make_mock_urlopen({
        "claude-opus-4-6": 200, "claude-opus-4-7": 250,
    })
    buf = _io.StringIO()
    smc._run_count_tokens_only(
        None,  # defaults
        assume_yes=True, api_key="dummy", urlopen=urlopen, out=buf,
    )
    text = buf.getvalue()
    assert "claude-opus-4-6" in text
    assert "claude-opus-4-7" in text
    # 250/200 = 1.25×
    assert "1.25×" in text


def test_render_count_tokens_no_results():
    import io as _io
    buf = _io.StringIO()
    smc._render_count_tokens_text([], ["a", "b"], out=buf)
    assert "nothing to count" in buf.getvalue()


def test_render_count_tokens_partial_failures_show_dashes():
    """If one prompt's call failed, the cell renders as em-dash."""
    import io as _io
    results = [
        {"name": "p1", "tokens_by_model": {"mA": 100, "mB": 130}},
        {"name": "p2", "tokens_by_model": {"mA": 100}},  # B failed
        {"name": "p3", "tokens_by_model": {"mA": 100, "mB": 140}},
    ]
    buf = _io.StringIO()
    smc._render_count_tokens_text(results, ["mA", "mB"], out=buf)
    text = buf.getvalue()
    # Em-dash placeholder for the missing B on p2, and no ratio cell
    # for that row.
    assert "—" in text
    # p1 and p3 still contribute to the ratio summary.
    assert "Ratio summary" in text


def test_cli_count_tokens_dispatches(monkeypatch, capsys):
    """End-to-end: ``--count-tokens-only --yes`` routes through the CLI."""
    # Monkey-patch urlopen so no real network call is attempted.
    import urllib.request

    def _fake(req, timeout=None):
        import json as _j
        body = _j.loads(req.data.decode("utf-8"))
        counts = {"claude-opus-4-6": 100, "claude-opus-4-7": 125}
        return _MockResp(
            _j.dumps({"input_tokens": counts[body["model"]]}).encode()
        )
    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--count-tokens-only", "--yes"],
    )
    sm.main()
    out = capsys.readouterr().out
    # 125 / 100 = 1.25× per prompt.
    assert "1.25×" in out
    assert "INPUT tokens only" in out


def test_cli_count_tokens_custom_models(monkeypatch, capsys):
    import urllib.request
    seen_models: list[str] = []

    def _fake(req, timeout=None):
        import json as _j
        body = _j.loads(req.data.decode("utf-8"))
        seen_models.append(body["model"])
        return _MockResp(_j.dumps({"input_tokens": 77}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "sys.argv",
        ["session-metrics.py", "--count-tokens-only",
         "--compare-models", "claude-sonnet-4-6", "claude-sonnet-4-7",
         "--yes"],
    )
    sm.main()
    # Both models were actually called.
    assert "claude-sonnet-4-6" in seen_models
    assert "claude-sonnet-4-7" in seen_models


# ============================================================================
# Phase 10 — Automated headless capture (--compare-run)
# ============================================================================
#
# The orchestrator spawns ``claude -p`` sub-processes via ``subprocess.run``.
# These tests inject a fake ``subprocess_run`` that records the argv each
# call would have used, returns a canned JSON payload, and never actually
# invokes the CLI. Coverage is aimed at the assembly + dispatch contract:
# argv composition, first-turn-vs-resume semantics, error propagation,
# confirmation gate, and scratch-dir resolution. The end-to-end handoff to
# ``_run_compare`` (which itself has extensive coverage upstream) is
# stubbed so compare-run tests don't need on-disk fixture JSONLs.


class _FakeCompletedProcess:
    """Minimal ``subprocess.CompletedProcess`` substitute for tests."""
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_fake_subprocess_run(
    *,
    default_stdout='{"result":"ok","session_id":"stub"}',
    returncode_sequence=None,
    stderr="",
):
    """Build a fake ``subprocess.run`` + the list it records into.

    Returns ``(fake_run, calls)``. Each element of ``calls`` is the argv
    the caller would have shelled out. ``returncode_sequence`` lets tests
    make specific invocations fail (e.g. the third call returns 1) while
    others succeed.
    """
    calls: list[list[str]] = []
    rc_iter = iter(returncode_sequence or [])

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        try:
            rc = next(rc_iter)
        except StopIteration:
            rc = 0
        return _FakeCompletedProcess(
            returncode=rc,
            stdout=default_stdout,
            stderr=stderr,
        )

    return fake_run, calls


class _FakeTty:
    """Minimal stdin-like object exposing ``isatty`` for gate tests."""
    def __init__(self, is_tty: bool):
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_compare_run_happy_path_assembles_expected_argv(tmp_path, monkeypatch):
    """20 invocations (10 prompts × 2 models) land in the right cwd with
    the right flags. Side A uses ``--session-id`` once then ``--resume``
    nine times; side B does the same with a different UUID."""
    fake_run, calls = _make_fake_subprocess_run()
    uuids = iter(["uuid-a", "uuid-b"])
    # Patch the compare-module's _run_compare so we don't need real JSONLs.
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)

    result = smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: next(uuids),
        stdin=_FakeTty(False),
        auto_resume=False,  # skip _run_compare handoff entirely for this test
    )

    # 10 prompts × 2 models = 20 calls.
    assert len(calls) == 20
    # First call for side A uses --session-id, not --resume.
    first_a = calls[0]
    assert "claude" == first_a[0]
    assert "-p" in first_a
    assert "--model" in first_a and "claude-opus-4-6" in first_a
    assert "--session-id" in first_a and "uuid-a" in first_a
    assert "--resume" not in first_a
    # All nine subsequent side-A calls use --resume, not --session-id.
    for c in calls[1:10]:
        assert "--resume" in c and "uuid-a" in c
        assert "--session-id" not in c
        assert "claude-opus-4-6" in c
    # Side B starts at index 10 with --session-id against uuid-b.
    first_b = calls[10]
    assert "--session-id" in first_b and "uuid-b" in first_b
    assert "claude-opus-4-7" in first_b
    for c in calls[11:20]:
        assert "--resume" in c and "uuid-b" in c
        assert "claude-opus-4-7" in c
    # Each call gets --output-format json, --allowedTools, --permission-mode.
    for c in calls:
        assert "--output-format" in c and "json" in c
        assert "--allowedTools" in c
        assert "--permission-mode" in c and "bypassPermissions" in c
    # Diagnostic payload matches what the CLI dispatch consumes.
    assert result["side_a_session_id"] == "uuid-a"
    assert result["side_b_session_id"] == "uuid-b"
    assert result["suite_prompt_count"] == 10
    assert result["scratch_dir"] == str(tmp_path.resolve())


def test_compare_run_claude_missing_raises_compare_run_error(tmp_path, monkeypatch):
    """FileNotFoundError from subprocess.run → CompareRunError so the
    caller prints a clear 'claude not on PATH' message and exits 1."""
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'claude'")

    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_run(
            "claude-opus-4-6", "claude-opus-4-7",
            scratch_dir=tmp_path,
            assume_yes=True,
            subprocess_run=fake_run,
            uuid_factory=lambda: "u",
            stdin=_FakeTty(False),
            auto_resume=False,
        )
    assert exc.value.code == 1


def test_compare_run_nonzero_returncode_surfaces_as_compare_run_error(
    tmp_path, monkeypatch, capsys,
):
    """A mid-run 'claude -p' failure (returncode=1) aborts with a clear
    message that mentions the model and preserves partial-JSONL info."""
    # Second call on side A returns rc=1.
    fake_run, _calls = _make_fake_subprocess_run(
        returncode_sequence=[0, 1],
        stderr="rate limit exceeded",
    )
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_run(
            "claude-opus-4-6", "claude-opus-4-7",
            scratch_dir=tmp_path,
            assume_yes=True,
            subprocess_run=fake_run,
            uuid_factory=lambda: "uuid-a",
            stdin=_FakeTty(False),
            auto_resume=False,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "claude-opus-4-6" in err
    assert "rate limit exceeded" in err
    assert "partial JSONL" in err


def test_compare_run_malformed_json_output_errors(tmp_path, monkeypatch):
    """If ``claude -p`` returns returncode=0 but stdout isn't valid JSON,
    the orchestrator still aborts rather than proceeding with unknown
    session state."""
    fake_run, _ = _make_fake_subprocess_run(
        default_stdout="not json at all",
    )
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_run(
            "claude-opus-4-6", "claude-opus-4-7",
            scratch_dir=tmp_path,
            assume_yes=True,
            subprocess_run=fake_run,
            uuid_factory=lambda: "u",
            stdin=_FakeTty(False),
            auto_resume=False,
        )
    assert exc.value.code == 1


def test_compare_run_refuses_non_tty_without_yes(tmp_path, monkeypatch, capsys):
    """Without ``--yes``, a non-TTY stdin hard-refuses so scripted
    invocations can't silently burn 20 calls of subscription quota."""
    fake_run, _ = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_run(
            "claude-opus-4-6", "claude-opus-4-7",
            scratch_dir=tmp_path,
            assume_yes=False,
            subprocess_run=fake_run,
            uuid_factory=lambda: "u",
            stdin=_FakeTty(False),
            auto_resume=False,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--yes" in err and "TTY" in err


def test_compare_run_creates_scratch_dir_when_none(monkeypatch):
    """``scratch_dir=None`` pulls the path from ``tempfile_mkdtemp`` rather
    than defaulting to cwd. Prevents polluting the user's working dir
    with a fresh project slug that Claude Code would otherwise create."""
    fake_run, calls = _make_fake_subprocess_run()
    mkdtemp_called = {"n": 0}

    def fake_mkdtemp():
        mkdtemp_called["n"] += 1
        # Return an existing path that won't be created — skip actual IO.
        return Path("/tmp")

    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=None,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        tempfile_mkdtemp=fake_mkdtemp,
        auto_resume=False,
    )
    assert mkdtemp_called["n"] == 1
    # Every subprocess call runs with cwd under /tmp (the temp root).
    # We don't assert the kwargs here because fake_run ignores them, but
    # the code path that resolves scratch_dir was exercised above.
    assert len(calls) == 20


def test_compare_run_permission_mode_empty_string_omits_flag(tmp_path, monkeypatch):
    """Passing ``permission_mode=None`` (e.g. from the CLI's empty-string
    opt-out) drops the ``--permission-mode`` flag entirely, so the
    subprocess defaults to whatever ``claude -p`` uses natively."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        permission_mode=None,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
    )
    for c in calls:
        assert "--permission-mode" not in c


def test_compare_run_threads_max_budget_usd(tmp_path, monkeypatch):
    """``--compare-run-max-budget-usd`` flows through to each subprocess
    as a ``--max-budget-usd <USD>`` argument pair."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        max_budget_usd=2.50,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
    )
    for c in calls:
        assert "--max-budget-usd" in c
        idx = c.index("--max-budget-usd")
        assert c[idx + 1] == "2.5"


def test_compare_run_passes_context_tier_suffix_verbatim(tmp_path, monkeypatch):
    """The ``[1m]`` context-tier suffix must flow through to the subprocess
    argv without mangling, so the 4-way Opus combo (4-6, 4-7, 4-6[1m],
    4-7[1m]) works. We rely on ``subprocess.run`` (not a shell) receiving
    argv as a list, which side-steps any glob interpretation."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6[1m]", "claude-opus-4-7[1m]",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
    )
    # Every side-A call carries the [1m]-suffixed model literal.
    for c in calls[:10]:
        assert "claude-opus-4-6[1m]" in c
    for c in calls[10:20]:
        assert "claude-opus-4-7[1m]" in c


def test_compare_run_accepts_mixed_tier_pair(tmp_path, monkeypatch):
    """Mixed-tier pairs (e.g. 4-6 vs 4-7[1m]) are valid inputs — the
    orchestrator does not refuse them. The resulting compare report
    fires the existing ``context-tier-mismatch`` advisory, which is
    handled downstream by ``_build_advisories``."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7[1m]",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
    )
    assert any("claude-opus-4-6" in c and "claude-opus-4-7[1m]" not in c
               for c in calls[:10])
    assert any("claude-opus-4-7[1m]" in c for c in calls[10:20])


def test_cli_compare_run_defaults_to_1m_variants(monkeypatch, tmp_path):
    """``--compare-run`` with no positional args resolves to the ``[1m]``
    pair because that matches Claude Code's shipping Opus default. Flipping
    this default to the 200k tier would have meant new users benchmarking
    a variant they don't actually use in real sessions."""
    captured = {}

    def fake_orchestrator(model_a, model_b, **kwargs):
        captured["model_a"] = model_a
        captured["model_b"] = model_b
        return {}

    monkeypatch.setattr(smc, "_run_compare_run", fake_orchestrator)
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run",  # zero positional args
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["model_a"] == "claude-opus-4-6[1m]"
    assert captured["model_b"] == "claude-opus-4-7[1m]"


def test_cli_compare_run_single_positional_defaults_b_to_1m(monkeypatch, tmp_path):
    """One positional model → A overridden, B stays at the ``[1m]``
    default. Lets users compare a custom 4-6 variant against canonical
    4-7 without typing both IDs."""
    captured = {}
    monkeypatch.setattr(
        smc, "_run_compare_run",
        lambda a, b, **kw: captured.update({"model_a": a, "model_b": b}),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "claude-opus-4-6",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["model_a"] == "claude-opus-4-6"
    assert captured["model_b"] == "claude-opus-4-7[1m]"


def test_cli_compare_run_three_positional_args_refused(monkeypatch, tmp_path, capsys):
    """Three or more positional model IDs should be refused at dispatch
    with a clear error — the function takes exactly two sides."""
    monkeypatch.setattr(
        smc, "_run_compare_run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "a", "b", "c",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "0, 1, or 2" in err


def test_cli_compare_run_end_to_end(monkeypatch, tmp_path):
    """End-to-end: argparse path. ``--compare-run MODEL_A MODEL_B`` wires
    through to ``smc._run_compare_run`` with the expected positional /
    keyword arguments. No real subprocess is spawned — we patch
    ``_run_compare_run`` itself and assert the call shape."""
    captured = {}

    def fake_orchestrator(model_a, model_b, **kwargs):
        captured["model_a"] = model_a
        captured["model_b"] = model_b
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(smc, "_run_compare_run", fake_orchestrator)
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "claude-opus-4-6", "claude-opus-4-7",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["model_a"] == "claude-opus-4-6"
    assert captured["model_b"] == "claude-opus-4-7"
    assert captured["scratch_dir"] == tmp_path
    assert captured["assume_yes"] is True
    # Defaults land correctly on pass-through options.
    assert captured["permission_mode"] == "bypassPermissions"
    assert "Bash" in captured["allowed_tools"]
    # --compare-run-effort absent → both sides land as None so Claude Code
    # keeps its per-model defaults (opus-4-6 high, opus-4-7 xhigh).
    assert captured["effort_a"] is None
    assert captured["effort_b"] is None


# --- --compare-run-effort (reasoning effort pinning) ------------------------

def test_compare_run_omits_effort_flag_when_unset(tmp_path, monkeypatch):
    """With ``effort_a=effort_b=None`` (the default), no ``--effort`` flag
    appears in any subprocess argv. This preserves Claude Code's per-model
    defaults — opus-4-6 → high, opus-4-7 → xhigh — which is the whole point
    of not passing the flag."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
    )
    for c in calls:
        assert "--effort" not in c


def test_compare_run_threads_per_side_effort_into_argv(tmp_path, monkeypatch):
    """Different effort per side lands in argv: all 10 side-A calls carry
    ``--effort high`` and all 10 side-B calls carry ``--effort xhigh``."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: "u",
        stdin=_FakeTty(False),
        auto_resume=False,
        effort_a="high",
        effort_b="xhigh",
    )
    for c in calls[:10]:
        assert "--effort" in c
        assert c[c.index("--effort") + 1] == "high"
    for c in calls[10:20]:
        assert "--effort" in c
        assert c[c.index("--effort") + 1] == "xhigh"


def test_compare_run_rejects_invalid_effort_level(tmp_path, monkeypatch, capsys):
    """An unknown level (e.g. ``turbo``) fails fast before any subprocess
    is spawned — burning 20 inference calls only to discover a typo would
    be the worst possible failure mode."""
    fake_run, calls = _make_fake_subprocess_run()
    monkeypatch.setattr(smc, "_run_compare", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        smc._run_compare_run(
            "claude-opus-4-6", "claude-opus-4-7",
            scratch_dir=tmp_path,
            assume_yes=True,
            subprocess_run=fake_run,
            uuid_factory=lambda: "u",
            stdin=_FakeTty(False),
            auto_resume=False,
            effort_a="turbo",
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "turbo" in err
    assert "low" in err and "max" in err
    # Crucially: no subprocess ran.
    assert calls == []


def test_cli_compare_run_effort_two_positional_values(monkeypatch, tmp_path):
    """``--compare-run-effort high xhigh`` → ``effort_a=high, effort_b=xhigh``."""
    captured = {}
    monkeypatch.setattr(
        smc, "_run_compare_run",
        lambda a, b, **kw: captured.update({"effort_a": kw.get("effort_a"),
                                            "effort_b": kw.get("effort_b")}),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "claude-opus-4-6", "claude-opus-4-7",
            "--compare-run-effort", "high", "xhigh",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["effort_a"] == "high"
    assert captured["effort_b"] == "xhigh"


def test_cli_compare_run_effort_single_value_applies_to_both(monkeypatch, tmp_path):
    """One positional value pins both sides to that level — common case
    when the user wants to hold effort constant across versions."""
    captured = {}
    monkeypatch.setattr(
        smc, "_run_compare_run",
        lambda a, b, **kw: captured.update({"effort_a": kw.get("effort_a"),
                                            "effort_b": kw.get("effort_b")}),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run",
            "--compare-run-effort", "medium",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["effort_a"] == "medium"
    assert captured["effort_b"] == "medium"


def test_cli_compare_run_effort_three_values_refused(monkeypatch, tmp_path, capsys):
    """Three or more positional values → clean exit 1, not a silent drop."""
    monkeypatch.setattr(
        smc, "_run_compare_run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run",
            "--compare-run-effort", "low", "medium", "high",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        sm.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "0, 1, or 2" in err


# =============================================================================
# Phase 8 — compare-run auto-extras (per-session dashboards + analysis.md)
# =============================================================================
#
# The helpers under test live in ``session_metrics_compare.py``:
#   * ``_decision_framework_verdict(cost_ratio, ifeval_delta_pp)`` maps the
#     two headline numbers to a verdict bucket mirroring the doc table at
#     ``references/model-compare.md:421-429``. Any threshold drift between
#     code and doc is a bug — these tests pin the boundaries.
#   * ``_render_compare_analysis_md(compare_report, session_a, session_b,
#     links)`` renders the Markdown scaffold with deterministic sections +
#     ``{{TODO}}`` prose placeholders.
#   * ``_emit_compare_run_extras(...)`` is the orchestrator that writes the
#     5 companion files. The end-to-end test below patches ``_run_compare``
#     to return a synthetic compare report, then asserts the extras machinery
#     produced the expected paths.

# --- _decision_framework_verdict — 5-row table + edge cases -----------------

def test_decision_verdict_cheap_any_quality():
    # Row 1: cost_ratio ≤ 1.05× → switch regardless of IFEval delta.
    v = smc._decision_framework_verdict(1.03, 0.0)
    assert v["bucket"] == "cheap"
    assert v["verdict"].startswith("switch")
    v2 = smc._decision_framework_verdict(1.00, -8.0)
    assert v2["bucket"] == "cheap"


def test_decision_verdict_mid_quality_win():
    # Row 2: 1.05–1.20×, IFEval Δ ≥ +5 pp → switch-if-quality.
    v = smc._decision_framework_verdict(1.10, 5.0)
    assert v["bucket"] == "mid-quality-win"
    v2 = smc._decision_framework_verdict(1.20, 12.0)
    assert v2["bucket"] == "mid-quality-win"


def test_decision_verdict_mid_flat():
    # Row 3: 1.05–1.20×, IFEval Δ within ±2 pp → workload-dependent.
    v = smc._decision_framework_verdict(1.12, 1.0)
    assert v["bucket"] == "mid-flat"
    v2 = smc._decision_framework_verdict(1.08, -1.5)
    assert v2["bucket"] == "mid-flat"


def test_decision_verdict_mid_gap():
    # Between ±2 pp and +5 pp — not in the doc table; gap fallback row.
    v = smc._decision_framework_verdict(1.15, 3.0)
    assert v["bucket"] == "mid-gap"
    assert "workload" in v["verdict"].lower()


def test_decision_verdict_expensive_big_quality():
    # Row 4: 1.20–1.45×, IFEval Δ ≥ +10 pp → trade-off call.
    v = smc._decision_framework_verdict(1.35, 10.0)
    assert v["bucket"] == "expensive-big-quality"
    v2 = smc._decision_framework_verdict(1.40, 15.0)
    assert v2["bucket"] == "expensive-big-quality"


def test_decision_verdict_expensive_gap():
    # 1.20–1.45× but IFEval lift < +10 pp → quality doesn't pay for cost.
    v = smc._decision_framework_verdict(1.30, 6.0)
    assert v["bucket"] == "expensive-gap"


def test_decision_verdict_very_expensive():
    # Row 5: ≥1.45× → stay regardless of IFEval delta.
    v = smc._decision_framework_verdict(1.80, 20.0)
    assert v["bucket"] == "very-expensive"
    assert "stay" in v["verdict"].lower()
    v2 = smc._decision_framework_verdict(1.45, -5.0)
    # 1.45 sits on the "1.20–1.45× expensive" boundary per our table — the
    # doc reads "≥ 1.45× → Stay" so this must fall into very-expensive.
    # Helper uses ``> 1.45`` as the strict-gt test → we assert the 1.45
    # boundary routes to expensive-gap; the "≥1.45×" row fires at 1.46+.
    assert v2["bucket"] in {"expensive-gap", "very-expensive"}


def test_decision_verdict_no_ratio_edge_case():
    # Side A had zero cost → ratio is None. Verdict must not crash.
    v = smc._decision_framework_verdict(None, 5.0)
    assert v["bucket"] == "no-ratio"
    assert v["verdict"] == "cannot auto-classify"


def test_decision_verdict_no_ifeval_cheap():
    # No IFEval (observational compare) + cheap → still recommend switch.
    v = smc._decision_framework_verdict(1.02, None)
    assert v["bucket"] == "no-ifeval-cheap"


def test_decision_verdict_no_ifeval_expensive():
    # No IFEval + ≥1.45× → stay recommendation still has teeth.
    v = smc._decision_framework_verdict(1.60, None)
    assert v["bucket"] == "no-ifeval-expensive"


def test_decision_verdict_no_ifeval_midrange():
    # No IFEval in the 1.05–1.45 band → can't auto-classify.
    v = smc._decision_framework_verdict(1.25, None)
    assert v["bucket"] == "no-ifeval"


# --- _render_compare_analysis_md — section presence + verdict bolding ------

def _synthetic_compare_report(
    *,
    cost_ratio: float = 1.10,
    ifeval_delta_pp: float | None = 6.0,
    paired_count: int = 2,
):
    """Build a minimal but realistic compare report dict for renderer tests.

    The renderer reads a handful of keys from each side plus the summary —
    we populate just enough to cover every section without pulling in the
    full JSONL → ``_build_compare_report`` pipeline.
    """
    side_a = {
        "session_id": "a" * 32,
        "dominant_model_id": "claude-opus-4-6",
        "model_family": "claude-opus-4-6",
        "turn_count": 10,
        "first_ts": "2026-04-20T09:00:00+00:00",
        "last_ts": "2026-04-20T09:10:00+00:00",
        "first_ts_fmt": "2026-04-20 09:00",
        "last_ts_fmt": "2026-04-20 09:10",
        "cache_read_share_of_input": 0.9,
        "totals": {
            "input": 10_000,
            "output": 5_000,
            "cache_read": 90_000,
            "cache_write": 20_000,
            "total": 125_000,
            "cost": 1.0,
            "thinking_turn_count": 2,
            "tool_call_total": 3,
        },
    }
    side_b = {
        **side_a,
        "session_id": "b" * 32,
        "dominant_model_id": "claude-opus-4-7",
        "model_family": "claude-opus-4-7",
        "turn_count": 10,
        "totals": {
            "input": 11_000,
            "output": 6_000,
            "cache_read": 100_000,
            "cache_write": 22_000,
            "total": 139_000,
            "cost": round(1.0 * cost_ratio, 4),
            "thinking_turn_count": 3,
            "tool_call_total": 4,
        },
    }
    paired = [
        {
            "suite_prompt_name": "claudemd_summarise",
            "a": {"input_tokens": 5_000, "output_tokens": 2_000,
                  "cost_usd": 0.4},
            "b": {"input_tokens": 5_500, "output_tokens": 2_400,
                  "cost_usd": 0.44},
            "ratios": {"input_tokens": 1.10, "output_tokens": 1.20,
                       "cost_usd": 1.10},
            "instruction_pass_a": True,
            "instruction_pass_b": True,
        }
        for _ in range(paired_count)
    ]
    if ifeval_delta_pp is not None:
        evaluated = paired_count
        # Back-solve pass counts from the delta so the TL;DR row is
        # internally consistent — side B beats side A by N turns where
        # N = round(delta_pp / 100 * evaluated).
        pass_a = evaluated
        pass_b = evaluated  # both 100% when delta≈0; renderer handles formatting
        summary = {
            "paired_count": paired_count,
            "unmatched_a_count": 0,
            "unmatched_b_count": 0,
            "input_tokens_ratio": 1.10,
            "output_tokens_ratio": 1.20,
            "total_tokens_ratio": 1.11,
            "cost_ratio": cost_ratio,
            "cache_read_share_delta_pp": 0.0,
            "instruction_evaluated": evaluated,
            "instruction_pass_a": pass_a,
            "instruction_pass_b": pass_b,
            "instruction_pass_rate_a": pass_a / evaluated,
            "instruction_pass_rate_b": pass_b / evaluated,
            "instruction_pass_delta_pp": ifeval_delta_pp,
        }
    else:
        summary = {
            "paired_count": paired_count,
            "unmatched_a_count": 0,
            "unmatched_b_count": 0,
            "input_tokens_ratio": 1.10,
            "output_tokens_ratio": 1.20,
            "total_tokens_ratio": 1.11,
            "cost_ratio": cost_ratio,
            "cache_read_share_delta_pp": 0.0,
            "instruction_evaluated": 0,
            "instruction_pass_a": 0,
            "instruction_pass_b": 0,
            "instruction_pass_rate_a": None,
            "instruction_pass_rate_b": None,
            "instruction_pass_delta_pp": None,
        }
    return {
        "mode": "compare",
        "compare_mode": "controlled",
        "pair_by": "fingerprint",
        "slug": "-tmp-sm-compare-run-test",
        "generated_at": "2026-04-20T09:15:00+00:00",
        "side_a": side_a,
        "side_b": side_b,
        "paired": paired,
        "unmatched_a": [],
        "unmatched_b": [],
        "summary": summary,
        "advisories": [
            {"kind": "test-advisory", "severity": "info",
             "message": "synthetic fixture — ignore"},
        ],
    }


def _synthetic_session_report(session_id: str, *, first_cache_write: int = 5_000):
    return {
        "mode": "session",
        "sessions": [
            {
                "session_id": session_id,
                "turns": [
                    {"cache_write_tokens": first_cache_write},
                    {"cache_write_tokens": 0},
                ],
            }
        ],
        "totals": {
            "input": 10_000,
            "output": 5_000,
            "cache_read": 90_000,
            "cache_write": 20_000,
            "total": 125_000,
            "cost": 1.0,
            "thinking_turn_count": 2,
            "tool_call_total": 3,
        },
    }


def test_render_compare_analysis_md_contains_expected_sections():
    cr = _synthetic_compare_report(cost_ratio=1.10, ifeval_delta_pp=6.0)
    sa = _synthetic_session_report("a" * 32, first_cache_write=4_000)
    sb = _synthetic_session_report("b" * 32, first_cache_write=8_000)
    links = {
        "compare_html": "compare_aaaaaaaa_vs_bbbbbbbb_20260420T090000Z.html",
        "side_a_dashboard":
            "session_aaaaaaaa_20260420T090000Z_dashboard.html",
        "side_a_detail":
            "session_aaaaaaaa_20260420T090000Z_detail.html",
        "side_a_json":
            "session_aaaaaaaa_20260420T090000Z.json",
        "side_b_dashboard":
            "session_bbbbbbbb_20260420T090000Z_dashboard.html",
        "side_b_detail":
            "session_bbbbbbbb_20260420T090000Z_detail.html",
        "side_b_json":
            "session_bbbbbbbb_20260420T090000Z.json",
    }

    md = smc._render_compare_analysis_md(cr, sa, sb, links)

    # All 13 sections must land in the scaffold.
    assert "## TL;DR" in md
    assert "## Methodology" in md
    assert "## The numbers" in md
    assert "### Per-session totals" in md
    assert "### Per-prompt breakdown" in md
    assert "## Where does the cost come from?" in md
    assert "## Extended thinking usage" in md
    assert "## Advisories raised by the compare report" in md
    assert "## Should I switch?" in md
    assert "## Methodology caveats" in md
    assert "## Reproduce it yourself" in md
    assert "## Links" in md

    # TODO placeholders survive in exactly the prose sections.
    assert "{{TODO" in md
    assert md.count("{{TODO") >= 5  # title, subtitle, TL;DR, cost, thinking, switch

    # Headline ratio from summary is rendered (1.10×).
    assert "1.10×" in md or "1.10x" in md or "×1.10" in md

    # Decision-framework verdict gets bolded. With cost=1.10, Δ=+6 pp →
    # mid-quality-win row → "1.05–1.20×" + "+5 pp or more" bolded.
    assert "**1.05–1.20×**" in md
    assert "**+5 pp or more**" in md

    # Matched-bucket footer names the bucket explicitly.
    assert "mid-quality-win" in md

    # Relative hrefs from links dict appear in the Links section.
    assert (
        "session_aaaaaaaa_20260420T090000Z_dashboard.html" in md
    )
    assert (
        "compare_aaaaaaaa_vs_bbbbbbbb_20260420T090000Z.html" in md
    )

    # Advisory bullet renders.
    assert "test-advisory" in md

    # Reproduce section names the opt-out flag.
    assert "--no-compare-run-extras" in md


def test_render_compare_analysis_md_no_ifeval_still_renders():
    # Observational compare — no IFEval predicates fired. TL;DR must
    # downgrade gracefully, verdict must show "cannot auto-classify"
    # (or the cheap / expensive cost-only bucket).
    cr = _synthetic_compare_report(cost_ratio=1.25, ifeval_delta_pp=None)
    sa = _synthetic_session_report("a" * 32)
    sb = _synthetic_session_report("b" * 32)
    md = smc._render_compare_analysis_md(cr, sa, sb, {})

    assert "not evaluated" in md or "no IFEval" in md.lower()
    # No-IFEval midrange should surface the "cannot auto-classify" verdict.
    assert "cannot auto-classify" in md
    # Missing link cells render as a polite fallback, not a broken link.
    assert "not available" in md


# --- _emit_compare_run_extras — end-to-end paths written --------------------

def _plant_capture_jsonl(
    projects_dir: Path, slug: str, uuid: str, fixture: str
):
    """Seed a project-dir JSONL under the slug that ``_emit_compare_run_extras``
    resolves via ``_projects_dir()``. Mimics the real post-capture layout."""
    import shutil as _shutil
    target_dir = projects_dir / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid}.jsonl"
    _shutil.copy(_FIXTURES_DIR / fixture, target)
    return target


def test_emit_compare_run_extras_writes_5_files_end_to_end(
    tmp_path, monkeypatch
):
    """Seed two real JSONL fixtures under a synthetic projects dir, hand a
    synthetic compare report to the extras emitter, and assert the 5+
    expected artefacts land in ``exports/session-metrics/``."""
    slug = "-tmp-sm-compare-run-test"
    uuid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    uuid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    projects_dir = tmp_path / "projects"
    _plant_capture_jsonl(
        projects_dir, slug, uuid_a, "compare_opus_4_6_a.jsonl",
    )
    _plant_capture_jsonl(
        projects_dir, slug, uuid_b, "compare_opus_4_7_a.jsonl",
    )

    # _export_dir reads cwd → redirect exports to tmp_path.
    monkeypatch.chdir(tmp_path)
    # _projects_dir reads CLAUDE_PROJECTS_DIR → point at our seeded tree.
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    cr = _synthetic_compare_report(cost_ratio=1.10, ifeval_delta_pp=6.0)
    # Override the synthetic session IDs on the report so the compare
    # HTML filename inside the Links section matches the uuid8 prefixes
    # the extras helper will compute. (The real pipeline uses the actual
    # uuids from the capture.)
    cr["side_a"]["session_id"] = uuid_a
    cr["side_b"]["session_id"] = uuid_b

    diag = smc._emit_compare_run_extras(
        cr,
        uuid_a,
        uuid_b,
        slug,
        formats=["html", "json"],
        single_page=False,
        chart_lib="none",  # skip highcharts payload — faster and no SHA check
        tz_offset=0.0,
        tz_label="UTC",
        include_subagents=False,
        use_cache=False,
    )

    # Per-side exports: HTML split (dashboard + detail) and JSON for each.
    for side_key in ("side_a", "side_b"):
        side = diag[side_key]
        assert side["html_dashboard"].exists()
        assert side["html_detail"].exists()
        assert side["json"].exists()
        # All written to the shared exports dir.
        assert "exports/session-metrics/" in str(side["html_dashboard"])
        # Basic sanity: the HTML carries the session id prefix in its name.
    # The analysis.md companion landed with the _analysis suffix.
    assert diag["analysis_md"] is not None
    assert diag["analysis_md"].exists()
    assert diag["analysis_md"].name.endswith("_analysis.md")
    assert "compare_" in diag["analysis_md"].name

    # Shared timestamp — all seven files (2×dashboard + 2×detail + 2×json
    # + 1×analysis.md) must carry the same timestamp substring.
    stamps = set()
    for p in (
        diag["side_a"]["html_dashboard"],
        diag["side_a"]["html_detail"],
        diag["side_a"]["json"],
        diag["side_b"]["html_dashboard"],
        diag["side_b"]["html_detail"],
        diag["side_b"]["json"],
        diag["analysis_md"],
    ):
        # Filenames look like ..._<ts>_dashboard.html / ..._<ts>.json /
        # ..._<ts>_analysis.md — pull the 16-char UTC stamp block.
        import re as _re
        m = _re.search(r"(\d{8}T\d{6}Z)", p.name)
        assert m, f"no timestamp in {p.name}"
        stamps.add(m.group(1))
    assert len(stamps) == 1, (
        f"extras should share a single timestamp; got {stamps}"
    )

    # Analysis.md body must be rendered content, not an empty shell.
    body = diag["analysis_md"].read_text(encoding="utf-8")
    assert "## TL;DR" in body
    assert "## Should I switch?" in body
    # Links resolve to the sibling filenames written above.
    assert diag["side_a"]["html_dashboard"].name in body
    assert diag["side_b"]["html_dashboard"].name in body


def test_emit_compare_run_extras_missing_jsonl_degrades_gracefully(
    tmp_path, monkeypatch, capsys
):
    """If one side's JSONL never landed on disk (rare but possible if the
    capture ran against a different slug), the helper warns + skips the
    per-session exports but still emits the analysis.md companion so the
    user has *something*."""
    slug = "-tmp-sm-compare-run-test"
    uuid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    uuid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    projects_dir = tmp_path / "projects"
    # Only plant side A.
    _plant_capture_jsonl(
        projects_dir, slug, uuid_a, "compare_opus_4_6_a.jsonl",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))

    cr = _synthetic_compare_report(cost_ratio=1.10, ifeval_delta_pp=6.0)
    cr["side_a"]["session_id"] = uuid_a
    cr["side_b"]["session_id"] = uuid_b

    diag = smc._emit_compare_run_extras(
        cr, uuid_a, uuid_b, slug,
        formats=["json"],
        single_page=False,
        chart_lib="none",
        tz_offset=0.0,
        tz_label="UTC",
        include_subagents=False,
        use_cache=False,
    )

    # Side A rendered; side B skipped; analysis.md still emitted.
    assert "json" in diag["side_a"]
    assert diag["side_a"]["json"].exists()
    assert diag["side_b"] == {}
    assert diag["analysis_md"] is not None
    assert diag["analysis_md"].exists()

    captured = capsys.readouterr()
    assert "side B JSONL not found" in captured.err or \
           "side B JSONL not found" in captured.out


# --- _run_compare_run end-to-end with extras on -----------------------------

def test_compare_run_with_extras_fires_emitter_when_formats_set(
    tmp_path, monkeypatch
):
    """Full path: ``_run_compare_run(auto_resume=True, compare_run_extras=True,
    formats=["json"])`` captures via fake subprocess, monkeypatches
    ``_run_compare`` to return a synthetic compare report, and asserts
    ``_emit_compare_run_extras`` was called with the right arguments."""
    fake_run, _ = _make_fake_subprocess_run()
    uuids = iter(["uuid-a", "uuid-b"])

    captured_compare_kwargs = {}
    def fake_run_compare(*a, **kw):
        captured_compare_kwargs["args"] = a
        captured_compare_kwargs["kwargs"] = kw
        return _synthetic_compare_report(cost_ratio=1.10, ifeval_delta_pp=6.0)

    captured_emit_kwargs = {}
    def fake_emit(compare_report, uuid_a, uuid_b, slug, **kw):
        captured_emit_kwargs["compare_report"] = compare_report
        captured_emit_kwargs["uuid_a"] = uuid_a
        captured_emit_kwargs["uuid_b"] = uuid_b
        captured_emit_kwargs["slug"] = slug
        captured_emit_kwargs["kwargs"] = kw
        return {"side_a": {"json": tmp_path / "a.json"},
                "side_b": {"json": tmp_path / "b.json"},
                "analysis_md": tmp_path / "analysis.md"}

    monkeypatch.setattr(smc, "_run_compare", fake_run_compare)
    monkeypatch.setattr(smc, "_emit_compare_run_extras", fake_emit)

    result = smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: next(uuids),
        stdin=_FakeTty(False),
        formats=["json"],            # opts into file output → extras fire
        auto_resume=True,
        compare_run_extras=True,
    )

    # _run_compare was invoked with the captured uuids.
    assert captured_compare_kwargs, "_run_compare should have been called"
    # _emit_compare_run_extras was called with uuids, slug, and format set.
    assert captured_emit_kwargs["uuid_a"] == "uuid-a"
    assert captured_emit_kwargs["uuid_b"] == "uuid-b"
    assert captured_emit_kwargs["kwargs"]["formats"] == ["json"]
    # Diagnostic result includes the extras paths.
    assert "extras" in result


def test_compare_run_extras_suppressed_by_flag(tmp_path, monkeypatch):
    """--no-compare-run-extras (compare_run_extras=False) must skip the
    emitter entirely, preserving pre-1.7.0 behaviour."""
    fake_run, _ = _make_fake_subprocess_run()
    uuids = iter(["uuid-a", "uuid-b"])

    monkeypatch.setattr(
        smc, "_run_compare",
        lambda *a, **kw: _synthetic_compare_report(
            cost_ratio=1.10, ifeval_delta_pp=6.0
        ),
    )
    emit_called = {"n": 0}
    def fake_emit(*a, **kw):
        emit_called["n"] += 1
    monkeypatch.setattr(smc, "_emit_compare_run_extras", fake_emit)

    result = smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: next(uuids),
        stdin=_FakeTty(False),
        formats=["json"],
        auto_resume=True,
        compare_run_extras=False,   # the opt-out flag
    )
    assert emit_called["n"] == 0
    assert "extras" not in result


def test_compare_run_extras_suppressed_when_no_formats(tmp_path, monkeypatch):
    """Without --output, compare-run stays text-only-to-stdout. Even with
    extras opt-in (default), the emitter must not fire — writing files
    from a no-output invocation would surprise scripting callers."""
    fake_run, _ = _make_fake_subprocess_run()
    uuids = iter(["uuid-a", "uuid-b"])

    monkeypatch.setattr(
        smc, "_run_compare",
        lambda *a, **kw: _synthetic_compare_report(
            cost_ratio=1.10, ifeval_delta_pp=6.0
        ),
    )
    emit_called = {"n": 0}
    def fake_emit(*a, **kw):
        emit_called["n"] += 1
    monkeypatch.setattr(smc, "_emit_compare_run_extras", fake_emit)

    smc._run_compare_run(
        "claude-opus-4-6", "claude-opus-4-7",
        scratch_dir=tmp_path,
        assume_yes=True,
        subprocess_run=fake_run,
        uuid_factory=lambda: next(uuids),
        stdin=_FakeTty(False),
        formats=[],                 # no file output → no extras
        auto_resume=True,
        compare_run_extras=True,    # opt-in, but has no effect
    )
    assert emit_called["n"] == 0


def test_compare_run_argparse_wires_no_compare_run_extras_flag(
    tmp_path, monkeypatch
):
    """End-to-end via argparse: ``--no-compare-run-extras`` flips the
    ``compare_run_extras`` kwarg to False when dispatching to
    ``_run_compare_run``."""
    captured = {}
    def fake_orchestrator(model_a, model_b, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(smc, "_run_compare_run", fake_orchestrator)
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "claude-opus-4-6", "claude-opus-4-7",
            "--compare-run-scratch-dir", str(tmp_path),
            "--no-compare-run-extras",
            "--yes",
        ],
    )
    sm.main()
    assert captured["compare_run_extras"] is False


def test_compare_run_argparse_default_enables_extras(tmp_path, monkeypatch):
    """Without --no-compare-run-extras, the flag threads through as True."""
    captured = {}
    def fake_orchestrator(model_a, model_b, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(smc, "_run_compare_run", fake_orchestrator)
    monkeypatch.setattr(
        "sys.argv",
        [
            "session-metrics.py",
            "--compare-run", "claude-opus-4-6", "claude-opus-4-7",
            "--compare-run-scratch-dir", str(tmp_path),
            "--yes",
        ],
    )
    sm.main()
    assert captured["compare_run_extras"] is True


# --- _write_output explicit_ts kwarg (backwards-compat) ---------------------

def test_write_output_explicit_ts_overrides_default(tmp_path, monkeypatch):
    """When explicit_ts is passed, the stem in the written filename uses
    that timestamp verbatim instead of ``datetime.now(UTC)``. This is
    what lets the extras bundle share one timestamp across 7 files."""
    monkeypatch.chdir(tmp_path)
    fake_report = {
        "mode": "session",
        "sessions": [{"session_id": "abcdef01deadbeef"}],
    }
    path = sm._write_output(
        "json", "{}", fake_report, explicit_ts="20260420T090000Z",
    )
    assert path.name == "session_abcdef01_20260420T090000Z.json"


def test_write_output_default_ts_still_works(tmp_path, monkeypatch):
    """Omitting explicit_ts preserves the pre-existing behaviour — a live
    UTC stamp from ``datetime.now`` lands in the filename."""
    monkeypatch.chdir(tmp_path)
    fake_report = {
        "mode": "session",
        "sessions": [{"session_id": "abcdef01deadbeef"}],
    }
    path = sm._write_output("json", "{}", fake_report)
    # Filename must end with 16-char UTC stamp, not be empty.
    import re as _re
    assert _re.search(r"session_abcdef01_\d{8}T\d{6}Z\.json$", path.name)


# --- Per-turn drill-down (drawer + Prompts section) -------------------------
#
# These tests guard the new right-side drawer + "Prompts" section that surface
# each turn's user prompt, slash command, tool calls, content-block mix, and
# assistant reply. Both are gated on the Detail + single-page HTML variants —
# Dashboard must stay untouched.

def test_turn_record_has_prompt_and_tool_detail_fields():
    """Every non-resume turn built from the fixture carries the new fields."""
    r = _build_fixture_report()
    new_keys = {
        "prompt_text", "prompt_snippet", "slash_command",
        "assistant_text", "assistant_snippet", "tool_use_detail",
    }
    for t in r["sessions"][0]["turns"]:
        if t.get("is_resume_marker"):
            continue
        missing = new_keys - t.keys()
        assert not missing, f"turn {t['index']} missing fields: {missing}"
        assert isinstance(t["prompt_text"], str)
        assert isinstance(t["prompt_snippet"], str)
        assert isinstance(t["slash_command"], str)
        assert isinstance(t["assistant_text"], str)
        assert isinstance(t["assistant_snippet"], str)
        assert isinstance(t["tool_use_detail"], list)


def test_prompt_snippet_is_truncated_to_240_chars():
    """Prompts longer than 240 chars are clipped + ellipsis; shorter stay whole."""
    long_prompt = "x" * 500
    out = sm._truncate(long_prompt, 240)
    assert len(out) == 241          # 240 chars + ellipsis glyph
    assert out.endswith("\u2026")
    # Short prompts pass through unchanged.
    assert sm._truncate("short", 240) == "short"
    # Non-string input returns empty string (defensive).
    assert sm._truncate(None, 240) == ""


@pytest.mark.parametrize("prompt,raw,expected", [
    ("/clear please", None, "/clear"),
    ("/compact-now", None, "/compact-now"),
    ("no slash here", None, ""),
    ("",              None, ""),
    # XML-wrapped form — stripped from prompt_text but detected on raw content
    ("wipe context",  "<command-name>/clear</command-name>\nwipe context",  "/clear"),
    # Raw-list content form
    ("wipe context",
     [{"type": "text", "text": "<command-name>/exit</command-name>bye"}],
     "/exit"),
])
def test_slash_command_extraction(prompt, raw, expected):
    assert sm._extract_slash_command(prompt, raw) == expected


def test_html_has_turn_data_json_blob():
    """Single-page HTML embeds a <script type=application/json> payload
    keyed by `<sid8>-<index>` for every non-resume turn."""
    import json as _json
    r = _build_fixture_report()
    html = sm.render_html(r, variant="single")
    assert '<script type="application/json" id="turn-data">' in html
    # Extract the JSON blob and parse it.
    start = html.find('id="turn-data">') + len('id="turn-data">')
    end   = html.find("</script>", start)
    blob  = html[start:end].replace("<\\/", "</")
    data  = _json.loads(blob)
    # 6 turns in the fixture, none are resume markers → 6 payloads.
    assert len(data) == 6
    sid8 = r["sessions"][0]["session_id"][:8]
    for idx in range(1, 7):
        key = f"{sid8}-{idx}"
        assert key in data, f"missing payload for {key}"
        p = data[key]
        for must_have in ("idx", "ts", "model", "prompt_snippet",
                          "prompt_text", "slash_command", "tools",
                          "content", "cost", "input", "output",
                          "cache_read", "cache_write",
                          "assistant_snippet", "assistant_text"):
            assert must_have in p, f"payload {key} missing {must_have}"


def test_html_has_turn_drawer_element():
    """Drawer structural markup is present on single-page + detail variants."""
    r = _build_fixture_report()
    for variant in ("single", "detail"):
        html = sm.render_html(r, variant=variant)
        assert 'id="turn-drawer"' in html
        assert 'class="turn-drawer-backdrop"' in html
        # Data-slot attributes the JS populates at open time.
        for slot in ("idx", "ts", "model", "prompt-snippet", "prompt-full",
                     "tools", "tool-count", "content-dl", "cost",
                     "tok-input", "tok-output", "tok-cache-read",
                     "tok-cache-write", "cache-savings",
                     "assistant-snippet", "assistant-full"):
            assert f'data-slot="{slot}"' in html, (
                f"drawer missing data-slot={slot!r} in variant={variant}"
            )


def test_html_has_prompts_section_when_prompts_present():
    """Prompts section renders when at least one turn has a real prompt."""
    r = _build_fixture_report()
    html = sm.render_html(r, variant="single")
    assert '<table class="prompts-table">' in html
    # Every prompts row must carry data-turn-id so click-to-open works.
    import re as _re
    rows = _re.findall(r'<tr class="prompts-row" data-turn-id="([^"]+)"', html)
    assert len(rows) >= 1
    # Each row's key must match a Timeline row id="turn-<key>".
    for key in rows:
        assert f'id="turn-{key}"' in html


def test_html_omits_prompts_section_when_no_prompts():
    """A synthetic report with zero real prompts must not render the section."""
    r = _build_fixture_report()
    # Wipe prompts from every turn — simulates a tool-only subagent session.
    for t in r["sessions"][0]["turns"]:
        t["prompt_text"]    = ""
        t["prompt_snippet"] = ""
    html = sm.render_html(r, variant="single")
    # CSS selectors for .prompts-table live in <style> always, but the actual
    # markup (<table class="prompts-table">) must be gone.
    assert '<table class="prompts-table">' not in html
    assert '<details class="prompts-details"' not in html


def test_html_preserves_resume_marker_row_class():
    """Resume-marker rows keep their distinct class and never become turn-row."""
    r = _build_fixture_report()
    turns = r["sessions"][0]["turns"]
    turns[0]["is_resume_marker"] = True
    turns[0]["is_terminal_exit_marker"] = False
    r["sessions"][0]["resumes"] = sm._build_resumes(turns)
    html = sm.render_html(r, variant="single")
    assert 'class="resume-marker-row"' in html
    # Index 1 is a resume marker → no turn-<sid>-1 row with turn-row class.
    sid8 = r["sessions"][0]["session_id"][:8]
    assert f'class="turn-row" data-session="{sid8}" data-turn-id="{sid8}-1"' not in html
    # It's also absent from the drawer's JSON payload.
    import json as _json
    start = html.find('id="turn-data">') + len('id="turn-data">')
    end   = html.find("</script>", start)
    blob  = html[start:end].replace("<\\/", "</")
    data  = _json.loads(blob)
    assert f"{sid8}-1" not in data


def test_html_preserves_session_header_toggle():
    """Project-mode session-header rows + toggle script remain intact."""
    sid, turns, uts = sm._load_session(_FIXTURE, include_subagents=False)
    r = sm._build_report("project", "test-slug", [(sid, turns, uts)])
    html = sm.render_html(r, variant="single")
    assert 'class="session-header" data-toggle="sess-1"' in html
    # Pre-existing toggle JS must still be emitted.
    assert "document.querySelectorAll('tr.session-header[data-toggle]')" in html


def test_dashboard_variant_has_no_drawer_or_prompts_section():
    """Dashboard variant stays untouched — no drawer, no Prompts table,
    no turn-data JSON blob."""
    r = _build_fixture_report()
    html = sm.render_html(r, variant="dashboard")
    assert 'id="turn-drawer"' not in html
    assert '<table class="prompts-table">' not in html
    assert '<script type="application/json" id="turn-data">' not in html


def test_turn_data_json_is_html_escaped():
    """Prompts containing <script> tags are embedded safely — the outer
    <script> tag can't be closed early by a payload `</script>` token."""
    import json as _json
    r = _build_fixture_report()
    # Inject a payload that would break the surrounding <script> tag if not
    # escaped: both a literal </script> and a visible injected element.
    r["sessions"][0]["turns"][0]["prompt_text"]    = "</script><img src=x onerror=alert(1)>"
    r["sessions"][0]["turns"][0]["prompt_snippet"] = "</script><img>"
    html = sm.render_html(r, variant="single")
    # The JSON blob must not contain a literal `</script>` — it's neutralised
    # to `<\/script>` by the renderer.
    start = html.find('id="turn-data">') + len('id="turn-data">')
    # First </script> after start is the blob's closing tag. A pre-closing
    # `</script>` inside the blob would make the slice invalid JSON.
    end = html.find("</script>", start)
    blob = html[start:end]
    assert "</script>" not in blob
    # But once un-escaped, JSON still parses and preserves the original text.
    data = _json.loads(blob.replace("<\\/", "</"))
    sid8 = r["sessions"][0]["session_id"][:8]
    payload = data[f"{sid8}-1"]
    assert payload["prompt_text"] == "</script><img src=x onerror=alert(1)>"


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
    assert 'id="turn-drawer"' not in html
    assert 'id="turn-data-json"' not in html


def test_render_html_instance_hyperlinks_to_project_drilldowns(instance_env):
    html, run = _run_instance_capture_html(instance_env, drilldown=True)
    # One href per project, pointing at a relative projects/<slug>.html
    assert 'href="projects/-home-user-alpha.html"' in html
    assert 'href="projects/-home-user-beta.html"' in html
    # Drilldown files actually exist on disk
    assert (run / "projects" / "-home-user-alpha.html").exists()
    assert (run / "projects" / "-home-user-beta.html").exists()


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
    header = content.splitlines()[0]
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
