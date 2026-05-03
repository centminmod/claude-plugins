"""Pricing-table parity, regex-anchor, and family-fallback tests.

Split out of test_session_metrics.py in v1.41.8 (Tier 4.1 of the
post-audit improvement plan). Covers:

- ``_pricing_for`` exact / regex / family-fallback resolution
- ``_PRICING`` / ``_PRICING_PATTERNS`` regex boundaries (v1.41.0 P0-B)
- ``_UNKNOWN_MODELS_SEEN`` accumulation behaviour
- audit-extract.py ``_input_rate_for_model`` parity with session-metrics

Run with: uv run python -m pytest tests/test_pricing.py -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_HERE       = Path(__file__).parent
_SCRIPT     = _HERE.parent / "scripts" / "session-metrics.py"
_AUDIT_EXTRACT = (_HERE.parent.parent / "audit-session-metrics"
                  / "scripts" / "audit-extract.py")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"could not locate module spec for {path}"
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None, f"spec has no loader for {path}"
    spec.loader.exec_module(mod)
    return mod


# Reuse the canonical module instance if test_session_metrics.py
# already loaded it. ``_load_module`` re-execs unconditionally — without
# this guard the cross-file ``sm`` references point to *different*
# module objects (whichever file pytest collects last wins the
# ``sys.modules['session_metrics']`` slot). Leaf modules under
# scripts/_*.py use ``_sm()`` to fetch the canonical instance from
# sys.modules at call time, so monkeypatch writes against the loser
# instance silently miss.
sm = sys.modules.get("session_metrics") or _load_module("session_metrics", _SCRIPT)


def _load_audit_extract():
    """Lazy-load audit-extract.py as a module so tests can call its
    functions directly without spawning a subprocess for each case."""
    return _load_module("audit_extract", _AUDIT_EXTRACT)


# Test-isolation guard — duplicate of the autouse fixture in
# test_session_metrics.py. Both fixtures are scoped to the module they
# are declared in (autouse only fires for tests in the same module),
# so the duplication is necessary to keep the pricing tests insulated
# from the user's real ``~/.claude/projects/`` directory. A future
# slice can lift both fixtures into a shared ``tests/conftest.py``
# once 2-3 split files exist and the duplication justifies the
# refactor.
@pytest.fixture(autouse=True)
def isolate_projects_dir(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("real_projects_dir"):
        return
    safe = tmp_path / "_autouse_projects"
    safe.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(safe))


# v1.41.0: ``_pricing_for`` is wrapped in ``functools.lru_cache``. Most
# tests don't mind the cache (deterministic input → deterministic
# output), but the unknown-model tests below monkeypatch
# ``_UNKNOWN_MODELS_SEEN`` and rely on the side effect refiring per
# call. Clearing the cache before every test guarantees that contract
# holds even if a future test reuses an unknown model name across
# cases.
@pytest.fixture(autouse=True)
def _clear_pricing_cache():
    sm._pricing_for.cache_clear()
    yield


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


def test_pricing_opus_4_bare_id_old_tier(monkeypatch):
    """Bare 'claude-opus-4' (no minor, no date) maps to OLD $15 tier.

    v1.41.2: the bare key was removed from _PRICING and replaced with an
    anchored regex in _PRICING_PATTERNS. This guards the regex still
    matches the bare ID and keeps it silent (no _UNKNOWN_MODELS_SEEN flag).
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4")
    assert r["input"] == 15.00
    assert r["output"] == 75.00
    assert "claude-opus-4" not in sm._UNKNOWN_MODELS_SEEN


