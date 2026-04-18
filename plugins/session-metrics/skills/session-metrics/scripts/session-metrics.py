#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
session-metrics.py — Claude Code session cost estimator

Reads the JSONL conversation log and produces a timeline-ordered table of
per-turn token usage and estimated USD cost.

Usage:
  uv run python session-metrics.py                        # auto-detect from cwd
  uv run python session-metrics.py --session <uuid>       # specific session
  uv run python session-metrics.py --slug <slug>          # specific project slug
  uv run python session-metrics.py --list                 # list sessions for project
  uv run python session-metrics.py --project-cost         # all sessions, timeline + totals
  uv run python session-metrics.py --output json html     # export to exports/session-metrics/
  uv run python session-metrics.py --include-subagents    # include spawned agents

--output accepts one or more of: text json csv md html
  Writes to <cwd>/exports/session-metrics/<name>_<timestamp>.<ext>
  Text is always printed to stdout; other formats are written to files.

Environment variables (all optional — CLI flags take precedence):
  CLAUDE_SESSION_ID       Session UUID to analyse
  CLAUDE_PROJECT_SLUG     Project slug override (e.g. -Volumes-foo-bar-project)
  CLAUDE_PROJECTS_DIR     Override ~/.claude/projects (default: ~/.claude/projects)
