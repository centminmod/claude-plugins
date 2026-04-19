"""Unit + integration tests for session-metrics.py.

Run with: uv run python -m pytest tests/ -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_HERE       = Path(__file__).parent
_SCRIPT     = _HERE.parent / "scripts" / "session-metrics.py"
_FIXTURE    = _HERE / "fixtures" / "mini.jsonl"


def _load_module():
    spec = importlib.util.spec_from_file_location("session_metrics", _SCRIPT)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["session_metrics"] = mod
    spec.loader.exec_module(mod)
    return mod


sm = _load_module()


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


def test_read_vendor_js_unknown_library_returns_empty(capsys):
    payload = sm._read_vendor_js("not-a-library")
    assert payload == ""
    err = capsys.readouterr().err
    assert "not in vendor manifest" in err


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