def test_pricing_opus_4_with_date_suffix_old_tier(monkeypatch):
    """'claude-opus-4-20250514' (Opus 4.0 with date) → OLD tier silently.

    v1.41.2 anchored regex: `^claude-opus-4(?:-\\d{8})?$` matches both the
    bare ID and an 8-digit date suffix, but rejects sub-version forms
    like `claude-opus-4-8` (which now route through the family fallback).
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4-20250514")
    assert r["input"] == 15.00
    assert r["output"] == 75.00
    assert not sm._UNKNOWN_MODELS_SEEN


def test_pricing_unknown_opus_4_minor_routes_to_new_tier(monkeypatch):
    """v1.41.2 bug-fix: unknown future Opus 4 minor → NEW $5 tier (not OLD $15).

    Before v1.41.2, `claude-opus-4-8` prefix-matched the bare
    `claude-opus-4` entry and silently 3x-overcharged at $15/$75. The
    family fallback now routes it to NEW $5/$25 AND flags the model so
    the at-exit advisory tells the user to add an explicit entry.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4-8")
    assert r["input"] == 5.00
    assert r["output"] == 25.00
    assert "claude-opus-4-8" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_unknown_opus_4_minor_with_date_routes_to_new_tier(monkeypatch):
    """`claude-opus-4-8-20260601` → NEW tier flagged (date-suffixed unknown)."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4-8-20260601")
    assert r["input"] == 5.00
    assert "claude-opus-4-8-20260601" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_future_opus_major_routes_to_new_tier(monkeypatch):
    """`claude-opus-5` / `claude-opus-6` → NEW tier flagged.

    Conservative bet: if Anthropic's Opus 5 raises rates, we under-count
    by a small margin and warn — preferable to silently 3x over-counting
    by falling to the OLD-tier prefix or 5x under-counting by falling
    to _DEFAULT_PRICING.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-5")
    assert r["input"] == 5.00
    assert r["output"] == 25.00
    assert "claude-opus-5" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_unknown_haiku_4_minor_routes_to_haiku_tier(monkeypatch):
    """v1.41.2 bug-fix: `claude-haiku-4-6` → Haiku $1 tier (not Sonnet $3 default).

    Before v1.41.2, `claude-haiku-4-6` had no Haiku prefix entry and fell
    through to _DEFAULT_PRICING (Sonnet $3/$15) — a 3x overcharge. The
    family fallback now routes it to Haiku $1/$5.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-haiku-4-6")
    assert r["input"] == 1.00
    assert r["output"] == 5.00
    assert "claude-haiku-4-6" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_future_haiku_major_routes_to_haiku_tier(monkeypatch):
    """`claude-haiku-5` / `claude-haiku-9` → Haiku tier flagged."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-haiku-9")
    assert r["input"] == 1.00
    assert r["output"] == 5.00
    assert "claude-haiku-9" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_known_opus_4_7_with_date_silent(monkeypatch):
    """`claude-opus-4-7-20251214` → NEW tier silently via prefix sweep.

    The family fallback intentionally runs AFTER the prefix sweep so a
    date-suffixed known model lands on its prefix entry (silent) rather
    than being unnecessarily flagged as unknown.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4-7-20251214")
    assert r["input"] == 5.00
    assert not sm._UNKNOWN_MODELS_SEEN


def test_pricing_opus_4_1_with_date_still_old_tier(monkeypatch):
    """`claude-opus-4-1-20250514` → OLD tier silently via prefix sweep.

    The family fallback regex is anchored to N>=5, so 4-1 doesn't match.
    Prefix sweep handles it via the explicit `claude-opus-4-1` entry.
    """
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-opus-4-1-20250514")
    assert r["input"] == 15.00
    assert not sm._UNKNOWN_MODELS_SEEN


# --- Unknown-model tracking --------------------------------------------------

def test_pricing_unknown_model_adds_to_seen_set(monkeypatch):
    """_pricing_for accumulates unknown model names into _UNKNOWN_MODELS_SEEN."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    sm._pricing_for("claude-entirely-fictional-99")
    assert "claude-entirely-fictional-99" in sm._UNKNOWN_MODELS_SEEN