"""

import argparse
import csv as csv_mod
import gzip
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Bump when the parsed-entries shape changes — invalidates old parse caches.
_SCRIPT_VERSION = "1.0-rc.4"

# ---------------------------------------------------------------------------
# Pricing table  (USD per million tokens)
# See references/pricing.md for notes and source.
# ---------------------------------------------------------------------------
# Per-million-token rates (USD). Source: https://platform.claude.com/docs/en/about-claude/pricing
# Snapshot: 2026-04-17. Two cache-write tiers: `cache_write` = 5-minute TTL
# (1.25x base input), `cache_write_1h` = 1-hour TTL (2x base input). The
# per-entry split is read from `usage.cache_creation.ephemeral_{5m,1h}_input_tokens`
# when present; legacy transcripts without the nested object fall back to the
# 5-minute rate via `_cost`.
#
# IMPORTANT: Opus 4.5 / 4.6 / 4.7 use the NEW cheaper tier ($5/$25) introduced
# with the 4.5 generation. Opus 4 / 4.1 retain the OLD tier ($15/$75). Dict
# order matters for prefix fallback — more-specific entries must appear first.
_PRICING: dict[str, dict[str, float]] = {
    # --- Opus 4.5-generation (new tier: $5 input / $25 output) ---
    "claude-opus-4-7":           {"input":  5.00, "output": 25.00, "cache_read": 0.50,  "cache_write":  6.25, "cache_write_1h": 10.00},
    "claude-opus-4-6":           {"input":  5.00, "output": 25.00, "cache_read": 0.50,  "cache_write":  6.25, "cache_write_1h": 10.00},
    "claude-opus-4-5":           {"input":  5.00, "output": 25.00, "cache_read": 0.50,  "cache_write":  6.25, "cache_write_1h": 10.00},
    # --- Opus 4 / 4.1 (old tier, retained for historical sessions) ---
    "claude-opus-4-1":           {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75, "cache_write_1h": 30.00},
    "claude-opus-4":             {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75, "cache_write_1h": 30.00},
    # --- Sonnet 4.x + 3.7 (shared rates) ---
    "claude-sonnet-4-7":         {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    "claude-sonnet-4-6":         {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    "claude-sonnet-4-5":         {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    "claude-sonnet-4":           {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    "claude-3-7-sonnet":         {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    "claude-3-5-sonnet":         {"input":  3.00, "output": 15.00, "cache_read": 0.30,  "cache_write":  3.75, "cache_write_1h":  6.00},
    # --- Haiku 4.5 (own tier: $1 input / $5 output) ---
    "claude-haiku-4-5-20251001": {"input":  1.00, "output":  5.00, "cache_read": 0.10,  "cache_write":  1.25, "cache_write_1h":  2.00},
    "claude-haiku-4-5":          {"input":  1.00, "output":  5.00, "cache_read": 0.10,  "cache_write":  1.25, "cache_write_1h":  2.00},
    # --- Haiku 3.5 (older, cheaper input) ---
    "claude-3-5-haiku":          {"input":  0.80, "output":  4.00, "cache_read": 0.08,  "cache_write":  1.00, "cache_write_1h":  1.60},
    # --- Opus 3 (deprecated; old-tier rates) ---
    "claude-3-opus":             {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75, "cache_write_1h": 30.00},
}
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00}


def _pricing_for(model: str) -> dict[str, float]:
    if model in _PRICING:
        return _PRICING[model]
    for prefix, rates in _PRICING.items():
        if model.startswith(prefix):
            return rates
    return _DEFAULT_PRICING


def _cache_write_split(u: dict) -> tuple[int, int]:
    """Return ``(tokens_5m, tokens_1h)`` for the cache write on this turn.

    Reads ``usage.cache_creation.ephemeral_{5m,1h}_input_tokens`` when the
    nested object is present. Legacy transcripts without ``cache_creation``
    fall back to treating the flat ``cache_creation_input_tokens`` total as
    5-minute-tier tokens — preserving pre-v1.2.0 cost math for those files.
    """
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        return (
            int(cc.get("ephemeral_5m_input_tokens", 0) or 0),
            int(cc.get("ephemeral_1h_input_tokens", 0) or 0),
        )
    return int(u.get("cache_creation_input_tokens", 0) or 0), 0


def _cost(u: dict, model: str) -> float:
    r = _pricing_for(model)
    tokens_5m, tokens_1h = _cache_write_split(u)
    return (
        u.get("input_tokens", 0)              * r["input"]           / 1_000_000
        + u.get("output_tokens", 0)           * r["output"]          / 1_000_000
        + u.get("cache_read_input_tokens", 0) * r["cache_read"]      / 1_000_000
        + tokens_5m                           * r["cache_write"]     / 1_000_000
        + tokens_1h                           * r["cache_write_1h"]  / 1_000_000
    )


def _no_cache_cost(u: dict, model: str) -> float:
    r = _pricing_for(model)
    total_input = (
        u.get("input_tokens", 0)
        + u.get("cache_read_input_tokens", 0)
        + u.get("cache_creation_input_tokens", 0)
    )
    return total_input * r["input"] / 1_000_000 + u.get("output_tokens", 0) * r["output"] / 1_000_000


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _parse_jsonl(path: Path) -> list[dict]:
    entries = []
    skipped = 0
    first_err: str | None = None
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                skipped += 1
                if first_err is None:
                    first_err = f"line {lineno}: {exc}"
    if skipped:
        suffix = f" (first: {first_err})" if first_err else ""
        print(f"[warn] {path.name}: {skipped} malformed line{'s' if skipped != 1 else ''} skipped{suffix}",
              file=sys.stderr)
    return entries


def _parse_cache_dir() -> Path:
    """Return the directory for serialized parse-cache blobs."""
    return Path.home() / ".cache" / "session-metrics" / "parse"


def _parse_cache_key(path: Path, mtime_ns: int) -> str:
    """Build a stable cache-key filename from path stem, mtime, and script ver.

    Using ``mtime_ns`` (nanoseconds since epoch) means a touched JSONL always
    invalidates the cache. Bumping ``_SCRIPT_VERSION`` invalidates every
    existing blob — safe default when the parser shape changes.
    """
    return f"{path.stem}__{mtime_ns}__{_SCRIPT_VERSION}.json.gz"


def _cached_parse_jsonl(path: Path, use_cache: bool = True) -> list[dict]:
    """Return parsed entries from ``path``, using a gzip-JSON cache on disk.

    Cache hit (typical re-run): ~10x faster than parsing JSONL line-by-line,
    since ``json.loads`` on one preassembled blob avoids the per-line state
    machine overhead. Cache miss or ``use_cache=False``: parse fresh and
    (if caching) write the blob for next time.

    Cache invalidation is automatic on (a) JSONL mtime change and
    (b) ``_SCRIPT_VERSION`` bump. On I/O errors the cache is silently
    skipped — correctness first, speed second.
    """
    if not use_cache:
        return _parse_jsonl(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return _parse_jsonl(path)

    cache_dir = _parse_cache_dir()
    cache_path = cache_dir / _parse_cache_key(path, mtime_ns)
    if cache_path.exists():
        try:
            with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            # Corrupt or unreadable — fall through to fresh parse.
            pass

    entries = _parse_jsonl(path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash mid-write doesn't leave a corrupt cache.
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(entries, fh, separators=(",", ":"))
        tmp.replace(cache_path)
    except OSError:
        # Non-fatal — the parse already succeeded.
        pass
    return entries


def _extract_turns(entries: list[dict]) -> list[dict]:
    """Deduplicate on message.id, keep last occurrence, sort by timestamp."""
    seen: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        if "usage" not in msg:
            continue
        msg_id = msg.get("id")
        if msg_id:
            seen[msg_id] = entry
    turns = list(seen.values())
    turns.sort(key=lambda e: e.get("timestamp", ""))
    return turns


# ---------------------------------------------------------------------------
# Time-of-day analysis
# ---------------------------------------------------------------------------

_TOD_PERIODS = (
    ("night",     0,  6),   # 00:00–05:59
    ("morning",   6, 12),   # 06:00–11:59
    ("afternoon", 12, 18),  # 12:00–17:59
    ("evening",   18, 24),  # 18:00–23:59
)


def _is_user_prompt(entry: dict) -> bool:
    """Return True for genuine user-typed prompts only.

    Claude Code's JSONL records three kinds of ``type == "user"`` entry:
    - real user messages typed by the human (what we want to count)
    - tool_result entries auto-generated after every tool call (inflates counts)
    - system-injected meta entries (``isMeta``)

    A user-typed message has ``message.content`` that is either a plain
    string, or a list containing at least one ``text`` or ``image`` block
    (never only ``tool_result`` blocks). Sampling real JSONLs showed both
    shapes in the wild; the original schema doc listed only the list shape.
    """
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta"):
        return False
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text" or t == "image":
                return True
        return False
    return False


def _extract_user_timestamps(
    entries: list[dict], include_sidechain: bool = False,
) -> list[int]:
    """Extract UTC epoch-seconds for every genuine user prompt.

    Uses ``_is_user_prompt`` to exclude tool_result and meta entries, which
    the original implementation wrongly counted as user activity. By default,
    also excludes ``isSidechain`` (subagent) entries; pass
    ``include_sidechain=True`` when the caller wants them folded in (matches
    the ``--include-subagents`` CLI flag).

    Returns:
        Sorted list of integer timestamps (seconds since Unix epoch, UTC).
        Malformed or missing timestamps are silently skipped.
    """
    timestamps: list[int] = []
    for entry in entries:
        if not _is_user_prompt(entry):
            continue
        if entry.get("isSidechain") and not include_sidechain:
            continue
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            timestamps.append(int(dt.timestamp()))
        except (ValueError, OSError):
            continue
    timestamps.sort()
    return timestamps


def _bucket_time_of_day(epoch_secs: list[int], offset_hours: float = 0) -> dict[str, int]:
    """Bucket UTC epoch-second timestamps into four time-of-day periods.

    Uses pure integer arithmetic for performance — no datetime objects are
    allocated in the hot loop.  Python's ``%`` operator always returns a
    non-negative result when the divisor is positive, so no extra guard is
    needed server-side (the JS counterpart uses a double-modulo idiom).

    Args:
        epoch_secs: Sorted list of UTC epoch-seconds (from
            ``_extract_user_timestamps``).
        offset_hours: UTC offset for the display timezone, e.g. ``-8`` for
            PT or ``10`` for Brisbane.  Accepts float for half-hour offsets
            (e.g. ``5.5`` for IST).

    Returns:
        Dict with keys ``night``, ``morning``, ``afternoon``, ``evening``,
        and ``total`` — each an integer count of user messages in that period.
    """
    offset_sec = int(offset_hours * 3600)
    counts = {key: 0 for key, _, _ in _TOD_PERIODS}
    for epoch in epoch_secs:
        local_hour = ((epoch + offset_sec) % 86400) // 3600
        for key, start, end in _TOD_PERIODS:
            if start <= local_hour < end:
                counts[key] += 1
                break
    counts["total"] = sum(counts[k] for k, _, _ in _TOD_PERIODS)
    return counts


def _build_hour_of_day(epoch_secs: list[int], offset_hours: float = 0.0) -> dict:
    """Build 24-bucket hour-of-day counts from user timestamps.

    Returns ``{"hours": [24 ints], "total": int, "offset_hours": float}``.
    ``hours[0]`` is 00:00-00:59 in the display tz; ``hours[23]`` is 23:00-23:59.
    """
    offset_sec = int(offset_hours * 3600)
    hours = [0] * 24
    for e in epoch_secs:
        h = ((e + offset_sec) % 86400) // 3600
        hours[h] += 1
    return {"hours": hours, "total": sum(hours), "offset_hours": offset_hours}


def _build_weekday_hour_matrix(epoch_secs: list[int], offset_hours: float = 0.0) -> dict:
    """Build a 7x24 weekday-by-hour activity matrix in the display tz.

    Row 0 is Monday (matches ``datetime.weekday()``); row 6 is Sunday.
    1970-01-01 was a Thursday (weekday=3), so a day count since the UTC
    epoch maps to weekday via ``(days + 3) % 7``. Python's floor-div gives
    correct day counts for negative operands, so a negative ``offset_hours``
    on a near-epoch timestamp still produces a valid weekday.
    """
    offset_sec = int(offset_hours * 3600)
    matrix = [[0] * 24 for _ in range(7)]
    for e in epoch_secs:
        local = e + offset_sec
        days = local // 86400
        weekday = (days + 3) % 7
        hour = (local % 86400) // 3600
        matrix[weekday][hour] += 1
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[r][h] for r in range(7)) for h in range(24)]
    return {
        "matrix":       matrix,
        "row_totals":   row_totals,
        "col_totals":   col_totals,
        "total":        sum(row_totals),
        "offset_hours": offset_hours,
    }


def _build_time_of_day(epoch_secs: list[int], offset_hours: float = 0.0) -> dict:
    """Build the ``time_of_day`` report section from user timestamps.

    Args:
        epoch_secs: Sorted UTC epoch-seconds for genuine user prompts.
        offset_hours: Display-timezone offset applied to the ``buckets``,
            ``hour_of_day``, and ``weekday_hour`` views (for static exports).
            The raw ``epoch_secs`` array is preserved so HTML client-side JS
            can re-bucket to any tz.

    Returns:
        Dict with ``epoch_secs``, ``message_count``, ``buckets`` (4-period),
        ``hour_of_day`` (24-bucket), ``weekday_hour`` (7x24 matrix), and
        ``offset_hours``.
    """
    return {
        "epoch_secs":    epoch_secs,
        "message_count": len(epoch_secs),
        "buckets":       _bucket_time_of_day(epoch_secs, offset_hours=offset_hours),
        "hour_of_day":   _build_hour_of_day(epoch_secs, offset_hours=offset_hours),
        "weekday_hour":  _build_weekday_hour_matrix(epoch_secs, offset_hours=offset_hours),
        "offset_hours":  offset_hours,
    }


# ---------------------------------------------------------------------------
# Timezone helpers (Step 5)
# ---------------------------------------------------------------------------

def _local_tz_offset() -> float:
    """Detect the system timezone offset in hours (float, supports :30/:45).

    Returns 0.0 on failure (e.g. no TZ info available).
    """
    try:
        delta = datetime.now().astimezone().utcoffset()
        if delta is None:
            return 0.0
        return delta.total_seconds() / 3600.0
    except Exception:
        return 0.0


def _local_tz_label() -> str:
    """Detect the system timezone IANA name, best-effort.

    Returns a string like ``"Australia/Brisbane"`` or falls back to a
    ``"UTC+10"``-style label if the name isn't available.
    """
    try:
        name = datetime.now().astimezone().tzname()
        if name:
            return name
    except Exception:
        pass
    off = _local_tz_offset()
    sign = "+" if off >= 0 else "-"
    return f"UTC{sign}{abs(off):g}"


def _parse_peak_hours(value: str) -> tuple[int, int]:
    """Parse ``--peak-hours "5-11"`` into ``(start, end)`` with end exclusive.

    Accepts ``H-H`` or ``HH-HH`` with 0 <= start <= 23 and 1 <= end <= 24.
    Wrap-around (end <= start) is rejected; split it across two flags if
    genuinely needed (rare case; keeping v1 simple).
    """
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", value or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid peak-hours {value!r} (expected H-H, e.g. '5-11')"
        )
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start < end <= 24):
        raise argparse.ArgumentTypeError(
            f"invalid peak-hours {value!r} (need 0 <= start < end <= 24)"
        )
    return (start, end)


def _build_peak(peak_hours: tuple[int, int] | None,
                peak_tz: str | None) -> dict | None:
    """Build a ``peak`` section from CLI inputs, resolving the peak tz offset.

    Returns None when ``peak_hours`` is not set. Defaults ``peak_tz`` to
    ``America/Los_Angeles`` (where the "peak hours" terminology originates
    in community reports) when only ``peak_hours`` is provided.
    """
    if peak_hours is None:
        return None
    tz_name = peak_tz or "America/Los_Angeles"
    try:
        zi = ZoneInfo(tz_name)
        delta = datetime.now(zi).utcoffset()
        off = delta.total_seconds() / 3600.0 if delta else 0.0
    except ZoneInfoNotFoundError:
        print(f"[warn] unknown peak-tz {tz_name!r}; using UTC",
              file=sys.stderr)
        off, tz_name = 0.0, "UTC"
    start, end = peak_hours
    return {
        "start":           start,
        "end":             end,
        "tz_offset_hours": off,
        "tz_label":        tz_name,
        "note":            "unofficial \u2014 community-reported",
    }


def _resolve_tz(tz_name: str | None, utc_offset: float | None) -> tuple[float, str]:
    """Resolve the display timezone from CLI/env inputs.

    Priority: ``tz_name`` (IANA, DST-aware) > ``utc_offset`` (fixed float) >
    local system tz.  Returns ``(offset_hours, label)``.

    Note: with an IANA name, the offset returned is the *current* offset —
    adequate for static exports but ``ZoneInfo`` must be used by the HTML
    client for per-event DST-aware bucketing across historical dates.
    """
    if tz_name:
        try:
            zi = ZoneInfo(tz_name)
            now = datetime.now(zi)
            delta = now.utcoffset()
            off = delta.total_seconds() / 3600.0 if delta else 0.0
            return off, tz_name
        except ZoneInfoNotFoundError:
            print(f"[warn] unknown tz {tz_name!r}; using UTC", file=sys.stderr)
            return 0.0, "UTC"
    if utc_offset is not None:
        sign = "+" if utc_offset >= 0 else "-"
        return utc_offset, f"UTC{sign}{abs(utc_offset):g}"
    return _local_tz_offset(), _local_tz_label()


# ---------------------------------------------------------------------------
# 5-hour session blocks (rate-limit debugging)
# ---------------------------------------------------------------------------

_BLOCK_WINDOW_SEC = 5 * 3600


def _parse_iso_epoch(ts: str) -> int:
    """Parse an ISO-8601 timestamp to UTC epoch seconds; 0 on failure."""
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError, OSError):
        return 0


def _build_session_blocks(
    sessions_raw: list[tuple[str, list[dict], list[int]]],
) -> list[dict]:
    """Group all events into 5-hour blocks anchored at each block's first event.

    A block starts when an event arrives more than 5 hours after the previous
    block's anchor.  Events are the union of filtered user prompts and
    assistant-turn timestamps across every session in the project — this
    matches what Anthropic's rate-limit window sees (users can ``/clear``
    mid-block and the window keeps running).

    Each block records: anchor and last timestamps, elapsed minutes, turn
    count, user-message count, per-bucket token totals, USD cost, model mix,
    and which session IDs touched the block.
    """
    events: list[tuple[int, str, str, dict | None]] = []
    for session_id, raw_turns, user_ts in sessions_raw:
        for u in user_ts:
            events.append((u, "user", session_id, None))
        for t in raw_turns:
            e = _parse_iso_epoch(t.get("timestamp", ""))
            if e:
                events.append((e, "turn", session_id, t))
    events.sort(key=lambda x: x[0])

    blocks: list[dict] = []
    for epoch, kind, sid, turn in events:
        if not blocks or (epoch - blocks[-1]["anchor_epoch"]) >= _BLOCK_WINDOW_SEC:
            blocks.append({
                "anchor_epoch":     epoch,
                "last_epoch":       epoch,
                "turn_count":       0,
                "user_msg_count":   0,
                "input":            0,
                "output":           0,
                "cache_read":       0,
                "cache_write":      0,
                "cost_usd":         0.0,
                "models":           {},
                "sessions_touched": set(),
            })
        b = blocks[-1]
        b["last_epoch"] = epoch
        b["sessions_touched"].add(sid)
        if kind == "user":
            b["user_msg_count"] += 1
        else:
            msg   = turn["message"]
            u     = msg["usage"]
            model = msg.get("model", "unknown")
            b["turn_count"]  += 1
            b["input"]       += u.get("input_tokens", 0)
            b["output"]      += u.get("output_tokens", 0)
            b["cache_read"]  += u.get("cache_read_input_tokens", 0)
            b["cache_write"] += u.get("cache_creation_input_tokens", 0)
            b["cost_usd"]    += _cost(u, model)
            b["models"][model] = b["models"].get(model, 0) + 1

    for b in blocks:
        b["sessions_touched"] = sorted(b["sessions_touched"])
        b["elapsed_min"]      = (b["last_epoch"] - b["anchor_epoch"]) / 60.0
        b["anchor_iso"]       = datetime.fromtimestamp(
            b["anchor_epoch"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        b["last_iso"]         = datetime.fromtimestamp(
            b["last_epoch"],   tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return blocks


def _build_weekly_rollup(
    sessions_out: list[dict],
    sessions_raw: list[tuple[str, list[dict], list[int]]],
    session_blocks: list[dict],
    now_epoch: int | None = None,
) -> dict:
    """Compare the trailing 7 days against the prior 7 days.

    Uses **deduped** assistant turns from ``sessions_out`` (match the report's
    cost/token totals) and filtered user prompts from ``sessions_raw``.
    Block counts use each block's anchor epoch — a block "belongs" to the
    window its first event lands in.

    Returns ``{"trailing_7d": {...}, "prior_7d": {...}, "has_data": bool,
    "now_epoch": int}``. When ``prior_7d`` has zero turns, callers should
    render deltas as "new period" rather than infinite percentage.
    """
    if now_epoch is None:
        now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
    cutoff7  = now_epoch - 7  * 86400
    cutoff14 = now_epoch - 14 * 86400

    user_ts_all = sorted(ts for _, _, uts in sessions_raw for ts in uts)
    turns_with_epoch: list[tuple[int, dict]] = []
    for s in sessions_out:
        for t in s["turns"]:
            e = _parse_iso_epoch(t.get("timestamp", ""))
            if e:
                turns_with_epoch.append((e, t))

    def bucket(start: int, end: int) -> dict:
        b = {
            "turns": 0, "user_prompts": 0, "cost": 0.0,
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "blocks": 0,
        }
        for u in user_ts_all:
            if start <= u < end:
                b["user_prompts"] += 1
        for e, t in turns_with_epoch:
            if start <= e < end:
                b["turns"]       += 1
                b["input"]       += t["input_tokens"]
                b["output"]      += t["output_tokens"]
                b["cache_read"]  += t["cache_read_tokens"]
                b["cache_write"] += t["cache_write_tokens"]
                b["cost"]        += t["cost_usd"]
        for blk in session_blocks:
            if start <= blk["anchor_epoch"] < end:
                b["blocks"] += 1
        total_in = b["input"] + b["cache_read"] + b["cache_write"]
        b["cache_hit_pct"] = 100 * b["cache_read"] / max(1, total_in)
        return b

    trailing = bucket(cutoff7, now_epoch)
    prior    = bucket(cutoff14, cutoff7)
    return {
        "now_epoch":   now_epoch,
        "trailing_7d": trailing,
        "prior_7d":    prior,
        "has_data":    (trailing["turns"] + prior["turns"]) > 0,
    }


def _weekly_block_counts(blocks: list[dict], now_epoch: int | None = None) -> dict:
    """Count blocks active (``last_epoch`` >= cutoff) in trailing windows.

    ``now_epoch`` is the upper bound for the window; defaults to current UTC.
    Returns counts for the trailing 7/14/30 days plus the grand total, which
    answers "am I tracking toward a weekly cap" at a glance.
    """
    if now_epoch is None:
        now_epoch = int(datetime.now(tz=timezone.utc).timestamp())

    def cnt(days: int) -> int:
        cutoff = now_epoch - days * 86400
        return sum(1 for b in blocks if b["last_epoch"] >= cutoff)

    return {
        "trailing_7":  cnt(7),
        "trailing_14": cnt(14),
        "trailing_30": cnt(30),
        "total":       len(blocks),
    }


# ---------------------------------------------------------------------------
# Session / project discovery
# ---------------------------------------------------------------------------

# Accept any non-empty filename-safe token, length <= 64.  Claude Code's
# identifier scheme may evolve — don't hard-code UUID format.
_SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
# Slug preserves the leading "-" Claude Code uses for cwd-derived paths.
_SLUG_RE    = re.compile(r'^-?[A-Za-z0-9_-]+$')


def _validate_session_id(value: str) -> str:
    if not _SESSION_RE.match(value or ""):
        raise argparse.ArgumentTypeError(
            f"invalid session id: {value!r} "
            f"(expected filename-safe token, got chars outside [A-Za-z0-9._-] or length > 64)"
        )
    return value


def _validate_slug(value: str) -> str:
    if not _SLUG_RE.match(value or ""):
        raise argparse.ArgumentTypeError(
            f"invalid project slug: {value!r} "
            f"(expected /-safe token matching {_SLUG_RE.pattern})"
        )
    return value


def _projects_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_dir():
            print(f"[error] CLAUDE_PROJECTS_DIR={env!r} is not a directory", file=sys.stderr)
            sys.exit(1)
        return p
    return Path.home() / ".claude" / "projects"


def _ensure_within_projects(path: Path) -> Path:
    """Resolve ``path`` and assert it lives under the projects directory.

    Catches path-traversal (``..``), symlink escapes, and absolute-path
    injection via the slug/session-id arguments.
    """
    root = _projects_dir().resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        print(f"[error] refusing to read outside {root}: {resolved}", file=sys.stderr)
        sys.exit(1)
    return resolved


def _cwd_to_slug(cwd: str | None = None) -> str:
    return (cwd or os.getcwd()).replace("/", "-")


def _find_jsonl_files(slug: str, include_subagents: bool = False) -> list[Path]:
    project_dir = _projects_dir() / slug
    if not project_dir.exists():
        return []
    files = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if include_subagents:
        files += list(project_dir.glob("*/subagents/*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _resolve_session(args) -> tuple[Path, str]:
    slug: str = args.slug or _env_slug() or _cwd_to_slug()
    _validate_slug(slug)
    session_id: str | None = args.session or _env_session_id()

    if session_id:
        candidate = _ensure_within_projects(_projects_dir() / slug / f"{session_id}.jsonl")
        if candidate.exists():
            return candidate, slug
        for p in _projects_dir().rglob(f"{session_id}.jsonl"):
            return _ensure_within_projects(p), p.parent.name
        print(f"[error] Session {session_id!r} not found", file=sys.stderr)
        sys.exit(1)

    files = _find_jsonl_files(slug)
    if not files:
        print(f"[error] No sessions found for slug: {slug}", file=sys.stderr)
        print(f"        Try --slug=<slug> or set CLAUDE_PROJECT_SLUG", file=sys.stderr)
        sys.exit(1)
    return files[0], slug


def _env_slug() -> str | None:
    v = os.environ.get("CLAUDE_PROJECT_SLUG")
    if v is None:
        return None
    try:
        return _validate_slug(v)
    except argparse.ArgumentTypeError as exc:
        print(f"[error] CLAUDE_PROJECT_SLUG: {exc}", file=sys.stderr)
        sys.exit(1)


def _env_session_id() -> str | None:
    v = os.environ.get("CLAUDE_SESSION_ID")
    if v is None:
        return None
    try:
        return _validate_session_id(v)
    except argparse.ArgumentTypeError as exc:
        print(f"[error] CLAUDE_SESSION_ID: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data model — build structured report from raw turns
# ---------------------------------------------------------------------------

def _build_turn_record(global_index: int, entry: dict,
                       tz_offset_hours: float = 0.0) -> dict:
    msg = entry["message"]
    u = msg["usage"]
    model = msg.get("model", "unknown")
    inp = u.get("input_tokens", 0)
    out = u.get("output_tokens", 0)
    crd = u.get("cache_read_input_tokens", 0)
    cwr_5m, cwr_1h = _cache_write_split(u)
    cwr = cwr_5m + cwr_1h
    if cwr == 0:
        ttl = ""
    elif cwr_1h == 0:
        ttl = "5m"
    elif cwr_5m == 0:
        ttl = "1h"
    else:
        ttl = "mix"
    c = _cost(u, model)
    nc = _no_cache_cost(u, model)
    return {
        "index":                  global_index,
        "timestamp":              entry.get("timestamp", ""),
        "timestamp_fmt":          _fmt_ts(entry.get("timestamp", ""), tz_offset_hours),
        "model":                  model,
        "input_tokens":           inp,
        "output_tokens":          out,
        "cache_read_tokens":      crd,
        "cache_write_tokens":     cwr,
        "cache_write_5m_tokens":  cwr_5m,
        "cache_write_1h_tokens":  cwr_1h,
        "cache_write_ttl":        ttl,
        "total_tokens":           inp + out + crd + cwr,
        "cost_usd":               c,
        "no_cache_cost_usd":      nc,
        "speed":                  u.get("speed", ""),
    }


def _totals_from_turns(turn_records: list[dict]) -> dict:
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
         "cache_write_5m": 0, "cache_write_1h": 0, "extra_1h_cost": 0.0,
         "cost": 0.0, "no_cache_cost": 0.0, "turns": len(turn_records)}
    for r in turn_records:
        t["input"]        += r["input_tokens"]
        t["output"]       += r["output_tokens"]
        t["cache_read"]   += r["cache_read_tokens"]
        t["cache_write"]  += r["cache_write_tokens"]
        t["cache_write_5m"] += r.get("cache_write_5m_tokens", 0)
        t["cache_write_1h"] += r.get("cache_write_1h_tokens", 0)
        t["cost"]         += r["cost_usd"]
        t["no_cache_cost"] += r["no_cache_cost_usd"]
        # Extra cost paid for opting into the 1h TTL tier (vs pricing those
        # same tokens at the 5m rate). Meaningful only when cache_write_1h > 0.
        tokens_1h = r.get("cache_write_1h_tokens", 0)
        if tokens_1h:
            rates = _pricing_for(r["model"])
            t["extra_1h_cost"] += tokens_1h * (rates["cache_write_1h"] - rates["cache_write"]) / 1_000_000
    t["total"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    t["total_input"] = t["input"] + t["cache_read"] + t["cache_write"]
    t["cache_savings"] = t["no_cache_cost"] - t["cost"]
    t["cache_hit_pct"] = 100 * t["cache_read"] / max(1, t["total_input"])
    return t


def _model_counts(turn_records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in turn_records:
        counts[r["model"]] = counts.get(r["model"], 0) + 1
    return counts


def _build_report(
    mode: str,
    slug: str,
    sessions_raw: list[tuple[str, list[dict], list[int]]],
    tz_offset_hours: float = 0.0,
    tz_label: str = "UTC",
    peak: dict | None = None,
) -> dict:
    """Build a structured report dict from raw session data.

    Args:
        mode: ``"session"`` for single-session or ``"project"`` for all sessions.
        slug: Project slug derived from the working directory path.
        sessions_raw: List of ``(session_id, assistant_turns, user_epoch_secs)``
            triples in chronological order (oldest first).  ``assistant_turns``
            are raw JSONL entries for assistant messages; ``user_epoch_secs``
            are sorted UTC epoch-seconds for non-meta user entries.

    Returns:
        Report dict containing ``sessions`` (list), ``totals``, ``models``,
        and ``time_of_day`` (project-wide).  Each session entry also has its
        own ``time_of_day`` for per-session breakdowns.
    """
    sessions_out = []
    global_idx = 1

    for session_id, raw_turns, user_ts in sessions_raw:
        turn_records = [_build_turn_record(global_idx + i, t, tz_offset_hours)
                        for i, t in enumerate(raw_turns)]
        global_idx += len(turn_records)
        sessions_out.append({
            "session_id":  session_id,
            "first_ts":    _fmt_ts(raw_turns[0].get("timestamp", ""), tz_offset_hours) if raw_turns else "",
            "last_ts":     _fmt_ts(raw_turns[-1].get("timestamp", ""), tz_offset_hours) if raw_turns else "",
            "turns":       turn_records,
            "subtotal":    _totals_from_turns(turn_records),
            "models":      _model_counts(turn_records),
            "time_of_day": _build_time_of_day(user_ts, offset_hours=tz_offset_hours),
        })

    all_turns = [t for s in sessions_out for t in s["turns"]]
    all_user_ts = sorted(ts for _, _, uts in sessions_raw for ts in uts)
    blocks = _build_session_blocks(sessions_raw)
    return {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "mode":            mode,
        "slug":            slug,
        "tz_offset_hours": tz_offset_hours,
        "tz_label":        tz_label,
        "sessions":        sessions_out,
        "totals":          _totals_from_turns(all_turns),
        "models":          _model_counts(all_turns),
        "time_of_day":     _build_time_of_day(all_user_ts, offset_hours=tz_offset_hours),
        "session_blocks":  blocks,
        "block_summary":   _weekly_block_counts(blocks),
        "weekly_rollup":   _build_weekly_rollup(sessions_out, sessions_raw, blocks),
        "peak":            peak,
    }


# ---------------------------------------------------------------------------
# Formatting helpers (shared)
# ---------------------------------------------------------------------------

COL  = "{:<4} {:<19} {:>11} {:>7} {:>9} {:>9} {:>10} {:>9}"
# Mode (speed) column — appended when any turn in the report used fast mode
COL_M  = COL + "  {:<4}"


def _text_table_headers(tz_offset_hours: float = 0.0,
                         show_mode: bool = False) -> tuple[str, str, str]:
    """Return (hdr, sep, wide) for the text timeline table in the given tz."""
    time_col = f"Time ({_short_tz_label(tz_offset_hours)})"
    if show_mode:
        hdr = COL_M.format("#", time_col, "Input (new)", "Output",
                           "CacheRd", "CacheWr", "Total", "Cost $", "Mode")
    else:
        hdr = COL.format("#", time_col, "Input (new)", "Output",
                         "CacheRd", "CacheWr", "Total", "Cost $")
    return hdr, "-" * len(hdr), "=" * len(hdr)


def _has_fast(report: dict) -> bool:
    """Return True if any turn in the report used fast mode."""
    for s in report["sessions"]:
        for t in s["turns"]:
            if t.get("speed") == "fast":
                return True
    return False


def _has_1h_cache(report: dict) -> bool:
    """Return True if any turn used the 1-hour cache TTL tier."""
    for s in report["sessions"]:
        for t in s["turns"]:
            if t.get("cache_write_1h_tokens", 0) > 0:
                return True
    return False


def _fmt_ts(ts: str, offset_hours: float = 0.0) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if offset_hours:
            dt = dt.astimezone(timezone(timedelta(hours=offset_hours)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts[:19] if len(ts) >= 19 else ts


def _short_tz_label(offset_hours: float) -> str:
    if offset_hours == 0:
        return "UTC"
    sign = "+" if offset_hours > 0 else "-"
    return f"UTC{sign}{abs(offset_hours):g}"


def _fmt_epoch_local(epoch: int, offset_hours: float = 0.0,
                     fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format an integer epoch in the given UTC offset."""
    offset_sec = int(offset_hours * 3600)
    return datetime.fromtimestamp(
        epoch + offset_sec, tz=timezone.utc,
    ).strftime(fmt)


