#!/usr/bin/env python3
"""Pre-compute audit triggers and metrics from a session-metrics JSON export.

The audit-session-metrics skill calls this script once with the export path
and consumes the digest from stdout. That replaces multiple Bash exploration
roundtrips during a Haiku audit turn.

Usage:
    python3 audit-extract.py <path-to-session-metrics.json> [--mode quick|detailed]

Output: a single JSON object on stdout containing baseline metrics, fired
triggers (with suggested severity + estimated impact where computable),
top-3 expensive turns (with cross-finding correlation flags), and — in
detailed mode — pre-computed scans (file re-reads, paste-bombs, wrong-model
turns, verbose responses, weekly rollup deltas, subagent orphans).

The digest schema is consumed by the audit-session-metrics playbook's
markdown render step. Bumping the digest shape is a coordinated change
across this script, references/quick-audit.md, references/detailed-audit.md,
and the test fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any

# Opus 4.7 input rate per 1M tokens — used for cache_break impact estimates.
# Kept as a constant rather than read from references/pricing.md to keep this
# script standalone.
OPUS_INPUT_RATE_PER_M = 5.00

DIGEST_SCHEMA_VERSION = "1.0"


def session_filename_parts(path: str) -> tuple[str, str]:
    """Return (id8, ts_str) parsed from a session-metrics export filename.

    Recognises:
      session_<id8>_<YYYYMMDD>T<HHMMSS>Z.json   (current)
      session_<id8>_<YYYYMMDD_HHMMSS>.json      (legacy)
    Falls back to splitting the stem on '_'."""
    name = os.path.basename(path)
    m = re.match(r"^session_([0-9a-f]{8})_(\d{8}T\d{6}Z)\.json$", name)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^session_([0-9a-f]{8})_(\d{8}_\d{6})\.json$", name)
    if m:
        return m.group(1), m.group(2)
    stem = os.path.splitext(name)[0]
    parts = stem.split("_")
    if len(parts) >= 3:
        return parts[1], "_".join(parts[2:])
    return "unknown", "unknown"


def flatten_turns(data: dict) -> list[dict]:
    return [t for s in data.get("sessions", []) for t in s.get("turns", [])]


def compute_baseline(data: dict) -> dict:
    totals = data.get("totals", {})
    output = totals.get("output", 0)
    uncached_input = totals.get("total_input", 0) - totals.get("cache_read", 0)
    ratio = int(round(uncached_input / output)) if output else 0
    return {
        "total_cost_usd": round(totals.get("cost", 0), 2),
        "turns": totals.get("turns", 0),
        "models": data.get("models", {}),
        "input_output_ratio": ratio,
        "cache_hit_pct": round(totals.get("cache_hit_pct", 0), 1),
        "cache_savings_usd": round(totals.get("cache_savings", 0), 2),
        "no_cache_cost_usd": round(totals.get("no_cache_cost", 0), 2),
    }


def _safe_div_pct(num: float, denom: float) -> float:
    return (num / denom) * 100 if denom else 0.0


def evaluate_triggers(data: dict, turns: list[dict]) -> list[dict]:
    """Evaluate every metric in the audit enum. Return only fired triggers
    with evidence + suggested severity (downgrade_reason populated when
    the data is milder than the trigger threshold suggests) + estimated
    impact in USD where it can be computed honestly."""
    fired: list[dict] = []
    totals = data.get("totals", {})
    cache_breaks = data.get("cache_breaks", []) or []
    cost = totals.get("cost", 0) or 0.0

    # cache_break — any cache_breaks entry
    if cache_breaks:
        n = len(cache_breaks)
        total_uncached = sum(cb.get("uncached", 0) for cb in cache_breaks)
        impact = round(total_uncached * OPUS_INPUT_RATE_PER_M / 1_000_000, 2)
        break_pct = (n / max(totals.get("turns", 1), 1)) * 100
        suggested = "medium"
        downgrade_reason = None
        if break_pct < 2 and n <= 2:
            suggested = "low"
            downgrade_reason = (
                f"{n} break(s) in {totals.get('turns', 0)} turns ({break_pct:.1f}%) — "
                "below typical concern threshold"
            )
        fired.append({
            "metric": "cache_break",
            "default_severity": "medium",
            "suggested_severity": suggested,
            "downgrade_reason": downgrade_reason,
            "evidence": {
                "turn_index": cache_breaks[0].get("turn_index"),
                "uncached_tokens": cache_breaks[0].get("uncached"),
                "count": n,
            },
            "estimated_impact_usd": impact,
            "impact_basis": (
                f"{total_uncached:,} uncached tokens × ${OPUS_INPUT_RATE_PER_M:.2f}/M (Opus input rate)"
            ),
        })

    # top_turn_share — top single turn > 30% of cost
    if turns and cost > 0:
        top = max(turns, key=lambda t: t.get("cost_usd", 0) or 0)
        top_cost = top.get("cost_usd", 0) or 0
        top_pct = _safe_div_pct(top_cost, cost)
        if top_pct > 30:
            fired.append({
                "metric": "top_turn_share",
                "default_severity": "high",
                "suggested_severity": "high",
                "downgrade_reason": None,
                "evidence": {
                    "turn_index": top.get("index"),
                    "cost_usd": round(top_cost, 4),
                    "pct_of_total": round(top_pct, 1),
                    "slash_command": top.get("slash_command") or None,
                    "prompt_excerpt": (top.get("prompt_text") or "")[:80] or None,
                },
                "estimated_impact_usd": None,
                "impact_basis": "n/a — already-realised cost, not a recoverable saving",
            })

    # input_output_ratio_uncached — ratio > 50:1 AND cache hit < 60%
    output = totals.get("output", 0)
    uncached_input = totals.get("total_input", 0) - totals.get("cache_read", 0)
    ratio = (uncached_input / output) if output else 0
    cache_hit = totals.get("cache_hit_pct", 0) or 0
    if ratio > 50 and cache_hit < 60:
        fired.append({
            "metric": "input_output_ratio_uncached",
            "default_severity": "high",
            "suggested_severity": "high",
            "downgrade_reason": None,
            "evidence": {
                "ratio": int(round(ratio)),
                "cache_hit_pct": round(cache_hit, 1),
                "total_input": totals.get("total_input", 0),
                "output": output,
            },
            "estimated_impact_usd": None,
            "impact_basis": (
                "savings depend on whether the bloat is reusable across turns; "
                "skill cannot estimate without re-running with prompt caching applied"
            ),
        })

    # subagent_share — subagent_share_stats.share_pct > 50
    sub = data.get("subagent_share_stats", {}) or {}
    sub_share = sub.get("share_pct", 0) or 0
    if sub_share > 50:
        fired.append({
            "metric": "subagent_share",
            "default_severity": "medium",
            "suggested_severity": "medium",
            "downgrade_reason": None,
            "evidence": {
                "share_pct": round(sub_share, 1),
                "total_cost_usd": round(sub.get("total_cost", 0), 2),
                "attributed_cost_usd": round(sub.get("attributed_cost", 0), 2),
            },
            "estimated_impact_usd": round(sub.get("attributed_cost", 0), 2),
            "impact_basis": "subagent_share_stats.attributed_cost (already realised)",
        })

    # cache_ttl_1h_unused — extra_1h_cost > 0 AND cache_read < 50% of cache_write_1h
    extra_1h = totals.get("extra_1h_cost", 0) or 0
    cache_write_1h = totals.get("cache_write_1h", 0) or 0
    cache_read = totals.get("cache_read", 0) or 0
    if extra_1h > 0 and cache_write_1h > 0 and cache_read < (0.5 * cache_write_1h):
        fired.append({
            "metric": "cache_ttl_1h_unused",
            "default_severity": "medium",
            "suggested_severity": "medium",
            "downgrade_reason": None,
            "evidence": {
                "extra_1h_cost_usd": round(extra_1h, 2),
                "cache_write_1h": cache_write_1h,
                "cache_read": cache_read,
            },
            "estimated_impact_usd": round(extra_1h, 2),
            "impact_basis": "totals.extra_1h_cost (1h-tier surcharge over 5m baseline)",
        })

    # session_warmup_overhead — first turn > 20% of cost AND turns <= 15
    if turns and len(turns) <= 15 and cost > 0:
        first = turns[0]
        first_cost = first.get("cost_usd", 0) or 0
        first_pct = _safe_div_pct(first_cost, cost)
        if first_pct > 20:
            fired.append({
                "metric": "session_warmup_overhead",
                "default_severity": "medium",
                "suggested_severity": "medium",
                "downgrade_reason": None,
                "evidence": {
                    "first_turn_cost_usd": round(first_cost, 2),
                    "total_cost_usd": round(cost, 2),
                    "pct_of_total": round(first_pct, 1),
                    "total_turns": len(turns),
                },
                "estimated_impact_usd": None,
                "impact_basis": "n/a — short-session warmup, no direct savings figure",
            })

    # tool_result_bloat — turn with cache_write > 50K right after Bash/Read/WebFetch
    bloat: list[dict] = []
    for i in range(len(turns) - 1):
        prior = turns[i]
        nxt = turns[i + 1]
        prior_tools = prior.get("tool_use_names", []) or []
        match = next((t for t in ("Bash", "Read", "WebFetch") if t in prior_tools), None)
        cw = nxt.get("cache_write_tokens", 0) or 0
        if match and cw > 50_000:
            bloat.append({
                "turn_index": nxt.get("index"),
                "prior_turn_index": prior.get("index"),
                "prior_tool": match,
                "cache_write_tokens": cw,
            })
    if bloat:
        bloat.sort(key=lambda b: b["cache_write_tokens"], reverse=True)
        fired.append({
            "metric": "tool_result_bloat",
            "default_severity": "medium",
            "suggested_severity": "medium",
            "downgrade_reason": None,
            "evidence": {
                "turn_index": bloat[0]["turn_index"],
                "prior_turn_index": bloat[0]["prior_turn_index"],
                "prior_tool": bloat[0]["prior_tool"],
                "cache_write_tokens": bloat[0]["cache_write_tokens"],
                "examples": bloat[:3],
            },
            "estimated_impact_usd": None,
            "impact_basis": "savings depend on cache reuse across subsequent turns",
        })

    # heavy_reader_tools — Read or WebFetch in tool_names_top3
    top3 = totals.get("tool_names_top3", []) or []
    if any(t in top3 for t in ("Read", "WebFetch")):
        fired.append({
            "metric": "heavy_reader_tools",
            "default_severity": "low",
            "suggested_severity": "low",
            "downgrade_reason": None,
            "evidence": {
                "tool_names_top3": top3,
                "tool_call_total": totals.get("tool_call_total", 0),
            },
            "estimated_impact_usd": None,
            "impact_basis": "n/a — informational",
        })

    # cache_savings_low — cache_savings < 10% of cost
    cache_savings = totals.get("cache_savings", 0) or 0
    cache_save_pct = _safe_div_pct(cache_savings, cost)
    if cost > 0 and cache_save_pct < 10:
        fired.append({
            "metric": "cache_savings_low",
            "default_severity": "low",
            "suggested_severity": "low",
            "downgrade_reason": None,
            "evidence": {
                "cache_savings_usd": round(cache_savings, 2),
                "cost_usd": round(cost, 2),
                "pct": round(cache_save_pct, 1),
            },
            "estimated_impact_usd": None,
            "impact_basis": "potential savings depend on user's prompt-reuse pattern",
        })

    # thinking_engagement_high — thinking_turn_pct > 30
    thinking_pct = totals.get("thinking_turn_pct", 0) or 0
    if thinking_pct > 30:
        fired.append({
            "metric": "thinking_engagement_high",
            "default_severity": "low",
            "suggested_severity": "low",
            "downgrade_reason": None,
            "evidence": {
                "thinking_turn_pct": round(thinking_pct, 1),
                "thinking_turn_count": totals.get("thinking_turn_count", 0),
                "total_turns": totals.get("turns", 0),
            },
            "estimated_impact_usd": None,
            "impact_basis": "thinking tokens billed at output rate; savings depend on user's tolerance for shallower reasoning",
        })

    # truncated_outputs — any turn with stop_reason="max_tokens"
    truncated = [t for t in turns if t.get("stop_reason") == "max_tokens"]
    if truncated:
        fired.append({
            "metric": "truncated_outputs",
            "default_severity": "low",
            "suggested_severity": "low",
            "downgrade_reason": None,
            "evidence": {
                "truncated_count": len(truncated),
                "turn_indices": [t.get("index") for t in truncated[:5]],
            },
            "estimated_impact_usd": None,
            "impact_basis": "n/a — quality issue, not a cost issue",
        })

    # advisor_share — advisor_cost_usd > 5% of cost
    advisor_cost = totals.get("advisor_cost_usd", 0) or 0
    advisor_pct = _safe_div_pct(advisor_cost, cost)
    if totals.get("advisor_call_count", 0) > 0 and advisor_pct >= 5:
        # Resolve the model from the first advisor turn
        advisor_model = None
        for t in turns:
            if (t.get("advisor_calls") or 0) > 0:
                advisor_model = t.get("advisor_model")
                break
        fired.append({
            "metric": "advisor_share",
            "default_severity": "low",
            "suggested_severity": "low",
            "downgrade_reason": None,
            "evidence": {
                "advisor_call_count": totals.get("advisor_call_count", 0),
                "advisor_cost_usd": round(advisor_cost, 2),
                "pct_of_total": round(advisor_pct, 1),
                "advisor_model": advisor_model,
            },
            "estimated_impact_usd": round(advisor_cost, 2),
            "impact_basis": "totals.advisor_cost_usd (already realised)",
        })

    return fired


def top_expensive_turns(turns: list[dict], cache_breaks: list[dict]) -> list[dict]:
    """Return the 3 most expensive turns with hypothesis + cross-finding flags."""
    cb_indices = {cb.get("turn_index") for cb in cache_breaks}
    top = sorted(turns, key=lambda t: t.get("cost_usd", 0) or 0, reverse=True)[:3]
    out = []
    for t in top:
        idx = t.get("index")
        cost = t.get("cost_usd", 0) or 0
        slash = t.get("slash_command") or ""
        prompt = t.get("prompt_text") or ""
        if slash:
            label = slash[:80]
        elif prompt:
            label = prompt[:80].replace("\n", " ")
        else:
            label = "(no prompt text — tool-result follow-up)"

        cw = t.get("cache_write_tokens", 0) or 0
        cr = t.get("cache_read_tokens", 0) or 0
        out_tok = t.get("output_tokens", 0) or 0
        attr_sub = t.get("attributed_subagent_cost", 0) or 0
        tool_names = t.get("tool_use_names", []) or []
        model = (t.get("model") or "").lower()

        if "Read" in tool_names and cw > 50_000:
            hypothesis = f"large file Read baked into cache ({cw // 1000}K cw)"
        elif len(prompt) > 5000:
            hypothesis = f"paste-bomb prompt (~{len(prompt) // 1024} KB)"
        elif "opus" in model and 0 < cost < 0.05:
            hypothesis = "Opus on a trivial-looking task"
        elif attr_sub > cost > 0:
            hypothesis = "expensive subagent spawned from a small prompt"
        elif cw > max(cr, 100_000):
            hypothesis = f"cache-write heavy ({cw // 1000}K cw)"
        elif cr > 500_000 and cr > out_tok * 100:
            hypothesis = f"cache-read heavy ({cr // 1000}K cr)"
        else:
            hypothesis = "output-heavy"

        out.append({
            "turn_index": idx,
            "cost_usd": round(cost, 4),
            "label": label,
            "hypothesis": hypothesis,
            "is_cache_break": idx in cb_indices,
            "drivers": {
                "input_tokens": t.get("input_tokens", 0) or 0,
                "output_tokens": out_tok,
                "cache_read_tokens": cr,
                "cache_write_tokens": cw,
                "attributed_subagent_cost_usd": round(attr_sub, 4) if attr_sub else 0,
            },
        })
    return out


def detailed_candidates(data: dict, turns: list[dict]) -> dict:
    """Pre-compute scans that detailed mode needs but quick mode skips."""
    # File re-reads (>2 reads of same path)
    read_counts: Counter[str] = Counter()
    read_indices: dict[str, list[int]] = {}
    for t in turns:
        for d in t.get("tool_use_detail", []) or []:
            if d.get("name") == "Read":
                fp = (d.get("input") or {}).get("file_path")
                if fp:
                    read_counts[fp] += 1
                    read_indices.setdefault(fp, []).append(t.get("index"))
    re_reads = sorted(
        ({"file_path": p, "read_count": c, "turn_indices": read_indices[p][:5]}
         for p, c in read_counts.items() if c > 2),
        key=lambda r: r["read_count"], reverse=True,
    )

    # Paste bombs: prompt_text > 5000 chars
    paste_bombs = []
    for t in turns:
        pt = t.get("prompt_text") or ""
        if len(pt) > 5000:
            paste_bombs.append({
                "turn_index": t.get("index"),
                "chars": len(pt),
                "excerpt": pt[:80].replace("\n", " "),
            })

    # Wrong-model turns: Opus on trivial work
    wrong_model = []
    for t in turns:
        model = (t.get("model") or "").lower()
        c = t.get("cost_usd", 0) or 0
        if "opus" in model and 0 < c < 0.05:
            wrong_model.append({
                "turn_index": t.get("index"),
                "cost_usd": round(c, 4),
                "prompt_excerpt": (t.get("prompt_text") or "")[:80].replace("\n", " "),
            })

    # Subagent dominant parents
    sub_dominant = []
    for t in turns:
        parent = t.get("cost_usd", 0) or 0
        sub = t.get("attributed_subagent_cost", 0) or 0
        if parent > 0 and sub > 5 * parent:
            sub_dominant.append({
                "turn_index": t.get("index"),
                "parent_cost_usd": round(parent, 4),
                "subagent_cost_usd": round(sub, 2),
                "ratio": round(sub / parent, 1),
            })

    # Verbose response: output_tokens / (input_tokens + cache_read_tokens) > 5
    # Use total billable input as denominator so cache-heavy sessions don't
    # inflate the ratio artificially (uncached `input_tokens` alone is
    # misleading once cache hit > 50%).
    verbose_count = 0
    sampled = 0
    samples = []
    for t in turns:
        ip = (t.get("input_tokens", 0) or 0) + (t.get("cache_read_tokens", 0) or 0)
        op = t.get("output_tokens", 0) or 0
        if ip > 0:
            sampled += 1
            ratio = op / ip
            if ratio > 5:
                verbose_count += 1
                if len(samples) < 3:
                    samples.append({"turn_index": t.get("index"), "ratio": round(ratio, 2)})
    verbose_pct = round(verbose_count / sampled * 100, 1) if sampled >= 10 else None

    # Weekly rollup deltas — only meaningful with two weeks of data.
    weekly = data.get("weekly_rollup") or {}
    weekly_summary = None
    if weekly.get("has_data"):
        trail = weekly.get("trailing_7d", {}) or {}
        prior = weekly.get("prior_7d", {}) or {}
        prior_cost = prior.get("cost", 0) or 0
        trail_cost = trail.get("cost", 0) or 0
        # Suppress entirely when prior_7d has no usage — first-week-of-data
        # case where any "delta" is meaningless.
        if prior_cost > 0:
            cost_delta_pct = round((trail_cost - prior_cost) / prior_cost * 100, 1)
            cache_delta = round(trail.get("cache_hit_pct", 0) - prior.get("cache_hit_pct", 0), 1)
            weekly_summary = {
                "trailing_7d_cost_usd": round(trail_cost, 2),
                "prior_7d_cost_usd": round(prior_cost, 2),
                "cost_delta_pct": cost_delta_pct,
                "trailing_7d_cache_hit_pct": round(trail.get("cache_hit_pct", 0), 1),
                "prior_7d_cache_hit_pct": round(prior.get("cache_hit_pct", 0), 1),
                "cache_hit_delta_pp": cache_delta,
            }

    # Subagent attribution orphans
    sub_summary = data.get("subagent_attribution_summary") or {}
    orphan_summary = None
    if sub_summary.get("orphan_subagent_turns", 0) > 0:
        orphan_summary = {
            "orphan_turns": sub_summary["orphan_subagent_turns"],
            "attributed_turns": sub_summary.get("attributed_turns", 0),
            "nested_levels_seen": sub_summary.get("nested_levels_seen", 0),
            "cycles_detected": sub_summary.get("cycles_detected", 0),
        }

    return {
        "file_re_reads": re_reads[:10],
        "paste_bombs": paste_bombs[:5],
        "wrong_model_turns": wrong_model[:5],
        "subagent_dominant_parents": sub_dominant[:5],
        "verbose_response": {
            "pct_of_turns": verbose_pct,
            "total_turns_sampled": sampled,
            "samples": samples,
        },
        "weekly_rollup": weekly_summary,
        "subagent_orphan": orphan_summary,
    }


def build_digest(data: dict, json_path: str, mode: str) -> dict:
    turns = flatten_turns(data)
    cache_breaks = data.get("cache_breaks", []) or []
    id8, ts = session_filename_parts(json_path)
    digest: dict[str, Any] = {
        "digest_schema_version": DIGEST_SCHEMA_VERSION,
        "session_id_short": id8,
        "ts_str": ts,
        "input_json": os.path.abspath(json_path),
        "mode_hint": mode,
        "baseline": compute_baseline(data),
        "fired_triggers": evaluate_triggers(data, turns),
        "top_expensive_turns": top_expensive_turns(turns, cache_breaks),
    }
    if mode == "detailed":
        digest["detailed_candidates"] = detailed_candidates(data, turns)
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-compute audit triggers + metrics from a session-metrics JSON export.")
    parser.add_argument("json_path", help="Path to a session-metrics JSON export.")
    parser.add_argument(
        "--mode", choices=["quick", "detailed"], default="quick",
        help="Audit mode the digest is being built for. Detailed mode adds re-read / "
             "paste-bomb / wrong-model / weekly-delta / orphan scans.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.json_path):
        print(json.dumps({"error": f"file not found: {args.json_path}"}), file=sys.stderr)
        return 2

    with open(args.json_path, encoding="utf-8") as f:
        data = json.load(f)

    digest = build_digest(data, args.json_path, args.mode)
    json.dump(digest, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