def test_pricing_known_model_not_added_to_seen_set(monkeypatch):
    """A model present in _PRICING must not be added to _UNKNOWN_MODELS_SEEN."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    sm._pricing_for("claude-opus-4-7")
    assert not sm._UNKNOWN_MODELS_SEEN


def test_pricing_unknown_model_returns_default_rates(monkeypatch):
    """An unrecognised model falls back to the Sonnet-rate default pricing dict."""
    monkeypatch.setattr(sm, "_UNKNOWN_MODELS_SEEN", set())
    r = sm._pricing_for("claude-totally-unknown-model")
    assert r is sm._DEFAULT_PRICING


# --- audit-extract pricing-table parity --------------------------------------

def test_audit_extract_input_rate_for_model_table():
    """P1.2 regression — _input_rate_for_model returns the correct $/M rate for
    each canonical model family. Previously a single OPUS_INPUT_RATE_PER_M=$5
    was applied uniformly, overstating Sonnet impact by 67% and Haiku by 400%."""
    ae = _load_audit_extract()
    assert ae._input_rate_for_model("claude-opus-4-7") == 5.00
    assert ae._input_rate_for_model("claude-opus-4-1") == 15.00
    assert ae._input_rate_for_model("claude-sonnet-4-6") == 3.00
    assert ae._input_rate_for_model("claude-haiku-4-5-20251001") == 1.00
    assert ae._input_rate_for_model("claude-3-5-haiku") == 0.80
    # Substring matching still works on family-only strings.
    assert ae._input_rate_for_model("claude-haiku") == 1.00
    # Unknown / missing / synthetic → default fallback (Sonnet rate).
    assert ae._input_rate_for_model("") == ae._DEFAULT_INPUT_RATE_PER_M
    assert ae._input_rate_for_model(None) == ae._DEFAULT_INPUT_RATE_PER_M
    assert ae._input_rate_for_model("<synthetic>") == ae._DEFAULT_INPUT_RATE_PER_M
    assert ae._input_rate_for_model("gpt-5.5") == ae._DEFAULT_INPUT_RATE_PER_M


# Bare-prefix entries in audit-extract's table that intentionally diverge
# from session_metrics._pricing_for. Substring-matching catches real model
# ids carrying these prefixes (e.g. "claude-opus" matches "claude-opus-4-7"),
# but feeding the bare prefix itself to _pricing_for falls through every
# resolution step (no exact, no regex, no startswith on a longer key, no
# family-fallback regex without a version digit) and lands on _DEFAULT_PRICING.
# These three entries exist as defensive family-tier fallbacks for
# hypothetical Anthropic IDs that arrive without a version suffix; real
# transcripts always carry a version, so the divergence is dormant.
_AUDIT_EXTRACT_BARE_PREFIX_NEEDLES = frozenset({
    "claude-sonnet", "claude-haiku", "claude-opus",
})


def test_audit_extract_pricing_parity_forward():
    """P2 forward parity — every Anthropic-prefixed key in session-metrics'
    ``_PRICING`` must resolve to the same input rate via audit-extract's
    ``_input_rate_for_model``. Catches drift when a new Anthropic model
    ships in session-metrics but is forgotten in audit-extract (silent
    fallback to Sonnet $3/M would mis-estimate cache_break impact).

    Non-Anthropic models (glm-*, openai/*, deepseek/*, etc.) are out of
    scope: cache_break / idle_gap_cache_decay never fire on them because
    those models lack prompt caching.
    """
    ae = _load_audit_extract()
    for key, rates in sm._PRICING.items():
        if not key.startswith("claude-"):
            continue
        sm_rate = rates["input"]
        ae_rate = ae._input_rate_for_model(key)
        assert ae_rate == sm_rate, (
            f"pricing parity broken on {key!r}: audit-extract returns "
            f"${ae_rate}/M, session-metrics has ${sm_rate}/M. "
            f"Update _INPUT_RATE_PER_M_BY_MODEL in audit-extract.py."
        )


def test_audit_extract_pricing_parity_reverse():
    """P2 reverse parity — every entry in audit-extract's
    ``_INPUT_RATE_PER_M_BY_MODEL`` (excluding the documented bare-prefix
    catchalls) must resolve to the same input rate via
    ``session_metrics._pricing_for``. Catches drift when an Anthropic
    rate change lands in session-metrics' ``_PRICING`` but the
    hand-maintained audit-extract table keeps the stale value.
    """
    ae = _load_audit_extract()
    for needle, audit_rate in ae._INPUT_RATE_PER_M_BY_MODEL:
        if needle in _AUDIT_EXTRACT_BARE_PREFIX_NEEDLES:
            continue
        sm_rate = sm._pricing_for(needle)["input"]
        assert sm_rate == audit_rate, (
            f"pricing drift on {needle!r}: audit-extract has "
            f"${audit_rate}/M but session-metrics resolves to "
            f"${sm_rate}/M. Reconcile both tables."
        )


def test_audit_extract_bare_prefix_needles_match_documented_set():
    """P2 sentinel — the documented bare-prefix needles list above must
    stay in sync with audit-extract's table. Adding or removing a bare
    prefix without updating ``_AUDIT_EXTRACT_BARE_PREFIX_NEEDLES`` would
    silently weaken the parity guard."""
    ae = _load_audit_extract()
    actual_bare = {
        needle for needle, _ in ae._INPUT_RATE_PER_M_BY_MODEL
        if needle in {"claude-sonnet", "claude-haiku", "claude-opus"}
    }
    assert actual_bare == _AUDIT_EXTRACT_BARE_PREFIX_NEEDLES


# --- Pricing regex boundaries (v1.41.0 P0-B) ---------------------------------

@pytest.mark.parametrize("model,expected_input_rate", [
    # Happy paths — exact matches and well-formed family IDs
    ("claude-opus-4-7",                5.00),
    ("openai/gpt-5.5",                 5.00),     # exact dict key
    ("gpt-5.5",                        5.00),     # bare suffix via prefix sweep
    ("openai/gpt-5.5-pro",             30.00),    # exact dict key
    ("openai/gpt-5.5-pro:1m",          30.00),    # \b boundary handles :tag suffix
    ("deepseek/deepseek-v4-pro",       1.74),
    ("deepseek/deepseek-v4-flash",     0.14),
    ("deepseek.v4-flash",              0.14),     # dotted separator preserved
    ("xiaomi/mimo-v2.5-pro",           1.00),
    ("xiaomi/mimo-v2.5",               0.40),
    ("moonshotai/kimi-k2.6",           0.7448),
    ("qwen/qwen3.6-plus",              0.325),
    ("minimax/minimax-m2.7",           0.30),
    ("z-ai/glm-5-turbo",               1.20),
    # Should fall through to default Sonnet rates after the regex tightening
    ("gpt-5.55",                       3.00),     # NOT gpt-5.5 — (?!\d)
    ("gpt-5.55-pro",                   3.00),     # NOT gpt-5.5-pro
    ("qwen3.60-plus",                  3.00),     # NOT qwen3.6-plus
    ("mimo-v2.55",                     3.00),
    ("kimi-k2.66",                     3.00),
    ("minimax-m2.77",                  3.00),
    ("deepseekXv4Yflash",              3.00),     # bare-`.` no longer permissive
])
def test_pricing_regex_boundaries_v1_41_0(model, expected_input_rate):
    """v1.41.0: regex over-match guards (P0-B). Numeric-suffix families
    carry (?!\\d) so one-extra-digit IDs fall through; separator class is
    [-_/.] so non-separator letters can't satisfy the family pattern."""
    rates = sm._pricing_for(model)
    assert rates["input"] == expected_input_rate, (
        f"{model}: expected input rate {expected_input_rate}, "
        f"got {rates['input']} from {rates}"
    )