def _fmt_cwr_row(t: dict) -> str:
    """Per-turn CacheWr cell. Appends `*` when the turn used 1h-tier cache."""
    n = t["cache_write_tokens"]
    if t.get("cache_write_ttl") in ("1h", "mix"):
        return f"{n:>8,}*"
    return f"{n:>9,}"


def _fmt_cwr_subtotal(s: dict) -> str:
    """Subtotal/total CacheWr cell. `*` when any 1h tokens are in the sum."""
    n = s.get("cache_write", 0)
    if s.get("cache_write_1h", 0) > 0:
        return f"{n:>8,}*"
    return f"{n:>9,}"


def _row_text(t: dict, show_mode: bool = False) -> str:
    base = COL_M if show_mode else COL
    args = [
        t["index"], t["timestamp_fmt"],
        f"{t['input_tokens']:>7,}", f"{t['output_tokens']:>7,}",
        f"{t['cache_read_tokens']:>9,}", _fmt_cwr_row(t),
        f"{t['total_tokens']:>10,}",
        f"${t['cost_usd']:>8.4f}",
    ]
    if show_mode:
        spd = t.get("speed", "")
        args.append("fast" if spd == "fast" else "std")
    return base.format(*args)


def _subtotal_text(label: str, s: dict, show_mode: bool = False) -> str:
    base = COL_M if show_mode else COL
    args = [
        label, "",
        f"{s['input']:>7,}", f"{s['output']:>7,}",
        f"{s['cache_read']:>9,}", _fmt_cwr_subtotal(s),
        f"{s['total']:>10,}",
        f"${s['cost']:>8.4f}",
    ]
    if show_mode:
        args.append("")
    return base.format(*args)


def _text_legend(tz_label: str, show_mode: bool, show_ttl: bool) -> str:
    """Build the column legend emitted above the timeline table."""
    rows = [
        ("#",       "deduplicated turn index"),
        ("Time",    f"turn start, local tz ({tz_label})"),
    ]
    if show_mode:
        rows.append(("Mode",  "fast / standard (only shown when fast mode was used)"))
    rows.extend([
        ("Input",   "net new input tokens (uncached)"),
        ("Output",  "generated tokens (includes thinking + tool_use block tokens)"),
        ("CacheRd", "tokens read from cache (cheap)"),
    ])
    if show_ttl:
        rows.append(("CacheWr", "tokens written to cache; `*` = includes 1h-tier (see footer)"))
    else:
        rows.append(("CacheWr", "tokens written to cache (one-time)"))
    rows.extend([
        ("Total",   "sum of the four billable token buckets"),
        ("Cost $",  "estimated USD for this turn"),
    ])
    w = max(len(k) for k, _ in rows)
    lines = ["Columns:"] + [f"  {k:<{w}}  {v}" for k, v in rows]
    return "\n".join(lines)


