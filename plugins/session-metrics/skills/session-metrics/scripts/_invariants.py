"""CI-friendly metric-contract checks for session-metrics reports.

Sourced from cognitive-claude's ``cost-audit.py --invariants`` framing —
a small predicate set that returns a non-zero exit code when one of the
operational-discipline contracts is violated. Designed to be wired into
a pre-commit / CI step so cost or cache regressions surface as build
failures, not just as numbers buried in an HTML dashboard.

Defaults are conservative; per-predicate threshold overrides land via
CLI flags (``--invariants-cache-hit-min``, etc.). Set a threshold to a
sentinel (negative for ``min`` predicates, ``0`` for ``max`` predicates)
to skip an individual check without disabling the whole suite.

Exit code 4 is reserved for "invariant violation" so callers can
distinguish it from generic CLI failures (1) or argparse errors (2).
"""
from __future__ import annotations


_INVARIANT_EXIT_CODE = 4


def _default_thresholds() -> dict:
    """Hard-coded defaults. Overridable per-predicate via CLI flags."""
    return {
        "cache_hit_min":            90.0,    # %, cache_read / total_input
        "cost_per_turn_max":         0.50,   # USD per turn
        "subagent_turn_share_min":   0.0,    # % — disabled by default since
                                              # not every project is sub-agent
                                              # heavy; opt in by passing > 0
        "cache_1h_share_max":       50.0,    # % of cache_write at 1h tier
        "tool_calls_per_turn_max":   5.0,    # avg tool calls per turn
    }


def _run_invariants(report: dict, thresholds: dict | None = None) -> list[dict]:
    """Evaluate every enabled predicate against ``report``.

    Returns a list of result dicts, one per predicate that ran. Each has::

        name        — short identifier
        comparator  — '>=' or '<='
        threshold   — the configured limit
        actual      — the measured value
        passed      — bool
        message     — single-line human-readable summary

    Predicates with a sentinel threshold (negative for `_min`, zero or
    negative for `_max`) are skipped. Empty input or zero turns yield no
    results — invariant checks against zero data would be misleading.
    """
    th = {**_default_thresholds(), **(thresholds or {})}
    totals = report.get("totals") or {}
    turns = int(totals.get("turns", 0) or 0)
    out: list[dict] = []
    if turns <= 0:
        return out

    # 1. Cache hit ratio floor.
    if th["cache_hit_min"] >= 0:
        actual = float(totals.get("cache_hit_pct", 0.0) or 0.0)
        passed = actual >= th["cache_hit_min"]
        out.append({
            "name":       "cache-hit-min",
            "comparator": ">=",
            "threshold":  th["cache_hit_min"],
            "actual":     actual,
            "passed":     passed,
            "message": (
                f"cache_hit_pct={actual:.2f}% "
                f"{'>=' if passed else '<'} threshold={th['cache_hit_min']:.2f}%"
            ),
        })

    # 2. Cost per turn ceiling.
    if th["cost_per_turn_max"] > 0:
        cost = float(totals.get("cost", 0.0) or 0.0)
        actual = cost / turns
        passed = actual <= th["cost_per_turn_max"]
        out.append({
            "name":       "cost-per-turn-max",
            "comparator": "<=",
            "threshold":  th["cost_per_turn_max"],
            "actual":     actual,
            "passed":     passed,
            "message": (
                f"cost/turn=${actual:.4f} "
                f"{'<=' if passed else '>'} threshold=${th['cost_per_turn_max']:.4f}"
            ),
        })

    # 3. Sub-agent turn share floor (count basis).
    if th["subagent_turn_share_min"] > 0:
        share = (report.get("subagent_share_stats") or {})
        actual = float(share.get("turn_share_pct", 0.0) or 0.0)
        passed = actual >= th["subagent_turn_share_min"]
        out.append({
            "name":       "subagent-turn-share-min",
            "comparator": ">=",
            "threshold":  th["subagent_turn_share_min"],
            "actual":     actual,
            "passed":     passed,
            "message": (
                f"subagent_turn_share={actual:.2f}% "
                f"{'>=' if passed else '<'} threshold={th['subagent_turn_share_min']:.2f}%"
            ),
        })

    # 4. Share of cache writes at the 1h TTL tier (more expensive than 5m).
    #    High share without explanation usually signals an unintended default.
    if th["cache_1h_share_max"] > 0:
        cw_5m = int(totals.get("cache_write_5m", 0) or 0)
        cw_1h = int(totals.get("cache_write_1h", 0) or 0)
        cw_tot = cw_5m + cw_1h
        if cw_tot > 0:
            actual = 100.0 * cw_1h / cw_tot
            passed = actual <= th["cache_1h_share_max"]
            out.append({
                "name":       "cache-1h-share-max",
                "comparator": "<=",
                "threshold":  th["cache_1h_share_max"],
                "actual":     actual,
                "passed":     passed,
                "message": (
                    f"cache_write_1h_share={actual:.2f}% "
                    f"{'<=' if passed else '>'} threshold={th['cache_1h_share_max']:.2f}%"
                ),
            })

    # 5. Average tool calls per turn ceiling — guards against tool-storm
    #    sessions where a single prompt fans out into dozens of edits/reads.
    if th["tool_calls_per_turn_max"] > 0:
        actual = float(totals.get("tool_call_avg_per_turn", 0.0) or 0.0)
        passed = actual <= th["tool_calls_per_turn_max"]
        out.append({
            "name":       "tool-calls-per-turn-max",
            "comparator": "<=",
            "threshold":  th["tool_calls_per_turn_max"],
            "actual":     actual,
            "passed":     passed,
            "message": (
                f"tool_calls_avg/turn={actual:.2f} "
                f"{'<=' if passed else '>'} threshold={th['tool_calls_per_turn_max']:.2f}"
            ),
        })
    return out


def _format_invariant_results(results: list[dict]) -> str:
    """Render results as a multi-line text block for stderr."""
    if not results:
        return "[invariants] no predicates ran (empty report or all skipped)."
    lines = ["[invariants] predicate results:"]
    for r in results:
        flag = "PASS" if r["passed"] else "FAIL"
        lines.append(f"  [{flag}] {r['name']}: {r['message']}")
    failed = [r for r in results if not r["passed"]]
    if failed:
        lines.append(
            f"[invariants] {len(failed)} of {len(results)} predicate"
            f"{'s' if len(results) != 1 else ''} failed."
        )
    else:
        lines.append(
            f"[invariants] all {len(results)} predicate"
            f"{'s' if len(results) != 1 else ''} passed."
        )
    return "\n".join(lines)


def _invariants_exit_code(results: list[dict]) -> int:
    """Return ``_INVARIANT_EXIT_CODE`` if any predicate failed, else 0."""
    if not results:
        return 0
    return _INVARIANT_EXIT_CODE if any(not r["passed"] for r in results) else 0