def _footer_text(totals: dict, models: dict[str, int],
                 time_of_day: dict | None = None,
                 tz_label: str = "UTC",
                 session_blocks: list[dict] | None = None,
                 block_summary: dict | None = None) -> str:
    """Build the text footer with cache stats, model breakdown, and time-of-day.

    Args:
        totals: Aggregated token/cost totals dict.
        models: ``{model_id: turn_count}`` mapping.
        time_of_day: Optional ``time_of_day`` report section.  When provided,
            a UTC-bucketed user activity summary is appended.
    """
    lines = [
        "",
        f"Cache savings vs no-cache baseline : ${totals['cache_savings']:.4f}",
        f"Cache hit ratio (read / total input): {totals['cache_hit_pct']:.1f}%",
    ]
    if totals.get("cache_write_1h", 0) > 0:
        lines.append(
            f"Extra cost paid for 1h cache tier  : ${totals.get('extra_1h_cost', 0.0):.4f}"
        )
        pct_1h = 100 * totals["cache_write_1h"] / max(1, totals["cache_write"])
        lines.append(
            f"Cache TTL mix (1h share of writes) : {pct_1h:.1f}%  "
            f"[* in CacheWr column = includes 1h-tier cache write]"
        )
    if models:
        lines.append("")
        lines.append("Models used:")
        for m, cnt in sorted(models.items(), key=lambda x: -x[1]):
            r = _pricing_for(m)
            lines.append(
                f"  {m:<40}  {cnt:>3} turns  "
                f"(${r['input']:.2f}/${r['output']:.2f}/${r['cache_read']:.2f}/${r['cache_write']:.2f} per 1M in/out/rd/wr)"
            )
    if time_of_day and time_of_day.get("message_count", 0) > 0:
        b = time_of_day["buckets"]
        lines.append("")
        lines.append(f"User prompts by time of day ({tz_label}):")
        lines.append(f"  Night (0\u20136):      {b.get('night', 0):>5,}")
        lines.append(f"  Morning (6\u201312):   {b.get('morning', 0):>5,}")
        lines.append(f"  Afternoon (12\u201318):{b.get('afternoon', 0):>5,}")
        lines.append(f"  Evening (18\u201324):  {b.get('evening', 0):>5,}")

        hod = time_of_day.get("hour_of_day")
        if hod and hod.get("total", 0) > 0:
            hours = hod["hours"]
            mx = max(hours) or 1
            lines.append("")
            lines.append(f"Hour-of-day ({tz_label}) — each \u2588 \u2248 {mx/20:.1f} prompts:")
            for h in range(24):
                bar = "\u2588" * int(hours[h] / mx * 20)
                lines.append(f"  {h:02d}:00  {hours[h]:>4,}  {bar}")

        wh = time_of_day.get("weekday_hour")
        if wh and wh.get("total", 0) > 0:
            row_totals = wh["row_totals"]
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            lines.append("")
            lines.append(f"Weekday totals ({tz_label}):")
            for i, d in enumerate(days):
                lines.append(f"  {d}:  {row_totals[i]:>5,}")

    if session_blocks:
        lines.append("")
        s7  = block_summary.get("trailing_7",  0) if block_summary else 0
        s14 = block_summary.get("trailing_14", 0) if block_summary else 0
        tot = block_summary.get("total", len(session_blocks)) if block_summary else len(session_blocks)
        lines.append(f"5-hour session blocks ({tot} total; "
                     f"{s7} in last 7d, {s14} in last 14d):")
        recent = session_blocks[-8:]
        for b in recent:
            anchor = b["anchor_iso"][:16].replace("T", " ")
            dur    = b["elapsed_min"]
            lines.append(
                f"  {anchor}Z  "
                f"dur={dur:>5.0f}m  "
                f"turns={b['turn_count']:>3}  "
                f"prompts={b['user_msg_count']:>3}  "
                f"${b['cost_usd']:>7.3f}"
            )
        if len(session_blocks) > len(recent):
            lines.append(f"  ... ({len(session_blocks) - len(recent)} earlier blocks omitted)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_text(report: dict) -> str:
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    sessions = report["sessions"]

    m = _has_fast(report)
    has_1h = _has_1h_cache(report)
    tz_offset = report.get("tz_offset_hours", 0.0)
    tz_label = report.get("tz_label", "UTC")
    hdr, sep, wide = _text_table_headers(tz_offset, show_mode=m)

    p(_text_legend(tz_label, show_mode=m, show_ttl=has_1h))
    p()

    if report["mode"] == "project":
        p(f"Project: {report['slug']}")
        p(f"Sessions with data: {len(sessions)}")
        p()
        for i, s in enumerate(sessions, 1):
            p(wide)
            p(f"  Session {s['session_id'][:8]}…  {s['first_ts']} → {s['last_ts']}  ({len(s['turns'])} turns)")
            p(wide)
            p(hdr)
            for t in s["turns"]:
                p(_row_text(t, m))
            p(sep)
            p(_subtotal_text(f"S{i:02}", s["subtotal"], m))
            p()
        p(wide)
        p(f"  PROJECT TOTAL — {len(sessions)} session{'s' if len(sessions) != 1 else ''}, {report['totals']['turns']} turns")
        p(wide)
        p(hdr)
        p(sep)
        p(_subtotal_text("TOT", report["totals"], m))
    else:
        s = sessions[0]
        p(hdr)
        for t in s["turns"]:
            p(_row_text(t, m))
        p(sep)
        p(_subtotal_text("TOT", s["subtotal"], m))

    p(_footer_text(report["totals"], report["models"], report.get("time_of_day"),
                    tz_label=report.get("tz_label", "UTC"),
                    session_blocks=report.get("session_blocks"),
                    block_summary=report.get("block_summary")))
    return out.getvalue()


def _tod_for_json(tod: dict) -> dict:
    """Convert a ``time_of_day`` section for JSON export.

    Replaces internal ``epoch_secs`` (integer list) with human-readable
    ``utc_timestamps`` (ISO-8601 strings).  The conversion is O(n) but only
    runs once per export — no deep-copy of the full report is needed.
    """
    return {
        "utc_timestamps": [
            datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for e in tod.get("epoch_secs", [])
        ],
        "message_count": tod.get("message_count", 0),
        "buckets":       tod.get("buckets", {}),
        "hour_of_day":   tod.get("hour_of_day", {}),
        "weekday_hour":  tod.get("weekday_hour", {}),
        "offset_hours":  tod.get("offset_hours", 0.0),
    }


def render_json(report: dict) -> str:
    """Render the full report as indented JSON.

    Internal ``epoch_secs`` lists in ``time_of_day`` sections are converted to
    ISO-8601 ``utc_timestamps`` for human readability.  The transform uses a
    shallow copy of the report — session turns, subtotals, and model dicts are
    shared by reference to avoid copying ~thousands of turn record dicts.
    """
    # Shallow-transform: only replace time_of_day sections
    export = {**report}
    if "time_of_day" in export:
        export["time_of_day"] = _tod_for_json(export["time_of_day"])
    if "sessions" in export:
        export["sessions"] = [
            {**s, "time_of_day": _tod_for_json(s["time_of_day"])}
            if "time_of_day" in s else s
            for s in export["sessions"]
        ]
    return json.dumps(export, indent=2)


def render_csv(report: dict) -> str:
    """Render turn-level CSV with an appended time-of-day summary section.

    The first section contains one row per assistant turn (unchanged).
    A blank separator row is followed by a ``USER ACTIVITY BY TIME OF DAY``
    summary with per-session and project-wide counts bucketed at UTC.
    """
    out = io.StringIO()
    w = csv_mod.writer(out)
    w.writerow(["session_id", "turn", "timestamp", "model", "speed",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_ttl",
                "total_tokens", "cost_usd", "no_cache_cost_usd"])
    for s in report["sessions"]:
        for t in s["turns"]:
            w.writerow([
                s["session_id"], t["index"], t["timestamp"], t["model"],
                t.get("speed", ""),
                t["input_tokens"], t["output_tokens"],
                t["cache_read_tokens"], t["cache_write_tokens"],
                t.get("cache_write_5m_tokens", 0),
                t.get("cache_write_1h_tokens", 0),
                t.get("cache_write_ttl", ""),
                t["total_tokens"],
                f"{t['cost_usd']:.6f}", f"{t['no_cache_cost_usd']:.6f}",
            ])

    # Time-of-day summary section
    tz_label = report.get("tz_label", "UTC")
    w.writerow([])
    w.writerow([f"# USER ACTIVITY BY TIME OF DAY ({tz_label})"])
    w.writerow(["scope", "id", "night_0_6", "morning_6_12",
                "afternoon_12_18", "evening_18_24", "total"])
    for s in report["sessions"]:
        tod = s.get("time_of_day", {})
        b = tod.get("buckets", {})
        w.writerow(["session", s["session_id"],
                     b.get("night", 0), b.get("morning", 0),
                     b.get("afternoon", 0), b.get("evening", 0),
                     tod.get("message_count", 0)])
    tod = report.get("time_of_day", {})
    b = tod.get("buckets", {})
    w.writerow(["project", report["slug"],
                 b.get("night", 0), b.get("morning", 0),
                 b.get("afternoon", 0), b.get("evening", 0),
                 tod.get("message_count", 0)])

    # Hour-of-day section (project-wide)
    hod = tod.get("hour_of_day")
    if hod and hod.get("total", 0) > 0:
        w.writerow([])
        w.writerow([f"# HOUR OF DAY ({tz_label})"])
        w.writerow(["hour"] + [f"{h:02d}" for h in range(24)] + ["total"])
        w.writerow(["prompts"] + list(hod["hours"]) + [hod["total"]])

    # Weekday x hour matrix (project-wide)
    wh = tod.get("weekday_hour")
    if wh and wh.get("total", 0) > 0:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        w.writerow([])
        w.writerow([f"# WEEKDAY x HOUR ({tz_label})"])
        w.writerow(["weekday"] + [f"{h:02d}" for h in range(24)] + ["row_total"])
        for i, d in enumerate(days):
            w.writerow([d] + list(wh["matrix"][i]) + [wh["row_totals"][i]])
        w.writerow(["col_total"] + list(wh["col_totals"]) + [wh["total"]])

    # 5-hour session blocks
    blocks  = report.get("session_blocks") or []
    summary = report.get("block_summary") or {}
    if blocks:
        w.writerow([])
        w.writerow(["# 5-HOUR SESSION BLOCKS"])
        w.writerow(["trailing_7", "trailing_14", "trailing_30", "total"])
        w.writerow([summary.get("trailing_7", 0), summary.get("trailing_14", 0),
                    summary.get("trailing_30", 0), summary.get("total", len(blocks))])
        w.writerow([])
        w.writerow(["anchor_utc", "last_utc", "elapsed_min", "turns",
                    "user_prompts", "input", "output", "cache_read",
                    "cache_write", "cost_usd", "sessions_touched"])
        for b in blocks:
            w.writerow([
                b["anchor_iso"], b["last_iso"], f"{b['elapsed_min']:.1f}",
                b["turn_count"], b["user_msg_count"],
                b["input"], b["output"], b["cache_read"], b["cache_write"],
                f"{b['cost_usd']:.6f}", len(b["sessions_touched"]),
            ])
    return out.getvalue()


def render_md(report: dict) -> str:
    """Render the full report as GitHub-flavored Markdown.

    Includes summary cards, user activity by time of day (UTC), model pricing
    table, and per-session turn-level tables with subtotals.
    """
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    slug = report["slug"]
    totals = report["totals"]
    mode = report["mode"]
    tz_offset = report.get("tz_offset_hours", 0.0)
    try:
        _gen_dt = datetime.fromisoformat(
            report["generated_at"].replace("Z", "+00:00")
        ).astimezone(timezone(timedelta(hours=tz_offset)))
        generated = _gen_dt.strftime("%Y-%m-%d %H:%M:%S") + f" {_short_tz_label(tz_offset)}"
    except Exception:
        generated = report["generated_at"][:19].replace("T", " ") + " UTC"

    p(f"# Session Metrics — {slug}")
    p()
    p(f"Generated: {generated}  |  Mode: {mode}")
    p()

    # Summary cards
    p("## Summary")
    p()
    p(f"| Metric | Value |")
    p(f"|--------|-------|")
    p(f"| Sessions | {len(report['sessions'])} |")
    p(f"| Total turns | {totals['turns']:,} |")
    p(f"| Total cost | ${totals['cost']:.4f} |")
    p(f"| Cache savings | ${totals['cache_savings']:.4f} |")
    p(f"| Cache hit ratio | {totals['cache_hit_pct']:.1f}% |")
    p(f"| Total input tokens | {totals['total_input']:,} |")
    p(f"| Input tokens (new) | {totals['input']:,} |")
    p(f"| Output tokens | {totals['output']:,} |")
    p(f"| Cache read tokens | {totals['cache_read']:,} |")
    p(f"| Cache write tokens | {totals['cache_write']:,} |")
    if totals.get("cache_write_1h", 0) > 0:
        pct_1h = 100 * totals["cache_write_1h"] / max(1, totals["cache_write"])
        p(f"| Cache TTL mix (1h share of writes) | {pct_1h:.1f}% |")
        p(f"| Extra cost paid for 1h cache tier | ${totals.get('extra_1h_cost', 0.0):.4f} |")
    p()

    # Time-of-day section
    tod = report.get("time_of_day", {})
    tz_label = report.get("tz_label", "UTC")
    if tod.get("message_count", 0) > 0:
        b = tod["buckets"]
        p(f"## User Activity by Time of Day ({tz_label})")
        p()
        p("| Period | Hours | Messages |")
        p("|--------|------:|---------:|")
        p(f"| Night | 0\u20136 | {b.get('night', 0):,} |")
        p(f"| Morning | 6\u201312 | {b.get('morning', 0):,} |")
        p(f"| Afternoon | 12\u201318 | {b.get('afternoon', 0):,} |")
        p(f"| Evening | 18\u201324 | {b.get('evening', 0):,} |")
        p(f"| **Total** | | **{tod['message_count']:,}** |")
        p()

        hod = tod.get("hour_of_day")
        if hod and hod.get("total", 0) > 0:
            hours = hod["hours"]
            p(f"### Hour of day ({tz_label})")
            p()
            p("| Hour | Prompts |")
            p("|-----:|--------:|")
            for h in range(24):
                p(f"| {h:02d}:00 | {hours[h]:,} |")
            p()

        wh = tod.get("weekday_hour")
        if wh and wh.get("total", 0) > 0:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            p(f"### Weekday x hour ({tz_label})")
            p()
            header = "| Day | " + " | ".join(f"{h:02d}" for h in range(24)) + " | Total |"
            sep = "|-----|" + "|".join(["---:"] * 24) + "|------:|"
            p(header)
            p(sep)
            for i, d in enumerate(days):
                row = wh["matrix"][i]
                cells = " | ".join(str(c) if c else "" for c in row)
                p(f"| {d} | {cells} | **{wh['row_totals'][i]:,}** |")
            p()

    blocks  = report.get("session_blocks", [])
    summary = report.get("block_summary", {})
    if blocks:
        p(f"## 5-hour session blocks ({tz_label})")
        p()
        p(f"- Trailing 7 days: **{summary.get('trailing_7', 0)}** blocks")
        p(f"- Trailing 14 days: **{summary.get('trailing_14', 0)}** blocks")
        p(f"- Trailing 30 days: **{summary.get('trailing_30', 0)}** blocks")
        p(f"- All time: **{summary.get('total', len(blocks))}** blocks")
        p()
        p(f"| Anchor ({tz_label}) | Duration | Turns | Prompts | Cost | Sessions |")
        p("|-------------|---------:|------:|--------:|-----:|---------:|")
        for b in reversed(blocks[-12:]):
            anchor_local = _fmt_epoch_local(b["anchor_epoch"], tz_offset, "%Y-%m-%d %H:%M")
            p(f"| {anchor_local} | {b['elapsed_min']:.0f}m "
              f"| {b['turn_count']:,} | {b['user_msg_count']:,} "
              f"| ${b['cost_usd']:.3f} | {len(b['sessions_touched'])} |")
        p()

    if report["models"]:
        p("## Models")
        p()
        p("| Model | Turns | $/M in | $/M out | $/M rd | $/M wr |")
        p("|-------|------:|------:|------:|------:|------:|")
        for m, cnt in sorted(report["models"].items(), key=lambda x: -x[1]):
            r = _pricing_for(m)
            p(f"| `{m}` | {cnt:,} | ${r['input']:.2f} | ${r['output']:.2f} | ${r['cache_read']:.2f} | ${r['cache_write']:.2f} |")
        p()

    has_1h_cache = _has_1h_cache(report)
    p("## Column legend")
    p()
    p("- **#** — deduplicated turn index")
    p(f"- **Time** — turn start, local tz ({tz_label})")
    p("- **Input (new)** — net new input tokens (uncached)")
    p("- **Output** — generated tokens (includes thinking + tool_use block tokens)")
    p("- **CacheRd** — tokens read from cache (cheap)")
    if has_1h_cache:
        p("- **CacheWr** — tokens written to cache; `*` suffix marks turns that used the 1-hour TTL tier")
    else:
        p("- **CacheWr** — tokens written to cache (one-time)")
    p("- **Total** — sum of the four billable token buckets")
    p("- **Cost $** — estimated USD for this turn")
    p()

    for i, s in enumerate(report["sessions"], 1):
        if mode == "project":
            st = s["subtotal"]
            p(f"## Session {i}: `{s['session_id'][:8]}…`")
            p()
            p(f"{s['first_ts']} → {s['last_ts']} &nbsp;·&nbsp; {len(s['turns'])} turns &nbsp;·&nbsp; **${st['cost']:.4f}**")
            p()

        p(f"| # | Time ({tz_label}) | Input (new) | Output | CacheRd | CacheWr | Total | Cost $ |")
        p("|--:|-----------|------------:|------:|--------:|--------:|------:|-------:|")
        for t in s["turns"]:
            ttl = t.get("cache_write_ttl", "")
            cwr_cell = f"{t['cache_write_tokens']:,}" + ("*" if ttl in ("1h", "mix") else "")
            p(f"| {t['index']} | {t['timestamp_fmt']} "
              f"| {t['input_tokens']:,} | {t['output_tokens']:,} "
              f"| {t['cache_read_tokens']:,} | {cwr_cell} "
              f"| {t['total_tokens']:,} | ${t['cost_usd']:.4f} |")
        st = s["subtotal"]
        st_cwr_cell = f"{st['cache_write']:,}" + ("*" if st.get("cache_write_1h", 0) > 0 else "")
        p(f"| **TOT** | | **{st['input']:,}** | **{st['output']:,}** "
          f"| **{st['cache_read']:,}** | **{st_cwr_cell}** "
          f"| **{st['total']:,}** | **${st['cost']:.4f}** |")
        if st.get("cache_write_1h", 0) > 0:
            p()
            p(f"_`*` = cache write includes the 1-hour TTL tier "
              f"(5m: {st.get('cache_write_5m', 0):,}, 1h: {st['cache_write_1h']:,} tokens)._")
        p()

    return out.getvalue()


def _session_duration_stats(session: dict) -> dict | None:
    """Per-session wall-clock + burn rate derived from turn timestamps.

    Returns None when fewer than 2 turns have usable timestamps. Burn rate
    metrics are clamped so a single-turn session doesn't divide by zero.
    """
    turns = session.get("turns", [])
    epochs = [_parse_iso_epoch(t.get("timestamp", "")) for t in turns]
    epochs = [e for e in epochs if e]
    if len(epochs) < 2:
        return None
    first, last = min(epochs), max(epochs)
    wall_sec    = last - first
    wall_min    = wall_sec / 60.0
    st          = session["subtotal"]
    minutes     = max(1e-6, wall_min)
    return {
        "first_epoch":  first,
        "last_epoch":   last,
        "wall_sec":     wall_sec,
        "wall_min":     wall_min,
        "tokens_per_min": st["total"] / minutes,
        "cost_per_min":   st["cost"]  / minutes,
        "turns":        st["turns"],
    }


def _fmt_duration(sec: int) -> str:
    """Format ``sec`` as a compact duration (``1h23m``, ``45m12s``, ``7s``)."""
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    hours, rem = divmod(sec, 3600)
    return f"{hours}h{rem // 60:02d}m"


def _build_session_duration_html(sessions: list[dict], tz_label: str,
                                  tz_offset_hours: float) -> str:
    """Build a per-session duration + burn-rate card.

    Shows the most-recent 10 sessions (newest first) with wall-clock time,
    turn count, total cost, tokens/min, and cost/min. Answers "how much
    am I spending per active minute" for a given session.
    """
    rows_data = []
    for s in sessions:
        stats = _session_duration_stats(s)
        if not stats:
            continue
        rows_data.append((s, stats))
    if not rows_data:
        return ""
    offset_sec = int(tz_offset_hours * 3600)

    def fmt_local(epoch: int) -> str:
        return datetime.fromtimestamp(
            epoch + offset_sec, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M")

    rows_data.sort(key=lambda x: x[1]["last_epoch"], reverse=True)
    rows_data = rows_data[:10]
    rows_html = []
    for s, st in rows_data:
        sid = s["session_id"][:8]
        rows_html.append(
            f'<tr><td style="padding:4px 10px;color:#e6edf3;font-family:monospace">{sid}\u2026</td>'
            f'<td style="padding:4px 10px;color:#8b949e">{fmt_local(st["first_epoch"])}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#e6edf3;'
            f'font-variant-numeric:tabular-nums">{_fmt_duration(st["wall_sec"])}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#e6edf3;'
            f'font-variant-numeric:tabular-nums">{st["turns"]:,}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#f0f6fc;'
            f'font-variant-numeric:tabular-nums">${s["subtotal"]["cost"]:.3f}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#8b949e;'
            f'font-variant-numeric:tabular-nums">{st["tokens_per_min"]:,.0f}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#8b949e;'
            f'font-variant-numeric:tabular-nums">${st["cost_per_min"]:.3f}</td></tr>'
        )
    return f"""\
<div id="session-duration" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
       letter-spacing:0.05em;margin-bottom:14px">Session duration \u2014 newest first</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>
      <tr style="color:#8b949e;text-transform:uppercase;font-size:10px;
          letter-spacing:0.05em;border-bottom:1px solid #30363d">
        <th style="padding:6px 10px;text-align:left">Session</th>
        <th style="padding:6px 10px;text-align:left">First turn ({tz_label})</th>
        <th style="padding:6px 10px;text-align:right">Wall</th>
        <th style="padding:6px 10px;text-align:right">Turns</th>
        <th style="padding:6px 10px;text-align:right">Cost</th>
        <th style="padding:6px 10px;text-align:right">tok/min</th>
        <th style="padding:6px 10px;text-align:right">$/min</th>
      </tr>
    </thead>
    <tbody>{"".join(rows_html)}</tbody>
  </table>
</div>"""


def _fmt_delta_pct(cur: float, prev: float) -> tuple[str, str]:
    """Format the relative delta of ``cur`` vs ``prev`` as ``("+12.3%", color)``.

    When ``prev`` is zero, returns ``("new", "#8b949e")`` — don't render
    infinite percentages. Positive deltas are red for cost/turns (caller
    picks the color-flip); this helper just returns a magenta/green by sign.
    """
    if prev <= 0:
        return ("new" if cur > 0 else "\u2013", "#8b949e")
    delta = (cur - prev) / prev * 100.0
    sign = "+" if delta > 0 else ""
    color = "#f47067" if delta > 0 else "#58a6ff" if delta < 0 else "#8b949e"
    return (f"{sign}{delta:.1f}%", color)


def _build_weekly_rollup_html(rollup: dict) -> str:
    """Render a trailing-7d vs prior-7d comparison card.

    Returns empty string when there's no data (skips the section cleanly
    on brand-new projects).
    """
    if not rollup or not rollup.get("has_data"):
        return ""
    cur  = rollup["trailing_7d"]
    prev = rollup["prior_7d"]

    rows = []
    metrics = [
        ("Cost (USD)",       f"${cur['cost']:.2f}",          f"${prev['cost']:.2f}",          cur["cost"],          prev["cost"]),
        ("Assistant turns",  f"{cur['turns']:,}",            f"{prev['turns']:,}",            cur["turns"],         prev["turns"]),
        ("User prompts",     f"{cur['user_prompts']:,}",     f"{prev['user_prompts']:,}",     cur["user_prompts"],  prev["user_prompts"]),
        ("5h blocks",        f"{cur['blocks']:,}",           f"{prev['blocks']:,}",           cur["blocks"],        prev["blocks"]),
        ("Cache hit ratio",  f"{cur['cache_hit_pct']:.1f}%", f"{prev['cache_hit_pct']:.1f}%", cur["cache_hit_pct"], prev["cache_hit_pct"]),
    ]
    for label, cur_s, prev_s, cur_v, prev_v in metrics:
        delta, color = _fmt_delta_pct(cur_v, prev_v)
        rows.append(
            f'<tr><td style="padding:6px 12px;color:#e6edf3">{label}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:#f0f6fc;'
            f'font-variant-numeric:tabular-nums">{cur_s}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:#8b949e;'
            f'font-variant-numeric:tabular-nums">{prev_s}</td>'
            f'<td style="padding:6px 12px;text-align:right;color:{color};'
            f'font-variant-numeric:tabular-nums">{delta}</td></tr>'
        )

    return f"""\
<div id="weekly-rollup" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
       letter-spacing:0.05em;margin-bottom:14px">Weekly roll-up</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>
      <tr style="color:#8b949e;text-transform:uppercase;font-size:10px;
          letter-spacing:0.05em;border-bottom:1px solid #30363d">
        <th style="padding:6px 12px;text-align:left">Metric</th>
        <th style="padding:6px 12px;text-align:right">Last 7d</th>
        <th style="padding:6px 12px;text-align:right">Prior 7d</th>
        <th style="padding:6px 12px;text-align:right">\u0394</th>
      </tr>
    </thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _build_session_blocks_html(
    blocks: list[dict], summary: dict, tz_label: str = "UTC",
    tz_offset_hours: float = 0.0,
) -> str:
    """Render 5-hour session blocks as a summary card + recent-blocks list.

    Includes a weekly-count card (trailing 7/14/30d) as the primary
    rate-limit-debugging signal, then the newest 12 blocks with duration,
    turn count, prompt count, cost, and session-count.
    """
    if not blocks:
        return ""
    offset_sec = int(tz_offset_hours * 3600)

    def fmt_local(epoch: int) -> str:
        return datetime.fromtimestamp(
            epoch + offset_sec, tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M")

    s7  = summary.get("trailing_7",  0)
    s14 = summary.get("trailing_14", 0)
    s30 = summary.get("trailing_30", 0)
    tot = summary.get("total", len(blocks))
    recent = list(reversed(blocks[-12:]))
    rows = "".join(
        f'<tr><td style="padding:4px 10px;color:#e6edf3">{fmt_local(b["anchor_epoch"])}</td>'
        f'<td style="padding:4px 10px;color:#8b949e;text-align:right;'
        f'font-variant-numeric:tabular-nums">{b["elapsed_min"]:.0f}m</td>'
        f'<td style="padding:4px 10px;color:#e6edf3;text-align:right;'
        f'font-variant-numeric:tabular-nums">{b["turn_count"]:,}</td>'
        f'<td style="padding:4px 10px;color:#e6edf3;text-align:right;'
        f'font-variant-numeric:tabular-nums">{b["user_msg_count"]:,}</td>'
        f'<td style="padding:4px 10px;color:#f0f6fc;text-align:right;'
        f'font-variant-numeric:tabular-nums">${b["cost_usd"]:.3f}</td>'
        f'<td style="padding:4px 10px;color:#8b949e;text-align:right;'
        f'font-variant-numeric:tabular-nums">{len(b["sessions_touched"])}</td></tr>'
        for b in recent
    )

    def card(label: str, value: str, hint: str = "") -> str:
        hint_html = (f'<span style="color:#8b949e;font-size:10px;margin-left:6px">'
                     f'{hint}</span>') if hint else ""
        return (
            f'<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;'
            f'padding:12px 16px;min-width:140px">'
            f'<div style="color:#8b949e;font-size:10px;text-transform:uppercase;'
            f'letter-spacing:0.05em;margin-bottom:4px">{label}</div>'
            f'<div style="color:#f0f6fc;font-size:24px;font-weight:600;'
            f'font-variant-numeric:tabular-nums">{value}{hint_html}</div></div>'
        )

    return f"""\
<div id="session-blocks" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
       letter-spacing:0.05em;margin-bottom:14px">5-hour session blocks</div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px">
    {card("Last 7 days", f"{s7}")}
    {card("Last 14 days", f"{s14}")}
    {card("Last 30 days", f"{s30}")}
    {card("All time", f"{tot}")}
  </div>
  <div style="color:#8b949e;font-size:11px;margin-bottom:8px">
    Recent blocks ({tz_label}) \u2014 a new block starts 5h after the previous anchor.
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>
      <tr style="color:#8b949e;text-transform:uppercase;font-size:10px;
          letter-spacing:0.05em;border-bottom:1px solid #30363d">
        <th style="padding:6px 10px;text-align:left">Anchor</th>
        <th style="padding:6px 10px;text-align:right">Duration</th>
        <th style="padding:6px 10px;text-align:right">Turns</th>
        <th style="padding:6px 10px;text-align:right">Prompts</th>
        <th style="padding:6px 10px;text-align:right">Cost</th>
        <th style="padding:6px 10px;text-align:right">Sessions</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def _build_hour_of_day_html(tod: dict, tz_label: str = "UTC",
                            default_offset_hours: float = 0.0,
                            peak: dict | None = None) -> str:
    """Build a 24-hour bar chart of user prompts, self-contained HTML + CSS + JS.

    Client-side JS rebuckets to any offset chosen from the tz dropdown. When
    ``peak`` is supplied (see ``_build_peak``), overlays a translucent band
    behind the bars in the peak-hours range, and reshifts the band when the
    user changes display tz.
    """
    epoch_secs = tod.get("epoch_secs", [])
    if not epoch_secs:
        return ""
    ts_json = json.dumps(epoch_secs, separators=(",", ":"))
    tz_options = _tz_dropdown_options(default_offset_hours, tz_label)

    peak_json = "null"
    peak_legend = ""
    if peak:
        peak_json = json.dumps({
            "start":   peak["start"],
            "end":     peak["end"],
            "tz_off":  peak["tz_offset_hours"],
            "tz_label": peak["tz_label"],
        }, separators=(",", ":"))
        peak_legend = (
            f'<span style="color:#8b949e;font-size:11px;display:inline-flex;'
            f'align-items:center;gap:6px">'
            f'<span style="display:inline-block;width:10px;height:10px;'
            f'background:rgba(239,197,75,0.25);border:1px solid rgba(239,197,75,0.6);'
            f'border-radius:2px"></span>'
            f'Peak ({peak["start"]:02d}\u2013{peak["end"]:02d} {peak["tz_label"]}, {peak["note"]})'
            f'</span>'
        )

    return f"""\
<div id="hod-chart" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <span style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
          letter-spacing:0.05em">Hour of day</span>
    <select id="hod-tz" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
            border-radius:4px;padding:4px 8px;font-size:11px;cursor:pointer">{tz_options}</select>
    <span style="color:#8b949e;font-size:11px">Peak:
      <strong id="hod-peak" style="color:#e6edf3">-</strong></span>
    {peak_legend}
  </div>
  <div id="hod-wrap" style="position:relative;height:140px;
       border-bottom:1px solid #30363d;padding-bottom:2px">
    <div id="hod-peak-band1" style="position:absolute;top:0;bottom:0;
         background:rgba(239,197,75,0.12);border-left:1px dashed rgba(239,197,75,0.35);
         border-right:1px dashed rgba(239,197,75,0.35);display:none;pointer-events:none"></div>
    <div id="hod-peak-band2" style="position:absolute;top:0;bottom:0;
         background:rgba(239,197,75,0.12);border-left:1px dashed rgba(239,197,75,0.35);
         border-right:1px dashed rgba(239,197,75,0.35);display:none;pointer-events:none"></div>
    <div id="hod-bars" style="position:relative;display:flex;align-items:flex-end;
         gap:2px;height:100%"></div>
  </div>
  <div style="display:flex;gap:2px;margin-top:6px;color:#8b949e;
       font-size:10px;font-variant-numeric:tabular-nums">
    {"".join(f'<div style="flex:1;text-align:center">{h:02d}</div>' for h in range(24))}
  </div>
</div>
<script>
(function(){{
  var TS={ts_json};
  var PEAK={peak_json};
  var bars=document.getElementById('hod-bars');
  var bs=[];
  for(var i=0;i<24;i++){{
    var b=document.createElement('div');
    b.style.cssText='flex:1;background:#8b5cf6;border-radius:2px 2px 0 0;'+
      'min-height:1px;transition:height 0.25s ease;position:relative';
    b.title=(i<10?'0':'')+i+':00';
    bars.appendChild(b);bs.push(b);
  }}
  function bandPct(startHour,endHour){{
    return {{left:(startHour/24*100)+'%',width:((endHour-startHour)/24*100)+'%'}};
  }}
  function positionPeak(displayOff){{
    var b1=document.getElementById('hod-peak-band1');
    var b2=document.getElementById('hod-peak-band2');
    if(!PEAK){{b1.style.display='none';b2.style.display='none';return;}}
    var shift=displayOff-PEAK.tz_off;
    var s=((PEAK.start+shift)%24+24)%24;
    var e=((PEAK.end  +shift)%24+24)%24;
    if(e===0)e=24;
    if(s<e){{
      var p=bandPct(s,e);
      b1.style.left=p.left;b1.style.width=p.width;b1.style.display='block';
      b2.style.display='none';
    }}else{{
      // wraps midnight: split into [s,24) + [0,e)
      var p1=bandPct(s,24),p2=bandPct(0,e);
      b1.style.left=p1.left;b1.style.width=p1.width;b1.style.display='block';
      b2.style.left=p2.left;b2.style.width=p2.width;b2.style.display='block';
    }}
  }}
  function render(off){{
    var c=new Array(24);for(var i=0;i<24;i++)c[i]=0;
    var s=off*3600;
    for(var j=0;j<TS.length;j++){{
      var h=(((TS[j]+s)%86400)+86400)%86400/3600|0;
      c[h]++;
    }}
    var mx=Math.max.apply(null,c)||1;
    var peak=0,peakH=0;
    for(var k=0;k<24;k++){{
      bs[k].style.height=(c[k]/mx*100)+'%';
      bs[k].title=(k<10?'0':'')+k+':00  '+c[k].toLocaleString()+' prompts';
      if(c[k]>peak){{peak=c[k];peakH=k;}}
    }}
    document.getElementById('hod-peak').textContent=
      peak?((peakH<10?'0':'')+peakH+':00 ('+peak.toLocaleString()+')'):'-';
    positionPeak(off);
  }}
  var sel=document.getElementById('hod-tz');
  sel.addEventListener('change',function(){{render(+this.value);}});
  render(+sel.value);
}})();
</script>"""


def _build_punchcard_html(tod: dict, tz_label: str = "UTC",
                          default_offset_hours: float = 0.0) -> str:
    """Build a 7x24 weekday-by-hour punchcard, GitHub-style dots.

    Rows: Mon..Sun.  Columns: 00..23 in the selected tz.  Dot radius scales
    with the cell count; empty cells render as faint dots.
    """
    epoch_secs = tod.get("epoch_secs", [])
    if not epoch_secs:
        return ""
    ts_json = json.dumps(epoch_secs, separators=(",", ":"))
    tz_options = _tz_dropdown_options(default_offset_hours, tz_label)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cells = []
    for r in range(7):
        row = []
        row.append(f'<div style="color:#8b949e;font-size:10px;width:30px;'
                   f'text-align:right;padding-right:6px;align-self:center">{days[r]}</div>')
        for h in range(24):
            row.append(f'<div class="pc-cell" data-r="{r}" data-h="{h}" '
                       f'style="flex:1;height:18px;display:flex;align-items:center;'
                       f'justify-content:center">'
                       f'<div class="pc-dot" style="width:2px;height:2px;background:#30363d;'
                       f'border-radius:50%;transition:all 0.2s ease"></div></div>')
        cells.append('<div style="display:flex;align-items:center">' + "".join(row) + "</div>")
    hour_header = ('<div style="display:flex;color:#8b949e;font-size:10px;margin-bottom:4px">'
                   '<div style="width:30px"></div>'
                   + "".join(f'<div style="flex:1;text-align:center">{h:02d}</div>' for h in range(24))
                   + '</div>')
    return f"""\
<div id="punchcard" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px;flex-wrap:wrap">
    <span style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
          letter-spacing:0.05em">Weekday \u00d7 hour</span>
    <select id="pc-tz" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
            border-radius:4px;padding:4px 8px;font-size:11px;cursor:pointer">{tz_options}</select>
    <span style="color:#8b949e;font-size:11px">Busiest:
      <strong id="pc-busy" style="color:#e6edf3">-</strong></span>
  </div>
  {hour_header}
  {"".join(cells)}
</div>
<script>
(function(){{
  var TS={ts_json};
  var cells=document.querySelectorAll('#punchcard .pc-cell');
  function render(off){{
    var m=[];for(var r=0;r<7;r++){{m.push(new Array(24));for(var k=0;k<24;k++)m[r][k]=0;}}
    var s=off*3600,mx=0,busyR=0,busyH=0;
    for(var i=0;i<TS.length;i++){{
      var t=TS[i]+s;
      var days=Math.floor(t/86400);
      var w=((days+3)%7+7)%7;
      var h=((t%86400)+86400)%86400/3600|0;
      m[w][h]++;
      if(m[w][h]>mx){{mx=m[w][h];busyR=w;busyH=h;}}
    }}
    mx=mx||1;
    cells.forEach(function(el){{
      var r=+el.dataset.r,h=+el.dataset.h,v=m[r][h];
      var dot=el.firstChild;
      if(v===0){{
        dot.style.width='2px';dot.style.height='2px';dot.style.background='#30363d';
      }}else{{
        var sz=Math.max(4,Math.min(14,4+v/mx*10));
        dot.style.width=sz+'px';dot.style.height=sz+'px';dot.style.background='#8b5cf6';
        el.title=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][r]+' '+(h<10?'0':'')+h+':00 — '+v;
      }}
    }});
    var DAYS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    document.getElementById('pc-busy').textContent=
      mx>1||(mx===1&&TS.length)?(DAYS[busyR]+' '+(busyH<10?'0':'')+busyH+':00 ('+mx+')'):'-';
  }}
  var sel=document.getElementById('pc-tz');
  sel.addEventListener('change',function(){{render(+this.value);}});
  render(+sel.value);
}})();
</script>"""


def _tz_dropdown_options(default_offset_hours: float, tz_label: str) -> str:
    """Build the <option> list for the tz dropdown used by hod/punchcard/heatmap.

    The resolved display tz (from CLI/env/auto-detect) is always present as
    the selected option and always first.  A small fixed set of common zones
    is appended below; duplicates are skipped.
    """
    def fmt(off: float) -> str:
        sign = "+" if off >= 0 else "\u2212"
        return f"UTC{sign}{abs(off):g}"
    items = [(default_offset_hours, f"{tz_label} ({fmt(default_offset_hours)})", True)]
    commons = [(0.0, "UTC"), (-8.0, "PT"), (-5.0, "ET"),
               (1.0, "CET"), (5.5, "IST"), (10.0, "AEST")]
    seen = {round(default_offset_hours, 2)}
    for off, label in commons:
        key = round(off, 2)
        if key in seen:
            continue
        seen.add(key)
        items.append((off, f"{label} ({fmt(off)})", False))
    return "".join(
        f'<option value="{off:g}"{" selected" if sel else ""}>{lbl}</option>'
        for off, lbl, sel in items
    )


def _build_tod_heatmap_html(tod: dict, tz_label: str = "UTC",
                            default_offset_hours: float = 0.0) -> str:
    """Build the Time-of-Day heatmap as self-contained HTML + CSS + JS.

    Renders a horizontal bar chart with four period rows (Night, Morning,
    Afternoon, Evening), a timezone dropdown pre-selected to the report's
    resolved display tz, and client-side re-bucketing via JavaScript.

    No Highcharts dependency — uses pure HTML/CSS bars with JS-driven width
    updates.  The epoch-seconds array is embedded as a compact integer list;
    bucketing uses ``(((epoch + off) % 86400) + 86400) % 86400`` (the
    standard double-modulo idiom) to guarantee non-negative results even
    when JS's sign-preserving ``%`` encounters negative operands.

    Args:
        tod: Report's ``time_of_day`` dict containing ``epoch_secs`` and
            ``buckets``.

    Returns:
        HTML string for embedding in the full report page.  Returns an empty
        string if no user timestamps are available.
    """
    epoch_secs = tod.get("epoch_secs", [])
    if not epoch_secs:
        return ""
    ts_json = json.dumps(epoch_secs, separators=(",", ":"))
    tz_options = _tz_dropdown_options(default_offset_hours, tz_label)

    return f"""\
<div id="tod-container" style="background:#161b22;border:1px solid #30363d;
     border-radius:8px;padding:20px 24px;margin-bottom:28px">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <span style="color:#f0f6fc;font-size:13px;font-weight:600;text-transform:uppercase;
          letter-spacing:0.05em">User Messages by Time of Day</span>
    <select id="tod-tz" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
            border-radius:4px;padding:4px 8px;font-size:11px;cursor:pointer">{tz_options}</select>
    <span style="color:#8b949e;font-size:11px">Total:
      <strong id="tod-total" style="color:#e6edf3">0</strong></span>
  </div>
  <div style="display:flex;flex-direction:column;gap:10px">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:#8b949e;font-size:12px;width:110px;text-align:right">Morning (6\u201312)</span>
      <div style="flex:1;position:relative;height:22px;background:#21262d;border-radius:3px">
        <div id="tod-bar-morning" style="height:100%;background:#8b5cf6;border-radius:3px;
             min-width:2px;transition:width 0.25s ease"></div>
      </div>
      <span id="tod-cnt-morning" style="color:#e6edf3;font-size:12px;min-width:48px;
            text-align:right;font-variant-numeric:tabular-nums">0</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:#8b949e;font-size:12px;width:110px;text-align:right">Afternoon (12\u201318)</span>
      <div style="flex:1;position:relative;height:22px;background:#21262d;border-radius:3px">
        <div id="tod-bar-afternoon" style="height:100%;background:#8b5cf6;border-radius:3px;
             min-width:2px;transition:width 0.25s ease"></div>
      </div>
      <span id="tod-cnt-afternoon" style="color:#e6edf3;font-size:12px;min-width:48px;
            text-align:right;font-variant-numeric:tabular-nums">0</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:#8b949e;font-size:12px;width:110px;text-align:right">Evening (18\u201324)</span>
      <div style="flex:1;position:relative;height:22px;background:#21262d;border-radius:3px">
        <div id="tod-bar-evening" style="height:100%;background:#8b5cf6;border-radius:3px;
             min-width:2px;transition:width 0.25s ease"></div>
      </div>
      <span id="tod-cnt-evening" style="color:#e6edf3;font-size:12px;min-width:48px;
            text-align:right;font-variant-numeric:tabular-nums">0</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px">
      <span style="color:#8b949e;font-size:12px;width:110px;text-align:right">Night (0\u20136)</span>
      <div style="flex:1;position:relative;height:22px;background:#21262d;border-radius:3px">
        <div id="tod-bar-night" style="height:100%;background:#8b5cf6;border-radius:3px;
             min-width:2px;transition:width 0.25s ease"></div>
      </div>
      <span id="tod-cnt-night" style="color:#e6edf3;font-size:12px;min-width:48px;
            text-align:right;font-variant-numeric:tabular-nums">0</span>
    </div>
  </div>
</div>
<script>
(function(){{
  var TS={ts_json};
  var KEYS=['night','morning','afternoon','evening'];

  function bucket(off){{
    var c=[0,0,0,0],s=off*3600;
    for(var i=0;i<TS.length;i++){{
      var h=(((TS[i]+s)%86400)+86400)%86400/3600|0;
      c[h<6?0:h<12?1:h<18?2:3]++;
    }}
    return c;
  }}

  function render(off){{
    var c=bucket(off);
    var mx=Math.max(1,Math.max.apply(null,c));
    var total=0;
    for(var i=0;i<4;i++){{
      var pct=c[i]/mx*100;
      document.getElementById('tod-bar-'+KEYS[i]).style.width=pct+'%';
      document.getElementById('tod-cnt-'+KEYS[i]).textContent=c[i].toLocaleString();
      total+=c[i];
    }}
    document.getElementById('tod-total').textContent=total.toLocaleString();
  }}

  var sel=document.getElementById('tod-tz');
  sel.addEventListener('change',function(){{render(+this.value);}});
  render(+sel.value);
}})();
</script>"""


_CHART_PAGE = 60   # max data points per chart panel before splitting into multiple


def _build_chart_html(
    cats: list, cache_rd: list, cache_wr: list,
    output: list, input_: list, cost: list, x_title: str,
    models: list[str] | None = None,
) -> str:
    """Return the full chart section HTML: containers + controls + JS.

    If len(cats) > _CHART_PAGE the data is split across multiple charts — one
    per page — each labelled 'Turns 1–60', 'Turns 61–120', etc.  A single set
    of 3D-rotation sliders drives all charts simultaneously.

    Optimisations:
    - Chart data is emitted once as a single JSON blob; a shared renderPage()
      function creates each Highcharts instance from that blob.
    - IntersectionObserver lazily renders charts only when scrolled into view.
    - Slider controls sync all rendered charts.

    models: optional per-bar model name list (same length as cats).  When
    provided, the tooltip header shows the model alongside the x-axis label.
    """
    n = len(cats)
    slices = [(s, min(s + _CHART_PAGE, n)) for s in range(0, n, _CHART_PAGE)]
    n_pages = len(slices)
    models_py = models or []

    # --- Build single DATA blob with all page slices -----------------------
    pages_data: list[dict] = []
    for s, e in slices:
        pages_data.append({
            "cats":     cats[s:e],
            "crd":      cache_rd[s:e],
            "cwr":      cache_wr[s:e],
            "out":      output[s:e],
            "inp":      input_[s:e],
            "cost":     cost[s:e],
            "models":   models_py[s:e] if models_py else [],
        })
    data_json = json.dumps(pages_data, separators=(",", ":"))

    # --- Container divs ---------------------------------------------------
    divs: list[str] = []
    for pg, (s, e) in enumerate(slices):
        label = (
            f'<div class="chart-page-label">{x_title}s {s + 1}\u2013{e} of {n}</div>'
            if n_pages > 1 else ""
        )
        divs.append(f'{label}<div id="hc-chart-{pg}" class="hc-lazy" '
                    f'data-pg="{pg}" style="height:380px;padding:8px"></div>')

    containers_html = "\n".join(divs)

    # --- Single JS block: data + renderPage + lazy observer + sliders -----
    script = f"""\
(function () {{
  var charts = [];
  var DATA = {data_json};
  var X_TITLE = '{x_title}';

  function renderPage(pg) {{
    var d = DATA[pg];
    var c = Highcharts.chart('hc-chart-' + pg, {{
      chart: {{
        type: 'column', backgroundColor: '#161b22', plotBorderColor: '#30363d',
        options3d: {{
          enabled: true, alpha: 12, beta: 10, depth: 50, viewDistance: 25,
          frame: {{
            back: {{ color: '#21262d', size: 1 }},
            bottom: {{ color: '#21262d', size: 1 }},
            side: {{ color: '#21262d', size: 1 }}
          }}
        }}
      }},
      title: {{ text: null }},
      xAxis: {{
        categories: d.cats,
        title: {{ text: X_TITLE, style: {{ color: '#8b949e' }} }},
        labels: {{ style: {{ color: '#8b949e', fontSize: '10px' }}, rotation: -45 }},
        lineColor: '#30363d', tickColor: '#30363d'
      }},
      yAxis: [
        {{
          title: {{ text: 'Tokens', style: {{ color: '#8b949e' }} }},
          labels: {{ style: {{ color: '#8b949e', fontSize: '10px' }},
                     formatter: function () {{
                       return this.value >= 1000 ? (this.value / 1000).toFixed(0) + 'k' : this.value;
                     }} }},
          gridLineColor: '#21262d', stackLabels: {{ enabled: false }}
        }},
        {{
          title: {{ text: 'Cost (USD)', style: {{ color: '#d29922' }} }},
          labels: {{ style: {{ color: '#d29922', fontSize: '10px' }},
                     formatter: function () {{ return '$' + this.value.toFixed(4); }} }},
          opposite: true, gridLineWidth: 0
        }}
      ],
      legend: {{
        enabled: true, margin: 20, padding: 12,
        itemStyle: {{ color: '#8b949e', fontSize: '11px', fontWeight: 'normal' }},
        itemHoverStyle: {{ color: '#e6edf3' }}
      }},
      tooltip: {{
        backgroundColor: '#1c2128', borderColor: '#30363d',
        style: {{ color: '#e6edf3', fontSize: '11px' }},
        shared: true,
        formatter: function () {{
          var s = '<b>' + this.x + '</b>';
          if (d.models.length && d.models[this.points[0].point.index]) {{
            s += '&nbsp; <span style="color:#a5d6ff;font-size:10px">' +
                 d.models[this.points[0].point.index] + '</span>';
          }}
          s += '<br/>';
          this.points.forEach(function (p) {{
            var val = p.series.options.yAxis === 1
              ? '$' + p.y.toFixed(4)
              : p.y.toLocaleString() + ' tokens';
            s += '<span style="color:' + p.color + '">\u25cf</span> ' +
                 p.series.name + ': <b>' + val + '</b><br/>';
          }});
          return s;
        }}
      }},
      plotOptions: {{
        column: {{ stacking: 'normal', depth: 30, borderWidth: 0, groupPadding: 0.1 }},
        line:   {{ depth: 0, zIndex: 10, marker: {{ enabled: true, radius: 3 }} }}
      }},
      series: [
        {{ name: 'Cache Read',  data: d.crd,  color: '#d29922', yAxis: 0 }},
        {{ name: 'Cache Write', data: d.cwr,  color: '#9e6a03', yAxis: 0 }},
        {{ name: 'Output',      data: d.out,  color: '#3fb950', yAxis: 0 }},
        {{ name: 'Input (new)', data: d.inp,  color: '#1f6feb', yAxis: 0 }},
        {{ name: 'Cost $', type: 'line', data: d.cost,
           color: '#f78166', yAxis: 1, lineWidth: 2, zIndex: 10 }}
      ],
      credits: {{ enabled: false }},
      exporting: {{ buttons: {{ contextButton: {{
        symbolStroke: '#8b949e', theme: {{ fill: '#161b22' }}
      }} }} }}
    }});
    charts.push(c);
  }}

  /* Render first page immediately, lazy-render the rest on scroll */
  renderPage(0);
  var lazy = document.querySelectorAll('.hc-lazy');
  if ('IntersectionObserver' in window && lazy.length > 1) {{
    var obs = new IntersectionObserver(function (entries) {{
      entries.forEach(function (e) {{
        if (e.isIntersecting) {{
          var pg = +e.target.getAttribute('data-pg');
          if (pg > 0) renderPage(pg);
          obs.unobserve(e.target);
        }}
      }});
    }}, {{ rootMargin: '200px' }});
    for (var i = 1; i < lazy.length; i++) obs.observe(lazy[i]);
  }} else {{
    for (var i = 1; i < DATA.length; i++) renderPage(i);
  }}

  function bindSlider(id, valId, opt) {{
    var el = document.getElementById(id);
    var vEl = document.getElementById(valId);
    el.addEventListener('input', function () {{
      vEl.textContent = el.value + (opt === 'depth' ? '' : '\u00b0');
      charts.forEach(function (c) {{
        var o = c.options.chart.options3d;
        o[opt] = +el.value;
        c.update({{ chart: {{ options3d: o }} }}, true, false, false);
      }});
    }});
  }}
  bindSlider('alpha', 'alpha-val', 'alpha');
  bindSlider('beta',  'beta-val',  'beta');
  bindSlider('depth', 'depth-val', 'depth');
}})();"""

    return f"""\
<div id="chart-container">
  <div class="chart-controls">
    <label>Alpha &nbsp;<input type="range" id="alpha" min="-30" max="30" value="12">
      <span id="alpha-val">12\u00b0</span></label>
    <label style="margin-left:12px">Beta &nbsp;<input type="range" id="beta" min="-30" max="30" value="10">
      <span id="beta-val">10\u00b0</span></label>
    <label style="margin-left:12px">Depth &nbsp;<input type="range" id="depth" min="10" max="120" value="50">
      <span id="depth-val">50</span></label>
  </div>
  {containers_html}
</div>
<script>
{script}
</script>"""


# ---------------------------------------------------------------------------
# Chart library dispatch (vendored, offline, SHA-256 verified)
# ---------------------------------------------------------------------------
#
# The HTML export supports pluggable chart renderers. Each renderer reads
# its JS payload from ``scripts/vendor/charts/<lib>/...`` — no CDN fetch,
# no runtime cache writes, no network access. ``manifest.json`` lists the
# expected SHA-256 per file; the verifier refuses to inline a file whose
# digest doesn't match (defense-in-depth against accidental edits or
# supply-chain tampering).
#
# Current renderers:
#   - "highcharts" — 3D stacked columns (non-commercial license; see LICENSE.txt).
#   - "uplot"      — flat 2D stacked bars + cost line (MIT). Lightest.
#   - "chartjs"    — 2D stacked bar + line combo (MIT). Familiar API.
#   - "none"       — emit the detail page with no chart at all.

_VENDOR_CHARTS_DIR = Path(__file__).parent / "vendor" / "charts"


def _load_chart_manifest() -> dict:
    """Parse ``vendor/charts/manifest.json``. Returns an empty libraries dict
    if the manifest is missing (keeps the tool usable in degraded mode)."""
    mpath = _VENDOR_CHARTS_DIR / "manifest.json"
    if not mpath.exists():
        return {"libraries": {}}
    try:
        return json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[warn] vendor/charts/manifest.json malformed: {exc}", file=sys.stderr)
        return {"libraries": {}}


def _read_vendor_files(library: str, suffix: str) -> str:
    """Read + concatenate vendor files for ``library`` whose path ends in
    ``suffix`` (``.js`` or ``.css``). Verifies each SHA-256 against the
    manifest before inclusion; skips files that fail verification with a
    stderr warning.
    """
    manifest = _load_chart_manifest()
    lib_entry = manifest.get("libraries", {}).get(library)
    if not lib_entry:
        print(f"[warn] chart library '{library}' not in vendor manifest; "
              f"HTML will render without this chart.", file=sys.stderr)
        return ""
    parts: list[str] = []
    for f in lib_entry.get("files", []):
        if not f["path"].endswith(suffix):
            continue
        path = _VENDOR_CHARTS_DIR / f["path"]
        if not path.exists():
            print(f"[warn] vendor file missing: {path}", file=sys.stderr)
            continue
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        expected = f.get("sha256", "")
        if expected and actual != expected:
            print(f"[warn] SHA-256 mismatch for {path.name}: "
                  f"expected {expected[:12]}…, got {actual[:12]}… (skipped)",
                  file=sys.stderr)
            continue
        parts.append(data.decode("utf-8", errors="replace"))
    sep = ";\n" if suffix == ".js" else "\n"
    return sep.join(parts)


def _read_vendor_js(library: str) -> str:
    """Read + concatenate the JS payload for ``library`` from the vendor tree.
    Thin wrapper over ``_read_vendor_files`` for backward compatibility."""
    return _read_vendor_files(library, ".js")


def _read_vendor_css(library: str) -> str:
    """Read + concatenate the CSS payload for ``library`` from the vendor tree.
    Returns empty string if the library has no CSS files."""
    return _read_vendor_files(library, ".css")


def _hc_scripts() -> str:
    """Return Highcharts JS inlined as a single script block.

    Reads the vendored files from ``scripts/vendor/charts/highcharts/v12/``
    and verifies each SHA-256 against the manifest. No CDN, no network.
    """
    return _read_vendor_js("highcharts")


def _extract_chart_series(all_turns: list[dict]) -> dict:
    """Pull the per-turn series the chart renderers all need.

    Returned keys mirror the JSON blob the body-side IIFE consumes:
    ``cats`` (x-axis labels), ``crd`` / ``cwr`` / ``out`` / ``inp`` (token
    series, stacked bottom-to-top), ``cost`` (USD per turn), ``models``
    (per-bar model name for tooltip headers).
    """
    return {
        "cats":   [t["timestamp_fmt"][5:16] for t in all_turns],
        "inp":    [t["input_tokens"]        for t in all_turns],
        "out":    [t["output_tokens"]       for t in all_turns],
        "crd":    [t["cache_read_tokens"]   for t in all_turns],
        "cwr":    [t["cache_write_tokens"]  for t in all_turns],
        "cost":   [round(t["cost_usd"], 4)  for t in all_turns],
        "models": [t["model"]               for t in all_turns],
    }


def _render_chart_highcharts(all_turns: list[dict]) -> tuple[str, str]:
    """Highcharts renderer. Returns ``(chart_body_html, head_html)``.

    ``chart_body_html`` is the full ``<div id="chart-container">…</div>`` block
    dropped in the report body; ``head_html`` is the vendored library bundle
    wrapped in a ready-to-inline ``<script>`` tag for ``<head>``.
    """
    if not all_turns:
        return ("", "")
    s = _extract_chart_series(all_turns)
    body = _build_chart_html(
        s["cats"], s["crd"], s["cwr"], s["out"], s["inp"], s["cost"], "Turn",
        models=s["models"],
    )
    return (body, f"<script>{_hc_scripts()}</script>")


def _build_lib_chart_pages(series: dict, x_title: str) -> tuple[str, str]:
    """Pagination scaffold shared by uPlot and Chart.js renderers.

    Returns ``(containers_html, data_json)``. The renderer wraps these with
    its own per-page render function + IntersectionObserver IIFE.
    Highcharts has its own (richer) builder; this is the lean version.
    """
    n = len(series["cats"])
    slices = [(s, min(s + _CHART_PAGE, n)) for s in range(0, n, _CHART_PAGE)]
    n_pages = len(slices)
    pages_data = [{
        "cats":   series["cats"][s:e],
        "crd":    series["crd"][s:e],
        "cwr":    series["cwr"][s:e],
        "out":    series["out"][s:e],
        "inp":    series["inp"][s:e],
        "cost":   series["cost"][s:e],
        "models": series["models"][s:e],
    } for s, e in slices]
    data_json = json.dumps(pages_data, separators=(",", ":"))

    divs: list[str] = []
    for pg, (s, e) in enumerate(slices):
        label = (
            f'<div class="chart-page-label">{x_title}s {s + 1}\u2013{e} of {n}</div>'
            if n_pages > 1 else ""
        )
        divs.append(f'{label}<div id="chart-pg-{pg}" class="chart-lazy" '
                    f'data-pg="{pg}" style="height:380px;padding:8px"></div>')
    return ("\n".join(divs), data_json)


def _render_chart_uplot(all_turns: list[dict]) -> tuple[str, str]:
    """uPlot renderer (MIT). Returns ``(body_html, head_html)``.

    uPlot has no built-in stacked-bars API — we pre-compute cumulative
    arrays caller-side so each bar series renders as a full stack from the
    baseline (the bottom-most series is drawn last so it sits on top
    visually).  Cost is a separate line series on a right-hand y-axis.
    Pagination + lazy rendering match the Highcharts renderer.
    """
    if not all_turns:
        return ("", "")
    series = _extract_chart_series(all_turns)
    containers_html, data_json = _build_lib_chart_pages(series, "Turn")

    css = _read_vendor_css("uplot")
    js  = _read_vendor_js("uplot")
    if not js:
        return ("", "")

    head_extra_css = """
      .uplot { width: 100% !important; }
      .uplot, .uplot * { color: #8b949e; }
      .u-title { display: none; }
      .u-legend { background: #161b22; color: #e6edf3; font-size: 11px;
                  border-top: 1px solid #30363d; padding: 6px 8px; }
      .u-legend .u-marker { border-radius: 2px; }
      .u-axis { color: #8b949e; }
      .u-cursor-pt { border-color: #58a6ff !important; }
    """

    init = f"""\
(function () {{
  var DATA = {data_json};
  var charts = [];
  function renderPage(pg) {{
    var d = DATA[pg];
    var n = d.cats.length;
    var xs = new Array(n);
    for (var i = 0; i < n; i++) xs[i] = i;
    /* Cumulative stacks bottom-to-top: cache_read | + cache_write |
       + output | + input. Drawing the totals as bars renders them as a
       visual stack because the smaller bars overpaint the bigger ones. */
    var s1 = d.crd.slice();
    var s2 = new Array(n), s3 = new Array(n), s4 = new Array(n);
    for (var i = 0; i < n; i++) {{
      s2[i] = s1[i] + d.cwr[i];
      s3[i] = s2[i] + d.out[i];
      s4[i] = s3[i] + d.inp[i];
    }}
    var bars = uPlot.paths.bars({{ size: [0.7, 60] }});
    var el = document.getElementById('chart-pg-' + pg);
    var w  = el.clientWidth || 800;
    var fmtTokens = function (v) {{
      if (v == null) return '';
      return v >= 1000 ? (v / 1000).toFixed(0) + 'k' : ('' + v);
    }};
    var opts = {{
      width: w, height: 380,
      title: '',
      cursor: {{ drag: {{ x: false, y: false }}, points: {{ size: 6 }} }},
      legend: {{ live: true }},
      scales: {{ x: {{ time: false }}, cost: {{ auto: true }} }},
      axes: [
        {{ stroke: '#8b949e', grid: {{ stroke: '#21262d' }},
           values: function (u, ticks) {{ return ticks.map(function (t) {{
             return d.cats[t] || '';
           }}); }},
           rotate: -45, size: 60 }},
        {{ stroke: '#8b949e', grid: {{ stroke: '#21262d' }},
           values: function (u, ticks) {{ return ticks.map(fmtTokens); }} }},
        {{ scale: 'cost', side: 1, stroke: '#d29922', grid: {{ show: false }},
           values: function (u, ticks) {{
             return ticks.map(function (v) {{ return '$' + v.toFixed(4); }});
           }} }},
      ],
      series: [
        {{ label: 'Turn' }},
        {{ label: 'Input (new)', stroke: '#1f6feb',
           fill: 'rgba(31,111,235,0.85)', paths: bars, points: {{ show: false }},
           value: function (u, v, sIdx, dIdx) {{
             return d.inp[dIdx] != null ? d.inp[dIdx].toLocaleString() : '';
           }} }},
        {{ label: 'Output', stroke: '#3fb950',
           fill: 'rgba(63,185,80,0.85)', paths: bars, points: {{ show: false }},
           value: function (u, v, sIdx, dIdx) {{
             return d.out[dIdx] != null ? d.out[dIdx].toLocaleString() : '';
           }} }},
        {{ label: 'Cache Write', stroke: '#9e6a03',
           fill: 'rgba(158,106,3,0.85)', paths: bars, points: {{ show: false }},
           value: function (u, v, sIdx, dIdx) {{
             return d.cwr[dIdx] != null ? d.cwr[dIdx].toLocaleString() : '';
           }} }},
        {{ label: 'Cache Read', stroke: '#d29922',
           fill: 'rgba(210,153,34,0.85)', paths: bars, points: {{ show: false }},
           value: function (u, v, sIdx, dIdx) {{
             return d.crd[dIdx] != null ? d.crd[dIdx].toLocaleString() : '';
           }} }},
        {{ label: 'Cost $', stroke: '#f78166', width: 2, scale: 'cost',
           points: {{ show: true, size: 4, stroke: '#f78166', fill: '#161b22' }},
           value: function (u, v) {{ return v == null ? '' : '$' + v.toFixed(4); }} }},
      ],
    }};
    /* uPlot wants series rows in the order declared; the bar series are
       drawn back-to-front so the smallest cumulative goes last → visible. */
    var data = [xs, s4, s3, s2, s1, d.cost];
    var u = new uPlot(opts, data, el);
    charts.push(u);
  }}
  renderPage(0);
  var lazy = document.querySelectorAll('.chart-lazy');
  if ('IntersectionObserver' in window && lazy.length > 1) {{
    var obs = new IntersectionObserver(function (entries) {{
      entries.forEach(function (e) {{
        if (e.isIntersecting) {{
          var pg = +e.target.getAttribute('data-pg');
          if (pg > 0) renderPage(pg);
          obs.unobserve(e.target);
        }}
      }});
    }}, {{ rootMargin: '200px' }});
    for (var i = 1; i < lazy.length; i++) obs.observe(lazy[i]);
  }} else {{
    for (var i = 1; i < DATA.length; i++) renderPage(i);
  }}
  window.addEventListener('resize', function () {{
    charts.forEach(function (u) {{
      var el = u.root.parentNode;
      u.setSize({{ width: el.clientWidth || 800, height: 380 }});
    }});
  }});
}})();"""

    body = f"""<div id="chart-container">
{containers_html}
</div>
<script>
{init}
</script>"""

    head_html = (
        f"<style>{css}{head_extra_css}</style>\n"
        f"<script>{js}</script>"
    )
    return (body, head_html)


def _render_chart_chartjs(all_turns: list[dict]) -> tuple[str, str]:
    """Chart.js v4 renderer (MIT). Returns ``(body_html, head_html)``.

    Mixed bar+line: four ``type: 'bar'`` datasets share ``stack: 'tokens'``
    on the left y-axis (``stacked: true``), one ``type: 'line'`` dataset
    rides on the right y-axis ``y1`` for cost. Pagination + lazy
    rendering match the Highcharts renderer.
    """
    if not all_turns:
        return ("", "")
    series = _extract_chart_series(all_turns)
    containers_html, data_json = _build_lib_chart_pages(series, "Turn")

    js = _read_vendor_js("chartjs")
    if not js:
        return ("", "")

    init = f"""\
(function () {{
  var DATA = {data_json};
  Chart.defaults.color = '#8b949e';
  Chart.defaults.borderColor = '#30363d';
  Chart.defaults.font.size = 11;
  function renderPage(pg) {{
    var d = DATA[pg];
    var holder = document.getElementById('chart-pg-' + pg);
    holder.innerHTML = '';
    var canvas = document.createElement('canvas');
    holder.appendChild(canvas);
    var ctx = canvas.getContext('2d');
    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: d.cats,
        datasets: [
          {{ label: 'Cache Read',  data: d.crd, backgroundColor: '#d29922',
             stack: 'tokens', yAxisID: 'y', order: 4 }},
          {{ label: 'Cache Write', data: d.cwr, backgroundColor: '#9e6a03',
             stack: 'tokens', yAxisID: 'y', order: 3 }},
          {{ label: 'Output',      data: d.out, backgroundColor: '#3fb950',
             stack: 'tokens', yAxisID: 'y', order: 2 }},
          {{ label: 'Input (new)', data: d.inp, backgroundColor: '#1f6feb',
             stack: 'tokens', yAxisID: 'y', order: 1 }},
          {{ label: 'Cost $', type: 'line', data: d.cost,
             borderColor: '#f78166', backgroundColor: '#f78166',
             borderWidth: 2, pointRadius: 3, yAxisID: 'y1', order: 0 }},
        ]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        scales: {{
          x: {{ stacked: true, ticks: {{ maxRotation: 45, minRotation: 45,
                color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
          y: {{ stacked: true, position: 'left',
                title: {{ display: true, text: 'Tokens', color: '#8b949e' }},
                ticks: {{ color: '#8b949e', callback: function (v) {{
                  return v >= 1000 ? (v / 1000).toFixed(0) + 'k' : v;
                }} }}, grid: {{ color: '#21262d' }} }},
          y1: {{ position: 'right', stacked: false,
                 title: {{ display: true, text: 'Cost (USD)', color: '#d29922' }},
                 ticks: {{ color: '#d29922', callback: function (v) {{
                   return '$' + v.toFixed(4);
                 }} }}, grid: {{ display: false }} }},
        }},
        plugins: {{
          legend: {{ labels: {{ color: '#8b949e', boxWidth: 12 }} }},
          tooltip: {{
            backgroundColor: '#1c2128', titleColor: '#e6edf3',
            bodyColor: '#e6edf3', borderColor: '#30363d', borderWidth: 1,
            callbacks: {{
              afterTitle: function (items) {{
                if (!items.length) return '';
                var m = d.models[items[0].dataIndex];
                return m ? m : '';
              }},
              label: function (ctx) {{
                var v = ctx.parsed.y;
                if (ctx.dataset.yAxisID === 'y1') {{
                  return ctx.dataset.label + ': $' + v.toFixed(4);
                }}
                return ctx.dataset.label + ': ' + v.toLocaleString() + ' tokens';
              }},
            }},
          }},
        }},
      }},
    }});
  }}
  renderPage(0);
  var lazy = document.querySelectorAll('.chart-lazy');
  if ('IntersectionObserver' in window && lazy.length > 1) {{
    var obs = new IntersectionObserver(function (entries) {{
      entries.forEach(function (e) {{
        if (e.isIntersecting) {{
          var pg = +e.target.getAttribute('data-pg');
          if (pg > 0) renderPage(pg);
          obs.unobserve(e.target);
        }}
      }});
    }}, {{ rootMargin: '200px' }});
    for (var i = 1; i < lazy.length; i++) obs.observe(lazy[i]);
  }} else {{
    for (var i = 1; i < DATA.length; i++) renderPage(i);
  }}
}})();"""

    body = f"""<div id="chart-container">
{containers_html}
</div>
<script>
{init}
</script>"""

    head_html = f"<script>{js}</script>"
    return (body, head_html)


def _render_chart_none(all_turns: list[dict]) -> tuple[str, str]:
    """No-chart renderer. Emits an empty body + empty head — useful when the
    caller wants a minimal detail page with no JS dependencies."""
    del all_turns
    return ("", "")


CHART_RENDERERS = {
    "highcharts": _render_chart_highcharts,
    "uplot":      _render_chart_uplot,
    "chartjs":    _render_chart_chartjs,
    "none":       _render_chart_none,
}


def render_html(report: dict, variant: str = "single",
                nav_sibling: str | None = None,
                chart_lib: str = "highcharts") -> str:
    """Render the full report as a dark-themed HTML page with interactive charts.

    ``variant`` picks the page layout:
    - ``"single"`` (default): everything in one file. Backward-compatible.
    - ``"dashboard"``: summary cards + insight sections + links to the
      detail page. No chart, no turn-level table, no chart-library JS
      inline (massive size win).
    - ``"detail"``: token-usage chart + timeline table + models pricing
      table. No insight sections.

    ``nav_sibling`` is the relative href of the companion file shown in
    the top nav bar. When ``None`` (single-page mode) the nav bar is omitted.

    ``chart_lib`` selects the chart renderer (see ``CHART_RENDERERS``).
    Use ``"none"`` to emit the detail page with no chart at all — smallest
    possible output, no JS dependency.
    """
    include_insights = variant in ("single", "dashboard")
    include_chart    = variant in ("single", "detail")
    slug = report["slug"]
    totals = report["totals"]
    mode = report["mode"]
    _tz_off_for_gen = report.get("tz_offset_hours", 0.0)
    try:
        _gen_dt = datetime.fromisoformat(
            report["generated_at"].replace("Z", "+00:00")
        ).astimezone(timezone(timedelta(hours=_tz_off_for_gen)))
        generated = _gen_dt.strftime("%Y-%m-%d %H:%M:%S") + f" {_short_tz_label(_tz_off_for_gen)}"
    except Exception:
        generated = report["generated_at"][:19].replace("T", " ") + " UTC"
    sessions = report["sessions"]

    # ---- Chart data --------------------------------------------------------
    # Built only when the variant actually renders a chart — saves real work
    # (and, for the dashboard variant, drops the inline library JS bundle).
    # The renderer is selected via ``CHART_RENDERERS[chart_lib]``; each
    # returns ``(body_html, head_js)`` so the caller can place the JS in
    # ``<head>`` while the container div goes in the body.
    chart_html      = ""
    chart_head_html = ""
    if include_chart:
        if mode == "project":
            all_turns = [t for s in sessions for t in s["turns"]]
        else:
            all_turns = sessions[0]["turns"]
        renderer = CHART_RENDERERS.get(chart_lib) or _render_chart_none
        chart_html, chart_head_html = renderer(all_turns)

    # Always resolved for the timeline header (and anywhere else the HTML
    # renders timestamps) — the "detail" variant has no insights block
    # but still needs tz_label for the Timeline table.
    tz_label  = report.get("tz_label", "UTC")
    tz_offset = report.get("tz_offset_hours", 0.0)

    # ---- Insights sections (positioned above charts) ---------------------
    tod_html  = ""
    if include_insights:
        tod_section    = report.get("time_of_day", {})
        rollup_html    = _build_weekly_rollup_html(report.get("weekly_rollup", {}))
        blocks_html    = _build_session_blocks_html(
            report.get("session_blocks", []),
            report.get("block_summary", {}),
            tz_label, tz_offset,
        )
        duration_html  = _build_session_duration_html(sessions, tz_label, tz_offset)
        hod_html       = _build_hour_of_day_html(tod_section, tz_label, tz_offset,
                                                  peak=report.get("peak"))
        punchcard_html = _build_punchcard_html(tod_section, tz_label, tz_offset)
        heatmap_html   = _build_tod_heatmap_html(tod_section, tz_label, tz_offset)
        tod_html       = (rollup_html + blocks_html + duration_html
                          + hod_html + punchcard_html + heatmap_html)

    # ---- Table rows --------------------------------------------------------
    show_mode = _has_fast(report)
    show_ttl  = _has_1h_cache(report)

    # Total columns = #, Time, Model, [Mode], Input, Output, CacheRd, CacheWr, Total, Cost
    _full_cols = 10 + (1 if show_mode else 0)
    # Label cell in subtotal rows spans the non-numeric prefix: #, Time, Model, [Mode]
    _label_span = 4 if show_mode else 3

    def _cwr_cell(tokens: int, tokens_5m: int, tokens_1h: int,
                  ttl: str, bold: bool = False) -> str:
        num = f"{tokens:,}"
        inner = f"<strong>{num}</strong>" if bold else num
        if ttl in ("1h", "mix"):
            cls = "ttl-1h" if ttl == "1h" else "ttl-mix"
            title = f"5m: {tokens_5m:,} · 1h: {tokens_1h:,} tokens"
            badge = f'<span class="badge-ttl {cls}" title="{title}">{ttl}</span>'
            return f'<td class="num" title="{title}">{inner}{badge}</td>'
        return f'<td class="num">{inner}</td>'

    def turn_row(t: dict, session_id: str) -> str:
        bar_w = min(100, int(t["cost_usd"] * 2000))
        mode_td = ""
        if show_mode:
            spd = t.get("speed", "")
            label = "fast" if spd == "fast" else "std"
            cls = ' class="mode-fast"' if spd == "fast" else ' class="mode-std"'
            mode_td = f'<td{cls}>{label}</td>'
        cwr_td = _cwr_cell(
            t["cache_write_tokens"],
            t.get("cache_write_5m_tokens", 0),
            t.get("cache_write_1h_tokens", 0),
            t.get("cache_write_ttl", ""),
        )
        return (
            f'<tr data-session="{session_id[:8]}">'
            f'<td class="num">{t["index"]}</td>'
            f'<td class="ts">{t["timestamp_fmt"]}</td>'
            f'<td class="model">{t["model"]}</td>'
            f'{mode_td}'
            f'<td class="num">{t["input_tokens"]:,}</td>'
            f'<td class="num">{t["output_tokens"]:,}</td>'
            f'<td class="num">{t["cache_read_tokens"]:,}</td>'
            f'{cwr_td}'
            f'<td class="num">{t["total_tokens"]:,}</td>'
            f'<td class="cost"><span class="bar" style="width:{bar_w}px"></span>'
            f'${t["cost_usd"]:.4f}</td>'
            f'</tr>'
        )

    def session_header(i: int, s: dict) -> str:
        if mode != "project":
            return ""
        st = s["subtotal"]
        return (
            f'<tr class="session-header" data-toggle="sess-{i}" role="button">'
            f'<td colspan="{_full_cols}">'
            f'<span class="toggle-arrow">&#9654;</span> '
            f'<strong>Session {i}: {s["session_id"][:8]}…</strong>'
            f'&nbsp; {s["first_ts"]} → {s["last_ts"]}'
            f'&nbsp;·&nbsp; {len(s["turns"])} turns'
            f'&nbsp;·&nbsp; <strong>${st["cost"]:.4f}</strong>'
            f'</td></tr>'
        )

    def subtotal_row(label: str, st: dict) -> str:
        tokens_1h = st.get("cache_write_1h", 0)
        if tokens_1h > 0:
            tokens_5m = st.get("cache_write_5m", 0)
            sub_ttl = "mix" if st.get("cache_write_5m", 0) > 0 else "1h"
        else:
            tokens_5m = st.get("cache_write_5m", 0)
            sub_ttl = ""
        cwr_td = _cwr_cell(st["cache_write"], tokens_5m, tokens_1h, sub_ttl, bold=True)
        return (
            f'<tr class="subtotal">'
            f'<td colspan="{_label_span}"><strong>{label}</strong></td>'
            f'<td class="num"><strong>{st["input"]:,}</strong></td>'
            f'<td class="num"><strong>{st["output"]:,}</strong></td>'
            f'<td class="num"><strong>{st["cache_read"]:,}</strong></td>'
            f'{cwr_td}'
            f'<td class="num"><strong>{st["total"]:,}</strong></td>'
            f'<td class="cost"><strong>${st["cost"]:.4f}</strong></td>'
            f'</tr>'
        )

    table_rows: list[str] = []
    model_rows = ""
    if include_chart:
        for i, s in enumerate(sessions, 1):
            if mode == "project":
                table_rows.append(session_header(i, s))
                table_rows.append(f'<tbody class="session-body" id="sess-{i}" style="display:none">')
            for t in s["turns"]:
                table_rows.append(turn_row(t, s["session_id"]))
            if mode == "project":
                table_rows.append(subtotal_row(f"S{i:02} subtotal", s["subtotal"]))
                table_rows.append('</tbody>')
        table_rows.append(subtotal_row("PROJECT TOTAL" if mode == "project" else "TOTAL", totals))

        model_rows = "".join(
            f'<tr><td><code>{m}</code></td><td class="num">{cnt:,}</td>'
            f'<td class="num">${_pricing_for(m)["input"]:.2f}</td>'
            f'<td class="num">${_pricing_for(m)["output"]:.2f}</td>'
            f'<td class="num">${_pricing_for(m)["cache_read"]:.2f}</td>'
            f'<td class="num">${_pricing_for(m)["cache_write"]:.2f}</td></tr>'
            for m, cnt in sorted(report["models"].items(), key=lambda x: -x[1])
        )

    # Nav bar: cross-link to the companion page (only present in split mode).
    nav_html = ""
    if nav_sibling:
        label_here  = "Dashboard" if variant == "dashboard" else "Detail"
        label_other = "Detail \u2192" if variant == "dashboard" else "\u2190 Dashboard"
        nav_html = (
            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;'
            f'padding:8px 12px;background:#161b22;border:1px solid #30363d;border-radius:6px">'
            f'<span style="color:#8b949e;font-size:11px">You are on:</span>'
            f'<strong style="color:#58a6ff;font-size:12px">{label_here}</strong>'
            f'<a href="{nav_sibling}" style="margin-left:auto;color:#58a6ff;'
            f'font-size:12px;text-decoration:none">{label_other}</a>'
            f'</div>'
        )

    chart_section_html = ""
    if include_chart and chart_html:
        chart_section_html = f'<h2>Token Usage Over Time</h2>\n{chart_html}'

    table_section_html = ""
    if include_chart and table_rows:
        legend_parts = [
            '<b>#</b> turn index (deduplicated) · ',
            f'<b>Time</b> turn start ({tz_label}) · ',
            '<b>Model</b> short model alias · ',
        ]
        if show_mode:
            legend_parts.append('<b>Mode</b> fast / standard · ')
        legend_parts.extend([
            '<b>Input (new)</b> net new <code>input_tokens</code> (uncached) · ',
            '<b>Output</b> <code>output_tokens</code> (includes thinking + tool_use block tokens) · ',
            '<b>CacheRd</b> <code>cache_read_input_tokens</code> · ',
        ])
        if show_ttl:
            legend_parts.append(
                '<b>CacheWr</b> <code>cache_creation_input_tokens</code> '
                '(badge marks 1h-tier turns; hover for 5m/1h split) · '
            )
        else:
            legend_parts.append('<b>CacheWr</b> <code>cache_creation_input_tokens</code> · ')
        legend_parts.extend([
            '<b>Total</b> sum of the four billable token buckets · ',
            '<b>Cost $</b> estimated USD for this turn.',
        ])
        legend_html = '<p class="legend-block">' + ''.join(legend_parts) + '</p>'
        table_section_html = (
            '<h2>Timeline</h2>\n'
            + legend_html + '\n'
            + '<table>\n<thead><tr>\n'
            f'  <th class="num">#</th><th>Time ({tz_label})</th><th>Model</th>\n'
            f'  {"<th>Mode</th>" if show_mode else ""}\n'
            '  <th class="num">Input (new)</th><th class="num">Output</th>\n'
            '  <th class="num">CacheRd</th><th class="num">CacheWr</th>\n'
            '  <th class="num">Total</th><th class="num">Cost $</th>\n'
            f'</tr></thead>\n<tbody>\n{"".join(table_rows)}\n</tbody>\n</table>\n'
        )

    models_section_html = ""
    if include_chart and model_rows:
        models_section_html = (
            '<h2>Models</h2>\n<table class="models-table">\n'
            '<thead><tr><th>Model</th><th class="num">Turns</th>\n'
            '  <th class="num">$/M input</th><th class="num">$/M output</th>\n'
            '  <th class="num">$/M rd</th><th class="num">$/M wr</th></tr></thead>\n'
            f'<tbody>{model_rows}</tbody>\n</table>\n'
        )

    summary_cards_html = ""
    if include_insights:
        ttl_mix_card = ""
        if totals.get("cache_write_1h", 0) > 0:
            pct_1h = 100 * totals["cache_write_1h"] / max(1, totals["cache_write"])
            extra = totals.get("extra_1h_cost", 0.0)
            ttl_mix_card = (
                f'\n  <div class="card amber" '
                f'title="1-hour cache writes cost 2× input vs 1.25× for the 5-minute tier. '
                f'This card shows the premium you paid for longer cache reuse.">'
                f'<div class="val">{pct_1h:.0f}% 1h · ${extra:.4f}</div>'
                f'<div class="lbl">Cache TTL mix (extra paid for 1h)</div></div>'
            )
        summary_cards_html = f'''\
<div class="cards">
  <div class="card amber"><div class="val">${totals['cost']:.4f}</div><div class="lbl">Total cost (USD)</div></div>
  <div class="card green"><div class="val">${totals['cache_savings']:.4f}</div><div class="lbl">Cache savings</div></div>
  <div class="card"><div class="val">{totals['cache_hit_pct']:.1f}%</div><div class="lbl">Cache hit ratio</div></div>
  <div class="card"><div class="val">{totals['total_input']:,}</div><div class="lbl">Total input tokens</div></div>
  <div class="card"><div class="val">{totals['input']:,}</div><div class="lbl">Input tokens (new)</div></div>
  <div class="card"><div class="val">{totals['output']:,}</div><div class="lbl">Output tokens</div></div>
  <div class="card"><div class="val">{totals['cache_read']:,}</div><div class="lbl">Cache read tokens</div></div>
  <div class="card"><div class="val">{totals['cache_write']:,}</div><div class="lbl">Cache write tokens</div></div>{ttl_mix_card}
</div>'''

    toggle_script_html = ""
    if include_chart and mode == "project":
        toggle_script_html = """<script>
document.querySelectorAll('tr.session-header[data-toggle]').forEach(function (hdr) {
  hdr.addEventListener('click', function () {
    var body = document.getElementById(hdr.getAttribute('data-toggle'));
    if (!body) return;
    var open = body.style.display !== 'none';
    body.style.display = open ? 'none' : '';
    hdr.classList.toggle('open', !open);
  });
});
</script>"""

    title_suffix  = (" — Dashboard" if variant == "dashboard"
                     else " — Detail" if variant == "detail" else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Session Metrics — {slug}{title_suffix}</title>
{chart_head_html}
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 13px; padding: 24px; }}
  h1 {{ font-size: 18px; font-weight: 600; margin-bottom: 4px; color: #f0f6fc; }}
  .meta {{ color: #8b949e; font-size: 11px; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 28px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 14px 18px; min-width: 140px; }}
  .card .val {{ font-size: 22px; font-weight: 700; color: #58a6ff; }}
  .card .lbl {{ font-size: 11px; color: #8b949e; margin-top: 2px; }}
  .card.green .val {{ color: #3fb950; }}
  .card.amber .val {{ color: #d29922; }}
  h2 {{ font-size: 14px; font-weight: 600; color: #f0f6fc; margin: 24px 0 10px; }}
  h2 .legend {{ font-size: 11px; font-weight: 400; color: #8b949e;
                margin-left: 10px; }}
  h2 .legend code {{ background: #161b22; border: 1px solid #30363d;
                     border-radius: 3px; padding: 0 4px; font-size: 10px; }}
  h2 .legend b {{ color: #c9d1d9; font-weight: 600; }}
  #chart-container {{ background: #161b22; border: 1px solid #30363d;
                      border-radius: 8px; margin-bottom: 28px; min-height: 420px; }}
  .chart-controls {{ display: flex; gap: 10px; align-items: center;
                     padding: 10px 16px 0; flex-wrap: wrap; }}
  .chart-controls label {{ font-size: 11px; color: #8b949e; display: flex;
                           align-items: center; gap-5px; cursor: pointer; }}
  .chart-controls input[type=range] {{ width: 120px; accent-color: #58a6ff; }}
  .chart-controls span {{ font-size: 11px; color: #58a6ff; min-width: 28px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ background: #161b22; color: #8b949e; font-weight: 500; text-align: left;
        padding: 6px 10px; border-bottom: 1px solid #30363d; white-space: nowrap; }}
  td {{ padding: 4px 10px; border-bottom: 1px solid #21262d; vertical-align: middle; }}
  tr:hover td {{ background: #161b22; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.ts {{ color: #8b949e; white-space: nowrap; }}
  td.model {{ color: #a5d6ff; font-size: 11px; }}
  td.cost {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .bar {{ display: inline-block; height: 8px; background: #1f6feb44;
          border-radius: 2px; margin-right: 6px; vertical-align: middle; }}
  tr.session-header {{ cursor: pointer; }}
  tr.session-header td {{ background: #1c2128; color: #58a6ff; padding: 8px 10px;
                           border-top: 2px solid #30363d; font-size: 12px; }}
  tr.session-header:hover td {{ background: #1f2937; }}
  .toggle-arrow {{ display: inline-block; font-size: 10px; transition: transform 0.15s;
                   margin-right: 4px; }}
  tr.session-header.open .toggle-arrow {{ transform: rotate(90deg); }}
  tr.subtotal td {{ background: #161b22; color: #e6edf3; border-top: 1px solid #30363d; }}
  .models-table td {{ padding: 5px 10px; }}
  .models-table code {{ font-size: 11px; color: #a5d6ff; }}
  td.mode-fast {{ color: #3fb950; font-size: 10px; font-weight: 600; }}
  td.mode-std  {{ color: #484f58; font-size: 10px; }}
  .badge-ttl {{ display: inline-block; margin-left: 6px; padding: 0 5px;
                font-size: 9px; font-weight: 600; letter-spacing: 0.5px;
                border-radius: 3px; vertical-align: middle; cursor: help; }}
  .badge-ttl.ttl-1h  {{ background: #d2992233; color: #e3b341;
                        border: 1px solid #d2992266; }}
  .badge-ttl.ttl-mix {{ background: #8957e533; color: #bc8cff;
                        border: 1px solid #8957e566; }}
  .legend-block {{ color: #8b949e; font-size: 11px; margin: -4px 0 12px;
                   padding: 8px 12px; background: #161b22;
                   border: 1px solid #30363d; border-radius: 6px;
                   line-height: 1.6; }}
  .legend-block b {{ color: #c9d1d9; font-weight: 600; }}
  .legend-block code {{ background: #0d1117; border: 1px solid #30363d;
                        border-radius: 3px; padding: 0 4px; font-size: 10px;
                        color: #a5d6ff; }}
  .chart-page-label {{ font-size: 11px; color: #8b949e; padding: 8px 16px 0;
                       border-top: 1px solid #30363d; margin-top: 4px; }}
</style>
</head>
<body>
{nav_html}
<h1>Session Metrics — {slug}{title_suffix}</h1>
<p class="meta">Generated {generated} &nbsp;·&nbsp; Mode: {mode} &nbsp;·&nbsp;
{len(sessions)} session{'s' if len(sessions) != 1 else ''}, {totals['turns']:,} turns</p>
{summary_cards_html}
{tod_html}
{chart_section_html}
{table_section_html}
{models_section_html}
{toggle_script_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Output dispatch
# ---------------------------------------------------------------------------

_RENDERERS = {
    "text": render_text,
    "json": render_json,
    "csv":  render_csv,
    "md":   render_md,
    "html": render_html,
}
_EXTENSIONS = {"text": "txt", "json": "json", "csv": "csv", "md": "md", "html": "html"}


def _export_dir() -> Path:
    return Path(os.getcwd()) / "exports" / "session-metrics"


def _write_output(fmt: str, content: str, report: dict,
                   suffix: str = "") -> Path:
    """Write ``content`` to an export file; ``suffix`` is appended before
    the extension (e.g. ``"_dashboard"``, ``"_detail"``)."""
    out_dir = _export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = report["mode"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if mode == "project":
        stem = f"project_{ts}"
    else:
        sid = report["sessions"][0]["session_id"][:8]
        stem = f"session_{sid}_{ts}"
    path = out_dir / f"{stem}{suffix}.{_EXTENSIONS[fmt]}"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def _load_session(
    jsonl_path: Path, include_subagents: bool, use_cache: bool = True,
) -> tuple[str, list[dict], list[int]]:
    """Load a session JSONL and return structured data for report building.

    Parses the JSONL file, optionally merging subagent logs, then extracts
    both assistant turns (for token/cost tracking) and user timestamps (for
    time-of-day activity analysis).  User timestamps are extracted from the
    full entry list *before* assistant-only filtering discards them.

    Returns:
        3-tuple of (session_id, assistant_turns, user_epoch_secs) where
        session_id is the JSONL filename stem, assistant_turns is the
        deduplicated/sorted list of raw assistant entries, and
        user_epoch_secs is a sorted list of UTC epoch-seconds for every
        genuine user prompt (tool_results and meta entries excluded).
    """
    entries = _cached_parse_jsonl(jsonl_path, use_cache=use_cache)
    if include_subagents:
        subagent_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
        if subagent_dir.exists():
            for sub in sorted(subagent_dir.glob("*.jsonl")):
                entries += _cached_parse_jsonl(sub, use_cache=use_cache)
    return (
        jsonl_path.stem,
        _extract_turns(entries),
        _extract_user_timestamps(entries, include_sidechain=include_subagents),
    )


def _run_single_session(jsonl_path: Path, slug: str, include_subagents: bool,
                         formats: list[str], tz_offset: float, tz_label: str,
                         peak: dict | None = None,
                         single_page: bool = False,
                         use_cache: bool = True,
                         chart_lib: str = "highcharts") -> None:
    print(f"Session : {jsonl_path.stem}", file=sys.stderr)
    print(f"File    : {jsonl_path}", file=sys.stderr)
    print(file=sys.stderr)

    session_id, turns, user_ts = _load_session(jsonl_path, include_subagents,
                                                 use_cache=use_cache)
    if not turns:
        print("[info] No assistant turns with usage data found.", file=sys.stderr)
        return

    report = _build_report("session", slug, [(session_id, turns, user_ts)],
                            tz_offset_hours=tz_offset, tz_label=tz_label,
                            peak=peak)
    _dispatch(report, formats, single_page=single_page, chart_lib=chart_lib)


def _run_project_cost(slug: str, include_subagents: bool, formats: list[str],
                      tz_offset: float, tz_label: str,
                      peak: dict | None = None,
                      single_page: bool = False,
                      use_cache: bool = True,
                      chart_lib: str = "highcharts") -> None:
    files = _find_jsonl_files(slug)
    if not files:
        print(f"[error] No sessions found for slug: {slug}", file=sys.stderr)
        sys.exit(1)

    sessions_raw = []
    for path in reversed(files):   # oldest first
        sid, turns, user_ts = _load_session(path, include_subagents,
                                              use_cache=use_cache)
        if turns:
            sessions_raw.append((sid, turns, user_ts))

    if not sessions_raw:
        print("[info] No turns with usage data found across any session.", file=sys.stderr)
        return

    report = _build_report("project", slug, sessions_raw,
                            tz_offset_hours=tz_offset, tz_label=tz_label,
                            peak=peak)
    _dispatch(report, formats, single_page=single_page, chart_lib=chart_lib)


def _dispatch(report: dict, formats: list[str],
               single_page: bool = False,
               chart_lib: str = "highcharts") -> None:
    # Always render text to stdout
    print(render_text(report))

    for fmt in formats:
        if fmt == "text":
            continue   # already printed
        if fmt == "html" and not single_page:
            # Split into two files. Dashboard references detail as a sibling
            # by filename-only href so file:// works without a server.
            mode = report["mode"]
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stem = (f"project_{ts}" if mode == "project"
                    else f"session_{report['sessions'][0]['session_id'][:8]}_{ts}")
            dashboard_name = f"{stem}_dashboard.html"
            detail_name    = f"{stem}_detail.html"
            dash = render_html(report, variant="dashboard",
                                nav_sibling=detail_name, chart_lib=chart_lib)
            det  = render_html(report, variant="detail",
                                nav_sibling=dashboard_name, chart_lib=chart_lib)
            p1   = _export_dir() / dashboard_name
            p2   = _export_dir() / detail_name
            _export_dir().mkdir(parents=True, exist_ok=True)
            p1.write_text(dash, encoding="utf-8")
            p2.write_text(det,  encoding="utf-8")
            print(f"[export] HTML (dashboard) → {p1}", file=sys.stderr)
            print(f"[export] HTML (detail)    → {p2}", file=sys.stderr)
            continue
        if fmt == "html":
            content = render_html(report, variant="single", chart_lib=chart_lib)
        else:
            content = _RENDERERS[fmt](report)
        path = _write_output(fmt, content, report)
        print(f"[export] {fmt.upper():4} → {path}", file=sys.stderr)


def _list_sessions(slug: str) -> None:
    files = _find_jsonl_files(slug)
    if not files:
        print(f"No sessions found for slug: {slug}")
        return
    print(f"Sessions for {slug}:")
    print(f"  {'Session UUID':<40} {'Modified':<20} {'Size':>8}")
    print("  " + "-" * 72)
    for p in files:
        stat = p.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {p.stem:<40} {mtime:<20} {stat.st_size / 1024:>6.1f}K")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tally Claude Code session token usage and cost estimates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--session", "-s", metavar="UUID", type=_validate_session_id,
                   help="Session UUID to analyse. Also reads $CLAUDE_SESSION_ID.")
    p.add_argument("--slug", metavar="SLUG", type=_validate_slug,
                   help="Project slug (use --slug=<val> when value starts with '-'). "
                        "Also reads $CLAUDE_PROJECT_SLUG.")
    p.add_argument("--list", "-l", action="store_true",
                   help="List available sessions for this project and exit.")
    p.add_argument("--project-cost", "-p", action="store_true",
                   help="Show all sessions in chronological order with per-session "
                        "subtotals and a grand project total.")
    p.add_argument("--output", "-o", nargs="+", metavar="FMT",
                   choices=["text", "json", "csv", "md", "html"],
                   help="Export formats in addition to stdout text. "
                        "One or more of: json csv md html. "
                        "Written to exports/session-metrics/ in the project root.")
    p.add_argument("--include-subagents", action="store_true",
                   help="Also tally spawned subagent JSONL files.")
    p.add_argument("--tz", metavar="IANA",
                   help="IANA timezone for time-of-day bucketing "
                        "(e.g. 'America/Los_Angeles', 'Australia/Brisbane'). "
                        "Defaults to system local timezone.")
    p.add_argument("--utc-offset", type=float, metavar="H",
                   help="Fixed UTC offset in hours for time-of-day bucketing "
                        "(e.g. -8, 5.5). DST-naive; use --tz for DST-aware.")
    p.add_argument("--peak-hours", type=_parse_peak_hours, metavar="H-H",
                   help="Overlay a translucent band on the hour-of-day chart "
                        "for the given hour range (e.g. '5-11'). Community-reported; "
                        "not an official Anthropic SLA.")
    p.add_argument("--peak-tz", metavar="IANA",
                   help="IANA tz the peak hours are defined in (default: "
                        "'America/Los_Angeles'). Only used when --peak-hours is set.")
    p.add_argument("--single-page", action="store_true",
                   help="HTML export: emit a single self-contained file instead "
                        "of the default 2-page split (dashboard + detail).")
    p.add_argument("--no-cache", action="store_true",
                   help="Skip the parse cache at ~/.cache/session-metrics/parse/ "
                        "and always re-parse JSONL from scratch.")
    p.add_argument("--chart-lib", metavar="LIB",
                   choices=sorted(CHART_RENDERERS.keys()),
                   default="highcharts",
                   help="Chart renderer for HTML export. One of: "
                        f"{', '.join(sorted(CHART_RENDERERS.keys()))}. "
                        "Default: highcharts (vendored, non-commercial). "
                        "Alternatives: uplot/chartjs (MIT). "
                        "Use 'none' for a no-JS detail page.")
    return p


def _maybe_warn_chart_license(chart_lib: str, formats: list[str]) -> None:
    """Surface non-commercial licensing notice when HTML is exported with
    Highcharts. Silent for ``none`` or when the user isn't exporting HTML."""
    if "html" not in formats:
        return
    manifest = _load_chart_manifest()
    entry = manifest.get("libraries", {}).get(chart_lib, {})
    if entry.get("license", "").startswith("non-commercial"):
        print(f"[info] Chart library '{chart_lib}' is under a "
              f"{entry['license']} license. Commercial distribution of the "
              f"generated HTML may require a paid upstream license. Pass "
              f"--chart-lib none to opt out.", file=sys.stderr)


def main() -> None:
    args = _build_parser().parse_args()
    slug = args.slug or _env_slug() or _cwd_to_slug()
    _validate_slug(slug)
    formats: list[str] = args.output or []
    tz_offset, tz_label = _resolve_tz(args.tz, args.utc_offset)
    peak = _build_peak(args.peak_hours, args.peak_tz)
    chart_lib: str = args.chart_lib
    _maybe_warn_chart_license(chart_lib, formats)

    if args.list:
        _list_sessions(slug)
        return

    if args.project_cost:
        print(f"Slug : {slug}", file=sys.stderr)
        print(f"TZ   : {tz_label} (UTC{'+' if tz_offset >= 0 else '-'}{abs(tz_offset):g})", file=sys.stderr)
        print(file=sys.stderr)
        _run_project_cost(slug, args.include_subagents, formats, tz_offset, tz_label,
                           peak=peak, single_page=args.single_page,
                           use_cache=not args.no_cache,
                           chart_lib=chart_lib)
        return

    jsonl_path, resolved_slug = _resolve_session(args)
    print(f"Slug    : {resolved_slug}", file=sys.stderr)
    print(f"TZ      : {tz_label} (UTC{'+' if tz_offset >= 0 else '-'}{abs(tz_offset):g})", file=sys.stderr)
    _run_single_session(jsonl_path, resolved_slug, args.include_subagents, formats,
                         tz_offset, tz_label, peak=peak, single_page=args.single_page,
                         use_cache=not args.no_cache,
                         chart_lib=chart_lib)


if __name__ == "__main__":
    main()
