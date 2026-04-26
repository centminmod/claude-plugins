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
  uv run python session-metrics.py --no-include-subagents # skip spawned agents (default: included)

--output accepts one or more of: text json csv md html
  Writes to <cwd>/exports/session-metrics/<name>_<timestamp>.<ext>
  Text is always printed to stdout; other formats are written to files.

Environment variables (all optional — CLI flags take precedence):
  CLAUDE_SESSION_ID       Session UUID to analyse
  CLAUDE_PROJECT_SLUG     Project slug override (e.g. -Volumes-foo-bar-project)
  CLAUDE_PROJECTS_DIR     Override ~/.claude/projects (default: ~/.claude/projects)
"""

import argparse
import atexit
import csv as csv_mod
import functools
import gzip
import hashlib
import html as html_mod
import io
import json
import os
import re
import secrets
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
    # --- Non-Anthropic models (OpenRouter rates, 2026-04-25; no prompt caching) ---
    # GLM models — Z.ai / Zhipu AI
    "glm-4.7":                   {"input":  0.38, "output":  1.74, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "glm-5":                     {"input":  0.60, "output":  2.08, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "glm-5.1":                   {"input":  1.05, "output":  3.50, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # Google Gemma 4 — OpenRouter: google/gemma-4-26b-a4b-it @ $0.06/$0.33; prefix covers Ollama variants
    "google/gemma-4-26b-a4b":    {"input":  0.06, "output":  0.33, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "gemma4":                    {"input":  0.06, "output":  0.33, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # Qwen3.5 9B — OpenRouter: qwen/qwen3.5-9b @ $0.10/$0.15
    "qwen3.5:9b":                {"input":  0.10, "output":  0.15, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # OpenAI GPT-5.5 family (via OpenRouter, 2026-04-25) — Pro before base
    "openai/gpt-5.5-pro":        {"input": 30.00, "output": 180.00, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "openai/gpt-5.5":            {"input":  5.00, "output":  30.00, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # DeepSeek V4
    "deepseek/deepseek-v4-pro":  {"input":  1.74, "output":   3.48, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "deepseek/deepseek-v4-flash":{"input":  0.14, "output":   0.28, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # Xiaomi MiMo V2.5 — Pro before base
    "xiaomi/mimo-v2.5-pro":      {"input":  1.00, "output":   3.00, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    "xiaomi/mimo-v2.5":          {"input":  0.40, "output":   2.00, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # Moonshot Kimi K2.6
    "moonshotai/kimi-k2.6":      {"input": 0.7448, "output":  4.655, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # Qwen 3.6 Plus
    "qwen/qwen3.6-plus":         {"input": 0.325,  "output":   1.95, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # MiniMax M2.7
    "minimax/minimax-m2.7":      {"input":  0.30, "output":   1.20, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
    # GLM-5-Turbo (Z.ai) — must precede glm-5 in prefix scan; regex guard also added below
    "z-ai/glm-5-turbo":          {"input":  1.20, "output":   4.00, "cache_read": 0.00, "cache_write": 0.00, "cache_write_1h": 0.00},
}
_DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75, "cache_write_1h": 6.00}

# Regex patterns for flexible model-ID matching — checked between exact match and prefix
# sweep. re.search so partial IDs (no provider prefix, date suffixes, :tag qualifiers)
# still resolve. More-specific patterns must come first within each family.
_PRICING_PATTERNS: list[tuple[re.Pattern[str], dict[str, float]]] = [
    # OpenAI GPT-5.5 — Pro before base
    (re.compile(r"gpt-5\.5.*pro",           re.I), _PRICING["openai/gpt-5.5-pro"]),
    (re.compile(r"gpt-5\.5",                re.I), _PRICING["openai/gpt-5.5"]),
    # DeepSeek V4 (separator between provider prefix and v4 may vary)
    (re.compile(r"deepseek.v4.*pro",        re.I), _PRICING["deepseek/deepseek-v4-pro"]),
    (re.compile(r"deepseek.v4.*flash",      re.I), _PRICING["deepseek/deepseek-v4-flash"]),
    # Xiaomi MiMo V2.5 — Pro before base
    (re.compile(r"mimo.v2\.5.*pro",         re.I), _PRICING["xiaomi/mimo-v2.5-pro"]),
    (re.compile(r"mimo.v2\.5",              re.I), _PRICING["xiaomi/mimo-v2.5"]),
    # Moonshot Kimi K2.6
    (re.compile(r"kimi.k2\.6",              re.I), _PRICING["moonshotai/kimi-k2.6"]),
    # Qwen 3.6 Plus
    (re.compile(r"qwen3\.6.*plus",          re.I), _PRICING["qwen/qwen3.6-plus"]),
    # MiniMax M2.7
    (re.compile(r"minimax.m2\.7",           re.I), _PRICING["minimax/minimax-m2.7"]),
    # GLM-5-Turbo before the bare glm-5 prefix entry
    (re.compile(r"glm-5-turbo",             re.I), _PRICING["z-ai/glm-5-turbo"]),
]

# Module-level advisory state — populated during parsing, printed via atexit.
# Sets/lists avoid the `global` keyword; atexit fires at normal process exit.
_UNKNOWN_MODELS_SEEN: set[str] = set()
_FAST_MODE_TURNS: list[int] = [0]  # [0] is the running count


def _print_run_advisories() -> None:
    if _UNKNOWN_MODELS_SEEN:
        names = ", ".join(sorted(_UNKNOWN_MODELS_SEEN))
        print(
            f"[warn] Unknown model(s) priced at Sonnet rates ($3/$15 per 1M tokens): {names}. "
            "Add to references/pricing.md to fix.",
            file=sys.stderr,
        )
    if _FAST_MODE_TURNS[0]:
        n = _FAST_MODE_TURNS[0]
        print(
            f"[note] {n} fast-mode turn{'s' if n != 1 else ''} detected; "
            "cost shown is base-rate × 1.0 (actual is ~6×). "
            "See references/pricing.md § Fast mode.",
            file=sys.stderr,
        )


atexit.register(_print_run_advisories)


def _pricing_for(model: str) -> dict[str, float]:
    if model in _PRICING:
        return _PRICING[model]
    # Regex patterns before prefix sweep so specific variants (e.g. glm-5-turbo)
    # aren't swallowed by a shorter prefix (e.g. glm-5).
    for pattern, rates in _PRICING_PATTERNS:
        if pattern.search(model):
            return rates
    for prefix, rates in _PRICING.items():
        if model.startswith(prefix):
            return rates
    _UNKNOWN_MODELS_SEEN.add(model)
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
    # Known limitation: fast-mode turns (Opus 4.6 research preview, usage.speed
    # == "fast") bill at 6x standard base rates. Not multiplied here — fast-mode
    # cost is therefore underestimated by 6x for those turns. See
    # references/pricing.md § "Fast mode" for the full note.
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
    """Build a stable cache-key filename from path hash, stem, mtime, and ver.

    An 8-hex-char SHA1 of the resolved absolute path disambiguates two JSONLs
    that share a UUID stem (e.g. identical filenames in sibling project dirs).
    Using ``mtime_ns`` (nanoseconds since epoch) means a touched JSONL always
    invalidates the cache. Bumping ``_SCRIPT_VERSION`` invalidates every
    existing blob — safe default when the parser shape changes.
    """
    try:
        abs_path = str(path.resolve())
    except OSError:
        abs_path = str(path)
    path_hash = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
    return f"{path.stem}__{path_hash}__{mtime_ns}__{_SCRIPT_VERSION}.json.gz"


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
    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable — fall through to fresh parse.
        pass

    entries = _parse_jsonl(path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash mid-write doesn't leave a corrupt cache.
        # Randomize the tmp suffix with pid + 4 bytes of entropy so two
        # concurrent writers on the same cache_path never collide on the
        # same tmp file (POSIX os.replace is atomic, but two writers racing
        # on the same tmp could interleave bytes prior to replace()).
        tmp = cache_path.with_suffix(
            f"{cache_path.suffix}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(entries, fh, separators=(",", ":"))
        tmp.replace(cache_path)
    except OSError:
        # Non-fatal — the parse already succeeded.
        pass
    return entries


# Resume-marker detection: two high-precision fingerprints produce a no-op
# `model: "<synthetic>"` assistant turn we want to surface as a timeline
# divider rather than a billable row.
#
# 1. `/exit` local-command triplet replayed by `claude -c` into the resumed
#    JSONL (Session 22 discovery). Matched via _EXIT_CMD_MARKER in a
#    plain-string user content.
# 2. An `isMeta` user entry with text `Continue from where you left off.`
#    (Session 34 discovery) — the desktop client injects this placeholder
#    pair when an auto-continue attempt couldn't reach the backend (e.g.
#    five-hour rate-limit window). The user can't type `isMeta`, and the
#    synthetic self-reply `No response requested.` makes the pair
#    unambiguous. Matched via _CONTINUE_FROM_RESUME_MARKER in a
#    text-block list user content.
#
# See CLAUDE-session-metrics-development-history.md S22 for the original
# corpus-scan data; the S34 scan confirmed 3 new disjoint matches across
# 7,731 JSONLs with zero overlap into unrelated synthetic flows.
_EXIT_CMD_MARKER = "<command-name>/exit</command-name>"
_CONTINUE_FROM_RESUME_MARKER = "Continue from where you left off."
_RESUME_LOOKBACK_USER_ENTRIES = 10


def _resume_fingerprint_match(recent_user_contents: list) -> bool:
    """True if any recent user entry carries a resume-marker fingerprint."""
    for c in recent_user_contents:
        if isinstance(c, str) and _EXIT_CMD_MARKER in c:
            return True
        if isinstance(c, list):
            for block in c:
                if (isinstance(block, dict)
                        and block.get("type") == "text"
                        and _CONTINUE_FROM_RESUME_MARKER in (block.get("text") or "")):
                    return True
    return False


def _extract_turns(entries: list[dict]) -> list[dict]:
    """Deduplicate on message.id and return one entry per assistant turn.

    Claude Code writes a single assistant response across **multiple JSONL
    entries** that all share the same ``message.id`` and an identical
    ``usage`` dict, but each carries a **different single content block**
    (one thinking block, one text block, one tool_use block, etc.).  This
    is how Anthropic's streaming output is persisted.  Dedup strategy:

    - ``usage``, ``model``, and timestamp come from the **last** occurrence
      (canonical "message settled" snapshot; cost math was always correct
      because ``usage`` is constant across occurrences).
    - ``content`` is the **union** of content blocks across **every**
      occurrence (so the turn record reflects the full thinking + text +
      tool_use distribution the model actually emitted).  Empirically,
      each occurrence contributes exactly one distinct block and they never
      overlap; if Claude Code ever starts shipping cumulative snapshots
      alongside incremental ones, we'd need to dedup block-by-block here.

    Each returned entry has ``_preceding_user_content`` attached — the
    ``message.content`` of the user entry immediately before this turn's
    **first** occurrence in the raw stream (content-block counters use
    this to attribute ``tool_result`` / ``image`` blocks to the turn that
    consumed them).

    Also attaches ``_is_resume_marker``: True when the turn is a synthetic
    no-op whose preceding ``_RESUME_LOOKBACK_USER_ENTRIES`` user entries
    carry either of two high-precision fingerprints:

    - A ``/exit`` local-command triplet (``claude -c`` resume, Session 22).
    - A ``"Continue from where you left off."`` isMeta user entry (desktop
      auto-continue placeholder, Session 34 — typically a five-hour
      rate-limit backoff where the client couldn't reach the API).

    Precision is high (both fingerprints are client-generated and the
    ``<synthetic>`` assistant reply is unambiguous); recall is incomplete
    (resumes after Ctrl+C / crash leave no trace).
    """
    last_entry: dict[str, dict] = {}
    merged_content: dict[str, list] = {}
    preceding_user: dict[str, object] = {}
    # Per-turn predecessor timestamp — the ISO-8601 timestamp of the user or
    # tool_result entry immediately before this assistant turn's first
    # streaming chunk. Drives ``latency_seconds`` (the model's wall-clock
    # response time for this single turn). First-occurrence wins, mirroring
    # ``preceding_user`` above.
    preceding_user_ts: dict[str, str] = {}
    # Phase-B: links from a user entry's ``toolUseResult.agentId`` to the
    # ``tool_use_id`` of every ``tool_result`` block in its content. Indexed
    # by the *next* assistant ``msg_id`` so subagent attribution can map
    # ``tool_use.id → agentId`` after turn assembly.
    preceding_user_agent_links: dict[str, list[tuple[str, str]]] = {}
    resume_marker_msg_ids: set[str] = set()
    recent_user_contents: list[object] = []
    last_user_content = None
    last_user_timestamp: str = ""
    last_user_agent_links: list[tuple[str, str]] = []
    for entry in entries:
        t = entry.get("type")
        if t == "user":
            msg = entry.get("message") or {}
            last_user_content = msg.get("content")
            # Use the entry's own timestamp; do not fall back to the previous
            # user's. Empty/missing → blank, so downstream latency math
            # records ``None`` rather than fabricating a gap against an
            # earlier (unrelated) user turn.
            last_user_timestamp = entry.get("timestamp", "") or ""
            recent_user_contents.append(last_user_content)
            if len(recent_user_contents) > _RESUME_LOOKBACK_USER_ENTRIES:
                recent_user_contents.pop(0)
            # Phase-B: extract Agent/Task tool_result agentId linkage.
            # ``toolUseResult.agentId`` is a top-level field on the JSONL
            # entry that Claude Code synthesises when an Agent/Task
            # subagent completes. We pair it with every ``tool_result``
            # block's ``tool_use_id`` in the message content (typically
            # one block, but we scan all to be safe).
            agent_links: list[tuple[str, str]] = []
            tur = entry.get("toolUseResult")
            tur_agent_id = ""
            if isinstance(tur, dict):
                aid = tur.get("agentId")
                if isinstance(aid, str) and aid:
                    tur_agent_id = aid
            if tur_agent_id and isinstance(last_user_content, list):
                for block in last_user_content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tuid = block.get("tool_use_id")
                    if isinstance(tuid, str) and tuid:
                        agent_links.append((tuid, tur_agent_id))
            last_user_agent_links = agent_links
            continue
        if t != "assistant":
            continue
        msg = entry.get("message", {})
        if "usage" not in msg:
            continue
        msg_id = msg.get("id")
        if not msg_id:
            continue
        # Resume-marker detection runs once per msg_id (first occurrence);
        # streaming dupes of the same synthetic msg_id carry the same
        # preceding-user context by construction.
        if msg.get("model") == "<synthetic>" and msg_id not in resume_marker_msg_ids:
            if _resume_fingerprint_match(recent_user_contents):
                resume_marker_msg_ids.add(msg_id)
        # First-occurrence wins for the preceding user pointer — streaming
        # echo entries of the same msg_id don't see a new user prompt in
        # between, so the triggering user entry is the one we saw before
        # the first streaming chunk.
        if msg_id not in preceding_user:
            preceding_user[msg_id] = last_user_content
            preceding_user_ts[msg_id] = last_user_timestamp
            preceding_user_agent_links[msg_id] = list(last_user_agent_links)
        content = msg.get("content")
        if isinstance(content, list):
            merged_content.setdefault(msg_id, []).extend(content)
        last_entry[msg_id] = entry
    turns: list[dict] = []
    for msg_id, entry in last_entry.items():
        merged_msg = {**entry["message"], "content": merged_content.get(msg_id, [])}
        turns.append({
            **entry,
            "message": merged_msg,
            "_preceding_user_content": preceding_user.get(msg_id),
            "_preceding_user_timestamp": preceding_user_ts.get(msg_id, ""),
            "_preceding_user_agent_links": preceding_user_agent_links.get(msg_id, []),
            "_is_resume_marker": msg_id in resume_marker_msg_ids,
        })
    turns.sort(key=lambda e: e.get("timestamp", ""))
    return turns


# Content-block letter codes used in the per-turn Content cell.
_CONTENT_LETTERS = (
    ("thinking",    "T"),
    ("tool_use",    "u"),
    ("text",        "x"),
    ("tool_result", "r"),
    ("image",       "i"),
)


def _count_content_blocks(content) -> tuple[dict[str, int], list[str]]:
    """Count content blocks by type. Return (counts, tool_names).

    ``content`` is the ``message.content`` field, which is either a list of
    block dicts (normal case) or a plain string (rare: old-style user prompts)
    or missing entirely.  Non-list content has no structured blocks, so the
    returned counts are all zero.
    """
    counts = {"thinking": 0, "tool_use": 0, "text": 0,
              "tool_result": 0, "image": 0}
    names: list[str] = []
    if not isinstance(content, list):
        return counts, names
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t in counts:
            counts[t] += 1
        if t == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return counts, names


# ---------------------------------------------------------------------------
# Per-turn drill-down helpers
# ---------------------------------------------------------------------------
# These feed the HTML detail report's right-side drawer + Prompts section.
# All five are defensive against the JSONL's two observed user-content shapes
# (plain string OR list[block]) and return plain strings that are safe to
# HTML-escape at the point of insertion.

# `<command-name>/foo</command-name>` is the wrapped slash-command marker CC
# writes when the user types a local command. Unwrapped `/foo` appears when
# the user types a slash command as a chat message.
_SLASH_WRAPPED_RE  = re.compile(r"<command-name>\s*(/[A-Za-z][\w-]*)\s*</command-name>")
_SLASH_BARE_RE     = re.compile(r"^\s*(/[A-Za-z][\w-]*)\b")
# Stripped at prompt-extract time so the snippet shows the user's intent, not
# the plumbing. `<local-command-stdout>…</local-command-stdout>` wraps the
# stdout of a local command and isn't the user's typing.
_XML_MARKER_RE     = re.compile(
    r"<(?:command-name|command-message|command-args|local-command-stdout|"
    r"local-command-stderr|local-command-caveat|system-reminder)[^>]*>"
    r"[\s\S]*?</(?:command-name|command-message|command-args|local-command-stdout|"
    r"local-command-stderr|local-command-caveat|system-reminder)>",
    re.IGNORECASE,
)

# Bound on embedded assistant-text payload to keep the HTML JSON blob tractable
# even when a session has a few 10k-char monologues. Prompt text is bounded by
# the natural shape of user input and typically doesn't need a cap.
_ASSISTANT_TEXT_CAP = 2000
_PROMPT_TEXT_CAP   = 1000


def _truncate(text: str, n: int) -> str:
    """Slice to ``n`` characters, appending an ellipsis when truncated."""
    if not isinstance(text, str):
        return ""
    if len(text) <= n:
        return text
    # Prefer a clean break at whitespace within the last 20% of the window
    cut = text[:n].rstrip()
    return cut + "\u2026"


def _extract_user_prompt_text(content) -> str:
    """Flatten a user-entry ``message.content`` to a single prompt string.

    Accepts either a plain string (rare: old-style prompts) or a list of
    content blocks. Strips XML markers (<command-name>, <local-command-stdout>,
    <system-reminder>, etc.) so the returned snippet reflects the user's
    intent, not the plumbing around it. Ignores ``tool_result`` / ``image``
    blocks — those aren't user typing and are already counted separately.
    """
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str) and txt:
                    parts.append(txt)
        raw = "\n".join(parts)
    else:
        return ""
    # Strip XML markers (including their inner text) before collapsing whitespace.
    raw = _XML_MARKER_RE.sub("", raw).strip()
    # Collapse runs of whitespace so snippets don't waste characters on
    # indentation or blank lines.
    raw = re.sub(r"\s+", " ", raw)
    return raw


def _extract_slash_command(prompt_text: str, raw_content=None) -> str:
    """Return a leading slash-command name (``/clear``) or empty string.

    Checks the wrapped XML form first (matches even if ``prompt_text`` has
    been stripped of XML markers), then falls back to a bare `/foo` at the
    start of the user prompt. Returns "" when neither matches.
    """
    if isinstance(raw_content, str):
        m = _SLASH_WRAPPED_RE.search(raw_content)
        if m:
            return m.group(1)
    elif isinstance(raw_content, list):
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text") or ""
                m = _SLASH_WRAPPED_RE.search(txt)
                if m:
                    return m.group(1)
    if isinstance(prompt_text, str):
        m = _SLASH_BARE_RE.match(prompt_text)
        if m:
            return m.group(1)
    return ""


def _extract_assistant_text(content) -> str:
    """Join all assistant ``text`` blocks into a single string.

    Ignores ``thinking`` blocks (signature-only anyway) and ``tool_use``
    blocks (captured separately in ``tool_use_detail``). Caps at
    ``_ASSISTANT_TEXT_CAP`` characters so the embedded JSON payload stays
    bounded for very long monologue turns.
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
    raw = "\n\n".join(parts).strip()
    if len(raw) > _ASSISTANT_TEXT_CAP:
        raw = raw[:_ASSISTANT_TEXT_CAP].rstrip() + "\u2026"
    return raw


def _summarise_tool_input(name: str, tool_input) -> str:
    """One-line preview of a ``tool_use`` block's ``input`` dict.

    Picks the most meaningful field per tool to surface in the drawer's tool
    list. Falls back to a truncated ``repr`` for unknown tools. The returned
    string is plain text; escape at the point of insertion.
    """
    if not isinstance(tool_input, dict):
        return ""
    # Tool-specific fields that carry the actual "what did Claude do" signal.
    if name == "Bash":
        cmd = tool_input.get("command") or ""
        if isinstance(cmd, str):
            return cmd.splitlines()[0][:160] if cmd else ""
    if name in ("Read", "Write", "NotebookRead", "NotebookEdit"):
        p = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return str(p)[:160]
    if name == "Edit":
        p = tool_input.get("file_path") or ""
        return str(p)[:160]
    if name == "Grep":
        pat = tool_input.get("pattern") or ""
        path = tool_input.get("path") or ""
        return f"{pat}" + (f"  in {path}" if path else "")
    if name == "Glob":
        return str(tool_input.get("pattern") or "")[:160]
    if name == "Agent" or name == "Task":
        return str(tool_input.get("description") or tool_input.get("subagent_type") or "")[:160]
    if name == "WebFetch" or name == "WebSearch":
        return str(tool_input.get("url") or tool_input.get("query") or "")[:160]
    if name == "TodoWrite":
        todos = tool_input.get("todos")
        if isinstance(todos, list):
            return f"{len(todos)} todo item(s)"
    # Generic fallback: best-effort short JSON
    try:
        j = json.dumps(tool_input, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return j[:160] + ("\u2026" if len(j) > 160 else "")


# ---------------------------------------------------------------------------
# Time-of-day analysis
# ---------------------------------------------------------------------------

_TOD_PERIODS = (
    ("night",     0,  6),   # 00:00–05:59
    ("morning",   6, 12),   # 06:00–11:59
    ("afternoon", 12, 18),  # 12:00–17:59
    ("evening",   18, 24),  # 18:00–23:59
)


def _parse_iso_dt(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp to a tz-aware ``datetime``; ``None`` on failure.

    Catches the union of error types historically swallowed at every call
    site so each caller's existing safety net is preserved unchanged.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError, OSError):
        return None


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
        dt = _parse_iso_dt(entry.get("timestamp", ""))
        if dt is None:
            continue
        try:
            timestamps.append(int(dt.timestamp()))
        except (OSError, OverflowError):
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
                peak_tz: str | None,
                strict: bool = False) -> dict | None:
    """Build a ``peak`` section from CLI inputs, resolving the peak tz offset.

    Returns None when ``peak_hours`` is not set. Defaults ``peak_tz`` to
    ``America/Los_Angeles`` (where the "peak hours" terminology originates
    in community reports) when only ``peak_hours`` is provided.

    When ``strict`` is True and the IANA zone can't be resolved (e.g. on
    Windows without the ``tzdata`` pip package), raises ``SystemExit``
    with an actionable message instead of warning and falling back to UTC.
    """
    if peak_hours is None:
        return None
    tz_name = peak_tz or "America/Los_Angeles"
    try:
        zi = ZoneInfo(tz_name)
        delta = datetime.now(zi).utcoffset()
        off = delta.total_seconds() / 3600.0 if delta else 0.0
    except ZoneInfoNotFoundError:
        msg = (
            f"ZoneInfo not found for peak-tz {tz_name!r}. "
            "On Windows, install the 'tzdata' package "
            "(pip install tzdata) for IANA tz support."
        )
        if strict:
            print(f"[error] {msg}", file=sys.stderr)
            raise SystemExit(2)
        print(f"[warn] {msg} Falling back to UTC.", file=sys.stderr)
        off, tz_name = 0.0, "UTC"
    start, end = peak_hours
    return {
        "start":           start,
        "end":             end,
        "tz_offset_hours": off,
        "tz_label":        tz_name,
        "note":            "unofficial \u2014 community-reported",
    }


def _resolve_tz(tz_name: str | None, utc_offset: float | None,
                strict: bool = False) -> tuple[float, str]:
    """Resolve the display timezone from CLI/env inputs.

    Priority: ``tz_name`` (IANA, DST-aware) > ``utc_offset`` (fixed float) >
    local system tz.  Returns ``(offset_hours, label)``.

    **Contract — fixed scalar offset, by design.** With an IANA name, the
    offset returned is the *current* UTC offset captured once at parse time.
    Historical hour-of-day buckets in static exports (text / JSON / CSV / MD
    tables, and the Highcharts-rendered PNG) use this single scalar offset
    applied uniformly across every event — they do **not** reflect per-event
    DST (a spring-forward event in March and a summer event in July are
    bucketed against the same offset).

    This is intentional and historically stable. Static-export consumers
    expect one tz label per report, not per-event astimezone() jitter. Any
    switch to per-event ``ZoneInfo`` math here would perturb every existing
    report — treat as a breaking change if ever proposed.

    The HTML client's uPlot / Chart.js / Highcharts / hour-of-day /
    punchcard / time-of-day widgets use the **same fixed scalar offset**
    as the static path: the emitted JavaScript bucketizes events with
    ``(epoch + offset_seconds) % 86400`` arithmetic, not ``Intl.DateTimeFormat``.
    Static and client-side bucketing agree by design. A previous revision
    of this docstring claimed per-event DST via ``Intl.DateTimeFormat``;
    that was never implemented — the claim was aspirational and has been
    corrected to match the code.

    When ``strict`` is True and the IANA zone can't be resolved (e.g. on
    Windows without the ``tzdata`` pip package), raises ``SystemExit``
    with an actionable message instead of warning and falling back to UTC.

    See ``test_hour_of_day_dst_boundary_uses_fixed_offset`` for the
    behaviour-lock regression test.
    """
    if tz_name:
        try:
            zi = ZoneInfo(tz_name)
            now = datetime.now(zi)
            delta = now.utcoffset()
            off = delta.total_seconds() / 3600.0 if delta else 0.0
            return off, tz_name
        except ZoneInfoNotFoundError:
            msg = (
                f"ZoneInfo not found for tz {tz_name!r}. "
                "On Windows, install the 'tzdata' package "
                "(pip install tzdata) for IANA tz support."
            )
            if strict:
                print(f"[error] {msg}", file=sys.stderr)
                raise SystemExit(2)
            print(f"[warn] {msg} Falling back to UTC.", file=sys.stderr)
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
    dt = _parse_iso_dt(ts)
    if dt is None:
        return 0
    try:
        return int(dt.timestamp())
    except (OSError, OverflowError):
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


# Module-level override set by --projects-dir (instance mode). Takes
# precedence over $CLAUDE_PROJECTS_DIR so users running multiple Claude
# Code installs (e.g. one at ~/.claude, another under $CLAUDE_CONFIG_DIR)
# can point the tool at whichever projects dir they want in a single run.
_PROJECTS_DIR_OVERRIDE: Path | None = None


def _projects_dir() -> Path:
    if _PROJECTS_DIR_OVERRIDE is not None:
        return _PROJECTS_DIR_OVERRIDE
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
    # Claude Code writes JSONLs to ~/.claude/projects/<slug>/ where <slug>
    # is the cwd with every non-alphanumeric character (except `-`) mapped
    # to `-`. Runs of replaceable chars are preserved as consecutive `-`s
    # — e.g. `/Users/x/.claude-mem` → `-Users-x--claude-mem`. An earlier
    # version only replaced `/`, which drifted from Claude Code whenever
    # the path carried `_`, `.`, spaces, or apostrophes (e.g. $TMPDIR
    # paths under /private/var/folders/.../xxx_yyy/) and broke
    # compare-run extras that looked up session JSONLs via this slug.
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd or os.getcwd())


def _find_jsonl_files(slug: str, include_subagents: bool = False) -> list[Path]:
    project_dir = _projects_dir() / slug
    if not project_dir.exists():
        return []
    files = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    if include_subagents:
        files += list(project_dir.glob("*/subagents/*.jsonl"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _list_all_projects() -> list[tuple[str, Path]]:
    """Return ``[(slug, project_dir), ...]`` for every project under the
    projects directory that contains at least one ``.jsonl`` session file.

    Scans ``_projects_dir()`` (which honours ``--projects-dir`` override and
    ``CLAUDE_PROJECTS_DIR`` env var). Filters:
      - only immediate subdirectories whose name passes ``_SLUG_RE``
      - skips hidden entries (names starting with ``.``)
      - skips directories with zero session JSONLs so the instance dashboard
        doesn't list empty shells

    Sorted by most-recent-session mtime descending — most active projects
    surface first. Used exclusively by instance mode; single-session and
    project-cost paths keep their existing narrower helpers.
    """
    root = _projects_dir()
    if not root.is_dir():
        return []
    out: list[tuple[str, Path, float]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or not _SLUG_RE.match(name):
            continue
        jsonls = [p for p in entry.glob("*.jsonl") if p.is_file()]
        if not jsonls:
            continue
        newest = max(p.stat().st_mtime for p in jsonls)
        out.append((name, entry, newest))
    out.sort(key=lambda t: t[2], reverse=True)
    return [(slug, path) for slug, path, _ in out]


def _slug_to_friendly_path(slug: str) -> str:
    """Best-effort reverse of ``_cwd_to_slug`` for display purposes.

    Claude Code's slug encoding is lossy (``/``, ``_``, ``.``, spaces → ``-``),
    so we can't recover the original path exactly. Heuristic: leading ``-``
    becomes ``/`` (absolute path marker), and we check whether the guessed
    path exists on disk and use it if so; otherwise fall back to inserting
    ``/`` at every single hyphen while collapsing ``--`` back to ``-`` —
    the common case where the cwd had no underscores/dots/spaces. If nothing
    matches, return the slug unchanged so users at least see the raw string.
    """
    if not slug:
        return slug
    if slug.startswith("-"):
        guess = "/" + slug[1:].replace("-", "/")
        collapsed = re.sub(r"/+", "/", guess)
        if Path(collapsed).exists():
            return collapsed
        parts = re.split(r"-+", slug[1:])
        guess2 = "/" + "/".join(parts)
        if Path(guess2).exists():
            return guess2
        return collapsed
    return slug


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


def _env_validated(env_key: str, validator) -> str | None:
    """Read ``env_key`` and run it through ``validator``.

    Returns the validated value, ``None`` if the env var is unset, or
    exits 1 with an `[error] <KEY>: <msg>` line on validation failure.
    """
    v = os.environ.get(env_key)
    if v is None:
        return None
    try:
        return validator(v)
    except argparse.ArgumentTypeError as exc:
        print(f"[error] {env_key}: {exc}", file=sys.stderr)
        sys.exit(1)


def _env_slug() -> str | None:
    return _env_validated("CLAUDE_PROJECT_SLUG", _validate_slug)


def _env_session_id() -> str | None:
    return _env_validated("CLAUDE_SESSION_ID", _validate_session_id)


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
    # Content-block distribution: assistant blocks come from this turn's own
    # message.content; tool_result / image blocks are attributed from the user
    # entry that immediately preceded this turn in the raw JSONL stream.
    assist_content = msg.get("content")
    user_raw       = entry.get("_preceding_user_content")
    assist_counts, tool_names = _count_content_blocks(assist_content)
    user_counts, _ = _count_content_blocks(user_raw)
    content_blocks = {
        "thinking":    assist_counts["thinking"],
        "tool_use":    assist_counts["tool_use"],
        "text":        assist_counts["text"],
        "tool_result": user_counts["tool_result"],
        "image":       user_counts["image"],
    }
    # Per-turn drill-down payload: the user prompt that triggered this turn,
    # the assistant's text reply, and a tool-call list with input previews.
    # All three feed the HTML detail drawer + Prompts section. Resume-marker
    # turns keep empty strings here — the drawer excludes them anyway.
    prompt_text = _extract_user_prompt_text(user_raw)
    slash_cmd   = _extract_slash_command(prompt_text, user_raw)
    asst_text   = _extract_assistant_text(assist_content)
    tool_detail: list[dict] = []
    # Phase-A additions (v1.6.0): cross-turn signals for the skill/subagent-type
    # tables. Extracted once here so aggregators can walk ``turn_records``
    # without re-parsing content. Empty lists/string for main-session turns or
    # turns without the respective signal.
    skill_invocations: list[str] = []
    spawned_subagents: list[str] = []
    # Phase-B (v1.7.0): tool_use ids of Agent/Task spawn blocks on this
    # turn. Used by ``_attribute_subagent_tokens`` to map
    # ``tool_use_id → prompt_anchor_index`` so subagent tokens roll up
    # to the spawning user prompt.
    tool_use_ids: list[str] = []
    if isinstance(assist_content, list):
        for block in assist_content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            tool_detail.append({
                "name":          name if isinstance(name, str) else str(name),
                "input_preview": _summarise_tool_input(name, block.get("input")),
            })
            binput = block.get("input")
            if not isinstance(binput, dict):
                binput = {}
            if name == "Skill":
                sk = binput.get("skill")
                if isinstance(sk, str) and sk:
                    skill_invocations.append(sk)
            elif name in ("Agent", "Task"):
                st = binput.get("subagent_type")
                if isinstance(st, str) and st:
                    spawned_subagents.append(st)
                bid = block.get("id")
                if isinstance(bid, str) and bid:
                    tool_use_ids.append(bid)
    # Subagent-type tag propagated from ``_load_session`` when the entry came
    # from a ``subagents/*.jsonl`` file. Main-session turns: empty string.
    subagent_type = str(entry.get("_subagent_type") or "")
    # Phase-B: filename-derived agentId (only present on subagent turns).
    subagent_agent_id = str(entry.get("_subagent_agent_id") or "")
    # Phase-B: ``(tool_use_id, agentId)`` pairs surfaced from the user
    # entry preceding this turn (set in ``_extract_turns``). Empty for
    # turns whose preceding user message was not an Agent/Task result.
    raw_links = entry.get("_preceding_user_agent_links") or []
    agent_links: list[tuple[str, str]] = []
    if isinstance(raw_links, list):
        for pair in raw_links:
            if (isinstance(pair, (list, tuple)) and len(pair) == 2
                    and isinstance(pair[0], str) and isinstance(pair[1], str)):
                agent_links.append((pair[0], pair[1]))
    if u.get("speed") == "fast":
        _FAST_MODE_TURNS[0] += 1
    # Per-turn latency: wall-clock seconds from the immediately preceding
    # user / tool_result entry to this assistant turn's settled timestamp.
    # ``_preceding_user_timestamp`` is set in ``_extract_turns`` (first
    # streaming chunk wins). For headless ``claude -p`` benchmark runs this
    # is the model's response time for the single turn; for tool-using
    # turns it represents the model's time after the tool result landed.
    # ``None`` when either timestamp is missing or unparseable, or when the
    # gap is non-positive (clock skew on truncated files, synthetic resume
    # markers — the JSONL writer guarantees monotone timestamps within one
    # session in practice).
    _prev_iso = entry.get("_preceding_user_timestamp", "") or ""
    _this_iso = entry.get("timestamp", "") or ""
    latency_seconds: float | None = None
    if _prev_iso and _this_iso:
        _prev_dt = _parse_iso_dt(_prev_iso)
        _this_dt = _parse_iso_dt(_this_iso)
        if _prev_dt and _this_dt:
            try:
                _gap = (_this_dt - _prev_dt).total_seconds()
                if _gap >= 0:
                    latency_seconds = round(_gap, 3)
            except (ValueError, AttributeError, TypeError, OSError):
                latency_seconds = None
    return {
        "index":                  global_index,
        "timestamp":              entry.get("timestamp", ""),
        "timestamp_fmt":          _fmt_ts(entry.get("timestamp", ""), tz_offset_hours),
        "latency_seconds":        latency_seconds,
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
        "content_blocks":         content_blocks,
        "tool_use_names":         tool_names,
        "is_resume_marker":       bool(entry.get("_is_resume_marker", False)),
        "prompt_text":            prompt_text,
        "prompt_snippet":         _truncate(prompt_text, 240),
        "slash_command":          slash_cmd,
        "assistant_text":         asst_text,
        "assistant_snippet":      _truncate(asst_text, 240),
        "tool_use_detail":        tool_detail,
        "skill_invocations":      skill_invocations,
        "spawned_subagents":      spawned_subagents,
        "subagent_type":          subagent_type,
        # Phase-B (v1.7.0): subagent → parent-prompt attribution fields.
        # ``tool_use_ids`` / ``agent_links`` / ``subagent_agent_id`` are
        # the linkage primitives. ``prompt_anchor_index`` is filled in
        # by a one-shot pass over ``turn_records`` in ``_build_report``.
        # ``attributed_subagent_*`` start at zero and are accumulated by
        # ``_attribute_subagent_tokens`` on the spawning prompt's row.
        "tool_use_ids":              tool_use_ids,
        "agent_links":               agent_links,
        "subagent_agent_id":         subagent_agent_id,
        "prompt_anchor_index":       0,
        "attributed_subagent_tokens": 0,
        "attributed_subagent_cost":   0.0,
        "attributed_subagent_count":  0,
    }


def _totals_from_turns(turn_records: list[dict]) -> dict:
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
         "cache_write_5m": 0, "cache_write_1h": 0, "extra_1h_cost": 0.0,
         "cost": 0.0, "no_cache_cost": 0.0, "turns": len(turn_records)}
    content_block_totals = {"thinking": 0, "tool_use": 0, "text": 0,
                            "tool_result": 0, "image": 0}
    thinking_turn_count = 0
    name_counts: dict[str, int] = {}
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
        cb = r.get("content_blocks") or {}
        for k in content_block_totals:
            content_block_totals[k] += cb.get(k, 0)
        if cb.get("thinking", 0) > 0:
            thinking_turn_count += 1
        for name in r.get("tool_use_names", []) or []:
            name_counts[name] = name_counts.get(name, 0) + 1
    n_turns = len(turn_records)
    t["total"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    t["total_input"] = t["input"] + t["cache_read"] + t["cache_write"]
    t["cache_savings"] = t["no_cache_cost"] - t["cost"]
    t["cache_hit_pct"] = 100 * t["cache_read"] / max(1, t["total_input"])
    t["content_blocks"] = content_block_totals
    t["thinking_turn_count"] = thinking_turn_count
    t["thinking_turn_pct"] = (
        100 * thinking_turn_count / n_turns if n_turns else 0.0
    )
    t["tool_call_total"] = content_block_totals["tool_use"]
    t["tool_call_avg_per_turn"] = (
        content_block_totals["tool_use"] / n_turns if n_turns else 0.0
    )
    # Stable ordering: count desc, then name asc so ties are deterministic.
    ranked = sorted(name_counts.items(), key=lambda x: (-x[1], x[0]))
    t["tool_names_top3"] = [name for name, _ in ranked[:3]]
    return t


def _model_counts(turn_records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in turn_records:
        counts[r["model"]] = counts.get(r["model"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Phase-A aggregators (v1.6.0) — inspired by Anthropic's session-report skill.
# Three new cross-cutting breakdowns the existing renderers did not expose:
#   1. ``cache_breaks``    — single turns above a configurable uncached+cache-
#                             create threshold, with ±2 user-prompt context.
#   2. ``by_skill``        — per-skill/slash-command aggregation (sticky
#                             attribution to the most recent slash-prefixed
#                             user prompt, overridden turn-locally by Skill
#                             tool_use blocks).
#   3. ``by_subagent_type``— per-subagent-type table (spawn count from
#                             Agent/Task tool_use `input.subagent_type` +
#                             actual consumed tokens when --include-subagents
#                             tags each sidechain turn with its resolved
#                             subagent_type).
# These are computed once per report build and attached at both the per-
# session level (session dict) and the report level (aggregated across the
# report's sessions). The instance-mode builder then aggregates across
# projects on top.
# ---------------------------------------------------------------------------


# Cache-break threshold: any single turn with
# ``input_tokens + cache_write_tokens > CACHE_BREAK_THRESHOLD`` is flagged.
# Matches the Anthropic session-report default (100k uncached). Override via
# ``--cache-break-threshold`` on the CLI.
_CACHE_BREAK_DEFAULT_THRESHOLD = 100_000


def _detect_cache_breaks(session: dict,
                          threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD,
                          context_radius: int = 2) -> list[dict]:
    """Flag turns whose uncached+cache-create token spend exceeds ``threshold``.

    "Cache break" = the cached prompt context was evicted or not reused, so
    the model had to re-ingest a large block of uncached tokens. Surfacing
    these lets users trace *which* turn lost the cache (vs. a summary cache-
    hit% which doesn't name events).

    Returns a list of dicts in descending-uncached order, each with:
        session_id, turn_index, timestamp, timestamp_fmt,
        uncached (input + cache_write), total_tokens, cache_break_pct,
        prompt_snippet, slash_command, model,
        context: [{ts, text, slash, here: bool}] — ±2 user prompts around
                 the flagged turn, ordered chronologically.
    """
    turns = session.get("turns") or []
    if not turns:
        return []
    # Build an ordered list of user-prompt records from the turn stream.
    # A "user prompt" here is the non-empty ``prompt_text`` of a turn — i.e.
    # the genuine typed input that triggered this turn (or the first turn
    # of a tool-use chain rooted in that prompt). Adjacent turns sharing
    # the same prompt reference are deduped so the ±2 window scopes to
    # distinct user actions, not tool-loop continuations.
    prompts: list[dict] = []
    last_text: str | None = None
    for t in turns:
        if t.get("is_resume_marker"):
            continue
        txt = (t.get("prompt_text") or "").strip()
        if not txt or txt == last_text:
            continue
        prompts.append({
            "ts":    t.get("timestamp", ""),
            "ts_fmt": t.get("timestamp_fmt", ""),
            "text":   t.get("prompt_snippet") or txt[:240],
            "slash":  t.get("slash_command", ""),
            "turn_index": t.get("index"),
        })
        last_text = txt
    # Detect flagged turns, attach context window.
    breaks: list[dict] = []
    for t in turns:
        if t.get("is_resume_marker"):
            continue
        uncached = int(t.get("input_tokens", 0)) + int(t.get("cache_write_tokens", 0))
        if uncached <= threshold:
            continue
        total = int(t.get("total_tokens", 0))
        pct = (100.0 * uncached / total) if total else 0.0
        # Locate this turn's position in the prompt stream — match by
        # turn_index >= prompt.turn_index. The closest prompt whose
        # turn_index <= flagged turn's index is "this turn's" prompt; its
        # ±context_radius neighbours form the context window.
        anchor = -1
        ti = t.get("index")
        for i, p in enumerate(prompts):
            if p["turn_index"] is not None and ti is not None and p["turn_index"] <= ti:
                anchor = i
            else:
                break
        ctx: list[dict] = []
        if anchor >= 0:
            lo = max(0, anchor - context_radius)
            hi = min(len(prompts), anchor + context_radius + 1)
            for i in range(lo, hi):
                p = prompts[i]
                ctx.append({
                    "ts":    p["ts_fmt"] or p["ts"],
                    "text":  p["text"],
                    "slash": p["slash"],
                    "here":  (i == anchor),
                })
        breaks.append({
            "session_id":     session.get("session_id", ""),
            "turn_index":     t.get("index"),
            "timestamp":      t.get("timestamp", ""),
            "timestamp_fmt":  t.get("timestamp_fmt", ""),
            "uncached":       uncached,
            "total_tokens":   total,
            "cache_break_pct": round(pct, 1),
            "prompt_snippet": t.get("prompt_snippet", ""),
            "slash_command":  t.get("slash_command", ""),
            "model":          t.get("model", ""),
            "context":        ctx,
        })
    breaks.sort(key=lambda b: -b["uncached"])
    return breaks


def _empty_skill_row(name: str) -> dict:
    return {
        "name":             name,
        "invocations":      0,
        "turns_attributed": 0,
        "input":            0,
        "output":           0,
        "cache_read":       0,
        "cache_write":      0,
        "total_tokens":     0,
        "cost_usd":         0.0,
        "pct_total_cost":   0.0,
        "cache_hit_pct":    0.0,
        "session_count":    0,
        "_sessions":        set(),  # stripped before return
    }


def _accumulate_bucket(row: dict, t: dict) -> None:
    row["input"]        += int(t.get("input_tokens", 0))
    row["output"]       += int(t.get("output_tokens", 0))
    row["cache_read"]   += int(t.get("cache_read_tokens", 0))
    row["cache_write"]  += int(t.get("cache_write_tokens", 0))
    row["total_tokens"] += int(t.get("total_tokens", 0))
    row["cost_usd"]     += float(t.get("cost_usd", 0.0))
    row["turns_attributed"] += 1


def _finalise_skill_rows(rows: dict, total_cost: float) -> list[dict]:
    """Compute derived fields (pct_total_cost, cache_hit_pct) and drop the
    internal ``_sessions`` set; return a list ordered by cost descending."""
    out: list[dict] = []
    for name, row in rows.items():
        row = dict(row)
        row["session_count"] = len(row.pop("_sessions", set()) or set())
        total_input_side = (row["input"] + row["cache_read"] + row["cache_write"]) or 1
        row["cache_hit_pct"] = round(100.0 * row["cache_read"] / total_input_side, 1)
        row["pct_total_cost"] = (
            round(100.0 * row["cost_usd"] / total_cost, 2) if total_cost else 0.0
        )
        out.append(row)
    out.sort(key=lambda r: -r["cost_usd"])
    return out


def _build_by_skill(sessions: list[dict], total_cost: float) -> list[dict]:
    """Aggregate per-turn tokens/cost by the active skill or slash command.

    Attribution model (matches Anthropic's analyze-sessions.mjs approach):
      - A user prompt with a leading slash-command (``/foo``) sets the
        "current skill" to ``foo`` for that prompt and every follow-up
        assistant turn driven by it (tool-use loops count).
      - A new user prompt *without* a slash-command clears the current
        skill (subsequent turns are un-attributed).
      - A ``Skill`` tool_use block inside any turn overrides attribution
        for *that turn only* to the invoked skill name (``input.skill``).
      - Turns without any signal are simply not attributed (they still
        count toward the report's ``totals`` but not any skill row).

    Boundary detection between user prompts: we use ``prompt_text`` —
    each turn carries a snapshot of the user entry that immediately
    preceded its first occurrence (``_preceding_user_content``), which
    in a tool-use chain is either the original prompt (first turn) or
    a ``tool_result`` entry (subsequent turns). Only text-bearing prompts
    contribute a non-empty ``prompt_text``; tool_result-only content
    flattens to "". The boundary heuristic fires when ``prompt_text``
    becomes non-empty and differs from the previous prompt we tracked.
    """
    rows: dict[str, dict] = {}
    for session in sessions:
        sid = session.get("session_id", "")
        current_skill: str | None = None
        last_prompt_text: str = ""
        for t in session.get("turns", []) or []:
            if t.get("is_resume_marker"):
                continue
            prompt_text = (t.get("prompt_text") or "").strip()
            boundary_hit = bool(prompt_text) and prompt_text != last_prompt_text
            if boundary_hit:
                last_prompt_text = prompt_text
                raw_slash = t.get("slash_command") or ""
                # Strip the leading "/" so slash commands key-match Skill-tool
                # invocations (e.g. "/session-metrics" slash ↔ "session-metrics"
                # Skill tool-use invocation are merged into one row). This
                # matches Anthropic session-report's convention.
                new_skill = raw_slash.lstrip("/") if raw_slash else ""
                current_skill = new_skill or None
                if new_skill:
                    rows.setdefault(new_skill, _empty_skill_row(new_skill))["invocations"] += 1
            # Turn-scope override: Skill tool-use invocation attributes this
            # turn to the invoked skill name regardless of current_skill.
            invoked = t.get("skill_invocations") or []
            if invoked:
                skill_here = invoked[0]
                row = rows.setdefault(skill_here, _empty_skill_row(skill_here))
                _accumulate_bucket(row, t)
                row["_sessions"].add(sid)
                row["invocations"] += len(invoked)
            elif current_skill:
                row = rows.setdefault(current_skill, _empty_skill_row(current_skill))
                _accumulate_bucket(row, t)
                row["_sessions"].add(sid)
    return _finalise_skill_rows(rows, total_cost)


def _empty_subagent_row(name: str) -> dict:
    return {
        "name":             name,
        "spawn_count":      0,   # Agent/Task tool_use in main turns
        "turns_attributed": 0,   # subagent turns (only when --include-subagents)
        "input":            0,
        "output":           0,
        "cache_read":       0,
        "cache_write":      0,
        "total_tokens":     0,
        "cost_usd":         0.0,
        "pct_total_cost":   0.0,
        "cache_hit_pct":    0.0,
        "avg_tokens_per_call": 0.0,
        "_sessions":        set(),
    }


def _finalise_subagent_rows(rows: dict, total_cost: float) -> list[dict]:
    out: list[dict] = []
    for name, row in rows.items():
        row = dict(row)
        row["session_count"] = len(row.pop("_sessions", set()) or set())
        total_input_side = (row["input"] + row["cache_read"] + row["cache_write"]) or 1
        row["cache_hit_pct"] = round(100.0 * row["cache_read"] / total_input_side, 1)
        row["pct_total_cost"] = (
            round(100.0 * row["cost_usd"] / total_cost, 2) if total_cost else 0.0
        )
        calls_for_avg = row["spawn_count"] or row["turns_attributed"] or 1
        row["avg_tokens_per_call"] = round(row["total_tokens"] / calls_for_avg, 1)
        out.append(row)
    out.sort(key=lambda r: -(r["total_tokens"] or r["spawn_count"]))
    return out


def _build_by_subagent_type(sessions: list[dict], total_cost: float) -> list[dict]:
    """Aggregate spawns + consumed tokens per subagent_type.

    Two data sources per row:
      - ``spawn_count`` from **main** turns' ``spawned_subagents`` list
        (populated when the assistant emitted an ``Agent``/``Task`` tool_use
        with ``input.subagent_type``). Always available.
      - ``input``/``output``/``cache_*``/``cost_usd`` from **subagent**
        turns (turns with ``subagent_type`` set via ``_load_session``
        tagging). Only populated when the user ran with
        ``--include-subagents``; without it the token columns are all zero.

    The row ``name`` is the resolved subagent type string. Rows for spawn
    events whose type wasn't observed among the loaded subagent files still
    appear (with zero tokens) so users see the spawn signal even when the
    subagent JSONL wasn't loaded.
    """
    rows: dict[str, dict] = {}
    for session in sessions:
        sid = session.get("session_id", "")
        for t in session.get("turns", []) or []:
            if t.get("is_resume_marker"):
                continue
            # Spawn-count contribution from main turns.
            for st in (t.get("spawned_subagents") or []):
                row = rows.setdefault(st, _empty_subagent_row(st))
                row["spawn_count"] += 1
                row["_sessions"].add(sid)
            # Token contribution from subagent-tagged turns.
            stype = t.get("subagent_type") or ""
            if stype:
                row = rows.setdefault(stype, _empty_subagent_row(stype))
                _accumulate_bucket(row, t)
                row["_sessions"].add(sid)
    return _finalise_subagent_rows(rows, total_cost)


# ---------------------------------------------------------------------------
# Phase-B (v1.7.0): subagent → parent-prompt token attribution
# ---------------------------------------------------------------------------
#
# Roll subagent token usage onto the user prompt that spawned the subagent
# chain so the Prompts table reflects the *true* cost of an action ("a
# cheap-looking prompt that spawned a $1.20 Explore"). Implementation
# mirrors Anthropic's session-report 3-stage linkage but adapts to our
# post-load architecture:
#
#   Stage 1: ``tool_use.id → prompt_anchor_index`` (parent-side spawn)
#   Stage 2: ``agentId → prompt_anchor_index`` (via ``toolUseResult.agentId``)
#   Stage 3: roll subagent turns' tokens onto the resolved root prompt
#
# Key correction over Anthropic's reference: we use ``prompt_anchor_index``
# (the most recent turn whose ``prompt_text`` is non-empty) instead of the
# turn the spawn happens in. This avoids attribution landing on a turn
# that's invisible in the Prompts table (which filters on prompt_text).
# Nested chains resolve via iterative walk (no timestamp-sort dependency)
# with a cycle guard.


def _compute_prompt_anchor_indices(turn_records: list[dict]) -> None:
    """Forward pass: stamp ``prompt_anchor_index`` on every turn.

    The anchor is the index of the most recent turn (this one or earlier
    in chronological order) with non-empty ``prompt_text``. Subagent turns
    don't carry their own ``prompt_text`` and don't anchor for main-session
    rollup — they keep the most recent main-turn anchor that was seen.

    Mutates ``turn_records`` in place.
    """
    last_main_anchor: int | None = None
    for t in turn_records:
        # Subagent turns inherit the prior main-turn anchor (their own
        # ``prompt_text`` is "" by construction since the subagent JSONL
        # doesn't contain user prompts in the same shape).
        if t.get("subagent_agent_id"):
            t["prompt_anchor_index"] = (
                last_main_anchor if last_main_anchor is not None else t["index"]
            )
            continue
        if (t.get("prompt_text") or "").strip():
            last_main_anchor = t["index"]
        t["prompt_anchor_index"] = (
            last_main_anchor if last_main_anchor is not None else t["index"]
        )


def _attribute_subagent_tokens(turn_records: list[dict]) -> dict:
    """Roll subagent token usage onto the user prompt that spawned them.

    Modifies the matching turn record in-place: increments
    ``attributed_subagent_tokens``, ``attributed_subagent_cost`` and
    ``attributed_subagent_count`` on the *root* main-turn for every
    subagent turn whose chain resolves back to a known parent.

    The new fields are purely additive: ``cost_usd`` and ``total_tokens``
    on every turn are unchanged, so ``_totals_from_turns`` and existing
    aggregators see the same values they did pre-attribution. Display
    layers read ``attributed_subagent_*`` separately.

    Algorithm (no timestamp-sort dependency, with cycle guard):

    Pass 1 — ``tool_use_id → prompt_anchor_index``:
        Walk *all* turns (main + subagent). For each turn with
        ``tool_use_ids``, every id maps to that turn's
        ``prompt_anchor_index`` — i.e., the user prompt this spawn
        belongs to. Subagent turns also contribute (nested case): their
        anchor is the parent-subagent's resolved root, populated by
        ``_compute_prompt_anchor_indices`` to the most recent main
        prompt.

    Pass 2 — ``agent_id → anchor_index``:
        Walk *all* turns. For every ``(tuid, agent_id)`` in
        ``agent_links``, look up the spawn's ``prompt_anchor_index``
        from pass 1 and record it under ``agent_id``.

    Pass 3 — roll up subagent tokens:
        For every turn whose ``subagent_agent_id`` is non-empty, look
        up ``agent_id_anchor[subagent_agent_id]`` to find the root
        main-turn index. If found, accumulate; if not, increment the
        orphan counter.

    Returns a summary dict with totals useful for sanity checks.
    """
    summary = {
        "attributed_turns":       0,
        "orphan_subagent_turns":  0,
        "nested_levels_seen":     0,
        "cycles_detected":        0,
    }
    if not turn_records:
        return summary

    # ``index`` may not equal list position (global_idx is reset across
    # sessions in _build_report). Build a position map so anchor lookup
    # is O(1) regardless.
    index_to_pos = {t["index"]: i for i, t in enumerate(turn_records)}

    # Pass 1: tool_use_id -> prompt_anchor_index.
    tool_use_to_anchor: dict[str, int] = {}
    for t in turn_records:
        if t.get("is_resume_marker"):
            continue
        anchor = t.get("prompt_anchor_index", t["index"])
        for tuid in (t.get("tool_use_ids") or []):
            if isinstance(tuid, str) and tuid:
                tool_use_to_anchor[tuid] = anchor

    # Pass 2: agent_id -> anchor_index.
    agent_id_to_anchor: dict[str, int] = {}
    for t in turn_records:
        for pair in (t.get("agent_links") or []):
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            tuid, aid = pair[0], pair[1]
            if not (isinstance(tuid, str) and isinstance(aid, str) and tuid and aid):
                continue
            anchor = tool_use_to_anchor.get(tuid)
            if anchor is not None:
                agent_id_to_anchor[aid] = anchor

    # Pass 3: roll up subagent tokens onto root main turn.
    attributed_indices: set[int] = set()
    for t in turn_records:
        aid = t.get("subagent_agent_id") or ""
        if not aid:
            continue
        anchor = agent_id_to_anchor.get(aid)
        if anchor is None:
            summary["orphan_subagent_turns"] += 1
            continue
        # Iterative resolve with cycle guard. The anchor from pass 1 is
        # already the prompt-anchor index of the spawning turn; if that
        # spawning turn was itself a subagent turn, we step up via its
        # own ``subagent_agent_id`` until we land on a main turn.
        visited: set[str] = {aid}
        depth = 1
        while True:
            pos = index_to_pos.get(anchor)
            if pos is None:
                break
            anchor_turn = turn_records[pos]
            parent_aid = anchor_turn.get("subagent_agent_id") or ""
            if not parent_aid:
                break  # reached a main-session turn — root found
            if parent_aid in visited:
                summary["cycles_detected"] += 1
                break
            visited.add(parent_aid)
            next_anchor = agent_id_to_anchor.get(parent_aid)
            if next_anchor is None:
                break  # orphan in chain — roll onto current anchor anyway
            anchor = next_anchor
            depth += 1
        # Accumulate onto the resolved root (or the deepest known anchor
        # if the chain orphans partway). The anchor is a main turn iff
        # we broke on the no-parent-aid branch above.
        pos = index_to_pos.get(anchor)
        if pos is None:
            summary["orphan_subagent_turns"] += 1
            continue
        target = turn_records[pos]
        target["attributed_subagent_tokens"] += int(t.get("total_tokens", 0))
        target["attributed_subagent_cost"]   += float(t.get("cost_usd", 0.0))
        target["attributed_subagent_count"]  += 1
        attributed_indices.add(target["index"])
        if depth > summary["nested_levels_seen"]:
            summary["nested_levels_seen"] = depth

    summary["attributed_turns"] = len(attributed_indices)
    return summary


# ---------------------------------------------------------------------------
# Usage Insights — /usage-style prose characterisations of the report
# ---------------------------------------------------------------------------
#
# Each candidate is computed against the already-built report dict (no JSONL
# re-parse). Candidates with `shown=True` render in the dashboard's Usage
# Insights panel; the rest are kept in the JSON export for downstream tools.
# Thresholds are constants here — change-by-edit if you want to tune what
# qualifies as "noteworthy" for your workflow.

_INSIGHT_PARALLEL_PCT_THRESHOLD       = 20.0   # ≥ 20% of cost from multi-session 5h blocks
_INSIGHT_LONG_SESSION_HOURS           = 8      # session spans ≥ 8h wall-clock
_INSIGHT_LONG_SESSION_PCT_THRESHOLD   = 10.0
_INSIGHT_BIG_CONTEXT_TOKENS           = 150_000
_INSIGHT_BIG_CONTEXT_PCT_THRESHOLD    = 10.0
_INSIGHT_BIG_CACHE_MISS_TOKENS        = 100_000
_INSIGHT_BIG_CACHE_MISS_PCT_THRESHOLD = 5.0
_INSIGHT_SUBAGENT_TASK_COUNT          = 3      # ≥ 3 Task tool calls in a session
_INSIGHT_SUBAGENT_PCT_THRESHOLD       = 10.0
_INSIGHT_TOOL_DOMINANCE_MIN_CALLS     = 10     # gate, not %
_INSIGHT_OFF_PEAK_PCT_THRESHOLD       = 60.0   # heavy off-peak only (above ~58% baseline)
_INSIGHT_COST_CONCENTRATION_TOP_N     = 5
_INSIGHT_COST_CONCENTRATION_PCT       = 25.0
_INSIGHT_COST_CONCENTRATION_MIN_TURNS = 10     # avoid trivially-100% case for tiny sessions


def _session_task_count(session: dict) -> int:
    """Count `Task` tool invocations across a session's turns. The Task tool
    is Claude Code's subagent-dispatch mechanism — counting spawn calls in
    the main agent's transcript works regardless of `--include-subagents`."""
    n = 0
    for t in session.get("turns", []):
        for name in (t.get("tool_use_names") or []):
            if name == "Task":
                n += 1
    return n


def _turn_total_input(turn: dict) -> int:
    """Total tokens fed into the model on this turn (proxy for context fill)."""
    return (turn.get("input_tokens", 0)
            + turn.get("cache_read_tokens", 0)
            + turn.get("cache_write_tokens", 0))


def _is_off_peak_local(epoch_utc: int, tz_offset_hours: float) -> bool:
    """True iff the local-time hour is outside 09:00–18:00 on a weekday,
    OR the local day is Saturday/Sunday. Calibrated against a 9-to-6
    Mon–Fri baseline; ~58% of hours in a 24/7 distribution are off-peak."""
    if not epoch_utc:
        return False
    local = datetime.fromtimestamp(epoch_utc + int(tz_offset_hours * 3600), tz=timezone.utc)
    if local.weekday() >= 5:  # Sat / Sun
        return True
    return local.hour < 9 or local.hour >= 18


def _model_family(model_id: str) -> str:
    """Coarse family bucket from a model id like `claude-opus-4-7`."""
    m = (model_id or "").lower()
    if "opus" in m:
        return "Opus"
    if "sonnet" in m:
        return "Sonnet"
    if "haiku" in m:
        return "Haiku"
    return "Other"


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. `values` is assumed unsorted; sorted internally."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


def _fmt_long_duration(seconds: float) -> str:
    """Compact human duration for insight prose: `42m`, `3.2h`, `1d 4h`.

    Distinct from the existing ``_fmt_duration(int)`` helper used by the
    per-session burn-rate card (which formats short, exact intervals like
    ``45m12s``). Insight strings prefer rounder numbers and multi-day
    coverage at the cost of second-level precision.
    """
    s = max(0, int(seconds))
    if s < 3600:
        return f"{s // 60}m"
    h = s / 3600.0
    if h < 24:
        return f"{h:.1f}h"
    days = int(h // 24)
    rem  = int(h - days * 24)
    return f"{days}d {rem}h" if rem else f"{days}d"


# ---------------------------------------------------------------------------
# Compare-insight state marker + multi-family detection (Phase 7)
# ---------------------------------------------------------------------------

def _compare_state_marker_path(slug: str) -> Path:
    """File whose presence means the user has run ``--compare`` at least
    once for this project.

    Lives under the project's JSONL directory (not the session-metrics
    cache) so uninstalling session-metrics doesn't lose the marker, and
    so deleting a project's session dir cleans up the marker alongside
    everything else.
    """
    return _projects_dir() / slug / ".session-metrics-compare-used"


def _touch_compare_state_marker(slug: str) -> None:
    """Drop the opt-in marker before running ``--compare``.

    Best-effort: a filesystem failure here shouldn't abort the compare
    run. Callers wrap the call in a try/except that swallows ``OSError``.
    The marker content is an ISO-8601 timestamp so later tooling could
    show "first compare run on date X" — not used yet, but cheap to
    record.
    """
    marker = _compare_state_marker_path(slug)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8",
    )


def _has_compare_state_marker(slug: str) -> bool:
    """True iff :func:`_touch_compare_state_marker` has been called
    for this project (i.e., the user opted into compare-aware insights)."""
    return _compare_state_marker_path(slug).is_file()


def _scan_project_family_mix(slug: str) -> list[str]:
    """Return the sorted set of fine-grained model family slugs
    (``"opus-4-6"`` etc.) observed across every session in the project.

    Pulled via the compare module's ``_project_family_inventory`` so
    the family slug matches compare-mode conventions (1M-context suffix
    stripped). Called only by ``_compute_model_compare_insight`` — the
    main-report insight bank doesn't re-scan the disk for the other
    cards because the report already has all the data it needs.
    """
    try:
        smc = sys.modules.get("session_metrics_compare")
        if smc is None:
            # Lazy-load. The helper is in a sibling file; import here so
            # regular single-session reports don't pay for it.
            here = Path(__file__).resolve().parent
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "session_metrics_compare",
                here / "session_metrics_compare.py",
            )
            if spec is None or spec.loader is None:
                return []
            smc = importlib.util.module_from_spec(spec)
            sys.modules.setdefault("session_metrics", sys.modules[__name__])
            sys.modules["session_metrics_compare"] = smc
            spec.loader.exec_module(smc)
        inventory = smc._project_family_inventory(slug, use_cache=True)
    except (OSError, AttributeError, ImportError):
        return []
    return sorted(f for f in inventory.keys() if f)


def _version_suffix_of_family(family: str) -> tuple[int, ...]:
    """Parse trailing integer-dash segments out of a family slug.

    ``opus-4-7`` → ``(4, 7)``; ``sonnet-4-5-haiku`` → ``(4, 5)``.
    Used to order families for the "newer / older" insight copy. Returns
    ``()`` when no trailing ints are present — families compared as
    equal in that case fall back to alphabetical ordering in the caller.
    """
    parts = family.split("-")
    nums: list[int] = []
    # Walk from the right and collect integers until we hit a non-int.
    for part in reversed(parts):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(reversed(nums))


def _order_family_pair(families: list[str]) -> tuple[str, str] | None:
    """Pick a deterministic (older, newer) pair from a family list.

    - If exactly two families, orders by version suffix (higher =
      newer), falling back to alphabetical.
    - If more than two, picks the two most distinct by version: the
      lowest-version family as "older" and the highest as "newer". Ties
      fall back to alphabetical.
    - Returns ``None`` when fewer than two families are present.
    """
    distinct = [f for f in dict.fromkeys(families) if f]
    if len(distinct) < 2:
        return None
    keyed = sorted(distinct, key=lambda f: (_version_suffix_of_family(f), f))
    return (keyed[0], keyed[-1])


def _compute_model_compare_insight(report: dict) -> dict | None:
    """Build the Phase-7 model-compare insight card for a report.

    Fires with a soft hint when:
    - the user has NOT yet run ``--compare`` in this project, AND
    - at least two distinct model families appear in the project's
      sessions (not just this report's sessions — we scan the project
      dir so the hint still shows on a single-session report that only
      used one family, as long as the *project* has two).

    Fires with a stronger card ("run '--compare' for an attribution-
    grade benchmark") once the marker exists — the hint shape is the
    same, but the copy acknowledges the user has already engaged.

    Returns ``None`` (caller suppresses the card) when:
    - fewer than two families are present in the project, or
    - ``--no-model-compare-insight`` was passed (caller handles this;
      the builder itself doesn't read CLI flags), or
    - the project slug can't be determined.
    """
    slug = report.get("slug") or ""
    if not slug:
        return None
    families = _scan_project_family_mix(slug)
    pair = _order_family_pair(families)
    if not pair:
        return None
    older, newer = pair
    already_used = _has_compare_state_marker(slug)
    n_families = len([f for f in families if f])
    if already_used:
        headline = f"{n_families} model families &mdash; run a fresh compare"
        body = (
            f" &mdash; <code>{html_mod.escape(older)}</code> and "
            f"<code>{html_mod.escape(newer)}</code> both appear in this "
            f"project. Re-run <code>session-metrics --compare last-"
            f"{html_mod.escape(older)} last-{html_mod.escape(newer)}</code> "
            f"to refresh attribution numbers with your latest sessions."
        )
    else:
        headline = f"{n_families} model families detected"
        body = (
            f" in this project's sessions &mdash; "
            f"<code>{html_mod.escape(older)}</code> and "
            f"<code>{html_mod.escape(newer)}</code>. "
            f"Run <code>session-metrics --compare-prep</code> to set up a "
            f"controlled comparison that isolates tokenizer / output-length "
            f"effects from workload shift."
        )
    return {
        "id":        "model_compare",
        "headline":  headline,
        "body":      body,
        "value":     float(n_families),
        "threshold": 2.0,
        "shown":     True,
        "always_on": True,
    }


def _compute_usage_insights(report: dict) -> list[dict]:
    """Compute the Usage Insights candidate list. See module-level
    `_INSIGHT_*` constants for thresholds. Each entry:
        {id, headline, body, value, threshold, shown, always_on}
    Returns `[]` if total cost is zero (avoids percentage division by zero).
    """
    totals     = report.get("totals", {}) or {}
    total_cost = float(totals.get("cost", 0.0) or 0.0)
    if total_cost <= 0:
        return []

    sessions       = report.get("sessions", []) or []
    blocks         = report.get("session_blocks", []) or []
    tz_off         = float(report.get("tz_offset_hours", 0.0) or 0.0)
    all_turns      = [t for s in sessions for t in s.get("turns", [])]
    total_turns    = len(all_turns)
    candidates: list[dict] = []

    # 1. Parallel sessions — cost from 5h blocks where multiple sessions touched the window.
    parallel_cost = sum(b.get("cost_usd", 0.0) for b in blocks
                        if len(b.get("sessions_touched") or []) > 1)
    parallel_pct  = 100.0 * parallel_cost / total_cost
    candidates.append({
        "id":        "parallel_sessions",
        "headline":  f"{parallel_pct:.0f}%",
        "body":      f" of cost came from 5-hour windows where you ran more than one session in parallel — concurrent sessions share the same rate-limit window.",
        "value":     parallel_pct,
        "threshold": _INSIGHT_PARALLEL_PCT_THRESHOLD,
        "shown":     parallel_pct >= _INSIGHT_PARALLEL_PCT_THRESHOLD,
        "always_on": False,
    })

    # 2. Long sessions — cost share from sessions ≥ 8h wall-clock.
    long_cutoff = _INSIGHT_LONG_SESSION_HOURS * 3600
    long_cost   = sum(s.get("subtotal", {}).get("cost", 0.0)
                      for s in sessions
                      if s.get("duration_seconds", 0) >= long_cutoff)
    long_pct    = 100.0 * long_cost / total_cost
    candidates.append({
        "id":        "long_sessions",
        "headline":  f"{long_pct:.0f}%",
        "body":      f" of cost came from sessions active for {_INSIGHT_LONG_SESSION_HOURS}+ hours — long-lived sessions accumulate context cost over time.",
        "value":     long_pct,
        "threshold": _INSIGHT_LONG_SESSION_PCT_THRESHOLD,
        "shown":     long_pct >= _INSIGHT_LONG_SESSION_PCT_THRESHOLD,
        "always_on": False,
    })

    # 3. Big-context turns — cost share of turns where total input ≥ 150k.
    big_ctx_cost = sum(t.get("cost_usd", 0.0) for t in all_turns
                       if _turn_total_input(t) >= _INSIGHT_BIG_CONTEXT_TOKENS)
    big_ctx_pct  = 100.0 * big_ctx_cost / total_cost
    candidates.append({
        "id":        "big_context_turns",
        "headline":  f"{big_ctx_pct:.0f}%",
        "body":      f" of cost was spent on turns with ≥{_INSIGHT_BIG_CONTEXT_TOKENS // 1000}k context filled — `/compact` mid-task or `/clear` between tasks keeps the running input down.",
        "value":     big_ctx_pct,
        "threshold": _INSIGHT_BIG_CONTEXT_PCT_THRESHOLD,
        "shown":     big_ctx_pct >= _INSIGHT_BIG_CONTEXT_PCT_THRESHOLD,
        "always_on": False,
    })

    # 4. Big cache misses — cost share of turns sending ≥ 100k uncached input.
    miss_cost = sum(t.get("cost_usd", 0.0) for t in all_turns
                    if (t.get("input_tokens", 0) + t.get("cache_write_tokens", 0))
                       >= _INSIGHT_BIG_CACHE_MISS_TOKENS)
    miss_pct  = 100.0 * miss_cost / total_cost
    candidates.append({
        "id":        "big_cache_misses",
        "headline":  f"{miss_pct:.0f}%",
        "body":      f" of cost came from turns with ≥{_INSIGHT_BIG_CACHE_MISS_TOKENS // 1000}k tokens of uncached input — typically a cold-start after a session went idle, or a large new prompt that wasn't cached.",
        "value":     miss_pct,
        "threshold": _INSIGHT_BIG_CACHE_MISS_PCT_THRESHOLD,
        "shown":     miss_pct >= _INSIGHT_BIG_CACHE_MISS_PCT_THRESHOLD,
        "always_on": False,
    })

    # 5. Subagent-heavy sessions — cost share from sessions with ≥ 3 Task calls.
    subagent_cost = sum(s.get("subtotal", {}).get("cost", 0.0)
                        for s in sessions
                        if _session_task_count(s) >= _INSIGHT_SUBAGENT_TASK_COUNT)
    subagent_pct  = 100.0 * subagent_cost / total_cost
    candidates.append({
        "id":        "subagent_heavy",
        "headline":  f"{subagent_pct:.0f}%",
        "body":      f" of cost came from sessions that ran {_INSIGHT_SUBAGENT_TASK_COUNT}+ subagent dispatches (Task tool) — each subagent runs its own request loop.",
        "value":     subagent_pct,
        "threshold": _INSIGHT_SUBAGENT_PCT_THRESHOLD,
        "shown":     subagent_pct >= _INSIGHT_SUBAGENT_PCT_THRESHOLD,
        "always_on": False,
    })

    # 6. Tool dominance — top-3 tool names' share of all tool calls.
    name_counts: dict[str, int] = {}
    for t in all_turns:
        for name in (t.get("tool_use_names") or []):
            name_counts[name] = name_counts.get(name, 0) + 1
    total_tool_calls = sum(name_counts.values())
    if total_tool_calls >= _INSIGHT_TOOL_DOMINANCE_MIN_CALLS:
        ranked = sorted(name_counts.items(), key=lambda x: (-x[1], x[0]))
        top3   = ranked[:3]
        top3_share = 100.0 * sum(c for _, c in top3) / total_tool_calls
        names_str  = ", ".join(html_mod.escape(n) for n, _ in top3)
        candidates.append({
            "id":        "top3_tools",
            "headline":  f"{top3_share:.0f}%",
            "body":      f" of all tool calls were {names_str} — your top-3 tools dominate this {total_tool_calls:,}-call workload.",
            "value":     top3_share,
            "threshold": 0.0,
            "shown":     True,
            "always_on": False,
        })
    else:
        candidates.append({
            "id":        "top3_tools",
            "headline":  "0%",
            "body":      " (insufficient tool-call volume).",
            "value":     0.0,
            "threshold": 0.0,
            "shown":     False,
            "always_on": False,
        })

    # 7. Off-peak share — cost share with timestamps outside 09:00–18:00 local weekday.
    off_peak_cost = sum(t.get("cost_usd", 0.0) for t in all_turns
                        if _is_off_peak_local(_parse_iso_epoch(t.get("timestamp", "")), tz_off))
    off_peak_pct  = 100.0 * off_peak_cost / total_cost
    candidates.append({
        "id":        "off_peak_share",
        "headline":  f"{off_peak_pct:.0f}%",
        "body":      f" of cost happened outside business hours (before 09:00, after 18:00, or on weekends in your local timezone) — heads-up that long-running subagents while you're AFK still bill.",
        "value":     off_peak_pct,
        "threshold": _INSIGHT_OFF_PEAK_PCT_THRESHOLD,
        "shown":     off_peak_pct >= _INSIGHT_OFF_PEAK_PCT_THRESHOLD,
        "always_on": False,
    })

    # 8. Cost concentration — top-N turns' cost share (gated on total turns ≥ 10).
    if total_turns >= _INSIGHT_COST_CONCENTRATION_MIN_TURNS:
        sorted_costs = sorted((t.get("cost_usd", 0.0) for t in all_turns), reverse=True)
        topn_share   = 100.0 * sum(sorted_costs[:_INSIGHT_COST_CONCENTRATION_TOP_N]) / total_cost
        candidates.append({
            "id":        "cost_concentration",
            "headline":  f"{topn_share:.0f}%",
            "body":      f" of cost was driven by just the top {_INSIGHT_COST_CONCENTRATION_TOP_N} most-expensive turns out of {total_turns:,} total — a few large turns dominate the bill.",
            "value":     topn_share,
            "threshold": _INSIGHT_COST_CONCENTRATION_PCT,
            "shown":     topn_share >= _INSIGHT_COST_CONCENTRATION_PCT,
            "always_on": False,
        })
    else:
        candidates.append({
            "id":        "cost_concentration",
            "headline":  "0%",
            "body":      " (too few turns to call concentration meaningful).",
            "value":     0.0,
            "threshold": _INSIGHT_COST_CONCENTRATION_PCT,
            "shown":     False,
            "always_on": False,
        })

    # 9. Model mix — cost share by family, shown iff ≥ 2 families seen.
    family_cost: dict[str, float] = {}
    for t in all_turns:
        fam = _model_family(t.get("model", ""))
        family_cost[fam] = family_cost.get(fam, 0) + t.get("cost_usd", 0.0)
    families_used = [f for f, c in family_cost.items() if c > 0]
    if len(families_used) >= 2:
        ranked_fams = sorted(family_cost.items(), key=lambda x: -x[1])
        parts       = [f"{html_mod.escape(f)} {100.0 * c / total_cost:.0f}%"
                       for f, c in ranked_fams if c > 0]
        candidates.append({
            "id":        "model_mix",
            "headline":  f"{len(families_used)} families",
            "body":      f" — cost split: {' · '.join(parts)}.",
            "value":     float(len(families_used)),
            "threshold": 2.0,
            "shown":     True,
            "always_on": True,
        })
    else:
        candidates.append({
            "id":        "model_mix",
            "headline":  "1 family",
            "body":      " (single-model project).",
            "value":     1.0,
            "threshold": 2.0,
            "shown":     False,
            "always_on": True,
        })

    # 10. Session pacing — turn-count distribution + duration extremes (≥ 2 sessions).
    if len(sessions) >= 2:
        durations = [s.get("duration_seconds", 0) for s in sessions if s.get("duration_seconds", 0) > 0]
        turn_counts = [len(s.get("turns", [])) for s in sessions]
        median_dur  = _percentile(durations, 50) if durations else 0
        longest_dur = max(durations) if durations else 0
        tc_min  = min(turn_counts) if turn_counts else 0
        tc_max  = max(turn_counts) if turn_counts else 0
        tc_avg  = (sum(turn_counts) / len(turn_counts)) if turn_counts else 0
        tc_p95  = _percentile([float(x) for x in turn_counts], 95) if turn_counts else 0
        candidates.append({
            "id":        "session_pacing",
            "headline":  f"{len(sessions)} sessions",
            "body":      (f" — median duration {_fmt_long_duration(median_dur)}, longest {_fmt_long_duration(longest_dur)};"
                          f" turns/session min {tc_min:,} · avg {tc_avg:.0f} · p95 {int(tc_p95):,} · max {tc_max:,}."),
            "value":     float(len(sessions)),
            "threshold": 2.0,
            "shown":     True,
            "always_on": True,
        })
    else:
        candidates.append({
            "id":        "session_pacing",
            "headline":  "1 session",
            "body":      " (no distribution to summarise).",
            "value":     1.0,
            "threshold": 2.0,
            "shown":     False,
            "always_on": True,
        })

    # 11. Model compare hint — fires when the project has ≥2 distinct
    # model families. Gated behind a state marker so the card escalates
    # from "hint you can run a benchmark" to "re-run for fresh numbers"
    # once the user actually tries --compare. Suppressed CLI-side via
    # --no-model-compare-insight.
    if not report.get("_suppress_model_compare_insight"):
        mc = _compute_model_compare_insight(report)
        if mc is not None:
            candidates.append(mc)

    return candidates


def _build_report(
    mode: str,
    slug: str,
    sessions_raw: list[tuple[str, list[dict], list[int]]],
    tz_offset_hours: float = 0.0,
    tz_label: str = "UTC",
    peak: dict | None = None,
    suppress_model_compare_insight: bool = False,
    cache_break_threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD,
    subagent_attribution: bool = True,
    sort_prompts_by: str | None = None,
    include_subagents: bool = False,
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
    attribution_summary = {
        "attributed_turns":      0,
        "orphan_subagent_turns": 0,
        "nested_levels_seen":    0,
        "cycles_detected":       0,
    }

    for session_id, raw_turns, user_ts in sessions_raw:
        turn_records = [_build_turn_record(global_idx + i, t, tz_offset_hours)
                        for i, t in enumerate(raw_turns)]
        global_idx += len(turn_records)
        # Phase-B (v1.7.0): subagent → parent-prompt attribution. Anchor
        # computation must precede attribution; both modify turn records
        # in place. Always-on by default; ``--no-subagent-attribution``
        # suppresses Pass 3's accumulation while still computing anchors
        # so other features (sort tie-breaks) keep working.
        _compute_prompt_anchor_indices(turn_records)
        if subagent_attribution:
            session_summary = _attribute_subagent_tokens(turn_records)
            for k, v in session_summary.items():
                if k == "nested_levels_seen":
                    attribution_summary[k] = max(attribution_summary[k], v)
                else:
                    attribution_summary[k] += v
        resumes = _build_resumes(turn_records)
        # Stamp `is_terminal_exit_marker` onto the last-turn marker (if any) so
        # the timeline divider can distinguish "user came back" from "user's
        # most recent /exit with no subsequent work in this JSONL". The
        # dashboard card already splits these in its sublabel; the timeline
        # needs the same distinction to stay internally consistent.
        for r in resumes:
            if r["terminal"]:
                idx = r["turn_index"]
                for t in turn_records:
                    if t["index"] == idx:
                        t["is_terminal_exit_marker"] = True
                        break
        # Raw epoch span — used by usage-insights (long_sessions, session_pacing).
        # Computed here while raw_turns is still in scope; the formatted
        # display strings would be brittle to re-parse for arithmetic.
        first_epoch = _parse_iso_epoch(raw_turns[0].get("timestamp", "")) if raw_turns else 0
        last_epoch  = _parse_iso_epoch(raw_turns[-1].get("timestamp", "")) if raw_turns else 0
        duration_seconds = (last_epoch - first_epoch) if (first_epoch and last_epoch and last_epoch > first_epoch) else 0
        # Wall-clock seconds (first user prompt → last assistant turn). Picks
        # up the initial pre-first-response wait that ``duration_seconds``
        # excludes — relevant for benchmark / headless ``claude -p`` runs
        # where prompt #1 lands at session start. Falls back to
        # ``duration_seconds`` when ``user_ts`` is empty (e.g. resumed
        # session whose first user entry was filtered out).
        first_user_epoch = user_ts[0] if user_ts else 0
        wall_clock_seconds = (
            (last_epoch - first_user_epoch)
            if (first_user_epoch and last_epoch and last_epoch > first_user_epoch)
            else duration_seconds
        )
        session_dict = {
            "session_id":         session_id,
            "first_ts":           _fmt_ts(raw_turns[0].get("timestamp", ""), tz_offset_hours) if raw_turns else "",
            "last_ts":            _fmt_ts(raw_turns[-1].get("timestamp", ""), tz_offset_hours) if raw_turns else "",
            "duration_seconds":   duration_seconds,
            "wall_clock_seconds": wall_clock_seconds,
            "turns":              turn_records,
            "subtotal":         _totals_from_turns(turn_records),
            "models":           _model_counts(turn_records),
            "time_of_day":      _build_time_of_day(user_ts, offset_hours=tz_offset_hours),
            "resumes":          resumes,
        }
        # Per-session phase-A aggregators: cache-breaks are intrinsically
        # session-scoped (a turn either breaks the cache in this session's
        # context or it doesn't). by_skill / by_subagent_type are computed
        # at both per-session and report scopes so either drilldown has a
        # self-consistent table when displayed in isolation.
        session_dict["cache_breaks"] = _detect_cache_breaks(
            session_dict, threshold=cache_break_threshold,
        )
        session_dict["by_skill"] = _build_by_skill(
            [session_dict], session_dict["subtotal"]["cost"],
        )
        session_dict["by_subagent_type"] = _build_by_subagent_type(
            [session_dict], session_dict["subtotal"]["cost"],
        )
        sessions_out.append(session_dict)

    all_turns = [t for s in sessions_out for t in s["turns"]]
    all_user_ts = sorted(ts for _, _, uts in sessions_raw for ts in uts)
    blocks = _build_session_blocks(sessions_raw)
    totals = _totals_from_turns(all_turns)
    report = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "mode":            mode,
        "slug":            slug,
        "tz_offset_hours": tz_offset_hours,
        "tz_label":        tz_label,
        "sessions":        sessions_out,
        "totals":          totals,
        "models":          _model_counts(all_turns),
        "time_of_day":     _build_time_of_day(all_user_ts, offset_hours=tz_offset_hours),
        "session_blocks":  blocks,
        "block_summary":   _weekly_block_counts(blocks),
        "weekly_rollup":   _build_weekly_rollup(sessions_out, sessions_raw, blocks),
        "peak":            peak,
        "resumes":         [r for s in sessions_out for r in s["resumes"]],
        # Phase-A cross-cutting tables (v1.6.0). All three are always
        # attached; renderers auto-hide when the list/dict is empty.
        "cache_breaks":        [cb for s in sessions_out for cb in s.get("cache_breaks", [])],
        "by_skill":            _build_by_skill(sessions_out, totals.get("cost", 0.0)),
        "by_subagent_type":    _build_by_subagent_type(sessions_out, totals.get("cost", 0.0)),
        "cache_break_threshold": cache_break_threshold,
        # Phase-B (v1.7.0): subagent → parent-prompt attribution summary.
        # Renderers read ``attributed_subagent_*`` directly off turn
        # records; this top-level dict surfaces orphan/cycle counts +
        # nested-depth observed for footer + JSON consumers.
        "subagent_attribution_summary": attribution_summary,
        # User-requested prompt sort mode (or None = renderer default).
        # HTML/MD default to ``"total"`` (parent + attributed subagent
        # cost — bubbles up cheap-prompt-spawning-expensive-subagent
        # turns); CSV/JSON default to ``"self"`` (parent only) so
        # script consumers parsing the prior output ordering remain
        # stable. Value is preserved on the report dict so renderers
        # can do their own per-format defaulting.
        "sort_prompts_by": sort_prompts_by,
        # Whether the loader was invoked with --include-subagents.
        # Renderers read this to decide whether the Subagent-types table's
        # zero token columns mean "no spawns happened" vs "spawn-count
        # only · token data not loaded".
        "include_subagents": include_subagents,
        # CLI opt-out for the Phase 7 model-compare insight card. Keyed
        # with an underscore so downstream JSON exports don't leak the
        # flag into user-facing schema; `_compute_usage_insights` reads
        # it before returning the list.
        "_suppress_model_compare_insight": suppress_model_compare_insight,
    }
    # Sort global cache_breaks by uncached desc to keep "worst-first" order.
    report["cache_breaks"].sort(key=lambda b: -int(b.get("uncached", 0)))
    report["usage_insights"] = _compute_usage_insights(report)
    # Drop the internal flag after use so the report dict stays clean
    # for downstream renderers / JSON export.
    report.pop("_suppress_model_compare_insight", None)
    return report


def _build_resumes(turn_records: list[dict]) -> list[dict]:
    """Extract resume markers from per-session turn records.

    A resume marker is a turn flagged ``is_resume_marker=True`` by
    `_extract_turns` (synthetic no-op preceded by a `/exit` local-command
    replay in the last ~10 user entries). For each marker we compute the
    wall-clock gap to the previous assistant turn in the same session —
    the practical "away" time between the user's prior work and the
    resumed work. When the marker is the first turn in the session
    (prior-session context not observable from this file), gap is null.
    When the marker is the last turn in the session (user exited and did
    not return), ``terminal`` is True — render as an exit marker rather
    than a resume divider.

    Returns a list ordered by ``turn_index``; each entry is a dict with
    ``timestamp``, ``timestamp_fmt``, ``turn_index``, ``gap_seconds``,
    ``terminal``.
    """
    markers: list[dict] = []
    for i, t in enumerate(turn_records):
        if not t.get("is_resume_marker"):
            continue
        gap: float | None = None
        if i > 0:
            prev_dt = _parse_iso_dt(turn_records[i-1].get("timestamp", ""))
            cur_dt  = _parse_iso_dt(t.get("timestamp", ""))
            if prev_dt and cur_dt:
                try:
                    gap = (cur_dt - prev_dt).total_seconds()
                except (ValueError, AttributeError, TypeError, OSError):
                    gap = None
        terminal = (i == len(turn_records) - 1)
        markers.append({
            "timestamp":     t.get("timestamp", ""),
            "timestamp_fmt": t.get("timestamp_fmt", ""),
            "turn_index":    t.get("index"),
            "gap_seconds":   gap,
            "terminal":      terminal,
        })
    return markers


# ---------------------------------------------------------------------------
# Formatting helpers (shared)
# ---------------------------------------------------------------------------

COL  = "{:<4} {:<19} {:>11} {:>7} {:>9} {:>9} {:>10} {:>9}"
# Optional suffix columns: Mode (fast mode), Content (per-turn block distribution)
_COL_MODE_SUFFIX    = "  {:<4}"
_COL_CONTENT_SUFFIX = "  {:<15}"
COL_M  = COL + _COL_MODE_SUFFIX  # retained for back-compat


def _text_format(show_mode: bool, show_content: bool) -> str:
    """Assemble the text-row format string with optional trailing columns."""
    fmt = COL
    if show_mode:
        fmt += _COL_MODE_SUFFIX
    if show_content:
        fmt += _COL_CONTENT_SUFFIX
    return fmt


def _text_table_headers(tz_offset_hours: float = 0.0,
                         show_mode: bool = False,
                         show_content: bool = False) -> tuple[str, str, str]:
    """Return (hdr, sep, wide) for the text timeline table in the given tz."""
    time_col = f"Time ({_short_tz_label(tz_offset_hours)})"
    fmt = _text_format(show_mode, show_content)
    args = ["#", time_col, "Input (new)", "Output",
            "CacheRd", "CacheWr", "Total", "Cost $"]
    if show_mode:
        args.append("Mode")
    if show_content:
        args.append("Content")
    hdr = fmt.format(*args)
    return hdr, "-" * len(hdr), "=" * len(hdr)


def _report_has_any(report: dict, predicate) -> bool:
    """Return True if any turn across any session matches ``predicate``."""
    return any(predicate(t) for s in report["sessions"] for t in s["turns"])


def _has_fast(report: dict) -> bool:
    """Return True if any turn in the report used fast mode."""
    return _report_has_any(report, lambda t: t.get("speed") == "fast")


def _has_1h_cache(report: dict) -> bool:
    """Return True if any turn used the 1-hour cache TTL tier."""
    return _report_has_any(report, lambda t: t.get("cache_write_1h_tokens", 0) > 0)


def _has_thinking(report: dict) -> bool:
    """Return True if any turn carried at least one thinking block."""
    return _report_has_any(
        report, lambda t: (t.get("content_blocks") or {}).get("thinking", 0) > 0
    )


def _has_tool_use(report: dict) -> bool:
    """Return True if any turn carried at least one tool_use block."""
    return _report_has_any(
        report, lambda t: (t.get("content_blocks") or {}).get("tool_use", 0) > 0
    )


def _has_content_blocks(report: dict) -> bool:
    """Return True if any turn carried any content block of any type.

    Drives conditional rendering of the Content column so legacy reports
    (or empty fixtures) stay visually unchanged.
    """
    def _any_nonzero(t):
        cb = t.get("content_blocks") or {}
        return any(v > 0 for v in cb.values())
    return _report_has_any(report, _any_nonzero)


def _fmt_content_cell(cb: dict) -> str:
    """Format the per-turn Content cell. Zeros are omitted.

    Example: ``{thinking: 3, tool_use: 2, text: 1}`` → ``"T3 u2 x1"``.
    Returns ``"-"`` when every count is zero so empty rows stay visible.
    """
    if not cb:
        return "-"
    parts: list[str] = []
    for key, letter in _CONTENT_LETTERS:
        n = cb.get(key, 0)
        if n:
            parts.append(f"{letter}{n}")
    return " ".join(parts) if parts else "-"


def _fmt_content_title(cb: dict) -> str:
    """Human-readable tooltip text for the per-turn Content cell."""
    if not cb:
        return ""
    parts = [f"{cb.get(key, 0)} {key}"
             for key, _ in _CONTENT_LETTERS if cb.get(key, 0) > 0]
    return ", ".join(parts)


def _fmt_ts(ts: str, offset_hours: float = 0.0) -> str:
    dt = _parse_iso_dt(ts)
    if dt is None:
        return ts[:19] if len(ts) >= 19 else ts
    try:
        if offset_hours:
            dt = dt.astimezone(timezone(timedelta(hours=offset_hours)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return ts[:19] if len(ts) >= 19 else ts


def _fmt_generated_at(report: dict) -> str:
    """Format ``report["generated_at"]`` in the report's display tz.

    Falls back to a UTC-suffixed string when the timestamp can't be
    parsed or shifted (preserves the prior bare-except behavior of the
    two markdown/HTML render sites this consolidates).
    """
    raw = report.get("generated_at", "")
    tz_offset = report.get("tz_offset_hours", 0.0)
    fallback = raw[:19].replace("T", " ") + " UTC"
    dt = _parse_iso_dt(raw)
    if dt is None:
        return fallback
    try:
        local = dt.astimezone(timezone(timedelta(hours=tz_offset)))
        return local.strftime("%Y-%m-%d %H:%M:%S") + f" {_short_tz_label(tz_offset)}"
    except (ValueError, OverflowError, OSError):
        return fallback


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


def _row_text(t: dict, show_mode: bool = False,
              show_content: bool = False) -> str:
    fmt = _text_format(show_mode, show_content)
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
    if show_content:
        args.append(_fmt_content_cell(t.get("content_blocks") or {}))
    return fmt.format(*args)


def _subtotal_text(label: str, s: dict, show_mode: bool = False,
                   show_content: bool = False) -> str:
    fmt = _text_format(show_mode, show_content)
    args = [
        label, "",
        f"{s['input']:>7,}", f"{s['output']:>7,}",
        f"{s['cache_read']:>9,}", _fmt_cwr_subtotal(s),
        f"{s['total']:>10,}",
        f"${s['cost']:>8.4f}",
    ]
    if show_mode:
        args.append("")
    if show_content:
        args.append("")
    return fmt.format(*args)


def _text_legend(tz_label: str, show_mode: bool, show_ttl: bool,
                 show_content: bool = False) -> str:
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
    if show_content:
        rows.append((
            "Content",
            "content blocks per turn: T thinking, u tool_use, x text, "
            "r tool_result, i image (zeros omitted)",
        ))
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
    if totals.get("thinking_turn_count", 0) > 0:
        lines.append(
            f"Extended thinking turns            : "
            f"{totals['thinking_turn_count']} of {totals.get('turns', 0)} "
            f"({totals.get('thinking_turn_pct', 0.0):.1f}%, "
            f"{(totals.get('content_blocks') or {}).get('thinking', 0)} blocks)"
        )
    if totals.get("tool_call_total", 0) > 0:
        top3 = totals.get("tool_names_top3") or []
        top3_str = ", ".join(top3) if top3 else "none"
        lines.append(
            f"Tool calls                         : "
            f"{totals['tool_call_total']} total, "
            f"{totals.get('tool_call_avg_per_turn', 0.0):.1f}/turn  "
            f"(top: {top3_str})"
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
    if report.get("mode") == "compare":
        return sys.modules["session_metrics_compare"].render_compare_text(report)
    if report.get("mode") == "instance":
        return _render_instance_text(report)
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    sessions = report["sessions"]

    m = _has_fast(report)
    has_1h = _has_1h_cache(report)
    has_content = _has_content_blocks(report)
    tz_offset = report.get("tz_offset_hours", 0.0)
    tz_label = report.get("tz_label", "UTC")
    hdr, sep, wide = _text_table_headers(tz_offset, show_mode=m,
                                          show_content=has_content)

    p(_text_legend(tz_label, show_mode=m, show_ttl=has_1h,
                    show_content=has_content))
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
                p(_row_text(t, m, has_content))
            p(sep)
            p(_subtotal_text(f"S{i:02}", s["subtotal"], m, has_content))
            p()
        p(wide)
        p(f"  PROJECT TOTAL — {len(sessions)} session{'s' if len(sessions) != 1 else ''}, {report['totals']['turns']} turns")
        p(wide)
        p(hdr)
        p(sep)
        p(_subtotal_text("TOT", report["totals"], m, has_content))
    else:
        s = sessions[0]
        p(hdr)
        for t in s["turns"]:
            p(_row_text(t, m, has_content))
        p(sep)
        p(_subtotal_text("TOT", s["subtotal"], m, has_content))

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
    if report.get("mode") == "compare":
        return sys.modules["session_metrics_compare"].render_compare_json(report)
    if report.get("mode") == "instance":
        return _render_instance_json(report)
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
    if report.get("mode") == "compare":
        return sys.modules["session_metrics_compare"].render_compare_csv(report)
    if report.get("mode") == "instance":
        return _render_instance_csv(report)
    out = io.StringIO()
    w = csv_mod.writer(out)
    w.writerow(["session_id", "turn", "timestamp", "model", "speed",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_ttl",
                "total_tokens", "cost_usd", "no_cache_cost_usd",
                "thinking_blocks", "tool_use_blocks", "text_blocks",
                "tool_result_blocks", "image_blocks",
                # Phase-B (v1.7.0) attribution columns. Always emitted so
                # column count is stable across reports; values are 0 on
                # turns that didn't spawn a subagent (the common case).
                "attributed_subagent_tokens", "attributed_subagent_cost",
                "attributed_subagent_count"])
    for s in report["sessions"]:
        for t in s["turns"]:
            cb = t.get("content_blocks") or {}
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
                cb.get("thinking", 0), cb.get("tool_use", 0),
                cb.get("text", 0), cb.get("tool_result", 0),
                cb.get("image", 0),
                t.get("attributed_subagent_tokens", 0),
                f"{float(t.get('attributed_subagent_cost', 0.0)):.6f}",
                t.get("attributed_subagent_count", 0),
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

    # Phase-A (v1.6.0): skill/subagent/cache-break sections.
    by_skill = report.get("by_skill") or []
    if by_skill:
        w.writerow([])
        w.writerow(["# SKILLS / SLASH COMMANDS"])
        w.writerow(["name", "invocations", "turns", "input", "output",
                    "cache_read", "cache_write", "total_tokens",
                    "cost_usd", "cache_hit_pct", "pct_total_cost"])
        for r in by_skill:
            w.writerow([
                r.get("name", ""), r.get("invocations", 0),
                r.get("turns_attributed", 0), r.get("input", 0),
                r.get("output", 0), r.get("cache_read", 0),
                r.get("cache_write", 0), r.get("total_tokens", 0),
                f"{float(r.get('cost_usd', 0.0)):.6f}",
                f"{float(r.get('cache_hit_pct', 0.0)):.1f}",
                f"{float(r.get('pct_total_cost', 0.0)):.2f}",
            ])

    by_subagent = report.get("by_subagent_type") or []
    if by_subagent:
        w.writerow([])
        w.writerow(["# SUBAGENT TYPES"])
        w.writerow(["name", "spawn_count", "turns", "input", "output",
                    "cache_read", "cache_write", "total_tokens",
                    "avg_tokens_per_call", "cost_usd",
                    "cache_hit_pct", "pct_total_cost"])
        for r in by_subagent:
            w.writerow([
                r.get("name", ""), r.get("spawn_count", 0),
                r.get("turns_attributed", 0), r.get("input", 0),
                r.get("output", 0), r.get("cache_read", 0),
                r.get("cache_write", 0), r.get("total_tokens", 0),
                f"{float(r.get('avg_tokens_per_call', 0.0)):.1f}",
                f"{float(r.get('cost_usd', 0.0)):.6f}",
                f"{float(r.get('cache_hit_pct', 0.0)):.1f}",
                f"{float(r.get('pct_total_cost', 0.0)):.2f}",
            ])

    cache_breaks = report.get("cache_breaks") or []
    if cache_breaks:
        w.writerow([])
        threshold = int(report.get("cache_break_threshold",
                                     _CACHE_BREAK_DEFAULT_THRESHOLD))
        w.writerow([f"# CACHE BREAKS (> {threshold:,} uncached)"])
        w.writerow(["session_id", "turn_index", "timestamp", "uncached",
                    "total_tokens", "cache_break_pct", "slash_command",
                    "project", "prompt_snippet"])
        for cb in cache_breaks:
            w.writerow([
                cb.get("session_id", ""), cb.get("turn_index", ""),
                cb.get("timestamp", ""), cb.get("uncached", 0),
                cb.get("total_tokens", 0),
                f"{float(cb.get('cache_break_pct', 0.0)):.1f}",
                cb.get("slash_command", ""),
                cb.get("project", ""),
                (cb.get("prompt_snippet") or "")[:240],
            ])
    return out.getvalue()


def render_md(report: dict) -> str:
    """Render the full report as GitHub-flavored Markdown.

    Includes summary cards, user activity by time of day (UTC), model pricing
    table, and per-session turn-level tables with subtotals.
    """
    if report.get("mode") == "compare":
        return sys.modules["session_metrics_compare"].render_compare_md(report)
    if report.get("mode") == "instance":
        return _render_instance_md(report)
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    slug = report["slug"]
    totals = report["totals"]
    mode = report["mode"]
    tz_offset = report.get("tz_offset_hours", 0.0)
    generated = _fmt_generated_at(report)

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
    # Wall clock + mean turn latency. ``Wall clock`` is the sum of per-session
    # first→last assistant-turn intervals; for benchmark / headless ``claude
    # -p`` runs this approximates the orchestrator's perceived wall-clock.
    # ``Mean turn latency`` is the average ``latency_seconds`` across every
    # assistant turn that had a parseable predecessor — drops resume markers
    # and any turn whose predecessor timestamp couldn't be parsed.
    _wall_total = sum(int(s.get("wall_clock_seconds", 0) or s.get("duration_seconds", 0)) for s in report["sessions"])
    _turn_lats = [t["latency_seconds"] for s in report["sessions"]
                   for t in s["turns"] if t.get("latency_seconds") is not None]
    if _wall_total > 0:
        p(f"| Wall clock | {_fmt_duration(_wall_total)} |")
    if _turn_lats:
        _mean_lat = sum(_turn_lats) / len(_turn_lats)
        p(f"| Mean turn latency | {_mean_lat:.2f}s ({len(_turn_lats)} turns) |")
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
    if totals.get("thinking_turn_count", 0) > 0:
        cb = totals.get("content_blocks") or {}
        p(
            f"| Extended thinking turns | "
            f"{totals['thinking_turn_count']} of {totals['turns']} "
            f"({totals.get('thinking_turn_pct', 0.0):.1f}%, "
            f"{cb.get('thinking', 0)} blocks) |"
        )
    if totals.get("tool_call_total", 0) > 0:
        top3 = totals.get("tool_names_top3") or []
        top3_str = ", ".join(top3) if top3 else "none"
        p(
            f"| Tool calls | {totals['tool_call_total']} total, "
            f"{totals.get('tool_call_avg_per_turn', 0.0):.1f}/turn "
            f"(top: {top3_str}) |"
        )
    p()

    # Usage Insights — derived from `_compute_usage_insights`. Renders only
    # when at least one insight crossed its threshold; otherwise the
    # section is omitted entirely so the existing layout flow is preserved.
    md_insights = _build_usage_insights_md(report.get("usage_insights", []) or [])
    if md_insights:
        p(md_insights)

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

    # Phase-A (v1.6.0) sections: skill / subagent / cache-break tables.
    by_skill_rows = report.get("by_skill") or []
    if by_skill_rows:
        p("## Skills & slash commands")
        p()
        p("| Name | Invocations | Turns | Input | Output | % cached | Cost $ | % of total |")
        p("|------|------------:|------:|------:|------:|--------:|------:|-----------:|")
        for r in by_skill_rows:
            p(f"| `{r.get('name', '')}` | {int(r.get('invocations', 0)):,} "
              f"| {int(r.get('turns_attributed', 0)):,} "
              f"| {int(r.get('input', 0)):,} "
              f"| {int(r.get('output', 0)):,} "
              f"| {float(r.get('cache_hit_pct', 0.0)):.1f}% "
              f"| ${float(r.get('cost_usd', 0.0)):.4f} "
              f"| {float(r.get('pct_total_cost', 0.0)):.2f}% |")
        p()

    by_subagent_rows = report.get("by_subagent_type") or []
    if by_subagent_rows:
        p("## Subagent types")
        p()
        p("| Subagent | Spawns | Turns | Input | Output | % cached | Avg/call | Cost $ | % of total |")
        p("|----------|-------:|------:|------:|------:|--------:|--------:|------:|-----------:|")
        for r in by_subagent_rows:
            p(f"| `{r.get('name', '')}` | {int(r.get('spawn_count', 0)):,} "
              f"| {int(r.get('turns_attributed', 0)):,} "
              f"| {int(r.get('input', 0)):,} "
              f"| {int(r.get('output', 0)):,} "
              f"| {float(r.get('cache_hit_pct', 0.0)):.1f}% "
              f"| {float(r.get('avg_tokens_per_call', 0.0)):,.0f} "
              f"| ${float(r.get('cost_usd', 0.0)):.4f} "
              f"| {float(r.get('pct_total_cost', 0.0)):.2f}% |")
        p()

    cache_breaks_rows = report.get("cache_breaks") or []
    if cache_breaks_rows:
        threshold = int(report.get("cache_break_threshold",
                                     _CACHE_BREAK_DEFAULT_THRESHOLD))
        p(f"## Cache breaks (> {threshold:,} uncached)")
        p()
        p(f"{len(cache_breaks_rows)} event{'s' if len(cache_breaks_rows) != 1 else ''} "
          f"— single turns where `input + cache_creation` exceeded the threshold. "
          f"Each row names *which* turn lost the cache.")
        p()
        p("| Uncached | % | When | Session | Prompt |")
        p("|---------:|--:|------|---------|--------|")
        for cb in cache_breaks_rows[:25]:
            sid8 = (cb.get("session_id") or "")[:8]
            snippet = (cb.get("prompt_snippet") or "").replace("|", "\\|")[:120]
            p(f"| {int(cb.get('uncached', 0)):,} "
              f"| {float(cb.get('cache_break_pct', 0.0)):.0f}% "
              f"| {cb.get('timestamp_fmt') or cb.get('timestamp', '')} "
              f"| `{sid8}` "
              f"| {snippet} |")
        if len(cache_breaks_rows) > 25:
            p()
            p(f"_Showing top 25 of {len(cache_breaks_rows)} — raw list available in JSON export._")
        p()

    has_1h_cache = _has_1h_cache(report)
    has_content  = _has_content_blocks(report)
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
    if has_content:
        p("- **Content** — per-turn content blocks: `T` thinking, `u` tool_use, "
          "`x` text, `r` tool_result, `i` image (zero counts omitted)")
    p()

    for i, s in enumerate(report["sessions"], 1):
        if mode == "project":
            st = s["subtotal"]
            p(f"## Session {i}: `{s['session_id'][:8]}…`")
            p()
            p(f"{s['first_ts']} → {s['last_ts']} &nbsp;·&nbsp; {len(s['turns'])} turns &nbsp;·&nbsp; **${st['cost']:.4f}**")
            p()

        if has_content:
            p(f"| # | Time ({tz_label}) | Input (new) | Output | CacheRd | CacheWr | Total | Cost $ | Content |")
            p("|--:|-----------|------------:|------:|--------:|--------:|------:|-------:|:--------|")
        else:
            p(f"| # | Time ({tz_label}) | Input (new) | Output | CacheRd | CacheWr | Total | Cost $ |")
            p("|--:|-----------|------------:|------:|--------:|--------:|------:|-------:|")
        for t in s["turns"]:
            ttl = t.get("cache_write_ttl", "")
            cwr_cell = f"{t['cache_write_tokens']:,}" + ("*" if ttl in ("1h", "mix") else "")
            row = (f"| {t['index']} | {t['timestamp_fmt']} "
                   f"| {t['input_tokens']:,} | {t['output_tokens']:,} "
                   f"| {t['cache_read_tokens']:,} | {cwr_cell} "
                   f"| {t['total_tokens']:,} | ${t['cost_usd']:.4f} |")
            if has_content:
                row += f" {_fmt_content_cell(t.get('content_blocks') or {})} |"
            p(row)
        st = s["subtotal"]
        st_cwr_cell = f"{st['cache_write']:,}" + ("*" if st.get("cache_write_1h", 0) > 0 else "")
        trow = (f"| **TOT** | | **{st['input']:,}** | **{st['output']:,}** "
                f"| **{st['cache_read']:,}** | **{st_cwr_cell}** "
                f"| **{st['total']:,}** | **${st['cost']:.4f}** |")
        if has_content:
            trow += " |"
        p(trow)
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
            f'<tr><td class="mono">{sid}\u2026</td>'
            f'<td class="mono">{fmt_local(st["first_epoch"])}</td>'
            f'<td class="num mono">{_fmt_duration(st["wall_sec"])}</td>'
            f'<td class="num">{st["turns"]:,}</td>'
            f'<td class="num"><strong>${s["subtotal"]["cost"]:.3f}</strong></td>'
            f'<td class="num muted">{st["tokens_per_min"]:,.0f}</td>'
            f'<td class="num muted">${st["cost_per_min"]:.3f}</td></tr>'
        )
    return (
        f'<section class="section" id="session-duration-section">\n'
        f'  <div class="section-title"><h2>Session duration</h2>'
        f'<span class="hint">top 10 by wall time ({tz_label})</span></div>\n'
        f'  <div class="rollup" id="session-duration">\n'
        f'  <table>\n'
        f'    <thead><tr>\n'
        f'      <th>Session</th><th>First turn ({tz_label})</th>'
        f'<th class="num">Wall</th><th class="num">Turns</th>'
        f'<th class="num">Cost</th><th class="num">tok/min</th><th class="num">$/min</th>\n'
        f'    </tr></thead>\n'
        f'    <tbody>{"".join(rows_html)}</tbody>\n'
        f'  </table>\n  </div>\n</section>'
    )


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
            f'<tr><td>{label}</td>'
            f'<td class="num"><strong>{cur_s}</strong></td>'
            f'<td class="num muted">{prev_s}</td>'
            f'<td class="num" style="color:{color}">{delta}</td></tr>'
        )

    return (
        '<section class="section" id="weekly-rollup-section">\n'
        '  <div class="section-title"><h2>Weekly rollup</h2>'
        '<span class="hint">trailing 7d vs prior 7d</span></div>\n'
        '  <div class="rollup" id="weekly-rollup">\n'
        '  <table>\n'
        '    <thead><tr>'
        '<th>Metric</th><th class="num">Last 7d</th>'
        '<th class="num">Prior 7d</th><th class="num">\u0394</th>'
        '</tr></thead>\n'
        f'    <tbody>{"".join(rows)}</tbody>\n'
        '  </table>\n  </div>\n</section>'
    )


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

    # Determine max cost for the block-row bars (preview .block-row pattern)
    max_cost = max((b["cost_usd"] for b in recent), default=0.0) or 1.0
    block_rows = "".join(
        f'<div class="block-row">'
        f'<span class="label">{fmt_local(b["anchor_epoch"])}</span>'
        f'<div class="bar"><div class="bar-fill" '
        f'style="width:{min(100, int(b["cost_usd"] / max_cost * 100))}%"></div></div>'
        f'<span class="num mono">${b["cost_usd"]:.3f}</span>'
        f'<span class="num mono">{b["turn_count"]:,} turns</span>'
        f'</div>'
        for b in recent
    )

    # Kpi-style stat cards for the trailing-window counts
    stat_card = lambda label, value: (
        f'<div class="kpi cat-time" style="min-height:auto;padding:12px 16px;min-width:140px">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-val">{value}</div></div>'
    )

    return (
        '<section class="section" id="session-blocks-section">\n'
        '  <div class="section-title"><h2>5-hour session blocks</h2>'
        f'<span class="hint">recent blocks · {tz_label}</span></div>\n'
        '  <div id="session-blocks" class="blocks">\n'
        '  <div class="grid kpi-grid" '
        'style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:16px">\n'
        f'    {stat_card("Last 7 days", s7)}\n'
        f'    {stat_card("Last 14 days", s14)}\n'
        f'    {stat_card("Last 30 days", s30)}\n'
        f'    {stat_card("All time", tot)}\n'
        '  </div>\n'
        f'  {block_rows}\n'
        '  </div>\n</section>'
    )


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
<section class="section" id="hod-section">
  <div class="section-title"><h2>Hour of day</h2>
    <span class="hint">user messages</span></div>
  <div id="hod-chart" class="chart-card">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <select id="hod-tz" class="tod-tz" style="background:var(--bg);color:var(--fg);
            border:1px solid var(--border);border-radius:6px;padding:6px 10px;
            font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer">{tz_options}</select>
    <span class="mono muted" style="font-size:11px">Peak:
      <strong id="hod-peak" class="mono" style="opacity:1">-</strong></span>
    {peak_legend}
  </div>
  <div id="hod-wrap" style="position:relative;height:160px;
       border-bottom:1px solid var(--border-dim);padding-bottom:2px">
    <div id="hod-peak-band1" style="position:absolute;top:0;bottom:0;
         background:rgba(239,197,75,0.12);border-left:1px dashed rgba(239,197,75,0.35);
         border-right:1px dashed rgba(239,197,75,0.35);display:none;pointer-events:none"></div>
    <div id="hod-peak-band2" style="position:absolute;top:0;bottom:0;
         background:rgba(239,197,75,0.12);border-left:1px dashed rgba(239,197,75,0.35);
         border-right:1px dashed rgba(239,197,75,0.35);display:none;pointer-events:none"></div>
    <div id="hod-bars" style="position:relative;display:flex;align-items:flex-end;
         gap:2px;height:100%"></div>
  </div>
  <div class="mono muted" style="display:flex;gap:2px;margin-top:6px;font-size:10px">
    {"".join(f'<div style="flex:1;text-align:center">{h:02d}</div>' for h in range(24))}
  </div>
  </div>
</section>
<script>
(function(){{
  var TS={ts_json};
  var PEAK={peak_json};
  var bars=document.getElementById('hod-bars');
  var bs=[];
  for(var i=0;i<24;i++){{
    var b=document.createElement('div');
    b.style.cssText='flex:1;background:var(--accent);border-radius:2px 2px 0 0;'+
      'min-height:1px;transition:height 0.25s ease;position:relative;opacity:.9';
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
        row = [f'<div class="punch-day">{days[r]}</div>']
        for h in range(24):
            row.append(f'<div class="punch-cell" data-r="{r}" data-h="{h}">'
                       f'<div class="punch-dot"></div></div>')
        cells.append('<div class="punch-row">' + "".join(row) + "</div>")
    hour_header = ('<div class="punch-row punch-head">'
                   '<div class="punch-day"></div>'
                   + "".join(f'<div class="punch-hour">{h:02d}</div>' for h in range(24))
                   + '</div>')
    return f"""\
<section class="section">
  <div class="section-title"><h2>Weekday \u00d7 hour</h2>
    <span class="hint">punchcard of user messages</span></div>
  <div id="punchcard" class="punch">
    <div class="punch-head-row">
      <select id="pc-tz" class="tz-select">{tz_options}</select>
      <span class="muted">Busiest: <strong id="pc-busy" class="mono">-</strong></span>
    </div>
    <div class="punch-grid">
      {hour_header}
      {"".join(cells)}
    </div>
  </div>
</section>
<script>
(function(){{
  var TS={ts_json};
  var cells=document.querySelectorAll('#punchcard .punch-cell');
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
    var accent=getComputedStyle(document.body).getPropertyValue('--accent').trim()||'#A58BFF';
    var dim=getComputedStyle(document.body).getPropertyValue('--border').trim()||'#30363d';
    cells.forEach(function(el){{
      var r=+el.dataset.r,h=+el.dataset.h,v=m[r][h];
      var dot=el.firstChild;
      if(v===0){{
        dot.style.width='2px';dot.style.height='2px';dot.style.background=dim;
      }}else{{
        var sz=Math.max(4,Math.min(14,4+v/mx*10));
        dot.style.width=sz+'px';dot.style.height=sz+'px';dot.style.background=accent;
        el.title=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][r]+' '+(h<10?'0':'')+h+':00 \u2014 '+v;
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
<section class="section">
  <div class="section-title"><h2>User messages by time of day</h2>
    <span class="hint">day-part distribution</span></div>
  <div id="tod-container" class="tod">
    <div class="tod-head">
      <select id="tod-tz" class="tod-tz">{tz_options}</select>
      <span class="muted">Total: <strong id="tod-total" class="tod-total mono">0</strong></span>
    </div>
    <div class="tod-rows">
      <div class="tod-row">
        <span class="tod-label">Morning (6\u201312)</span>
        <div class="tod-track"><div id="tod-bar-morning" class="tod-fill"></div></div>
        <span id="tod-cnt-morning" class="tod-cnt mono">0</span>
      </div>
      <div class="tod-row">
        <span class="tod-label">Afternoon (12\u201318)</span>
        <div class="tod-track"><div id="tod-bar-afternoon" class="tod-fill"></div></div>
        <span id="tod-cnt-afternoon" class="tod-cnt mono">0</span>
      </div>
      <div class="tod-row">
        <span class="tod-label">Evening (18\u201324)</span>
        <div class="tod-track"><div id="tod-bar-evening" class="tod-fill"></div></div>
        <span id="tod-cnt-evening" class="tod-cnt mono">0</span>
      </div>
      <div class="tod-row">
        <span class="tod-label">Night (0\u20136)</span>
        <div class="tod-track"><div id="tod-bar-night" class="tod-fill"></div></div>
        <span id="tod-cnt-night" class="tod-cnt mono">0</span>
      </div>
    </div>
  </div>
</section>
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

# When True, vendor-chart SHA mismatches / missing manifest entries / missing
# files degrade to a stderr warning (and the chart silently drops). When
# False (default), they raise :class:`RuntimeError` so a tampered or
# corrupted install fails loudly instead of shipping unverified JS to the
# browser. Flipped by ``--allow-unverified-charts``.
_ALLOW_UNVERIFIED_CHARTS = False


class VendorChartVerificationError(RuntimeError):
    """Raised when a vendored chart asset fails SHA-256 verification or is
    otherwise unavailable, and ``--allow-unverified-charts`` is not set."""


def _chart_verification_failure(msg: str) -> None:
    """Either raise a verification error or degrade to a stderr warning."""
    if _ALLOW_UNVERIFIED_CHARTS:
        print(f"[warn] {msg} (--allow-unverified-charts: continuing)",
              file=sys.stderr)
        return
    raise VendorChartVerificationError(msg)


@functools.lru_cache(maxsize=1)
def _load_chart_manifest() -> dict:
    """Parse ``vendor/charts/manifest.json``. Returns an empty libraries dict
    if the manifest is missing (keeps the tool usable in degraded mode).

    Cached for the process lifetime — callers (``_read_vendor_files`` and
    ``_maybe_warn_chart_license``) only read from the returned dict.
    """
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
    manifest before inclusion. On any failure (missing manifest entry,
    missing file, or SHA mismatch) raises :class:`VendorChartVerificationError`
    — fail-closed by default to prevent shipping unverified JS to the
    browser. Set ``--allow-unverified-charts`` to degrade to stderr warnings.
    """
    manifest = _load_chart_manifest()
    lib_entry = manifest.get("libraries", {}).get(library)
    if not lib_entry:
        _chart_verification_failure(
            f"chart library {library!r} not in vendor manifest at "
            f"{_VENDOR_CHARTS_DIR / 'manifest.json'}"
        )
        return ""
    parts: list[str] = []
    for f in lib_entry.get("files", []):
        if not f["path"].endswith(suffix):
            continue
        path = _VENDOR_CHARTS_DIR / f["path"]
        if not path.exists():
            _chart_verification_failure(f"vendor file missing: {path}")
            continue
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        expected = f.get("sha256", "")
        if not expected:
            _chart_verification_failure(
                f"vendor manifest entry for {path.name} has no sha256 field"
            )
            continue
        if actual != expected:
            _chart_verification_failure(
                f"SHA-256 mismatch for {path.name}: "
                f"expected {expected[:12]}…, got {actual[:12]}…"
            )
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


def _render_chart_highcharts(all_turns: list[dict],
                             x_title: str = "Turn") -> tuple[str, str]:
    """Highcharts renderer. Returns ``(chart_body_html, head_html)``.

    ``chart_body_html`` is the full ``<div id="chart-container">…</div>`` block
    dropped in the report body; ``head_html`` is the vendored library bundle
    wrapped in a ready-to-inline ``<script>`` tag for ``<head>``.

    ``x_title`` controls the x-axis label and the pagination header
    (e.g. "Turns 1–60 of 126"). Defaults to "Turn" for session/project
    scope; the instance dashboard passes "Day" since each data point
    is a calendar day rather than a per-turn record.
    """
    if not all_turns:
        return ("", "")
    s = _extract_chart_series(all_turns)
    body = _build_chart_html(
        s["cats"], s["crd"], s["cwr"], s["out"], s["inp"], s["cost"], x_title,
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


def _render_chart_uplot(all_turns: list[dict],
                        x_title: str = "Turn") -> tuple[str, str]:
    """uPlot renderer (MIT). Returns ``(body_html, head_html)``.

    uPlot has no built-in stacked-bars API — we pre-compute cumulative
    arrays caller-side so each bar series renders as a full stack from the
    baseline (the bottom-most series is drawn last so it sits on top
    visually).  Cost is a separate line series on a right-hand y-axis.
    Pagination + lazy rendering match the Highcharts renderer.

    ``x_title`` controls the x-series label and the pagination header.
    See :func:`_render_chart_highcharts` for the instance-scope rationale.
    """
    if not all_turns:
        return ("", "")
    series = _extract_chart_series(all_turns)
    containers_html, data_json = _build_lib_chart_pages(series, x_title)

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
      .u-cursor-pt { border-color: var(--accent, #58a6ff) !important; }
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
        {{ label: '{x_title}' }},
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


def _render_chart_chartjs(all_turns: list[dict],
                          x_title: str = "Turn") -> tuple[str, str]:
    """Chart.js v4 renderer (MIT). Returns ``(body_html, head_html)``.

    Mixed bar+line: four ``type: 'bar'`` datasets share ``stack: 'tokens'``
    on the left y-axis (``stacked: true``), one ``type: 'line'`` dataset
    rides on the right y-axis ``y1`` for cost. Pagination + lazy
    rendering match the Highcharts renderer.

    ``x_title`` controls the pagination header text (Chart.js itself has
    no x-axis title configured here; the instance dashboard still needs
    "Days 1–60 of N" instead of the default "Turns 1–60 of N").
    """
    if not all_turns:
        return ("", "")
    series = _extract_chart_series(all_turns)
    containers_html, data_json = _build_lib_chart_pages(series, x_title)

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


def _render_chart_none(all_turns: list[dict],
                       x_title: str = "Turn") -> tuple[str, str]:
    """No-chart renderer. Emits an empty body + empty head — useful when the
    caller wants a minimal detail page with no JS dependencies.

    ``x_title`` accepted for API parity with the other renderers; ignored.
    """
    del all_turns, x_title
    return ("", "")


CHART_RENDERERS = {
    "highcharts": _render_chart_highcharts,
    "uplot":      _render_chart_uplot,
    "chartjs":    _render_chart_chartjs,
    "none":       _render_chart_none,
}


def _fmt_cost(v: float) -> str:
    return f"${float(v or 0.0):.4f}"


def _build_by_skill_html(rows: list[dict],
                          heading: str = "Skills &amp; slash commands",
                          hint: str = "aggregated across this report scope · "
                                      "sticky attribution to slash-prefixed prompts") -> str:
    """Render the ``by_skill`` aggregation as a sortable section. Returns "" when empty."""
    if not rows:
        return ""
    body_rows: list[str] = []
    for r in rows:
        name = html_mod.escape(r.get("name") or "")
        body_rows.append(
            f'<tr>'
            f'<td><code>{name}</code></td>'
            f'<td class="num">{int(r.get("invocations", 0)):,}</td>'
            f'<td class="num">{int(r.get("turns_attributed", 0)):,}</td>'
            f'<td class="num">{int(r.get("input", 0)):,}</td>'
            f'<td class="num">{float(r.get("cache_hit_pct", 0.0)):.1f}%</td>'
            f'<td class="num">{int(r.get("output", 0)):,}</td>'
            f'<td class="num">{int(r.get("total_tokens", 0)):,}</td>'
            f'<td class="cost">{_fmt_cost(r.get("cost_usd", 0.0))}</td>'
            f'<td class="num">{float(r.get("pct_total_cost", 0.0)):.2f}%</td>'
            f'</tr>'
        )
    return (
        f'<section class="section">\n'
        f'<div class="section-title"><h2>{heading}</h2>'
        f'<span class="hint">{html_mod.escape(hint)}</span></div>\n'
        f'<table class="models-table">\n'
        f'<thead><tr>'
        f'<th>Name</th>'
        f'<th class="num">Invocations</th>'
        f'<th class="num">Turns</th>'
        f'<th class="num">Input</th>'
        f'<th class="num">% cached</th>'
        f'<th class="num">Output</th>'
        f'<th class="num">Total</th>'
        f'<th class="num">Cost $</th>'
        f'<th class="num">% of total</th>'
        f'</tr></thead>\n'
        f'<tbody>{"".join(body_rows)}</tbody>\n'
        f'</table>\n'
        f'</section>'
    )


def _build_by_subagent_type_html(rows: list[dict],
                                   heading: str = "Subagent types",
                                   subagents_included: bool = True) -> str:
    """Render ``by_subagent_type`` as a sortable section. Returns "" when empty.

    When the loader was invoked without ``--include-subagents``, token
    columns show only the *spawn-turn* contribution (zero for most rows).
    A footer note is rendered so users know to enable the flag for
    accurate per-type cost when relevant.
    """
    if not rows:
        return ""
    body_rows: list[str] = []
    for r in rows:
        name = html_mod.escape(r.get("name") or "")
        body_rows.append(
            f'<tr>'
            f'<td><code>{name}</code></td>'
            f'<td class="num">{int(r.get("spawn_count", 0)):,}</td>'
            f'<td class="num">{int(r.get("turns_attributed", 0)):,}</td>'
            f'<td class="num">{int(r.get("input", 0)):,}</td>'
            f'<td class="num">{float(r.get("cache_hit_pct", 0.0)):.1f}%</td>'
            f'<td class="num">{int(r.get("output", 0)):,}</td>'
            f'<td class="num">{int(r.get("total_tokens", 0)):,}</td>'
            f'<td class="num">{float(r.get("avg_tokens_per_call", 0.0)):,.0f}</td>'
            f'<td class="cost">{_fmt_cost(r.get("cost_usd", 0.0))}</td>'
            f'<td class="num">{float(r.get("pct_total_cost", 0.0)):.2f}%</td>'
            f'</tr>'
        )
    hint = ("aggregated across this report scope"
            if subagents_included else
            "spawn-count only · pass --include-subagents for full cost rollup")
    return (
        f'<section class="section">\n'
        f'<div class="section-title"><h2>{heading}</h2>'
        f'<span class="hint">{html_mod.escape(hint)}</span></div>\n'
        f'<table class="models-table">\n'
        f'<thead><tr>'
        f'<th>Subagent type</th>'
        f'<th class="num">Spawns</th>'
        f'<th class="num">Turns</th>'
        f'<th class="num">Input</th>'
        f'<th class="num">% cached</th>'
        f'<th class="num">Output</th>'
        f'<th class="num">Total</th>'
        f'<th class="num">Avg / call</th>'
        f'<th class="num">Cost $</th>'
        f'<th class="num">% of total</th>'
        f'</tr></thead>\n'
        f'<tbody>{"".join(body_rows)}</tbody>\n'
        f'</table>\n'
        f'</section>'
    )


def _build_cache_breaks_html(breaks: list[dict],
                               threshold: int,
                               max_rows: int = 100) -> str:
    """Render the cache-break section. Each row is an expandable <details>
    block showing the ±2 user-message context around the flagged turn.
    Returns "" when there are no breaks."""
    if not breaks:
        return ""
    rows_html: list[str] = []
    for cb in breaks[:max_rows]:
        proj = html_mod.escape(cb.get("project", "") or "")
        sid8 = (cb.get("session_id") or "")[:8]
        ts   = html_mod.escape(cb.get("timestamp_fmt") or cb.get("timestamp") or "")
        pct  = float(cb.get("cache_break_pct", 0.0))
        uncached = int(cb.get("uncached", 0))
        total    = int(cb.get("total_tokens", 0))
        snippet = html_mod.escape(cb.get("prompt_snippet") or "")
        context_rows: list[str] = []
        for ce in cb.get("context", []) or []:
            here_cls  = " cb-here" if ce.get("here") else ""
            here_mark = ' <span class="cb-mark">(this turn)</span>' if ce.get("here") else ""
            ctx_ts   = html_mod.escape(ce.get("ts", ""))
            ctx_text = html_mod.escape((ce.get("text") or "")[:240])
            slash    = ce.get("slash") or ""
            slash_html = (f' <code>/{html_mod.escape(slash)}</code>' if slash else "")
            context_rows.append(
                f'<li class="cb-ctx{here_cls}"><span class="cb-ts">{ctx_ts}</span>'
                f'{slash_html}{here_mark} — <span class="cb-txt">{ctx_text}</span></li>'
            )
        proj_cell = f'<span class="cb-proj">{proj}</span> · ' if proj else ''
        rows_html.append(
            f'<details class="cache-break-row">'
            f'<summary>'
            f'<span class="cb-uncached"><strong>{uncached:,}</strong> uncached</span>'
            f' · <span class="cb-pct">{pct:.0f}% of {total:,}</span>'
            f' · {proj_cell}<code>{sid8}</code> · <span class="cb-ts">{ts}</span>'
            f' · <span class="cb-snippet">{snippet}</span>'
            f'</summary>'
            f'<ul class="cb-context">{"".join(context_rows)}</ul>'
            f'</details>'
        )
    hint = f"single turns with input + cache_creation &gt; {threshold:,} · ±2 user-prompt context"
    count_text = f"{len(breaks)} event{'s' if len(breaks) != 1 else ''}"
    more_note = ""
    if len(breaks) > max_rows:
        more_note = (f'<p class="muted">Showing top {max_rows} of {len(breaks)} — '
                     f'raw list available in JSON export.</p>')
    return (
        f'<section class="section">\n'
        f'<div class="section-title"><h2>Cache breaks '
        f'<span class="hint-inline">({count_text})</span></h2>'
        f'<span class="hint">{hint}</span></div>\n'
        f'<div class="cache-breaks">{"".join(rows_html)}</div>\n'
        f'{more_note}'
        f'</section>'
    )


def _build_usage_insights_html(insights: list[dict]) -> str:
    """Render the Usage Insights panel for the dashboard variant.

    Top-of-fold = the highest-value insight that crossed its threshold
    (tie-break by candidate-list order). The remaining `shown` insights
    collapse into a native ``<details>``/``<summary>`` accordion. Returns
    `""` if no insights are shown — the panel disappears entirely so the
    layout reflows naturally to the existing rhythm.
    """
    shown = [i for i in (insights or []) if i.get("shown")]
    if not shown:
        return ""
    threshold_bearing = [i for i in shown if not i.get("always_on")]
    top = max(threshold_bearing, key=lambda i: i.get("value", 0)) if threshold_bearing else shown[0]
    rest = [i for i in shown if i is not top]

    def _li(insight: dict) -> str:
        # `body` and `headline` are constructed in `_compute_usage_insights`
        # with html_mod.escape already applied to identifier sub-strings
        # (model/tool names). Here we belt-and-braces escape the whole
        # string before wrapping in HTML tags. Numeric formatters
        # (`f"{pct:.0f}%"` etc.) are safe.
        h = html_mod.escape(insight.get("headline", ""))
        b = html_mod.escape(insight.get("body", ""))
        return f"      <li><strong>{h}</strong>{b}</li>"

    top_h = html_mod.escape(top.get("headline", ""))
    top_b = html_mod.escape(top.get("body", ""))
    if not rest:
        return (f'<section class="usage-insights" aria-label="Usage insights">\n'
                f'  <p class="ui-top"><strong>{top_h}</strong>{top_b}</p>\n'
                f'</section>')
    n = len(rest)
    plural = "" if n == 1 else "s"
    rest_html = "\n".join(_li(i) for i in rest)
    return (
        f'<section class="usage-insights" aria-label="Usage insights">\n'
        f'  <p class="ui-top"><strong>{top_h}</strong>{top_b}</p>\n'
        f'  <details>\n'
        f'    <summary>Show {n} more insight{plural}</summary>\n'
        f'    <ul class="ui-list">\n{rest_html}\n    </ul>\n'
        f'  </details>\n'
        f'</section>'
    )


def _build_usage_insights_md(insights: list[dict]) -> str:
    """Render the Usage Insights as a flat Markdown bullet list.
    Returns `""` if no insights are shown."""
    shown = [i for i in (insights or []) if i.get("shown")]
    if not shown:
        return ""
    threshold_bearing = [i for i in shown if not i.get("always_on")]
    top = max(threshold_bearing, key=lambda i: i.get("value", 0)) if threshold_bearing else shown[0]
    ordered = [top] + [i for i in shown if i is not top]
    lines = ["## Usage Insights", ""]
    for i in ordered:
        lines.append(f"- **{i.get('headline', '')}**{i.get('body', '')}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Theme layer — 4 themes (Beacon / Console / Lattice / Pulse) bundled in
# every HTML export, with a top-right picker. Ported from
# examples/claude-design-html-templates/variants-v1/{dashboard,detail}.html
# and layered over the existing class names (.cards/.card/.timeline-table/
# .turn-drawer/.prompts-table/.usage-insights/...) so the rewrite preserves
# every data contract the test suite asserts on while still producing the
# preview's visual output under each theme.
#
# Three helpers:
#   _theme_css()                 — full <style>...</style> block (base + 4 themes)
#   _theme_picker_markup()       — 4-button switcher for top-right
#   _theme_bootstrap_head_js()   — pre-paint hash/localStorage read (in <head>)
#   _theme_bootstrap_body_js()   — click handler + nav-forward (end of <body>)
# ---------------------------------------------------------------------------

def _theme_css() -> str:
    """Return the full themed stylesheet as a ``<style>...</style>`` block.

    Structure:
    - base reset + shared layout primitives (shell, page-header, topbar, nav,
      switcher, kpi grid, chart-card, punch, tod, rollup, blocks, chart-rail,
      timeline-table, drawer, prompts, foot)
    - four ``body.theme-<name>`` override blocks with matching colour tokens
    - legacy-class overlays (``.cards``/``.card``/``.usage-insights``/
      ``.turn-drawer``/``.prompts-table``/``.models-table``/timeline
      ``<table>`` inside ``.timeline-table`` etc.) mapped into theme
      surfaces so the Python renderer's existing f-string output keeps
      working under every theme.

    Intentionally kept as a non-f-string raw string so literal CSS braces
    don't need escaping.
    """
    return r"""<style>
/* =========================================================================
   BASE — shared reset, layout primitives, components
   ========================================================================= */
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
body{min-height:100vh;font-family:'Inter',system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;transition:background-color .15s ease,color .15s ease;font-size:13px;zoom:1.25}
a{color:inherit;text-decoration:none}
.mono{font-family:'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.num{text-align:right;font-variant-numeric:tabular-nums}
.muted{opacity:.6}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}

/* Outer frame */
.shell{max-width:1440px;margin:0 auto;padding:32px 40px 80px}
.page-header{display:flex;align-items:baseline;justify-content:space-between;gap:24px;flex-wrap:wrap;margin-bottom:32px}
.page-header h1{margin:0;font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:28px;letter-spacing:-.02em}
.page-header .meta{font-family:'JetBrains Mono',monospace;font-size:12px;opacity:.65;text-align:right}
.crumbs{display:flex;gap:12px;align-items:center;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.08em;text-transform:uppercase;opacity:.65;margin-bottom:10px;flex-wrap:wrap}
.crumbs .sep{opacity:.35}

.topbar{position:sticky;top:0;z-index:40;display:flex;justify-content:space-between;align-items:center;padding:14px 24px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.topbar .brand{display:flex;gap:10px;align-items:center;font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.16em;text-transform:uppercase}
.topbar .brand .dot{width:8px;height:8px;border-radius:50%}
.topbar .nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.navlink{padding:6px 12px;border-radius:999px;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;transition:all .15s ease}
.navlink.current{pointer-events:none}

.switcher{display:flex;gap:4px;padding:4px;border-radius:999px;margin-left:12px;flex-shrink:0}
.switcher button{padding:6px 12px;border-radius:999px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.12em;text-transform:uppercase;transition:all .15s ease;cursor:pointer;border:none;background:transparent}

.section{margin-top:40px}
.section-title{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:16px}
.section-title h2{margin:0;font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:18px;letter-spacing:-.01em}
.section-title .hint{font-family:'JetBrains Mono',monospace;font-size:11px;opacity:.55}

/* KPI grid + preview KPI cards */
.kpi-grid{display:grid;gap:16px;grid-template-columns:repeat(4,1fr)}
.kpi{padding:18px;border-radius:14px;position:relative;overflow:hidden;display:flex;flex-direction:column;gap:6px;min-height:100px}
.kpi .kpi-label{font-size:11px;letter-spacing:.1em;text-transform:uppercase;opacity:.7}
.kpi .kpi-val{font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:26px;letter-spacing:-.02em;line-height:1}
.kpi .kpi-sub{font-family:'JetBrains Mono',monospace;font-size:10px;opacity:.6;margin-top:auto}
.kpi .kpi-delta{font-family:'JetBrains Mono',monospace;font-size:10px}
.kpi .kpi-delta.up{color:#4ADE80}
.kpi .kpi-delta.down{color:#F87171}

/* Legacy ".cards"/".card" — maps into KPI-style surfaces */
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:0 0 24px 0}
.cards .card{padding:14px 18px;border-radius:10px;min-width:0;position:relative}
.cards .card .val{font-family:'Inter Tight','Inter',sans-serif;font-weight:700;font-size:22px;line-height:1.1}
.cards .card .lbl{font-size:11px;margin-top:4px;opacity:.7;letter-spacing:.02em}

/* Insights details panel (preview) */
details.insights{border-radius:12px;padding:0;overflow:hidden;margin-bottom:20px}
details.insights summary{cursor:pointer;padding:14px 20px;display:flex;align-items:center;justify-content:space-between;list-style:none;font-family:'Inter Tight','Inter',sans-serif;font-weight:500;font-size:14px}
details.insights summary::-webkit-details-marker{display:none}
details.insights summary .toggle{font-family:'JetBrains Mono',monospace;font-size:11px;opacity:.5;transition:transform .2s ease}
details.insights[open] summary .toggle{transform:rotate(90deg)}
details.insights .body{padding:4px 20px 20px;font-size:13px;line-height:1.65;opacity:.88}
details.insights .body ul{margin:0;padding-left:22px}
details.insights .body li{margin:6px 0}

/* Legacy .usage-insights wrapper — styled through theme rules */
.usage-insights{margin:0 0 24px;padding:14px 18px;border-radius:12px}
.usage-insights .ui-top{font-size:13px;line-height:1.55;margin:0}
.usage-insights .ui-top strong{font-size:15px;font-weight:600;margin-right:6px}
.usage-insights details{margin-top:10px;padding-top:8px;border-top:1px solid var(--border-dim)}
.usage-insights details > summary{list-style:none;cursor:pointer;font-size:12px;padding:4px 0;user-select:none;opacity:.75}
.usage-insights details > summary::-webkit-details-marker{display:none}
.usage-insights details > summary::before{content:"\25b8  ";font-size:10px;margin-right:4px}
.usage-insights details[open] > summary::before{content:"\25be  "}
.usage-insights ul.ui-list{list-style:none;padding:6px 0 0;margin:0}
.usage-insights ul.ui-list li{padding:7px 0;font-size:12px;line-height:1.5;border-top:1px dashed var(--border-dim)}
.usage-insights ul.ui-list li:first-child{border-top:none}
.usage-insights ul.ui-list li strong{font-weight:600;margin-right:6px}

/* Rollup / blocks / chart cards / punch / tod */
.rollup{padding:16px 20px;border-radius:12px}
.rollup table{width:100%;border-collapse:collapse;font-size:12px;font-family:'JetBrains Mono',monospace}
.rollup th,.rollup td{padding:8px 10px;text-align:right}
.rollup th:first-child,.rollup td:first-child{text-align:left}
.rollup thead th{font-weight:500;font-size:10px;letter-spacing:.1em;text-transform:uppercase;opacity:.55;border-bottom:1px solid var(--border);padding-bottom:10px}
.rollup tbody tr:hover td{background:var(--hover,transparent)}

.blocks{padding:16px 20px;border-radius:12px}
.block-row{display:grid;grid-template-columns:120px 1fr 80px 80px;gap:14px;align-items:center;padding:8px 0;font-size:12px;border-bottom:1px solid var(--border-dim)}
.block-row:last-child{border-bottom:0}
.block-row .label{font-family:'JetBrains Mono',monospace;opacity:.75}
.block-row .bar{height:8px;border-radius:4px;background:var(--bar-bg);overflow:hidden}
.block-row .bar-fill{height:100%;border-radius:4px;background:var(--accent)}

.chart-card{padding:16px 20px;border-radius:12px}
.chart-card .chart-body{width:100%;height:200px}
.chart-card svg{width:100%;height:100%;display:block}

.punch{padding:16px 20px;border-radius:12px;overflow-x:auto}
.punch-grid{min-width:580px}
.punch-row{display:flex;align-items:center;gap:3px;margin-bottom:3px}
.punch-day{flex:0 0 38px;font-family:'JetBrains Mono',monospace;font-size:10px;opacity:.45;text-align:right;padding-right:6px;white-space:nowrap}
.punch-hour{flex:1;font-family:'JetBrains Mono',monospace;font-size:9px;opacity:.45;text-align:center;overflow:hidden}
.punch-cell{flex:1;aspect-ratio:1;border-radius:3px;background:var(--punch-empty);display:flex;align-items:center;justify-content:center;min-width:0}
.punch-dot{border-radius:50%;transition:all .2s ease}
.punch-head-row{display:flex;align-items:center;gap:14px;margin-bottom:10px;flex-wrap:wrap}
.tz-select{background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer}
.tz-select:focus{outline:none;border-color:var(--accent)}

.tod{padding:16px 20px;border-radius:12px}
.tod-head{display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap}
.tod-head .tod-tz{background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer}
.tod-head .tod-tz:focus{outline:none;border-color:var(--accent)}
.tod-head .tod-total{font-family:'JetBrains Mono',monospace;font-size:11px;opacity:.65}
.tod-head .tod-total strong{opacity:1;font-weight:500}
.tod-rows{display:flex;flex-direction:column;gap:8px}
.tod-row{display:grid;grid-template-columns:130px 1fr 60px;align-items:center;gap:12px}
.tod-row .tod-label{font-family:'Inter',sans-serif;font-size:12px;opacity:.65;text-align:right}
.tod-row .tod-track{position:relative;height:20px;background:var(--punch-empty);border-radius:4px;overflow:hidden}
.tod-row .tod-fill{position:absolute;top:0;left:0;height:100%;background:var(--accent);border-radius:4px;min-width:2px;transition:width .25s ease}
.tod-row .tod-cnt{font-family:'JetBrains Mono',monospace;font-size:12px;text-align:right;opacity:.9;font-variant-numeric:tabular-nums}

/* Tables (legacy generic) — kept for Timeline / Prompts / Models */
table{width:100%;border-collapse:collapse;font-size:12px}
h1{font-size:22px;font-weight:600;margin:0 0 6px}
h2{font-size:15px;font-weight:600;margin:24px 0 12px;font-family:'Inter Tight','Inter',sans-serif;letter-spacing:-.005em}
h2 .legend{font-size:11px;font-weight:400;margin-left:10px;opacity:.6}
h2 .legend code{border-radius:3px;padding:0 4px;font-size:10px}
h2 .legend b{font-weight:600;opacity:.9}

.meta{font-size:11px;margin-bottom:20px;opacity:.65}
.meta code{border-radius:3px;padding:0 5px;font-size:10px}

th{font-weight:500;text-align:left;padding:8px 10px;white-space:nowrap;font-size:11px;letter-spacing:.04em;opacity:.75}
td{padding:6px 10px;vertical-align:middle}
tr:hover td{background:var(--hover,transparent)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.ts{white-space:nowrap;opacity:.75}
td.model{font-size:11px}
td.cost{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{display:inline-block;height:7px;border-radius:2px;margin-right:6px;vertical-align:middle}
tr.session-header{cursor:pointer}
tr.session-header td{padding:10px 12px;font-size:12px}
tr.session-header:hover td{filter:brightness(1.15)}
.toggle-arrow{display:inline-block;font-size:10px;transition:transform .15s;margin-right:4px}
tr.session-header.open .toggle-arrow{transform:rotate(90deg)}
tr.subtotal td{font-weight:600}
.models-table{padding:14px 16px;border-radius:12px}
.models-table table{font-size:12px;font-family:'JetBrains Mono',monospace}
.models-table code{font-size:11px}
.models-table th,.models-table td{padding:7px 12px}

/* Cache breaks (Phase A v1.6.0) — surface gets per-theme background via theme override blocks below; CSS-variable-driven inner styles work across all four variants. */
.cache-breaks{padding:14px 16px;border-radius:12px;display:flex;flex-direction:column;gap:8px}
.cache-break-row{padding:10px 14px;border-radius:8px;border:1px solid var(--border);background:var(--surface-deep,var(--border-dim));font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer;transition:border-color .15s ease,background .15s ease}
.cache-break-row[open]{background:var(--hover,rgba(165,139,255,.05));border-color:var(--accent)}
.cache-break-row summary{list-style:none;display:flex;flex-wrap:wrap;align-items:baseline;gap:6px;line-height:1.6}
.cache-break-row summary::-webkit-details-marker{display:none}
.cache-break-row summary::before{content:"\25b8";display:inline-block;color:var(--accent);font-size:10px;margin-right:4px;transition:transform .15s ease;width:10px}
.cache-break-row[open] summary::before{transform:rotate(90deg)}
.cache-break-row .cb-uncached{color:#F87171}
.cache-break-row .cb-uncached strong{font-size:12px;font-weight:600}
.cache-break-row .cb-pct{opacity:.7}
.cache-break-row .cb-proj{color:var(--accent);opacity:.85;font-weight:500}
.cache-break-row .cb-ts{opacity:.6;font-size:10px}
.cache-break-row .cb-snippet{opacity:.85;flex:1 1 240px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cb-context{list-style:none;margin:10px 0 4px;padding:8px 12px;border-left:2px solid var(--border);font-size:11px;line-height:1.6;background:var(--bg);border-radius:0 6px 6px 0}
.cb-context li{padding:3px 0;display:flex;gap:10px;align-items:baseline;font-family:'JetBrains Mono',monospace}
.cb-context .cb-ts{flex-shrink:0;opacity:.5;font-size:10px;min-width:140px}
.cb-context .cb-txt{opacity:.85;word-break:break-word}
.cb-context li.cb-here{background:rgba(251,191,36,.06);margin:4px -12px;padding:5px 12px;border-left:2px solid #FBBF24;border-radius:0}
.cb-context li.cb-here .cb-mark{color:#FBBF24;font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}

/* Phase-B (v1.7.0) "+N subagents" badge on Prompts table rows. Teal contrasts with the purple slash-command badge so the two badges stay distinguishable when both render on the same row. */
.prompts-subagent{display:inline-block;margin-left:6px;padding:1px 6px;font-size:10px;font-weight:500;letter-spacing:.04em;border-radius:4px;background:rgba(94,226,198,.14);color:#5EE2C6;border:1px solid rgba(94,226,198,.3);vertical-align:middle;cursor:help;white-space:nowrap}

td.mode-fast{font-size:10px;font-weight:600}
td.mode-std{font-size:10px;opacity:.55}

/* TTL + content-block badges (existing contract) */
.badge-ttl{display:inline-block;margin-left:6px;padding:0 5px;font-size:9px;font-weight:600;letter-spacing:.06em;border-radius:3px;vertical-align:middle;cursor:help}
.badge-ttl.ttl-1h{background:rgba(165,139,255,.18);color:var(--accent)}
.badge-ttl.ttl-mix{background:rgba(251,191,36,.18);color:#FBBF24}
td.content-blocks,th.content-blocks{font-variant-numeric:tabular-nums;font-family:'JetBrains Mono',monospace;font-size:11px;white-space:nowrap;cursor:help;opacity:.85}
td.content-blocks.muted{opacity:.35;cursor:default}

.legend-block{font-size:11px;margin:-4px 0 12px;padding:8px 12px;border-radius:6px;line-height:1.6;opacity:.85}
.legend-block b{font-weight:600}
.legend-block code{border-radius:3px;padding:0 4px;font-size:10px}

.chart-page-label{font-size:11px;padding:8px 12px 0;margin-top:4px;opacity:.65}

/* Resume markers */
tr.resume-marker-row td{padding:6px 10px;border-top:1px dashed var(--border);border-bottom:1px dashed var(--border)}
tr.resume-marker-row td.resume-marker-idx{color:var(--accent);opacity:.7}
tr.resume-marker-row td.resume-marker-cell{text-align:center;font-size:12px;opacity:.8}
.resume-marker-pill{display:inline-flex;align-items:center;gap:8px;padding:3px 10px;border-radius:12px;cursor:help;background:rgba(165,139,255,.08);border:1px solid rgba(165,139,255,.28)}
.resume-marker-pill strong{color:var(--accent);font-weight:600;font-size:12px;letter-spacing:.2px}
.resume-marker-pill .resume-marker-icon{color:var(--accent);font-size:14px;line-height:1}
.resume-marker-pill .resume-marker-time{font-size:11px;opacity:.7;font-variant-numeric:tabular-nums}
.resume-marker-pill.terminal{background:rgba(251,191,36,.1);border-color:rgba(251,191,36,.4)}
.resume-marker-pill.terminal strong,.resume-marker-pill.terminal .resume-marker-icon{color:#FBBF24}

tr.turn-row{cursor:pointer}
tr.turn-row:focus{outline:1px solid var(--accent);outline-offset:-1px}

/* Chart container + controls */
#chart-container{border-radius:12px;margin-bottom:24px;min-height:420px;overflow:hidden}
.chart-controls{display:flex;gap:10px;align-items:center;padding:10px 16px 0;flex-wrap:wrap}
.chart-controls label{font-size:11px;display:flex;align-items:center;gap:5px;cursor:pointer;opacity:.75}
.chart-controls input[type=range]{width:120px;accent-color:var(--accent)}
.chart-controls span{font-size:11px;color:var(--accent);min-width:28px}

/* Turn drawer (preview) */
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(520px,100%);transform:translateX(100%);transition:transform .25s cubic-bezier(.2,.8,.2,1);z-index:1000;display:flex;flex-direction:column;overflow:hidden;border-left:1px solid var(--border);background:var(--bg)}
.drawer.open{transform:translateX(0)}
.drawer-head{padding:24px 24px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;gap:16px}
.drawer-head h3{margin:0;font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:20px}
.drawer-head .x{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:18px;opacity:.6;background:none;border:0;cursor:pointer;color:inherit}
.drawer-head .x:hover{opacity:1;background:var(--hover,rgba(255,255,255,.05))}
.drawer-body{flex:1;overflow-y:auto;padding:20px 24px 32px}
.drawer-sec{margin-bottom:20px}
.drawer-sec h4{margin:0 0 8px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;opacity:.55;font-weight:500}
.drawer-kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-family:'JetBrains Mono',monospace;font-size:12px;margin:0}
.drawer-kv dt{opacity:.55}
.drawer-kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;word-break:break-word}
.drawer-prompt{padding:14px;border-radius:8px;background:var(--surface-deep,var(--border-dim));font-family:'JetBrains Mono',Menlo,Consolas,monospace;font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow-y:auto;border:1px solid var(--border)}
.drawer-more{margin-top:8px;border:1px solid var(--border);padding:4px 10px;font-size:11px;border-radius:4px;cursor:pointer;color:var(--accent);background:none}
.drawer-more:hover{border-color:var(--accent)}
.drawer-tools-list{list-style:none;padding:0;margin:0;font-family:'JetBrains Mono',monospace;font-size:11px}
.drawer-tools-list li{padding:5px 0;border-top:1px dashed var(--border-dim)}
.drawer-tools-list li:first-child{border-top:none}
.drawer-tool-preview{font-size:10px;opacity:.7;margin-left:6px;word-break:break-word}
.drawer-savings{color:#3fb950;font-size:11px;margin-top:6px;font-family:'JetBrains Mono',monospace}
.drawer-backdrop{position:fixed;inset:0;background:var(--backdrop,rgba(0,0,0,.5));opacity:0;pointer-events:none;transition:opacity .2s ease;z-index:999}
.drawer-backdrop.open{opacity:1;pointer-events:auto}

/* Chart-rail (horizontally-scrollable per-turn column chart) */
.chartrail-card{padding:20px 20px 16px;border-radius:20px;position:relative;--bar-h:200px;--head-h:0px;--foot-h:44px;--col-gap:4px}
.chartrail-legend{display:flex;gap:16px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;opacity:.7;margin-bottom:14px}
.chartrail-legend .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.chartrail-legend .sw.i{background:var(--accent)}
.chartrail-legend .sw.o{background:#5EE2C6}
.chartrail-legend .sw.cr{background:var(--accent);opacity:.3}
.chartrail-legend .sw.cw{background:#FBBF24}
.chartrail-legend .sw.cost{background:#F87171;border-radius:50%;width:8px;height:8px}
.chartrail-wrap{position:relative;display:grid;grid-template-columns:56px 1fr;gap:12px;align-items:start}
.chartrail-yaxis{position:relative;height:var(--bar-h);margin-top:var(--head-h);font-family:'JetBrains Mono',monospace;font-size:10px;opacity:.55}
.chartrail-yaxis .tick{position:absolute;right:4px;transform:translateY(-50%);white-space:nowrap}
.chartrail-yaxis .tick::after{content:"";position:absolute;right:-10px;top:50%;width:6px;height:1px;background:var(--border)}
.chartrail-scroll{position:relative;overflow-x:auto;overflow-y:hidden;scrollbar-width:thin;scroll-behavior:smooth;scroll-snap-type:x mandatory;padding-bottom:8px}
.chartrail-scroll::-webkit-scrollbar{height:6px}
.chartrail-scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.chartrail-scroll::-webkit-scrollbar-track{background:transparent}
.chartrail-inner{display:flex;gap:var(--col-gap,4px);align-items:flex-start;min-width:100%}
.tcol{flex:0 0 auto;width:40px;padding:6px 2px;scroll-snap-align:start;cursor:pointer;position:relative;display:flex;flex-direction:column;outline:none;border-radius:8px;border:1px solid transparent;background:transparent;transition:background .15s ease,border-color .15s ease,transform .15s ease}
.tcol:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tcol:hover,.tcol.active{background:var(--hover,rgba(165,139,255,.06));border-color:var(--border)}
.tcol.active{border-color:var(--accent)}
.tcol .tc-bar{position:relative;width:100%;height:var(--bar-h);display:flex;flex-direction:column-reverse;justify-content:flex-start;border-radius:4px;overflow:hidden;background:rgba(255,255,255,.015)}
.tcol .tc-bar .seg{width:100%;display:block;flex-shrink:0;transition:opacity .15s ease}
.tcol .tc-bar .seg.i{background:var(--accent)}
.tcol .tc-bar .seg.o{background:#5EE2C6}
.tcol .tc-bar .seg.cw{background:#FBBF24}
.tcol .tc-bar .seg.cr{background:var(--accent);opacity:.3}
.tcol .tc-bar .seg.cost{background:var(--accent)}
.tcol .tc-foot{height:var(--foot-h);padding-top:6px;display:flex;flex-direction:column;align-items:center;gap:2px;font-family:'JetBrains Mono',monospace;font-size:10px;line-height:1.2;overflow:hidden}
.tcol .tc-foot .tc-n{color:var(--accent);font-weight:500}
.tcol .tc-foot .tc-time{opacity:.6;font-size:9px}
.tcol .tc-foot .tc-cost{font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:11px;opacity:.9}
.tcol.session-break{margin-left:16px;padding-left:12px;border-left:1px dashed var(--border)}
.tcol.session-break .tc-seslabel{position:absolute;top:-16px;left:12px;font-family:'JetBrains Mono',monospace;font-size:9px;opacity:.55;letter-spacing:.08em;white-space:nowrap}
.tcol.resume .tc-bar{background:rgba(165,139,255,.1);display:flex;align-items:center;justify-content:center;flex-direction:row}
.tcol.resume .tc-bar::before{content:"\2634";color:var(--accent);font-size:16px}
.rail-chev{position:absolute;top:130px;width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--surface,#111);border:1px solid var(--border);z-index:3;cursor:pointer;opacity:.85;color:inherit;font-size:16px}
.rail-chev:hover{opacity:1}
.rail-chev.left{left:48px}
.rail-chev.right{right:-4px}
.rail-indicator{display:flex;align-items:center;gap:12px;justify-content:space-between;margin-top:14px;font-family:'JetBrains Mono',monospace;font-size:11px;opacity:.65}
.rail-progress{flex:1;height:2px;background:var(--border);border-radius:1px;overflow:hidden}
.rail-progress-fill{height:100%;background:var(--accent);width:10%;transition:width .1s linear}

/* Prompts (preview) */
.prompts{padding:20px;border-radius:16px;margin-top:16px}
.prompts table{font-size:12px}
.prompts th,.prompts td{padding:10px 12px;border-bottom:1px solid var(--border-dim);text-align:left;vertical-align:top}
.prompts th.num,.prompts td.num{text-align:right;font-family:'JetBrains Mono',monospace}
.prompts thead th{font-weight:500;font-size:10px;letter-spacing:.12em;text-transform:uppercase;opacity:.55;border-bottom:1px solid var(--border)}
.prompts .prompt-text{max-width:560px;font-family:'Inter',sans-serif;line-height:1.55;font-size:13px;opacity:.88}
.prompts .prompt-text.truncate{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.prompts tbody tr[data-turn]{cursor:pointer;transition:background .1s ease}
.prompts tbody tr[data-turn]:hover td,.prompts tbody tr[data-turn].active td{background:var(--hover,rgba(165,139,255,.05))}
.prompts tbody tr[data-turn].active td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.prompts tbody tr[data-turn]:focus{outline:1px solid var(--accent);outline-offset:-1px}
.prompts .prompt-turn-link{color:var(--accent);text-decoration:none;font-family:'JetBrains Mono',monospace}
.prompts .prompt-turn-link:hover{text-decoration:underline}
.prompts td.cost{color:#d29922;font-variant-numeric:tabular-nums;white-space:nowrap}
.prompts td.model code{font-size:11px}
.prompts .prompts-slash{display:inline-block;padding:0 5px;font-size:10px;border-radius:3px;margin-left:6px;background:rgba(137,87,229,.18);border:1px solid rgba(137,87,229,.4);color:#bc8cff}

/* Footer */
.foot{margin-top:60px;padding:20px 0;border-top:1px solid var(--border-dim);font-family:'JetBrains Mono',monospace;font-size:11px;opacity:.5;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}

/* =========================================================================
   THEME 1 — BEACON MINIMAL (default)
   ========================================================================= */
body.theme-beacon{
  --bg:#0A0A0C;--surface:#111114;--surface-deep:#0E0E12;--border:#1E1E22;--border-dim:#16161a;
  --fg:#EDECEF;--fg-dim:#8C8B93;--accent:#A58BFF;--accent-soft:#7C6BD9;
  --punch-empty:#141418;--bar-bg:#1a1a1f;--hover:rgba(165,139,255,.05);
  --backdrop:rgba(0,0,0,.65);
  background:#0A0A0C;color:#EDECEF;
}
body.theme-beacon .topbar{background:rgba(10,10,12,.78);border-bottom:1px solid #16161a}
body.theme-beacon .topbar .brand .dot{background:#A58BFF;box-shadow:0 0 12px rgba(165,139,255,.5)}
body.theme-beacon .navlink{color:#8C8B93}
body.theme-beacon .navlink.current{color:#EDECEF;background:rgba(165,139,255,.1)}
body.theme-beacon .navlink:hover{color:#EDECEF}
body.theme-beacon .switcher{background:rgba(17,17,20,.88);border:1px solid #1E1E22;backdrop-filter:blur(12px)}
body.theme-beacon .switcher button{color:#8C8B93}
body.theme-beacon .switcher button.active{background:#A58BFF;color:#0A0A0C}
body.theme-beacon .kpi{background:#111114;border:1px solid #1E1E22;position:relative}
body.theme-beacon .kpi::before{content:"";position:absolute;top:0;left:0;width:20px;height:1px;background:#A58BFF}
body.theme-beacon .kpi::after{content:"";position:absolute;top:0;left:0;width:1px;height:20px;background:#A58BFF}
body.theme-beacon .kpi.featured .kpi-val{color:#A58BFF}
body.theme-beacon details.insights,body.theme-beacon .usage-insights,
body.theme-beacon .rollup,body.theme-beacon .blocks,body.theme-beacon .chart-card,
body.theme-beacon .punch,body.theme-beacon .tod,body.theme-beacon .models-table,
body.theme-beacon .cache-breaks,
body.theme-beacon .cards .card,body.theme-beacon #chart-container,
body.theme-beacon .legend-block,body.theme-beacon .prompts,
body.theme-beacon .timeline-table,body.theme-beacon .chartrail-card,
body.theme-beacon .drawer,body.theme-beacon #weekly-rollup,
body.theme-beacon #session-blocks,body.theme-beacon #hod-chart{background:#111114;border:1px solid #1E1E22}
body.theme-beacon .cards .card .val{color:#A58BFF}
body.theme-beacon .cards .card.green .val{color:#3fb950}
body.theme-beacon .cards .card.amber .val{color:#d29922}
body.theme-beacon th{background:#0E0E12;border-bottom:1px solid #1E1E22;color:#8C8B93}
body.theme-beacon td{border-bottom:1px solid #16161a}
body.theme-beacon tr.session-header td{background:#14141a;color:#A58BFF;border-top:2px solid #1E1E22}
body.theme-beacon tr.subtotal td{background:#111114;border-top:1px solid #1E1E22}

/* =========================================================================
   THEME 2 — CONSOLE GLASS
   ========================================================================= */
body.theme-console{
  --bg:#08080A;--surface:rgba(165,139,255,.04);--surface-deep:rgba(165,139,255,.02);
  --border:rgba(165,139,255,.16);--border-dim:rgba(165,139,255,.08);
  --fg:#E8E6F0;--fg-dim:#8A88A0;--accent:#A58BFF;--accent-soft:#5EE2C6;
  --punch-empty:rgba(165,139,255,.05);--bar-bg:rgba(165,139,255,.08);--hover:rgba(165,139,255,.07);
  --backdrop:rgba(0,0,0,.7);
  background:#08080A;color:#E8E6F0;
  background-image:radial-gradient(circle at 1px 1px,#1A1A20 1px,transparent 1px);
  background-size:24px 24px;
}
body.theme-console .page-header h1{font-family:'JetBrains Mono',monospace;font-weight:500;text-transform:uppercase;letter-spacing:.04em;font-size:20px}
body.theme-console .page-header h1::before{content:"[ ";color:#A58BFF;opacity:.7}
body.theme-console .page-header h1::after{content:" ]";color:#A58BFF;opacity:.7}
body.theme-console .section-title h2{font-family:'JetBrains Mono',monospace;font-weight:500;text-transform:uppercase;letter-spacing:.08em;font-size:12px}
body.theme-console .section-title h2::before{content:"[ ";color:#A58BFF;opacity:.6}
body.theme-console .section-title h2::after{content:" ]";color:#A58BFF;opacity:.6}
body.theme-console h2{font-family:'JetBrains Mono',monospace;font-weight:500;text-transform:uppercase;letter-spacing:.06em;font-size:12px;color:#A58BFF}
body.theme-console .topbar{background:rgba(8,8,10,.88);border-bottom:1px solid rgba(165,139,255,.12)}
body.theme-console .topbar .brand .dot{background:#A58BFF;box-shadow:0 0 8px #A58BFF,0 0 16px rgba(165,139,255,.5)}
body.theme-console .navlink{color:#8A88A0;font-family:'JetBrains Mono',monospace}
body.theme-console .navlink.current{color:#A58BFF;background:rgba(165,139,255,.08);border:1px solid rgba(165,139,255,.25)}
body.theme-console .switcher{background:rgba(8,8,10,.92);border:1px solid rgba(165,139,255,.2);backdrop-filter:blur(12px)}
body.theme-console .switcher button{color:#8A88A0}
body.theme-console .switcher button.active{background:rgba(165,139,255,.15);color:#A58BFF;border:1px solid #A58BFF}
body.theme-console .kpi{background:rgba(165,139,255,.04);border:1px solid rgba(165,139,255,.16);border-radius:10px}
body.theme-console .kpi .kpi-val{font-family:'JetBrains Mono',monospace;font-weight:500;font-size:22px;color:#A58BFF}
body.theme-console .kpi .kpi-label{font-family:'JetBrains Mono',monospace;color:#8A88A0}
body.theme-console .kpi.teal .kpi-val{color:#5EE2C6}
body.theme-console details.insights,body.theme-console .usage-insights,
body.theme-console .rollup,body.theme-console .blocks,body.theme-console .chart-card,
body.theme-console .punch,body.theme-console .tod,body.theme-console .models-table,
body.theme-console .cache-breaks,
body.theme-console .cards .card,body.theme-console #chart-container,
body.theme-console .legend-block,body.theme-console .prompts,
body.theme-console .timeline-table,body.theme-console .chartrail-card,
body.theme-console .drawer,body.theme-console #weekly-rollup,
body.theme-console #session-blocks,body.theme-console #hod-chart{background:rgba(165,139,255,.03);border:1px solid rgba(165,139,255,.14);border-radius:10px}
body.theme-console .cards .card .val{color:#A58BFF;font-family:'JetBrains Mono',monospace;font-weight:500}
body.theme-console .cards .card.green .val{color:#5EE2C6}
body.theme-console .cards .card.amber .val{color:#FFB86B}
body.theme-console th{background:rgba(165,139,255,.05);border-bottom:1px solid rgba(165,139,255,.16);color:#8A88A0;font-family:'JetBrains Mono',monospace}
body.theme-console td{border-bottom:1px solid rgba(165,139,255,.08)}
body.theme-console tr.session-header td{background:rgba(165,139,255,.08);color:#A58BFF;border-top:1px solid rgba(165,139,255,.2)}
body.theme-console tr.subtotal td{background:rgba(165,139,255,.05);border-top:1px solid rgba(165,139,255,.16)}
body.theme-console .drawer{background:var(--bg)}

/* =========================================================================
   THEME 3 — LATTICE COMPACT
   ========================================================================= */
body.theme-lattice{
  --bg:#09090C;--surface:#101014;--surface-deep:#0C0C10;--border:#17171C;--border-dim:#121216;
  --fg:#E4E2E8;--fg-dim:#7E7C88;--accent:#A58BFF;--accent-soft:#7C6BD9;
  --punch-empty:#131318;--bar-bg:#17171C;--hover:rgba(165,139,255,.05);
  --backdrop:rgba(0,0,0,.65);
  background:#09090C;color:#E4E2E8;font-size:12px;
}
body.theme-lattice .shell{padding-top:24px}
body.theme-lattice .page-header h1{font-family:'Inter Tight','Inter',sans-serif;font-weight:600;font-size:22px;letter-spacing:-.015em}
body.theme-lattice .section{margin-top:32px}
body.theme-lattice .section-title h2{font-weight:600;font-size:14px}
body.theme-lattice h2{font-size:14px}
body.theme-lattice .topbar{background:rgba(9,9,12,.92);border-bottom:1px solid #17171C}
body.theme-lattice .topbar .brand .dot{width:6px;height:6px;background:#A58BFF;border-radius:1px}
body.theme-lattice .navlink{color:#7E7C88}
body.theme-lattice .navlink.current{background:rgba(165,139,255,.1);color:#A58BFF}
body.theme-lattice .switcher{background:#101014;border:1px solid #17171C;border-radius:6px}
body.theme-lattice .switcher button{border-radius:4px}
body.theme-lattice .switcher button.active{background:#A58BFF;color:#09090C}
body.theme-lattice .kpi-grid{grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
body.theme-lattice .kpi{background:#101014;border-radius:8px;padding:14px;min-height:80px;position:relative;border:0}
body.theme-lattice .kpi::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:2px;background:#A58BFF;border-radius:1px}
body.theme-lattice .kpi.cat-tokens::before{background:#5EE2C6}
body.theme-lattice .kpi.cat-time::before{background:#FBBF24}
body.theme-lattice .kpi.cat-save::before{background:#4ADE80}
body.theme-lattice .kpi .kpi-val{font-weight:600;font-size:22px}
body.theme-lattice .kpi .kpi-label{font-size:10px;letter-spacing:.08em}
body.theme-lattice details.insights,body.theme-lattice .usage-insights,
body.theme-lattice .rollup,body.theme-lattice .blocks,body.theme-lattice .chart-card,
body.theme-lattice .punch,body.theme-lattice .tod,body.theme-lattice .models-table,
body.theme-lattice .cache-breaks,
body.theme-lattice .cards .card,body.theme-lattice #chart-container,
body.theme-lattice .legend-block,body.theme-lattice .prompts,
body.theme-lattice .timeline-table,body.theme-lattice .chartrail-card,
body.theme-lattice .drawer,body.theme-lattice #weekly-rollup,
body.theme-lattice #session-blocks,body.theme-lattice #hod-chart{background:#101014;border:1px solid #17171C;border-radius:8px}
body.theme-lattice .cards .card{padding:12px 14px;position:relative}
body.theme-lattice .cards .card::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:2px;background:#A58BFF;border-radius:1px}
body.theme-lattice .cards .card.green::before{background:#4ADE80}
body.theme-lattice .cards .card.amber::before{background:#FBBF24}
body.theme-lattice .cards .card .val{font-size:20px}
body.theme-lattice th{background:#0C0C10;border-bottom:1px solid #17171C;color:#7E7C88}
body.theme-lattice td{border-bottom:1px solid #121216}
body.theme-lattice tr.session-header td{background:#13111a;color:#A58BFF;border-top:1px solid #17171C}
body.theme-lattice tr.subtotal td{background:#101014;border-top:1px solid #17171C}

/* =========================================================================
   THEME 4 — PULSE (amber+lilac gradient)
   ========================================================================= */
body.theme-pulse{
  --bg:#0D0B14;--surface:#15121C;--surface-deep:#110F18;--border:#2A2438;--border-dim:#1D1928;
  --fg:#F2EFF7;--fg-dim:#9E9AAE;--accent:#C084FC;--accent-soft:#FFB86B;
  --punch-empty:#1D1928;--bar-bg:#1D1928;--hover:rgba(192,132,252,.08);
  --backdrop:rgba(0,0,0,.65);
  background:radial-gradient(circle at 85% -20%,rgba(255,184,107,.08),transparent 40%),radial-gradient(circle at -10% 120%,rgba(192,132,252,.12),transparent 50%),#0D0B14;
  color:#F2EFF7;
}
body.theme-pulse .page-header h1{font-weight:700;font-size:30px;letter-spacing:-.025em;background:linear-gradient(90deg,#FFB86B,#C084FC 60%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
body.theme-pulse h2{font-weight:600;font-size:17px;letter-spacing:-.015em}
body.theme-pulse .topbar{background:rgba(13,11,20,.82);border-bottom:1px solid #1D1928}
body.theme-pulse .topbar .brand .dot{background:#FFB86B;box-shadow:0 0 10px rgba(255,184,107,.6)}
body.theme-pulse .navlink{color:#9E9AAE}
body.theme-pulse .navlink.current{background:rgba(192,132,252,.12);color:#C084FC}
body.theme-pulse .switcher{background:rgba(21,18,28,.92);border:1px solid #2A2438;backdrop-filter:blur(12px)}
body.theme-pulse .switcher button{color:#9E9AAE}
body.theme-pulse .switcher button.active{background:linear-gradient(90deg,#FFB86B,#C084FC);color:#0D0B14}
body.theme-pulse .kpi{background:#15121C;border:1px solid #2A2438;border-radius:14px;position:relative;overflow:hidden}
body.theme-pulse .kpi::before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 100% 0%,rgba(255,184,107,.08),transparent 50%);pointer-events:none}
body.theme-pulse .kpi .kpi-val{font-weight:700;font-size:28px;letter-spacing:-.02em}
body.theme-pulse .kpi.featured{background:linear-gradient(135deg,rgba(192,132,252,.18),rgba(255,184,107,.12) 60%,#15121C);border:1px solid rgba(192,132,252,.35);animation:sm-pulse-ring-lg 3s ease-in-out infinite}
body.theme-pulse .kpi.featured .kpi-val{font-size:44px;line-height:1;background:linear-gradient(90deg,#FFB86B,#C084FC 60%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}
body.theme-pulse .kpi.featured .kpi-label{color:#FFB86B;font-weight:600}
body.theme-pulse .kpi.cat-save .kpi-val,body.theme-pulse .kpi.teal .kpi-val{color:#5EE2C6}
body.theme-pulse .kpi.cat-time .kpi-val{color:#FFB86B}
@keyframes sm-pulse-ring-lg{0%,100%{box-shadow:0 0 0 0 rgba(192,132,252,.25)}50%{box-shadow:0 0 0 4px rgba(192,132,252,0)}}
body.theme-pulse details.insights,body.theme-pulse .usage-insights,
body.theme-pulse .rollup,body.theme-pulse .blocks,body.theme-pulse .chart-card,
body.theme-pulse .punch,body.theme-pulse .tod,body.theme-pulse .models-table,
body.theme-pulse .cache-breaks,
body.theme-pulse .cards .card,body.theme-pulse #chart-container,
body.theme-pulse .legend-block,body.theme-pulse .prompts,
body.theme-pulse .timeline-table,body.theme-pulse .chartrail-card,
body.theme-pulse .drawer,body.theme-pulse #weekly-rollup,
body.theme-pulse #session-blocks,body.theme-pulse #hod-chart{background:#15121C;border:1px solid #2A2438;border-radius:14px}
body.theme-pulse .cards .card .val{background:linear-gradient(90deg,#FFB86B,#C084FC 60%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;font-weight:700}
body.theme-pulse .cards .card.green .val{background:none;-webkit-text-fill-color:initial;color:#5EE2C6}
body.theme-pulse .cards .card.amber .val{background:none;-webkit-text-fill-color:initial;color:#FFB86B}
body.theme-pulse th{background:#110F18;border-bottom:1px solid #2A2438;color:#9E9AAE}
body.theme-pulse td{border-bottom:1px solid #1D1928}
body.theme-pulse tr.session-header td{background:#1D1928;color:#C084FC;border-top:1px solid #2A2438}
body.theme-pulse tr.subtotal td{background:#15121C;border-top:1px solid #2A2438}

/* =========================================================================
   Responsive
   ========================================================================= */
@media (max-width:1200px){
  .kpi-grid{grid-template-columns:repeat(3,1fr)}
}
@media (max-width:780px){
  .shell{padding:20px 16px 40px}
  .kpi-grid{grid-template-columns:repeat(2,1fr)}
  .topbar{flex-wrap:wrap;gap:8px}
  .topbar .nav{margin-left:0}
  .switcher{margin-left:0}
  .drawer{width:100%}
}
@media print{
  .drawer,.drawer-backdrop,.topbar,.switcher{display:none!important}
  .shell{max-width:none;padding:0}
}
</style>"""


def _theme_picker_markup() -> str:
    """4-button theme switcher embedded inside the topbar's <nav> element.

    The four buttons match the four themes in ``_theme_css()``. The active
    class is toggled by ``_theme_bootstrap_body_js()`` on apply.
    """
    return (
        '<div class="switcher" role="tablist" aria-label="Theme variant switcher">'
        '<button data-theme="theme-beacon">Beacon</button>'
        '<button data-theme="theme-console" class="active">Console</button>'
        '<button data-theme="theme-lattice">Lattice</button>'
        '<button data-theme="theme-pulse">Pulse</button>'
        '</div>'
    )


def _theme_bootstrap_head_js() -> str:
    """Pre-paint <head> script: reads URL hash (``#theme=X``), falls back to
    ``localStorage['sm_theme']``, defaults to ``console``. Writes the resolved
    theme onto ``<html data-sm-theme=...>`` so the body-end script can apply
    synchronously without a paint-flash."""
    return (
        '<script>'
        '(function(){try{'
          'var h=(location.hash.match(/theme=([a-z]+)/)||[])[1];'
          'var t=h||(function(){try{return localStorage.getItem("sm_theme");}'
                    'catch(e){return null;}})()||"console";'
          'if(!/^(beacon|console|lattice|pulse)$/.test(t))t="console";'
          'document.documentElement.setAttribute("data-sm-theme",t);'
        '}catch(e){}})();'
        '</script>'
    )


def _theme_bootstrap_body_js() -> str:
    """End-of-body script: applies the theme class to <body>, wires the
    switcher buttons, persists to ``localStorage`` wrapped in try/catch
    (Firefox ``privacy.file_unique_origin=true`` throws ``SecurityError``
    on ``file://``), and rewrites any ``a[data-sm-nav]`` href with the
    current ``#theme=`` so cross-file nav preserves the picked theme.

    Also re-skins accent-color-bearing chart libraries when possible —
    current strategy: reload with the hash preserved. uPlot/Highcharts
    have no cheap post-init accent API so a reload is the simplest
    correct answer, and the hash makes it seamless.
    """
    return (
        '<script>'
        '(function(){'
          'function apply(t,isUserAction){'
            'document.body.className='
              'document.body.className.replace(/\\btheme-\\w+\\b/g,"").trim()'
              '+" theme-"+t;'
            'var btns=document.querySelectorAll(".switcher button");'
            'btns.forEach(function(b){'
              'b.classList.toggle("active",b.dataset.theme==="theme-"+t);'
            '});'
            'try{localStorage.setItem("sm_theme",t);}catch(e){}'
            'var h="theme="+t;'
            'if(location.hash.indexOf("theme=")>=0){'
              'location.hash=location.hash.replace(/theme=[a-z]+/,h);'
            '}else if(location.hash&&location.hash.length>1){'
              'location.hash=location.hash.substring(1)+"&"+h;'
            '}else{'
              'location.hash=h;'
            '}'
            'document.querySelectorAll("a[data-sm-nav]").forEach(function(a){'
              'a.href=a.href.split("#")[0]+"#"+h;'
            '});'
            'if(isUserAction&&window.SM_RESKIN_CHARTS){'
              'try{window.SM_RESKIN_CHARTS();}catch(e){}'
            '}'
          '}'
          'var init=document.documentElement.getAttribute("data-sm-theme")||"console";'
          'apply(init,false);'
          'document.querySelectorAll(".switcher button").forEach(function(b){'
            'b.addEventListener("click",function(){'
              'apply(b.dataset.theme.replace("theme-",""),true);'
            '});'
          '});'
        '})();'
        '</script>'
    )


def _build_chartrail_section_html(chartrail_data: list) -> str:
    """Return the chartrail section HTML for a given list of turn dicts.

    Returns an empty string if ``chartrail_data`` is empty.
    """
    if not chartrail_data:
        return ""
    rail_json = json.dumps(chartrail_data, separators=(",", ":"),
                            default=str).replace("</", "<\\/")
    n_turns = len(chartrail_data)
    return (
        '<section class="section">\n'
        '<div class="section-title"><h2>Token usage over time</h2>'
        '<span class="hint">scroll horizontally &middot; click a turn '
        'to drill in &middot; \u2190 \u2192</span></div>\n'
        '<div class="chartrail-card">\n'
        '  <div class="chartrail-legend">\n'
        '    <span><span class="sw i"></span>Input (new)</span>\n'
        '    <span><span class="sw o"></span>Output</span>\n'
        '    <span><span class="sw cw"></span>Cache write</span>\n'
        '    <span><span class="sw cr"></span>Cache read</span>\n'
        '    <span><span class="sw cost"></span>Cost $</span>\n'
        '  </div>\n'
        '  <div class="chartrail-wrap">\n'
        '    <div class="chartrail-yaxis" id="chartrail-yaxis"></div>\n'
        '    <div class="chartrail-scroll" id="chartrail-scroll" '
        'tabindex="0">\n'
        '      <div class="chartrail-inner" id="chartrail-inner">'
        '</div>\n'
        '    </div>\n'
        '    <button class="rail-chev left" id="rail-prev" '
        'aria-label="Scroll turns left">\u2039</button>\n'
        '    <button class="rail-chev right" id="rail-next" '
        'aria-label="Scroll turns right">\u203a</button>\n'
        '  </div>\n'
        '  <div class="rail-indicator">\n'
        f'    <span><span id="rail-counter">01</span> / {n_turns}</span>\n'
        '    <div class="rail-progress">'
        '<div class="rail-progress-fill" id="rail-progress-fill">'
        '</div></div>\n'
        '    <span>scroll or use \u2190 \u2192</span>\n'
        '  </div>\n'
        '</div>\n'
        '<script type="application/json" id="chartrail-data">'
        f'{rail_json}</script>\n'
        '</section>'
    )


def _chartrail_script() -> str:
    """Return the full chartrail interaction JS string."""
    return """<script>
(function () {
  var root = document.getElementById('chartrail-data');
  if (!root) return;
  var rows; try { rows = JSON.parse(root.textContent); } catch (e) { return; }
  var scroll = document.getElementById('chartrail-scroll');
  var inner  = document.getElementById('chartrail-inner');
  var yaxis  = document.getElementById('chartrail-yaxis');
  var counter= document.getElementById('rail-counter');
  var progress = document.getElementById('rail-progress-fill');
  if (!scroll || !inner || !yaxis) return;

  // Max tokens = inp + out + cr + cw per turn
  var maxTok = 0;
  rows.forEach(function (t) {
    var tot = (t.inp||0) + (t.out||0) + (t.cr||0) + (t.cw||0);
    if (tot > maxTok) maxTok = tot;
  });
  if (!maxTok) maxTok = 1;

  // Y-axis ticks: 5 bands 0..max
  var yHtml = '';
  for (var i = 0; i <= 4; i++) {
    var v = (maxTok / 4) * i;
    var label = v >= 1e6 ? (v/1e6).toFixed(1) + 'M'
              : v >= 1e3 ? Math.round(v/1e3) + 'k'
              : Math.round(v);
    var pct = 100 - (i/4) * 100;
    yHtml += '<span class="tick" style="top:' + pct + '%">' + label + '</span>';
  }
  yaxis.innerHTML = yHtml;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  var parts = [];
  rows.forEach(function (t, i) {
    if (t.resm) {
      var label = t.term ? 'Session exited' : 'Session resumed';
      parts.push('<div class="tcol resume' +
        (t.sbrk && i > 0 ? ' session-break' : '') +
        '" title="' + esc(label + ' at ' + (t.ts || '')) + '">' +
        (t.sbrk && i > 0
          ? '<div class="tc-seslabel">' + esc(t.slbl || '') + '</div>'
          : '') +
        '<div class="tc-bar" aria-hidden="true"></div>' +
        '<div class="tc-foot"><span class="tc-n">' +
        String(t.n).padStart(2, '0') + '</span>' +
        '<span class="tc-time">' + esc(t.time || '') + '</span>' +
        '<span class="tc-cost" style="opacity:.5">&mdash;</span></div>' +
        '</div>');
      return;
    }
    var pctI  = (t.inp /maxTok) * 100;
    var pctO  = (t.out /maxTok) * 100;
    var pctCw = (t.cw  /maxTok) * 100;
    var pctCr = (t.cr  /maxTok) * 100;
    var tot = (t.inp||0) + (t.out||0) + (t.cr||0) + (t.cw||0);
    var title = 'Turn ' + t.n + ' \u00b7 ' + (t.time || '') + ' \u00b7 ' +
                (t.mdl || '') + ' \u00b7 tokens ' + tot.toLocaleString() +
                ' \u00b7 $' + (t.cost || 0).toFixed(4);
    parts.push('<div class="tcol' +
      (t.sbrk && i > 0 ? ' session-break' : '') +
      '" data-turn="' + esc(t.key) + '" tabindex="0" title="' + esc(title) + '">' +
      (t.sbrk && i > 0
        ? '<div class="tc-seslabel">' + esc(t.slbl || '') + '</div>'
        : '') +
      '<div class="tc-bar" aria-hidden="true">' +
      '<span class="seg i"  style="height:' + pctI.toFixed(2) + '%"></span>' +
      '<span class="seg o"  style="height:' + pctO.toFixed(2) + '%"></span>' +
      '<span class="seg cw" style="height:' + pctCw.toFixed(2) + '%"></span>' +
      '<span class="seg cr" style="height:' + pctCr.toFixed(2) + '%"></span>' +
      '</div>' +
      '<div class="tc-foot">' +
      '<span class="tc-n">' + String(t.n).padStart(2, '0') + '</span>' +
      '<span class="tc-time">' + esc(t.time || '') + '</span>' +
      '<span class="tc-cost">$' + (t.cost || 0).toFixed(3) + '</span>' +
      '</div></div>');
  });
  inner.innerHTML = parts.join('');

  // Click → open drawer via shared opener (from drawer script).
  inner.addEventListener('click', function (ev) {
    var col = ev.target && ev.target.closest ? ev.target.closest('.tcol') : null;
    if (!col) return;
    var key = col.getAttribute('data-turn');
    if (key && typeof window.smOpenDrawer === 'function') window.smOpenDrawer(key);
  });
  inner.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' || ev.key === ' ') {
      var el = document.activeElement;
      if (el && el.classList && el.classList.contains('tcol')) {
        var key = el.getAttribute('data-turn');
        if (key && typeof window.smOpenDrawer === 'function') {
          ev.preventDefault();
          window.smOpenDrawer(key);
        }
      }
    }
  });

  // Chevrons scroll-by a ~10-col chunk (320px is a sensible default).
  var lchev = document.querySelector('.rail-chev.left');
  var rchev = document.querySelector('.rail-chev.right');
  if (lchev) lchev.addEventListener('click', function () {
    scroll.scrollBy({left: -320, behavior: 'smooth'});
  });
  if (rchev) rchev.addEventListener('click', function () {
    scroll.scrollBy({left: 320, behavior: 'smooth'});
  });

  // Keyboard \u2190/\u2192 scroll the rail; Enter/Space opens drawer via click handler above.
  scroll.addEventListener('keydown', function (ev) {
    if (ev.key === 'ArrowRight') {
      ev.preventDefault();
      scroll.scrollBy({left: 160, behavior: 'smooth'});
    } else if (ev.key === 'ArrowLeft') {
      ev.preventDefault();
      scroll.scrollBy({left: -160, behavior: 'smooth'});
    }
  });

  // Wheel-to-horizontal: translate vertical wheel to horizontal scroll so users
  // can navigate without a horizontal trackpad gesture.
  scroll.addEventListener('wheel', function (ev) {
    if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
      scroll.scrollLeft += ev.deltaY;
      ev.preventDefault();
    }
  }, {passive: false});

  // Update counter + progress bar as user scrolls.
  function updateIndicator() {
    var max = scroll.scrollWidth - scroll.clientWidth;
    var t = max > 0 ? scroll.scrollLeft / max : 0;
    if (progress) progress.style.width = Math.max(2, t * 100) + '%';
    var firstCol = scroll.querySelector('.tcol');
    if (firstCol && counter) {
      var cw = firstCol.getBoundingClientRect().width + 4;
      var idx = Math.min(rows.length - 1,
        Math.max(0, Math.round(scroll.scrollLeft / Math.max(1, cw))));
      counter.textContent = String(rows[idx].n).padStart(2, '0');
    }
  }
  scroll.addEventListener('scroll', updateIndicator);
  updateIndicator();
})();
</script>"""


def _build_daily_cost_rail_html(daily_data: list) -> str:
    """Return a horizontally-scrollable daily-cost rail for the instance page.

    Each column is one calendar day; bar height is proportional to cost.
    Reuses ``.chartrail-card`` CSS layout; DOM IDs use the ``costail-``
    prefix so the element names don't clash with the per-session chartrail.

    Returns ``""`` if ``daily_data`` is empty.
    """
    if not daily_data:
        return ""
    rail_json = json.dumps(
        [{"n": i, "date": d.get("date", ""), "cost": float(d.get("cost", 0.0))}
         for i, d in enumerate(daily_data, 1)],
        separators=(",", ":"),
    ).replace("</", "<\\/")
    n_days = len(daily_data)
    return (
        '<section class="section">\n'
        '<div class="section-title"><h2>Daily cost timeline</h2>'
        '<span class="hint">one bar per calendar day &middot; '
        'scroll horizontally &middot; \u2190 \u2192</span></div>\n'
        '<div class="chartrail-card">\n'
        '  <div class="chartrail-wrap">\n'
        '    <div class="chartrail-yaxis" id="costail-yaxis"></div>\n'
        '    <div class="chartrail-scroll" id="costail-scroll" tabindex="0">\n'
        '      <div class="chartrail-inner" id="costail-inner"></div>\n'
        '    </div>\n'
        '    <button class="rail-chev left"  id="costail-prev" '
        'aria-label="Scroll days left">\u2039</button>\n'
        '    <button class="rail-chev right" id="costail-next" '
        'aria-label="Scroll days right">\u203a</button>\n'
        '  </div>\n'
        '  <div class="rail-indicator">\n'
        f'    <span><span id="costail-counter">01</span> / {n_days} days</span>\n'
        '    <div class="rail-progress">'
        '<div class="rail-progress-fill" id="costail-progress"></div></div>\n'
        '    <span>scroll or use \u2190 \u2192</span>\n'
        '  </div>\n'
        '</div>\n'
        '<script type="application/json" id="costail-data">'
        f'{rail_json}</script>\n'
        '</section>'
    )


def _daily_cost_rail_script() -> str:
    """Interaction JS for the daily-cost rail (costail).

    Renders one bar per calendar day whose height is proportional to cost.
    Y-axis ticks show dollar amounts.  Wires chevrons, keyboard, and wheel
    scroll — identical UX to the per-session chartrail.
    """
    return """<script>
(function () {
  var root = document.getElementById('costail-data');
  if (!root) return;
  var rows; try { rows = JSON.parse(root.textContent); } catch (e) { return; }
  var scroll   = document.getElementById('costail-scroll');
  var inner    = document.getElementById('costail-inner');
  var yaxis    = document.getElementById('costail-yaxis');
  var counter  = document.getElementById('costail-counter');
  var progress = document.getElementById('costail-progress');
  if (!scroll || !inner || !yaxis) return;

  var maxCost = 0;
  rows.forEach(function (r) { if (r.cost > maxCost) maxCost = r.cost; });
  if (!maxCost) maxCost = 1;

  // Y-axis: 5 dollar-amount ticks
  var yHtml = '';
  for (var i = 0; i <= 4; i++) {
    var v   = (maxCost / 4) * i;
    var lbl = v >= 100 ? '$' + Math.round(v)
            : v >= 1   ? '$' + v.toFixed(1)
            :            '$' + v.toFixed(2);
    var pct = 100 - (i / 4) * 100;
    yHtml += '<span class="tick" style="top:' + pct + '%">' + lbl + '</span>';
  }
  yaxis.innerHTML = yHtml;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  var parts = [];
  rows.forEach(function (r, i) {
    var pct   = (r.cost / maxCost) * 100;
    var label = esc(r.date) + ' \u00b7 $' + r.cost.toFixed(2);
    // Shorten date for column label: keep MM-DD portion
    var dateShort = String(r.date).slice(5);   // "YYYY-MM-DD" → "MM-DD"
    parts.push(
      '<div class="tcol" title="' + label + '">' +
      '<div class="tc-bar" aria-hidden="true">' +
      '<span class="seg cost" style="height:' + pct.toFixed(2) + '%"></span>' +
      '</div>' +
      '<div class="tc-foot">' +
      '<span class="tc-n">' + String(r.n).padStart(2,'0') + '</span>' +
      '<span class="tc-time">' + esc(dateShort) + '</span>' +
      '<span class="tc-cost">$' + r.cost.toFixed(2) + '</span>' +
      '</div></div>'
    );
  });
  inner.innerHTML = parts.join('');

  // Chevrons
  var lchev = document.getElementById('costail-prev');
  var rchev = document.getElementById('costail-next');
  if (lchev) lchev.addEventListener('click', function () {
    scroll.scrollBy({left: -320, behavior: 'smooth'});
  });
  if (rchev) rchev.addEventListener('click', function () {
    scroll.scrollBy({left: 320, behavior: 'smooth'});
  });

  // Keyboard ←/→
  scroll.addEventListener('keydown', function (ev) {
    if (ev.key === 'ArrowRight') {
      ev.preventDefault(); scroll.scrollBy({left: 160, behavior: 'smooth'});
    } else if (ev.key === 'ArrowLeft') {
      ev.preventDefault(); scroll.scrollBy({left: -160, behavior: 'smooth'});
    }
  });

  // Vertical wheel → horizontal scroll
  scroll.addEventListener('wheel', function (ev) {
    if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
      scroll.scrollLeft += ev.deltaY;
      ev.preventDefault();
    }
  }, {passive: false});

  function updateIndicator() {
    var max = scroll.scrollWidth - scroll.clientWidth;
    var t   = max > 0 ? scroll.scrollLeft / max : 0;
    if (progress) progress.style.width = Math.max(2, t * 100) + '%';
    var firstCol = scroll.querySelector('.tcol');
    if (firstCol && counter) {
      var cw  = firstCol.getBoundingClientRect().width + 4;
      var idx = Math.min(rows.length - 1,
        Math.max(0, Math.round(scroll.scrollLeft / Math.max(1, cw))));
      counter.textContent = String(rows[idx].n).padStart(2, '0');
    }
  }
  scroll.addEventListener('scroll', updateIndicator);
  updateIndicator();
})();
</script>"""


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
    if report.get("mode") == "instance":
        return _render_instance_html(report, chart_lib=chart_lib)
    include_insights = variant in ("single", "dashboard", "project")
    include_chart    = variant in ("single", "detail", "project")
    include_hc_chart = variant == "single"   # Highcharts 3D for single only; detail/project use chartrail
    slug = report["slug"]
    totals = report["totals"]
    mode = report["mode"]
    generated = _fmt_generated_at(report)
    sessions = report["sessions"]

    # ---- Chart data --------------------------------------------------------
    # Built only when the variant actually renders a chart — saves real work
    # (and, for the dashboard variant, drops the inline library JS bundle).
    # The renderer is selected via ``CHART_RENDERERS[chart_lib]``; each
    # returns ``(body_html, head_js)`` so the caller can place the JS in
    # ``<head>`` while the container div goes in the body.
    chart_html      = ""
    chart_head_html = ""
    if include_hc_chart:
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
    show_mode    = _has_fast(report)
    show_ttl     = _has_1h_cache(report)
    show_content = _has_content_blocks(report)

    # Total columns = #, Time, Model, [Mode], Input, Output, CacheRd, CacheWr,
    #                 [Content], Total, Cost
    _full_cols = 10 + (1 if show_mode else 0) + (1 if show_content else 0)
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

    def _content_cell(cb: dict) -> str:
        label = _fmt_content_cell(cb)
        title = _fmt_content_title(cb)
        if label == "-":
            return '<td class="content-blocks muted">&ndash;</td>'
        return (f'<td class="content-blocks" title="{title}">'
                f'<span>{label}</span></td>')

    def turn_row(t: dict, session_id: str) -> str:
        # Resume markers replace the normal data row with a full-width divider
        # so users see "session resumed here" inline with the timeline rather
        # than an all-zero row labelled `<synthetic>`. The marker is still
        # counted in the turn index; only the rendering changes.
        if t.get("is_resume_marker"):
            ts_fmt = html_mod.escape(t.get("timestamp_fmt", ""))
            is_terminal = t.get("is_terminal_exit_marker", False)
            # Terminal: this is the most recent /exit with no subsequent work
            # in the JSONL. The user may or may not have resumed yet — the
            # JSONL alone can't tell us. Resume: there is later work in the
            # file, so a return is observable.
            if is_terminal:
                pill_cls   = "resume-marker-pill terminal"
                icon_html  = "&#9211;"  # ⏻ power symbol
                label_text = "Session exited"
                tooltip    = ("Most recent /exit local command in this JSONL "
                              "with no subsequent assistant turn observed. "
                              "Whether the user has resumed since cannot be "
                              "determined from this file alone.")
            else:
                pill_cls   = "resume-marker-pill"
                icon_html  = "&#8634;"  # ↻ cycle
                label_text = "Session resumed"
                tooltip    = ("claude -c replayed a prior /exit local-command "
                              "into this session; CC emitted a no-op "
                              "`<synthetic>` assistant entry. Detection is "
                              "precise when it fires but may under-count "
                              "(resumes after Ctrl+C or crash leave no trace).")
            return (
                f'<tr class="resume-marker-row" data-session="{session_id[:8]}">'
                f'<td class="num resume-marker-idx">{t["index"]}</td>'
                f'<td colspan="{_full_cols - 1}" class="resume-marker-cell">'
                f'<span class="{pill_cls}" title="{tooltip}">'
                f'<span class="resume-marker-icon">{icon_html}</span>'
                f'<strong>{label_text}</strong>'
                f'<span class="resume-marker-time">at {ts_fmt}</span>'
                f'</span></td></tr>'
            )
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
        content_td = (_content_cell(t.get("content_blocks") or {})
                      if show_content else "")
        # data-turn-id is the key the drawer JS uses to pull this turn's
        # detail payload out of #turn-data. Namespaced by session_id[:8] so
        # project-mode reports with multiple sessions don't collide on the
        # per-session turn index.
        turn_key = f'{session_id[:8]}-{t["index"]}'
        return (
            f'<tr id="turn-{turn_key}" class="turn-row" data-session="{session_id[:8]}"'
            f' data-turn-id="{turn_key}" role="button" tabindex="0">'
            f'<td class="num">{t["index"]}</td>'
            f'<td class="ts">{t["timestamp_fmt"]}</td>'
            f'<td class="model">{html_mod.escape(t["model"])}</td>'
            f'{mode_td}'
            f'<td class="num">{t["input_tokens"]:,}</td>'
            f'<td class="num">{t["output_tokens"]:,}</td>'
            f'<td class="num">{t["cache_read_tokens"]:,}</td>'
            f'{cwr_td}'
            f'{content_td}'
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
        content_td = ('<td class="content-blocks muted">&nbsp;</td>'
                      if show_content else "")
        return (
            f'<tr class="subtotal">'
            f'<td colspan="{_label_span}"><strong>{label}</strong></td>'
            f'<td class="num"><strong>{st["input"]:,}</strong></td>'
            f'<td class="num"><strong>{st["output"]:,}</strong></td>'
            f'<td class="num"><strong>{st["cache_read"]:,}</strong></td>'
            f'{cwr_td}'
            f'{content_td}'
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

        def _model_row_html(m: str, cnt: int) -> str:
            r = _pricing_for(m)
            return (f'<tr><td><code>{html_mod.escape(m)}</code></td><td class="num">{cnt:,}</td>'
                    f'<td class="num">${r["input"]:.2f}</td>'
                    f'<td class="num">${r["output"]:.2f}</td>'
                    f'<td class="num">${r["cache_read"]:.2f}</td>'
                    f'<td class="num">${r["cache_write"]:.2f}</td></tr>')

        model_rows = "".join(
            _model_row_html(m, cnt)
            for m, cnt in sorted(report["models"].items(), key=lambda x: -x[1])
        )

    # Nav bar: cross-link to the companion page.
    # Switcher is embedded inside the topbar's <nav> to avoid positional overlap.
    # Split mode: brand left + [Dashboard | Detail | switcher] right.
    # Single mode: brand left + [switcher] right (no cross-link).
    _sw = _theme_picker_markup()
    if nav_sibling:
        label_here  = "Dashboard" if variant == "dashboard" else "Detail"
        label_other = "Detail"   if variant == "dashboard" else "Dashboard"
        nav_html = (
            f'<header class="topbar sm-nav">'
            f'<div class="brand"><span class="dot"></span>'
            f'<span>session-metrics</span></div>'
            f'<nav class="nav">'
            f'<span class="navlink current">{label_here}</span>'
            f'<a class="navlink" data-sm-nav href="{nav_sibling}">{label_other}</a>'
            f'{_sw}'
            f'</nav>'
            f'</header>'
        )
    else:
        nav_html = (
            f'<header class="topbar">'
            f'<div class="brand"><span class="dot"></span>'
            f'<span>session-metrics</span></div>'
            f'<nav class="nav">{_sw}</nav>'
            f'</header>'
        )

    chart_section_html = ""
    if include_hc_chart and chart_html:
        chart_section_html = (
            '<section class="section">\n'
            '<div class="section-title"><h2>Token Usage Over Time</h2></div>\n'
            f'{chart_html}\n'
            '</section>'
        )

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
        if show_content:
            legend_parts.append(
                '<b>Content</b> per-turn content blocks: '
                '<code>T</code> thinking, <code>u</code> tool_use, '
                '<code>x</code> text, <code>r</code> tool_result, '
                '<code>i</code> image (zero counts omitted) · '
            )
        legend_parts.extend([
            '<b>Total</b> sum of the four billable token buckets · ',
            '<b>Cost $</b> estimated USD for this turn.',
        ])
        legend_html = '<p class="legend-block">' + ''.join(legend_parts) + '</p>'
        content_th = ('<th class="content-blocks">Content</th>'
                      if show_content else "")
        table_section_html = (
            '<section class="section">\n'
            '<div class="section-title"><h2>Timeline</h2></div>\n'
            + legend_html + '\n'
            + '<table class="timeline-table">\n<thead><tr>\n'
            f'  <th class="num">#</th><th>Time ({tz_label})</th><th>Model</th>\n'
            f'  {"<th>Mode</th>" if show_mode else ""}\n'
            '  <th class="num">Input (new)</th><th class="num">Output</th>\n'
            '  <th class="num">CacheRd</th><th class="num">CacheWr</th>\n'
            f'  {content_th}\n'
            '  <th class="num">Total</th><th class="num">Cost $</th>\n'
            f'</tr></thead>\n<tbody>\n{"".join(table_rows)}\n</tbody>\n</table>\n'
            '</section>'
        )

    models_section_html = ""
    if include_chart and model_rows:
        models_section_html = (
            '<section class="section">\n'
            '<div class="section-title"><h2>Models</h2></div>\n'
            '<table class="models-table">\n'
            '<thead><tr><th>Model</th><th class="num">Turns</th>\n'
            '  <th class="num">$/M input</th><th class="num">$/M output</th>\n'
            '  <th class="num">$/M rd</th><th class="num">$/M wr</th></tr></thead>\n'
            f'<tbody>{model_rows}</tbody>\n</table>\n'
            '</section>'
        )

    summary_cards_html = ""
    if include_insights:
        ttl_mix_card = ""
        if totals.get("cache_write_1h", 0) > 0:
            pct_1h = 100 * totals["cache_write_1h"] / max(1, totals["cache_write"])
            extra = totals.get("extra_1h_cost", 0.0)
            ttl_mix_card = (
                f'\n  <div class="kpi cat-tokens" '
                f'title="1-hour cache writes cost 2× input vs 1.25× for the 5-minute tier. '
                f'This card shows the premium you paid for longer cache reuse.">'
                f'<div class="kpi-label">Cache TTL mix (extra paid for 1h)</div>'
                f'<div class="kpi-val">{pct_1h:.0f}% 1h · ${extra:.4f}</div></div>'
            )
        thinking_card = ""
        if totals.get("thinking_turn_count", 0) > 0:
            tn = totals["thinking_turn_count"]
            tp = totals.get("thinking_turn_pct", 0.0)
            blocks = (totals.get("content_blocks") or {}).get("thinking", 0)
            total_turns = totals.get("turns", 0)
            thinking_card = (
                f'\n  <div class="kpi" '
                f'title="Claude Code stores thinking blocks signature-only — '
                f'the count is real but per-block token counts aren\'t recoverable '
                f'from the transcript (thinking tokens are rolled into output_tokens).">'
                f'<div class="kpi-label">Extended thinking engagement '
                f'({tn} of {total_turns} turns)</div>'
                f'<div class="kpi-val">{tp:.0f}% · {blocks} blocks</div></div>'
            )
        tool_calls_card = ""
        if totals.get("tool_call_total", 0) > 0:
            tc = totals["tool_call_total"]
            avg = totals.get("tool_call_avg_per_turn", 0.0)
            top3 = totals.get("tool_names_top3") or []
            # Tool names originate from the JSONL and are attacker-controllable
            # in a compromised transcript — escape each before interpolating.
            top3_str = ", ".join(html_mod.escape(n) for n in top3) if top3 else "none"
            tool_calls_card = (
                f'\n  <div class="kpi">'
                f'<div class="kpi-label">Tool calls &middot; top: {top3_str}</div>'
                f'<div class="kpi-val">{tc} · {avg:.1f}/turn</div></div>'
            )
        resumes_card = ""
        resumes_list = report.get("resumes") or []
        if resumes_list:
            non_terminal = [r for r in resumes_list if not r.get("terminal")]
            n_resumes = len(non_terminal)
            # Collect short local times (HH:MM portion of timestamp_fmt)
            times = [r.get("timestamp_fmt", "").split(" ")[-1][:5]
                     for r in non_terminal if r.get("timestamp_fmt")]
            times_str = ", ".join(times) if times else ""
            terminal_note = ""
            n_terminal = len(resumes_list) - n_resumes
            if n_terminal:
                terminal_note = f' · {n_terminal} terminal exit'
                if n_terminal != 1:
                    terminal_note += "s"
            resumes_card = (
                f'\n  <div class="kpi">'
                f'<div class="kpi-label" title="Precise lower bound: detects claude -c '
                f'resumes that replay a prior /exit into this session. Resumes '
                f'after Ctrl+C or crash leave no trace and are not counted.">'
                f'Session resumes'
                f'{(" &middot; " + times_str) if times_str else ""}'
                f'{terminal_note}'
                f'</div>'
                f'<div class="kpi-val">&#8634; {n_resumes} detected</div></div>'
            )
        summary_cards_html = f'''\
<div class="kpi-grid">
  <div class="kpi featured cat-tokens"><div class="kpi-label">Total cost (USD)</div><div class="kpi-val">${totals['cost']:.4f}</div></div>
  <div class="kpi cat-save"><div class="kpi-label">Cache savings</div><div class="kpi-val">${totals['cache_savings']:.4f}</div></div>
  <div class="kpi"><div class="kpi-label">Cache hit ratio</div><div class="kpi-val">{totals['cache_hit_pct']:.1f}%</div></div>
  <div class="kpi cat-tokens"><div class="kpi-label">Total input tokens</div><div class="kpi-val">{totals['total_input']:,}</div></div>
  <div class="kpi cat-tokens"><div class="kpi-label">Input tokens (new)</div><div class="kpi-val">{totals['input']:,}</div></div>
  <div class="kpi cat-tokens"><div class="kpi-label">Output tokens</div><div class="kpi-val">{totals['output']:,}</div></div>
  <div class="kpi cat-tokens"><div class="kpi-label">Cache read tokens</div><div class="kpi-val">{totals['cache_read']:,}</div></div>
  <div class="kpi cat-tokens"><div class="kpi-label">Cache write tokens</div><div class="kpi-val">{totals['cache_write']:,}</div></div>{ttl_mix_card}{thinking_card}{tool_calls_card}{resumes_card}
</div>'''

    # Usage Insights panel — sits between the summary cards and the
    # weekly-rollup / time-of-day insight sections. Dashboard variant only;
    # rides the same `include_insights` gate as `summary_cards_html` above.
    usage_insights_html = (
        _build_usage_insights_html(report.get("usage_insights", []) or [])
        if include_insights else ""
    )

    # Phase-A (v1.6.0) sections — skill/subagent tables + cache-break events.
    # Dashboard/single only; detail page omits these (they already appear on dashboard).
    if include_insights:
        by_skill_html = _build_by_skill_html(report.get("by_skill", []) or [])
        by_subagent_type_html = _build_by_subagent_type_html(
            report.get("by_subagent_type", []) or [],
            subagents_included=bool(report.get("include_subagents", False)))
        cache_breaks_html = _build_cache_breaks_html(
            report.get("cache_breaks", []) or [],
            int(report.get("cache_break_threshold", _CACHE_BREAK_DEFAULT_THRESHOLD)),
        )
    else:
        by_skill_html = ""
        by_subagent_type_html = ""
        cache_breaks_html = ""

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

    # Per-turn drill-down: embed one JSON payload per page (keyed by
    # "<sid8>-<idx>"), render a right-side drawer + optional Prompts section,
    # and wire both Timeline rows and Prompts rows to the same open/close JS.
    # Skip resume-marker rows — the drawer doesn't open on them.
    turn_data_json_html  = ""
    turn_drawer_html     = ""
    prompts_section_html = ""
    drawer_script_html   = ""
    chartrail_section_html = ""
    if include_chart:
        turn_data: dict[str, dict] = {}
        prompts_rows: list[dict]   = []
        # Chart-rail data — one row per turn in document order.
        # Resume markers are rendered as a distinct column (.tcol.resume) rather
        # than a full stacked bar; they don't enter turn_data (no drawer).
        chartrail_data: list[dict] = []
        for s in sessions:
            sid8 = s["session_id"][:8]
            sess_label = f'{sid8} · {s.get("first_ts", "")}'
            first_in_session = True
            for t in s["turns"]:
                if t.get("is_resume_marker"):
                    chartrail_data.append({
                        "n":    t["index"],
                        "key":  "",
                        "ts":   t.get("timestamp_fmt", ""),
                        "time": (t.get("timestamp_fmt", "").split(" ")
                                  [-1][:5]),
                        "mdl":  "",
                        "inp":  0,
                        "out":  0,
                        "cr":   0,
                        "cw":   0,
                        "tot":  0,
                        "cost": 0.0,
                        "sid":  sid8,
                        "slbl": sess_label,
                        "sbrk": first_in_session,
                        "resm": True,
                        "term": bool(t.get("is_terminal_exit_marker")),
                    })
                    first_in_session = False
                    continue
                key = f'{sid8}-{t["index"]}'
                turn_data[key] = {
                    "idx":   t["index"],
                    "ts":    t.get("timestamp_fmt", ""),
                    "mdl":   t.get("model", ""),
                    "ps":    t.get("prompt_snippet", ""),
                    "pt":    _truncate(t.get("prompt_text", ""), _PROMPT_TEXT_CAP),
                    "sc":    t.get("slash_command", ""),
                    "tl":    t.get("tool_use_detail", []) or [],
                    "cb":    t.get("content_blocks") or {},
                    "cost":  t.get("cost_usd", 0.0),
                    "nc":    t.get("no_cache_cost_usd", 0.0),
                    "inp":   t.get("input_tokens", 0),
                    "out":   t.get("output_tokens", 0),
                    "cr":    t.get("cache_read_tokens", 0),
                    "cw":    t.get("cache_write_tokens", 0),
                    "cwt":   t.get("cache_write_ttl", ""),
                    "asnip": t.get("assistant_snippet", ""),
                    "atxt":  t.get("assistant_text", ""),
                }
                chartrail_data.append({
                    "n":    t["index"],
                    "key":  key,
                    "ts":   t.get("timestamp_fmt", ""),
                    "time": (t.get("timestamp_fmt", "").split(" ")
                              [-1][:5]),
                    "mdl":  t.get("model", ""),
                    "inp":  t.get("input_tokens", 0),
                    "out":  t.get("output_tokens", 0),
                    "cr":   t.get("cache_read_tokens", 0),
                    "cw":   t.get("cache_write_tokens", 0),
                    "tot":  t.get("total_tokens", 0),
                    "cost": t.get("cost_usd", 0.0),
                    "sid":  sid8,
                    "slbl": sess_label,
                    "sbrk": first_in_session,
                    "resm": False,
                    "term": False,
                })
                first_in_session = False
                if t.get("prompt_text"):
                    prompts_rows.append({
                        "key":    key,
                        "cost":   t.get("cost_usd", 0.0),
                        "idx":    t["index"],
                        "model":  t.get("model", ""),
                        "prompt": t.get("prompt_snippet", ""),
                        "tools":  [tu.get("name", "") for tu in
                                   (t.get("tool_use_detail") or [])],
                        "tokens": t.get("total_tokens", 0),
                        "slash":  t.get("slash_command", ""),
                        # Phase-B (v1.7.0): rolled-up subagent token/cost
                        # from this prompt's spawned chain. Zero on turns
                        # that didn't spawn or whose attribution is off.
                        "att_cost":   t.get("attributed_subagent_cost", 0.0),
                        "att_tokens": t.get("attributed_subagent_tokens", 0),
                        "att_count":  t.get("attributed_subagent_count", 0),
                    })
        # `</` sequences would close the surrounding <script> tag early.
        # Replace them with `<\/` (still valid JSON inside a string literal).
        payload_json = json.dumps(turn_data, separators=(",", ":"), default=str)
        payload_json = payload_json.replace("</", "<\\/")
        turn_data_json_html = (
            f'<script type="application/json" id="turn-data">{payload_json}</script>'
        )

        # Chart-rail: horizontally-scrollable column chart, one column per turn.
        # Rendered into #chartrail-inner by JS from the chartrail-data JSON blob.
        chartrail_section_html = _build_chartrail_section_html(chartrail_data)

        turn_drawer_html = '''<div class="drawer-backdrop" id="drawer-backdrop"></div>
<aside id="drawer" class="drawer" aria-hidden="true" role="dialog"
       aria-labelledby="drawer-title">
  <div class="drawer-head">
    <h3 id="drawer-title">Turn <span data-slot="idx"></span></h3>
    <button class="x" id="drawer-close" aria-label="Close">&times;</button>
  </div>
  <div class="drawer-body" id="drawer-body">
    <div class="drawer-sec">
      <h4>Meta</h4>
      <dl class="drawer-kv">
        <dt>Time</dt><dd data-slot="ts"></dd>
        <dt>Model</dt><dd><code data-slot="model"></code></dd>
        <dt data-slot="slash-wrap-dt" hidden>Slash</dt>
        <dd data-slot="slash-wrap" hidden><code data-slot="slash"></code></dd>
      </dl>
    </div>
    <div class="drawer-sec">
      <h4>Prompt</h4>
      <div data-slot="prompt-snippet" class="drawer-prompt"></div>
      <button class="drawer-more" data-state="collapsed" hidden>Show full prompt</button>
      <div data-slot="prompt-full" class="drawer-prompt" hidden></div>
    </div>
    <div class="drawer-sec" data-slot="tools-sec" hidden>
      <h4>Tools called (<span data-slot="tool-count"></span>)</h4>
      <ul data-slot="tools" class="drawer-tools-list"></ul>
    </div>
    <div class="drawer-sec">
      <h4>Content blocks</h4>
      <dl data-slot="content-dl" class="drawer-kv"></dl>
    </div>
    <div class="drawer-sec">
      <h4>Tokens</h4>
      <dl class="drawer-kv">
        <dt>Input (new)</dt><dd data-slot="tok-input"></dd>
        <dt>Output</dt><dd data-slot="tok-output"></dd>
        <dt>Cache read</dt><dd data-slot="tok-cache-read"></dd>
        <dt>Cache write</dt><dd data-slot="tok-cache-write"></dd>
      </dl>
    </div>
    <div class="drawer-sec">
      <h4>Cost</h4>
      <dl class="drawer-kv">
        <dt>Cost</dt><dd data-slot="cost"></dd>
      </dl>
      <p data-slot="cache-savings" class="drawer-savings" hidden></p>
    </div>
    <div class="drawer-sec" data-slot="assistant-sec" hidden>
      <h4>Assistant response</h4>
      <div data-slot="assistant-snippet" class="drawer-prompt"></div>
      <button class="drawer-more drawer-more-a" data-state="collapsed" hidden>Show full response</button>
      <div data-slot="assistant-full" class="drawer-prompt" hidden></div>
    </div>
  </div>
</aside>'''

        if prompts_rows:
            # Phase-B (v1.7.0): default sort is now ``self + attributed
            # subagent cost`` for HTML — surfaces cheap-prompt-spawning-
            # expensive-subagent turns. ``--sort-prompts-by self`` opts
            # back into pre-Phase-B parent-cost-only ordering. CSV/JSON
            # default to ``self`` separately so script consumers stay
            # stable.
            prompts_sort_mode = report.get("sort_prompts_by") or "total"
            if prompts_sort_mode == "self":
                prompts_rows.sort(key=lambda r: -r["cost"])
            else:
                prompts_rows.sort(
                    key=lambda r: -(r["cost"] + r.get("att_cost", 0.0)))
            top = prompts_rows[:20]
            # Hide the Subagents+$ column entirely when nothing in the
            # top-N actually has attribution — keeps the table tight on
            # sessions without subagent activity.
            show_att = any(r.get("att_count", 0) > 0 for r in top)
            rows_html: list[str] = []
            for r in top:
                tool_names = r["tools"]
                if tool_names:
                    tools_str = ", ".join(html_mod.escape(n)
                                          for n in tool_names[:3])
                    if len(tool_names) > 3:
                        tools_str += f" +{len(tool_names) - 3}"
                else:
                    tools_str = "&mdash;"
                slash_badge = ""
                if r.get("slash"):
                    slash_badge = (f' <span class="prompts-slash">'
                                   f'{html_mod.escape(r["slash"])}</span>')
                # Subagent annotation appended to the prompt cell when
                # the row has attributed cost — keeps the spawn signal
                # visible even when the dedicated column is hidden.
                sub_badge = ""
                if r.get("att_count", 0) > 0:
                    sub_badge = (
                        f' <span class="prompts-subagent" title="'
                        f'Includes ${r["att_cost"]:.4f} from {r["att_count"]} '
                        f'subagent turn(s) attributed to this prompt">'
                        f'+{r["att_count"]} subagent'
                        f'{"s" if r["att_count"] != 1 else ""}'
                        f'</span>'
                    )
                att_cell = (
                    f'<td class="num cost">${r["att_cost"]:.4f}</td>'
                    if show_att else ""
                )
                key_esc = html_mod.escape(r["key"])
                rows_html.append(
                    f'<tr data-turn="{key_esc}" tabindex="0">'
                    f'<td class="num">'
                    f'<a class="prompt-turn-link" href="#turn-{key_esc}">'
                    f'#{r["idx"]}</a></td>'
                    f'<td><div class="prompt-text truncate">'
                    f'{html_mod.escape(r["prompt"])}{slash_badge}{sub_badge}'
                    f'</div></td>'
                    f'<td class="cost">${r["cost"]:.4f}</td>'
                    f'{att_cell}'
                    f'<td class="model"><code>{html_mod.escape(r["model"])}</code></td>'
                    f'<td class="tools">{tools_str}</td>'
                    f'<td class="num">{r["tokens"]:,}</td>'
                    f'</tr>'
                )
            att_th = (
                '<th class="num" title="Subagent token cost rolled up '
                'onto this prompt (Phase-B attribution)">Subagents +$</th>'
                if show_att else ""
            )
            sort_hint = (
                "ranked by parent + attributed subagent cost"
                if prompts_sort_mode != "self"
                else "ranked by parent-turn cost only"
            )
            prompts_section_html = (
                '<section class="section">\n'
                '<div class="section-title"><h2>Prompts</h2>'
                f'<span class="hint">most-expensive user prompts in this report '
                f'&middot; {sort_hint} '
                f'&middot; click a row to open turn drawer</span></div>\n'
                '<div class="prompts">\n<table>\n<thead><tr>'
                '<th>Turn</th><th>Prompt</th><th class="num">Cost</th>'
                f'{att_th}'
                '<th>Model</th>'
                '<th>Tools</th><th class="num">Tokens</th></tr></thead>\n'
                f'<tbody>{"".join(rows_html)}</tbody></table>\n'
                '</div>\n</section>'
            )

        drawer_script_html = """<script>
(function () {
  var root = document.getElementById('turn-data');
  if (!root) return;
  var data; try { data = JSON.parse(root.textContent); } catch (e) { return; }
  var drawer   = document.getElementById('drawer');
  if (!drawer) return;
  var backdrop = document.getElementById('drawer-backdrop');
  var lastFocused = null;
  function sel(slot) { return drawer.querySelector('[data-slot="' + slot + '"]'); }
  function setText(slot, v) { var el = sel(slot); if (el) el.textContent = v == null ? '' : String(v); }
  function formatNum(n) { return typeof n === 'number' ? n.toLocaleString() : ''; }

  function openTurn(key) {
    var t = data[key]; if (!t) return;
    setText('idx', t.idx); setText('ts', t.ts); setText('model', t.mdl);
    var slashWrap = sel('slash-wrap');
    var slashWrapDt = sel('slash-wrap-dt');
    var slashEl = sel('slash');
    if (t.sc) {
      if (slashWrap) slashWrap.hidden = false;
      if (slashWrapDt) slashWrapDt.hidden = false;
      if (slashEl) slashEl.textContent = t.sc;
    } else {
      if (slashWrap) slashWrap.hidden = true;
      if (slashWrapDt) slashWrapDt.hidden = true;
    }

    var snip = t.ps || '(no prompt captured)';
    setText('prompt-snippet', snip);
    var full = sel('prompt-full'), moreBtn = drawer.querySelector('.drawer-more:not(.drawer-more-a)');
    if (t.pt && t.pt.length > (t.ps || '').length) {
      moreBtn.hidden = false; moreBtn.dataset.state = 'collapsed';
      moreBtn.textContent = 'Show full prompt';
      full.hidden = true; full.textContent = t.pt;
      sel('prompt-snippet').hidden = false;
    } else {
      moreBtn.hidden = true; full.hidden = true; full.textContent = '';
      sel('prompt-snippet').hidden = false;
    }

    var tools = t.tl || [];
    var toolsSect = sel('tools-sec');
    var toolsList = sel('tools');
    setText('tool-count', tools.length);
    toolsList.innerHTML = '';
    if (tools.length) {
      toolsSect.hidden = false;
      tools.forEach(function (tu) {
        var li = document.createElement('li');
        var nm = document.createElement('code'); nm.textContent = tu.name || '';
        li.appendChild(nm);
        if (tu.input_preview) {
          var pv = document.createElement('span');
          pv.className = 'drawer-tool-preview';
          pv.textContent = ' ' + tu.input_preview;
          li.appendChild(pv);
        }
        toolsList.appendChild(li);
      });
    } else { toolsSect.hidden = true; }

    var dl = sel('content-dl'); dl.innerHTML = '';
    var cb = t.cb || {};
    var labels = {thinking:'Thinking', tool_use:'Tool use', text:'Text',
                  tool_result:'Tool result', image:'Image'};
    Object.keys(labels).forEach(function (k) {
      var v = cb[k] || 0; if (!v) return;
      var dt = document.createElement('dt'); dt.textContent = labels[k];
      var dd = document.createElement('dd'); dd.textContent = v;
      dl.appendChild(dt); dl.appendChild(dd);
    });
    if (!dl.children.length) {
      var dt2 = document.createElement('dt'); dt2.textContent = 'No blocks';
      var dd2 = document.createElement('dd'); dd2.textContent = '\u2014';
      dl.appendChild(dt2); dl.appendChild(dd2);
    }

    setText('tok-input',       formatNum(t.inp));
    setText('tok-output',      formatNum(t.out));
    setText('tok-cache-read',  formatNum(t.cr));
    var cw = formatNum(t.cw);
    if (t.cwt) cw += '  (' + t.cwt + ')';
    setText('tok-cache-write', cw);
    setText('cost', '$' + (t.cost || 0).toFixed(4));
    var savings = (t.nc || 0) - (t.cost || 0);
    var sEl = sel('cache-savings');
    if (savings > 0) { sEl.textContent = 'Cache savings vs no-cache: $' + savings.toFixed(4); sEl.hidden = false; }
    else { sEl.textContent = ''; sEl.hidden = true; }

    var asstSect = sel('assistant-sec');
    var asstSnip = sel('assistant-snippet');
    var asstFull = sel('assistant-full');
    var asstMore = drawer.querySelector('.drawer-more-a');
    if (t.asnip) {
      asstSect.hidden = false;
      asstSnip.hidden = false;
      asstSnip.textContent = t.asnip;
      if (t.atxt && t.atxt.length > t.asnip.length) {
        asstMore.hidden = false; asstMore.dataset.state = 'collapsed';
        asstMore.textContent = 'Show full response';
        asstFull.hidden = true; asstFull.textContent = t.atxt;
      } else {
        asstMore.hidden = true; asstFull.hidden = true; asstFull.textContent = '';
      }
    } else { asstSect.hidden = true; }

    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    if (backdrop) backdrop.classList.add('open');
    lastFocused = document.activeElement;
    var closeBtn = document.getElementById('drawer-close');
    if (closeBtn) closeBtn.focus();

    // Sync highlight state on any clickable sources bound to this turn.
    document.querySelectorAll('tr.turn-row[data-turn-id]').forEach(function (tr) {
      tr.classList.toggle('active', tr.getAttribute('data-turn-id') === key);
    });
    document.querySelectorAll('.prompts tbody tr[data-turn]').forEach(function (tr) {
      tr.classList.toggle('active', tr.getAttribute('data-turn') === key);
    });
    document.querySelectorAll('.tcol[data-turn]').forEach(function (c) {
      c.classList.toggle('active', c.getAttribute('data-turn') === key);
    });
  }
  // Expose for other modules (chart-rail) to call.
  window.smOpenDrawer = openTurn;

  function closeDrawer() {
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    if (backdrop) backdrop.classList.remove('open');
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  }

  document.querySelectorAll('tr.turn-row[data-turn-id]').forEach(function (el) {
    el.addEventListener('click', function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest('a')) return;
      openTurn(el.getAttribute('data-turn-id'));
    });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openTurn(el.getAttribute('data-turn-id'));
      }
    });
  });

  document.querySelectorAll('.prompts tbody tr[data-turn]').forEach(function (el) {
    el.addEventListener('click', function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest('a')) return;
      openTurn(el.getAttribute('data-turn'));
    });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openTurn(el.getAttribute('data-turn'));
      }
    });
  });

  var closeBtnEl = document.getElementById('drawer-close');
  if (closeBtnEl) closeBtnEl.addEventListener('click', closeDrawer);
  if (backdrop) backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
  });

  var moreBtn2 = drawer.querySelector('.drawer-more:not(.drawer-more-a)');
  if (moreBtn2) moreBtn2.addEventListener('click', function () {
    var full = sel('prompt-full'), snippet = sel('prompt-snippet');
    if (moreBtn2.dataset.state === 'collapsed') {
      full.hidden = false; snippet.hidden = true;
      moreBtn2.textContent = 'Show snippet'; moreBtn2.dataset.state = 'expanded';
    } else {
      full.hidden = true; snippet.hidden = false;
      moreBtn2.textContent = 'Show full prompt'; moreBtn2.dataset.state = 'collapsed';
    }
  });
  var moreA2 = drawer.querySelector('.drawer-more-a');
  if (moreA2) moreA2.addEventListener('click', function () {
    var full = sel('assistant-full'), snippet = sel('assistant-snippet');
    if (moreA2.dataset.state === 'collapsed') {
      full.hidden = false; snippet.hidden = true;
      moreA2.textContent = 'Show snippet'; moreA2.dataset.state = 'expanded';
    } else {
      full.hidden = true; snippet.hidden = false;
      moreA2.textContent = 'Show full response'; moreA2.dataset.state = 'collapsed';
    }
  });
})();
</script>"""

    chartrail_script_html = _chartrail_script() if chartrail_section_html else ""

    title_suffix  = (" — Dashboard" if variant == "dashboard"
                     else " — Detail" if variant == "detail" else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="chart-lib" content="{chart_lib}">
<title>Session Metrics — {slug}{title_suffix}</title>
{chart_head_html}
{_theme_css()}
{_theme_bootstrap_head_js()}
</head>
<body class="theme-console">
<div class="shell">
{nav_html}
<header class="page-header">
  <h1>Session Metrics — {slug}{title_suffix}</h1>
  <p class="meta">Generated {generated} &nbsp;·&nbsp; Mode: {mode} &nbsp;·&nbsp;
  {len(sessions)} session{'s' if len(sessions) != 1 else ''}, {totals['turns']:,} turns</p>
</header>
{summary_cards_html}
{usage_insights_html}
{cache_breaks_html}
{by_skill_html}
{by_subagent_type_html}
{tod_html}
{chart_section_html}
{chartrail_section_html}
{table_section_html}
{prompts_section_html}
{models_section_html}
<footer class="foot">
  <span class="muted">session-metrics · {generated}</span>
</footer>
</div>
{toggle_script_html}
{turn_data_json_html}
{turn_drawer_html}
{drawer_script_html}
{chartrail_script_html}
{_theme_bootstrap_body_js()}
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
                   suffix: str = "",
                   explicit_ts: str | None = None) -> Path:
    """Write ``content`` to an export file; ``suffix`` is appended before
    the extension (e.g. ``"_dashboard"``, ``"_detail"``).

    ``explicit_ts`` overrides the default ``datetime.now(UTC)`` stamp in the
    filename. Used by ``_emit_compare_run_extras`` so a bundle of companion
    files (per-session dashboards + analysis.md) all share the same
    timestamp and the Markdown href links resolve.
    """
    out_dir = _export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = report["mode"]
    ts = explicit_ts or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if mode == "project":
        stem = f"project_{ts}"
    elif mode == "compare":
        a_sid = (report.get("side_a") or {}).get("session_id") or "a"
        b_sid = (report.get("side_b") or {}).get("session_id") or "b"
        stem = f"compare_{a_sid[:8]}_vs_{b_sid[:8]}_{ts}"
    else:
        sid = report["sessions"][0]["session_id"][:8]
        stem = f"session_{sid}_{ts}"
    path = out_dir / f"{stem}{suffix}.{_EXTENSIONS[fmt]}"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

# Pattern for resolving subagent type from its filename when a meta sidecar
# is missing. Matches the Anthropic session-report convention
# ``agent-a<label>-<hash>.jsonl`` — we peel off the label as the agent type.
_SUBAGENT_FILENAME_RE = re.compile(r"^agent-a([^-]+)-[0-9a-fA-F]+$")


def _resolve_subagent_type(sub_path: Path) -> str:
    """Three-tier fallback identical in spirit to Anthropic's session-report:
    (1) ``<stem>.meta.json`` → ``agentType`` field, (2) filename label via
    :data:`_SUBAGENT_FILENAME_RE`, (3) ``"fork"`` sentinel.
    """
    meta_path = sub_path.with_suffix(".meta.json")
    try:
        if meta_path.is_file():
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            agent_type = meta.get("agentType") if isinstance(meta, dict) else None
            if isinstance(agent_type, str) and agent_type:
                return agent_type
    except (OSError, json.JSONDecodeError):
        pass
    m = _SUBAGENT_FILENAME_RE.match(sub_path.stem)
    if m:
        return m.group(1)
    return "fork"


def _load_session(
    jsonl_path: Path, include_subagents: bool, use_cache: bool = True,
    seen_uuids: set[str] | None = None,
) -> tuple[str, list[dict], list[int]]:
    """Load a session JSONL and return structured data for report building.

    Parses the JSONL file, optionally merging subagent logs, then extracts
    both assistant turns (for token/cost tracking) and user timestamps (for
    time-of-day activity analysis).  User timestamps are extracted from the
    full entry list *before* assistant-only filtering discards them.

    ``seen_uuids`` is an opt-in cross-file dedup guard. When provided, any
    entry whose ``uuid`` field is already in the set is dropped; surviving
    entries are added. Callers supply a set shared across JSONLs they want
    to treat as one scope (project/instance); pass ``None`` to skip dedup
    (session scope — the in-file ``message.id`` dedup in ``_extract_turns``
    already handles streaming splits).

    Returns:
        3-tuple of (session_id, assistant_turns, user_epoch_secs) where
        session_id is the JSONL filename stem, assistant_turns is the
        deduplicated/sorted list of raw assistant entries, and
        user_epoch_secs is a sorted list of UTC epoch-seconds for every
        genuine user prompt (tool_results and meta entries excluded).
    """
    entries = list(_cached_parse_jsonl(jsonl_path, use_cache=use_cache))
    if include_subagents:
        subagent_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
        if subagent_dir.exists():
            for sub in sorted(subagent_dir.glob("*.jsonl")):
                agent_type = _resolve_subagent_type(sub)
                # Phase-B: filename stem sans ``agent-`` prefix is the
                # canonical agentId Claude Code uses to link a subagent
                # JSONL to the parent's ``toolUseResult.agentId``. Tag
                # every entry so ``_attribute_subagent_tokens`` can roll
                # tokens up onto the spawning prompt.
                agent_id = sub.stem
                if agent_id.startswith("agent-"):
                    agent_id = agent_id[len("agent-"):]
                sub_entries = _cached_parse_jsonl(sub, use_cache=use_cache)
                for e in sub_entries:
                    if isinstance(e, dict):
                        e["_subagent_type"] = agent_type
                        e["_subagent_agent_id"] = agent_id
                        entries.append(e)
    # Cross-file UUID dedup (opt-in). Anthropic's session-report uses this
    # to prevent resumed-session replays from double-counting across sibling
    # JSONLs. We do the same — but only when the caller provides the set
    # (scope = {session, project, instance}); otherwise the caller wants the
    # single-file-only ``message.id`` dedup handled by ``_extract_turns``.
    if seen_uuids is not None:
        filtered: list[dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            uid = e.get("uuid")
            if isinstance(uid, str) and uid:
                if uid in seen_uuids:
                    continue
                seen_uuids.add(uid)
            filtered.append(e)
        entries = filtered
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
                         chart_lib: str = "highcharts",
                         suppress_model_compare_insight: bool = False,
                         cache_break_threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD,
                         subagent_attribution: bool = True,
                         sort_prompts_by: str | None = None) -> None:
    print(f"Session : {jsonl_path.stem}", file=sys.stderr)
    print(f"File    : {jsonl_path}", file=sys.stderr)
    print(file=sys.stderr)

    # Single-session scope: ``message.id`` dedup in ``_extract_turns`` already
    # handles streaming splits for the one file being loaded, so we don't need
    # cross-file UUID dedup here. Pass ``None`` to disable.
    session_id, turns, user_ts = _load_session(jsonl_path, include_subagents,
                                                 use_cache=use_cache,
                                                 seen_uuids=None)
    if not turns:
        print("[info] No assistant turns with usage data found.", file=sys.stderr)
        return

    report = _build_report(
        "session", slug, [(session_id, turns, user_ts)],
        tz_offset_hours=tz_offset, tz_label=tz_label, peak=peak,
        suppress_model_compare_insight=suppress_model_compare_insight,
        cache_break_threshold=cache_break_threshold,
        subagent_attribution=subagent_attribution,
        sort_prompts_by=sort_prompts_by,
        include_subagents=include_subagents,
    )
    _dispatch(report, formats, single_page=single_page, chart_lib=chart_lib)


def _run_project_cost(slug: str, include_subagents: bool, formats: list[str],
                      tz_offset: float, tz_label: str,
                      peak: dict | None = None,
                      single_page: bool = False,
                      use_cache: bool = True,
                      chart_lib: str = "highcharts",
                      suppress_model_compare_insight: bool = False,
                      cache_break_threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD,
                      subagent_attribution: bool = True,
                      sort_prompts_by: str | None = None) -> None:
    files = _find_jsonl_files(slug)
    if not files:
        print(f"[error] No sessions found for slug: {slug}", file=sys.stderr)
        sys.exit(1)

    # Project scope: one shared ``seen_uuids`` across every JSONL in the
    # project so a resumed session replaying prior entries doesn't
    # double-count tokens in project totals (gap #8 fix).
    project_seen: set[str] = set()
    sessions_raw = []
    for path in reversed(files):   # oldest first
        sid, turns, user_ts = _load_session(path, include_subagents,
                                              use_cache=use_cache,
                                              seen_uuids=project_seen)
        if turns:
            sessions_raw.append((sid, turns, user_ts))

    if not sessions_raw:
        print("[info] No turns with usage data found across any session.", file=sys.stderr)
        return

    report = _build_report(
        "project", slug, sessions_raw,
        tz_offset_hours=tz_offset, tz_label=tz_label, peak=peak,
        suppress_model_compare_insight=suppress_model_compare_insight,
        cache_break_threshold=cache_break_threshold,
        subagent_attribution=subagent_attribution,
        sort_prompts_by=sort_prompts_by,
        include_subagents=include_subagents,
    )
    _dispatch(report, formats, single_page=single_page, chart_lib=chart_lib)


# === instance mode: begin ===================================================
# All code between this banner and the matching "end" banner is the
# --all-projects / instance-wide aggregation path. It layers on top of the
# existing session- and project-scope primitives (``_load_session``,
# ``_build_report``, ``render_html``, ``render_*``) without modifying them;
# the only non-local touchpoints are the CLI flags, the new "instance"
# branches inside renderers, and the dispatcher row in ``main()``.
#
# Keeping the new code in one contiguous block makes a future extraction
# to ``scripts/instance.py`` a copy-paste + import-fixup rather than an
# archaeology expedition. See the plan at
# /Users/george/.claude/plans/the-session-metrics-skill-stateful-stream.md
# ============================================================================


def _project_summary_from_report(project_report: dict) -> dict:
    """Condense a full ``_build_report(mode="project", ...)`` result into the
    lightweight summary that goes into ``instance_report["projects"]``.

    Per-turn records are dropped — they live inside the per-project
    drilldown HTML (rendered separately) so that the instance index stays
    small and the JSON/CSV exports are tractable.
    """
    slug = project_report["slug"]
    sessions = project_report["sessions"]
    totals = project_report["totals"]
    first_epoch = 0
    last_epoch = 0
    first_ts_fmt = ""
    last_ts_fmt = ""
    if sessions:
        first = sessions[0]
        last = sessions[-1]
        first_ts_fmt = first.get("first_ts", "")
        last_ts_fmt = last.get("last_ts", "")
        if first.get("turns"):
            first_epoch = _parse_iso_epoch(first["turns"][0].get("timestamp", ""))
        if last.get("turns"):
            last_epoch = _parse_iso_epoch(last["turns"][-1].get("timestamp", ""))
    session_summaries = []
    for s in sessions:
        session_summaries.append({
            "session_id":       s["session_id"],
            "first_ts":         s.get("first_ts", ""),
            "last_ts":          s.get("last_ts", ""),
            "duration_seconds": s.get("duration_seconds", 0),
            "turn_count":       len(s.get("turns", [])),
            "subtotal":         s.get("subtotal", {}),
            "models":           s.get("models", {}),
        })
    duration_seconds = 0
    if first_epoch and last_epoch and last_epoch > first_epoch:
        duration_seconds = last_epoch - first_epoch
    return {
        "slug":             slug,
        "friendly_path":    _slug_to_friendly_path(slug),
        "session_count":    len(sessions),
        "turn_count":       totals.get("turns", 0),
        "first_ts":         first_ts_fmt,
        "last_ts":          last_ts_fmt,
        "first_epoch":      first_epoch,
        "last_epoch":       last_epoch,
        "duration_seconds": duration_seconds,
        "totals":           totals,
        "models":           project_report.get("models", {}),
        "cost_usd":         float(totals.get("cost", 0.0)),
        "sessions":         session_summaries,
    }


def _build_instance_daily(project_reports: list[dict],
                          tz_offset_hours: float,
                          top_n: int = 10) -> tuple[list[dict], list[str]]:
    """Aggregate per-turn cost into daily buckets, attributed by project.

    Returns ``(daily, top_slugs)`` where ``daily`` is a list of
    ``{date, cost, tokens, input, output, cache_read, cache_write,
    by_project: {slug: cost_usd}}`` dicts sorted oldest-first, and
    ``top_slugs`` is the slug list that the instance chart stacks
    (all other projects are rolled into an "other" series by the renderer).

    The four per-token subcategories (``input`` / ``output`` / ``cache_read``
    / ``cache_write``) are tracked separately so the instance daily-cost
    chart can feed a real stacked-bar breakdown to the chart renderer,
    rather than flatlining those four series at 0 (the pre-v1.14.1 bug).
    """
    buckets: dict[str, dict] = {}
    project_cost: dict[str, float] = {}
    shift = timedelta(hours=tz_offset_hours)
    for pr in project_reports:
        slug = pr["slug"]
        for s in pr["sessions"]:
            for t in s.get("turns", []):
                ts = t.get("timestamp", "")
                dt = _parse_iso_dt(ts)
                if not dt:
                    continue
                local = (dt + shift).date().isoformat()
                cost = float(t.get("cost_usd", 0.0))
                tokens = int(t.get("total_tokens", 0))
                b = buckets.setdefault(local, {
                    "date": local, "cost": 0.0, "tokens": 0,
                    "input": 0, "output": 0,
                    "cache_read": 0, "cache_write": 0,
                    "by_project": {},
                })
                b["cost"] += cost
                b["tokens"] += tokens
                b["input"]       += int(t.get("input_tokens", 0) or 0)
                b["output"]      += int(t.get("output_tokens", 0) or 0)
                b["cache_read"]  += int(t.get("cache_read_tokens", 0) or 0)
                b["cache_write"] += int(t.get("cache_write_tokens", 0) or 0)
                b["by_project"][slug] = b["by_project"].get(slug, 0.0) + cost
                project_cost[slug] = project_cost.get(slug, 0.0) + cost
    daily = sorted(buckets.values(), key=lambda x: x["date"])
    top_slugs = [s for s, _ in sorted(project_cost.items(),
                                       key=lambda kv: kv[1], reverse=True)[:top_n]]
    return daily, top_slugs


def _aggregate_totals(project_reports: list[dict]) -> dict:
    """Sum per-project ``totals`` dicts into one instance-wide total."""
    keys = ["input", "output", "cache_read", "cache_write",
            "cache_write_5m", "cache_write_1h", "extra_1h_cost",
            "cost", "no_cache_cost", "turns"]
    out: dict = {k: 0 for k in keys}
    out["cost"] = 0.0
    out["no_cache_cost"] = 0.0
    out["extra_1h_cost"] = 0.0
    content_blocks = {"thinking": 0, "tool_use": 0, "text": 0,
                      "tool_result": 0, "image": 0}
    thinking_turn_count = 0
    name_counts: dict[str, int] = {}
    for pr in project_reports:
        t = pr.get("totals", {})
        for k in keys:
            out[k] = out.get(k, 0) + t.get(k, 0)
        cb = t.get("content_blocks") or {}
        for k, v in cb.items():
            content_blocks[k] = content_blocks.get(k, 0) + int(v or 0)
        thinking_turn_count += t.get("thinking_turn_count", 0)
        tun = t.get("tool_use_names") or {}
        for name, n in tun.items():
            name_counts[name] = name_counts.get(name, 0) + n
    if any(v for v in content_blocks.values()):
        out["content_blocks"] = content_blocks
    if thinking_turn_count:
        out["thinking_turn_count"] = thinking_turn_count
    if name_counts:
        out["tool_use_names"] = name_counts
    return out


def _aggregate_models(project_reports: list[dict]) -> dict:
    """Build a per-model breakdown across every project in the instance.

    Per-project ``models`` dicts produced by ``_build_report`` are simple
    ``{name: turn_count}`` maps (matches what the project-mode renderer
    expects). For the instance dashboard we want richer per-model stats
    (tokens + cost) so we walk each project's already-built turn records
    and accumulate the breakdown here. Pricing rates are attached via
    ``_pricing_for`` so the HTML models table can render rate columns
    without needing to re-run cost math.
    """
    merged: dict[str, dict] = {}
    for pr in project_reports:
        for s in pr.get("sessions", []):
            for t in s.get("turns", []):
                name = t.get("model", "unknown")
                m = merged.setdefault(name, {
                    "turns":              0,
                    "input_tokens":       0,
                    "output_tokens":      0,
                    "cache_read_tokens":  0,
                    "cache_write_tokens": 0,
                    "cache_write_5m_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "cost_usd":           0.0,
                })
                m["turns"]              += 1
                m["input_tokens"]       += int(t.get("input_tokens", 0))
                m["output_tokens"]      += int(t.get("output_tokens", 0))
                m["cache_read_tokens"]  += int(t.get("cache_read_tokens", 0))
                m["cache_write_tokens"] += int(t.get("cache_write_tokens", 0))
                m["cache_write_5m_tokens"] += int(t.get("cache_write_5m_tokens", 0))
                m["cache_write_1h_tokens"] += int(t.get("cache_write_1h_tokens", 0))
                m["cost_usd"]           += float(t.get("cost_usd", 0.0))
    return merged


def _merge_bucket_rows(project_reports: list[dict], key: str,
                        total_cost: float) -> list[dict]:
    """Merge per-project ``by_skill`` / ``by_subagent_type`` lists into a
    single instance-level list. Token counters and ``spawn_count`` /
    ``invocations`` / ``turns_attributed`` sum; ``session_count`` and the
    derived pct/cache-hit fields are recomputed from the sums.
    """
    merged: dict[str, dict] = {}
    session_accumulator: dict[str, set] = {}
    for pr in project_reports:
        for row in pr.get(key, []) or []:
            name = row.get("name", "")
            if not name:
                continue
            m = merged.setdefault(name, {
                "name": name,
                "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "total_tokens": 0, "cost_usd": 0.0,
                "turns_attributed": 0,
            })
            # Sum numeric counters generically.
            for field in ("input", "output", "cache_read", "cache_write",
                           "total_tokens", "turns_attributed", "invocations",
                           "spawn_count"):
                if field in row:
                    m[field] = m.get(field, 0) + int(row.get(field, 0) or 0)
            m["cost_usd"] = float(m.get("cost_usd", 0.0)) + float(row.get("cost_usd", 0.0))
            # Sessions are recomputed from summed session_count (best-effort
            # — we treat each project's session_count as independent because
            # project slugs partition session IDs).
            session_accumulator.setdefault(name, set())
            # Proxy: each project contributes at most session_count rows.
            sc = int(row.get("session_count", 0) or 0)
            if sc:
                # synth placeholder so len() matches total (deduped within
                # a project; across projects the union over namespaces is
                # the sum since slugs are disjoint).
                for i in range(sc):
                    session_accumulator[name].add(f"{pr.get('slug', '?')}::{i}")
    out: list[dict] = []
    for name, m in merged.items():
        m["session_count"] = len(session_accumulator.get(name, set()))
        total_input_side = (m["input"] + m["cache_read"] + m["cache_write"]) or 1
        m["cache_hit_pct"] = round(100.0 * m["cache_read"] / total_input_side, 1)
        m["pct_total_cost"] = (
            round(100.0 * m["cost_usd"] / total_cost, 2) if total_cost else 0.0
        )
        if "spawn_count" in m or key == "by_subagent_type":
            calls_for_avg = m.get("spawn_count", 0) or m.get("turns_attributed", 0) or 1
            m["avg_tokens_per_call"] = round(m.get("total_tokens", 0) / calls_for_avg, 1)
        out.append(m)
    out.sort(key=lambda r: -(r.get("cost_usd", 0.0) or r.get("total_tokens", 0) or r.get("spawn_count", 0)))
    return out


def _aggregate_attribution_summary(project_reports: list[dict]) -> dict:
    """Sum per-project Phase-B attribution summaries (counts add; nested
    depth maxes). Stable shape across modes so renderers don't branch."""
    out = {
        "attributed_turns":      0,
        "orphan_subagent_turns": 0,
        "nested_levels_seen":    0,
        "cycles_detected":       0,
    }
    for pr in project_reports:
        s = pr.get("subagent_attribution_summary") or {}
        for k in out:
            v = int(s.get(k, 0) or 0)
            if k == "nested_levels_seen":
                out[k] = max(out[k], v)
            else:
                out[k] += v
    return out


def _build_instance_report(
        project_reports: list[dict],
        all_sessions_raw: list[tuple[str, list[dict], list[int]]],
        tz_offset_hours: float,
        tz_label: str,
        projects_dir: Path,
        peak: dict | None = None,
        cache_break_threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD) -> dict:
    """Assemble the instance-wide report from per-project reports.

    Strategy: reuse ``_build_report(mode="project")`` for each project (done
    by the caller) to get full turn records, then flatten everything into a
    single virtual "project" to feed ``_build_time_of_day``,
    ``_build_weekly_rollup``, ``_build_session_blocks`` — they already work
    on lists of sessions, so we get identical rendering behaviour for free.
    Finally we strip per-turn payloads from the top-level ``projects`` list
    to keep the in-memory / JSON / CSV output bounded.

    ``all_sessions_raw`` is the concatenation of the ``sessions_raw`` tuples
    loaded per-project — shape ``(session_id, raw_turns, user_ts)``, same
    as what ``_build_report`` consumes. We need the raw JSONL entries (not
    the post-processed turn records) because ``_build_session_blocks``
    reaches into each raw turn's ``message.usage`` for token tallies.
    """
    # Collect per-project summaries (no turns)
    projects: list[dict] = []
    for pr in project_reports:
        projects.append(_project_summary_from_report(pr))
    # Sort by cost descending (matches plan: highest-spend first)
    projects.sort(key=lambda p: p["cost_usd"], reverse=True)

    # Flatten post-processed sessions for _build_weekly_rollup (it reads
    # per-session summary data, not raw entries, so the ``sessions`` lists
    # already produced by _build_report are the right input here).
    all_sessions_out = []
    for pr in project_reports:
        for s in pr["sessions"]:
            all_sessions_out.append(s)

    # Collect user-prompt timestamps across all projects so the instance
    # time_of_day / hour-of-day / punchcard charts reflect actual user
    # activity, not just assistant turns.
    all_user_ts: list[int] = sorted(
        ts for _, _, uts in all_sessions_raw for ts in uts
    )

    blocks = _build_session_blocks(all_sessions_raw)
    totals = _aggregate_totals(project_reports)
    models = _aggregate_models(project_reports)
    # Re-price models with a rates key if missing, using _pricing_for
    for model, info in models.items():
        if "rates" not in info:
            info["rates"] = _pricing_for(model)

    daily, top_slugs = _build_instance_daily(project_reports,
                                              tz_offset_hours=tz_offset_hours)

    total_cost_for_pct = float(totals.get("cost", 0.0))
    # Aggregated phase-A tables across projects.
    inst_by_skill = _merge_bucket_rows(project_reports, "by_skill",
                                         total_cost_for_pct)
    inst_by_subagent = _merge_bucket_rows(project_reports, "by_subagent_type",
                                            total_cost_for_pct)
    inst_cache_breaks: list[dict] = []
    for pr in project_reports:
        pr_slug = pr.get("slug", "")
        for cb in pr.get("cache_breaks", []) or []:
            tagged = dict(cb)
            tagged["project"] = pr_slug
            inst_cache_breaks.append(tagged)
    inst_cache_breaks.sort(key=lambda b: -int(b.get("uncached", 0)))

    report = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "mode":             "instance",
        "slug":             "all-projects",
        "projects_dir":     str(projects_dir),
        "tz_offset_hours":  tz_offset_hours,
        "tz_label":         tz_label,
        "projects":         projects,
        "project_count":    len(projects),
        "session_count":    sum(p["session_count"] for p in projects),
        "totals":           totals,
        "models":           models,
        "time_of_day":      _build_time_of_day(all_user_ts,
                                                offset_hours=tz_offset_hours),
        "session_blocks":   blocks,
        "block_summary":    _weekly_block_counts(blocks),
        "weekly_rollup":    _build_weekly_rollup(all_sessions_out,
                                                  all_sessions_raw,
                                                  blocks),
        "peak":             peak,
        "daily":            daily,
        "top_project_slugs": top_slugs,
        "cache_breaks":        inst_cache_breaks,
        "by_skill":            inst_by_skill,
        "by_subagent_type":    inst_by_subagent,
        "cache_break_threshold": cache_break_threshold,
        # Phase-B (v1.7.0): instance-wide attribution summary — sum
        # per-project counts; max nested depth observed across all
        # projects. Each project's per-turn ``attributed_subagent_*``
        # already lives on the per-project sessions/turns and renders
        # via the project drilldown — no instance-level aggregation
        # needed beyond the summary footer.
        "subagent_attribution_summary": _aggregate_attribution_summary(project_reports),
        # Placeholders so the existing renderers don't KeyError if they
        # reach into the report looking for these.
        "sessions":         [],
        "resumes":          [],
        "usage_insights":   [],
    }
    return report


def _run_all_projects(formats: list[str],
                      tz_offset: float, tz_label: str,
                      peak: dict | None = None,
                      single_page: bool = False,
                      use_cache: bool = True,
                      chart_lib: str = "highcharts",
                      include_subagents: bool = False,
                      drilldown: bool = True,
                      suppress_model_compare_insight: bool = False,
                      cache_break_threshold: int = _CACHE_BREAK_DEFAULT_THRESHOLD,
                      subagent_attribution: bool = True,
                      sort_prompts_by: str | None = None) -> None:
    projects_dir = _projects_dir()
    print(f"Scanning: {projects_dir}", file=sys.stderr)
    discovered = _list_all_projects()
    if not discovered:
        print(f"[error] No projects with session JSONLs found under {projects_dir}",
              file=sys.stderr)
        sys.exit(1)
    print(f"Found   : {len(discovered)} project(s)", file=sys.stderr)
    print(f"TZ      : {tz_label} (UTC{'+' if tz_offset >= 0 else '-'}{abs(tz_offset):g})",
          file=sys.stderr)
    print(file=sys.stderr)

    project_reports: list[dict] = []
    # Instance-scope UUID dedup: one set spans every JSONL across every
    # project so a session that was resumed (replaying prior UUIDs into a
    # new file) can't double-count in instance totals. Loaded entries add
    # their UUIDs; subsequent files skip anything already present.
    instance_seen: set[str] = set()
    # Raw sessions_raw tuples across every project — preserved so the
    # instance-scope insights (_build_session_blocks, _build_weekly_rollup)
    # see the same raw JSONL shape they do in project mode. Without this
    # the post-processed turn records lack the ``message.usage`` subtree
    # that session_blocks reads for token tallies.
    all_sessions_raw: list[tuple[str, list[dict], list[int]]] = []
    for i, (slug, project_dir) in enumerate(discovered, 1):
        jsonls = sorted(
            [p for p in project_dir.glob("*.jsonl") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
        )  # oldest first
        sessions_raw = []
        for path in jsonls:
            try:
                sid, turns, user_ts = _load_session(path, include_subagents,
                                                     use_cache=use_cache,
                                                     seen_uuids=instance_seen)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[warn] {slug}: skipping {path.name} ({exc})",
                      file=sys.stderr)
                continue
            if turns:
                sessions_raw.append((sid, turns, user_ts))
        if not sessions_raw:
            print(f"[skip] {slug}: no usable turns", file=sys.stderr)
            continue
        print(f"[{i}/{len(discovered)}] Loading {slug} "
              f"({len(sessions_raw)} session(s))...", file=sys.stderr)
        pr = _build_report(
            "project", slug, sessions_raw,
            tz_offset_hours=tz_offset, tz_label=tz_label, peak=peak,
            suppress_model_compare_insight=True,  # per-project, suppress noise
            cache_break_threshold=cache_break_threshold,
            subagent_attribution=subagent_attribution,
            sort_prompts_by=sort_prompts_by,
            include_subagents=include_subagents,
        )
        project_reports.append(pr)
        all_sessions_raw.extend(sessions_raw)

    if not project_reports:
        print("[info] No projects yielded usable turns.", file=sys.stderr)
        return

    instance_report = _build_instance_report(
        project_reports,
        all_sessions_raw,
        tz_offset_hours=tz_offset,
        tz_label=tz_label,
        projects_dir=projects_dir,
        peak=peak,
        cache_break_threshold=cache_break_threshold,
    )
    instance_report["_suppress_model_compare_insight"] = \
        suppress_model_compare_insight

    # ``single_page`` is accepted from the CLI but has no effect at instance
    # scope: the instance ``index.html`` is always a single page by design,
    # and the per-project drilldown HTMLs are always emitted as single-page
    # variants. The argument is kept for CLI symmetry only.
    _ = single_page
    _dispatch_instance(instance_report, project_reports, formats,
                        chart_lib=chart_lib,
                        drilldown=drilldown)


def _instance_export_root(now: datetime | None = None) -> Path:
    """Dated subfolder under ``exports/session-metrics/instance/`` for one run."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    return _export_dir() / "instance" / stamp


def _dispatch_instance(instance_report: dict,
                        project_reports: list[dict],
                        formats: list[str],
                        chart_lib: str = "highcharts",
                        drilldown: bool = True) -> None:
    """Write all instance exports (and, optionally, per-project drilldown
    HTMLs) into a dated subfolder so successive runs don't overwrite each
    other. The instance ``index.html`` uses relative ``projects/<slug>.html``
    hrefs so the folder is portable (zip, move, serve as static files).
    """
    # Always print text to stdout
    print(render_text(instance_report))

    root = _instance_export_root()
    root.mkdir(parents=True, exist_ok=True)

    # Note which project slugs will have a drilldown so the HTML renderer
    # knows which rows to hyperlink vs render as plain text.
    drilldown_slugs: set[str] = set()
    if drilldown:
        projects_sub = root / "projects"
        projects_sub.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, Path]] = []
    for fmt in formats or []:
        if fmt == "text":
            continue
        if fmt == "html":
            instance_report_for_html = dict(instance_report)
            instance_report_for_html["_drilldown_slugs"] = \
                {pr["slug"] for pr in project_reports} if drilldown else set()
            content = render_html(instance_report_for_html, variant="single",
                                   chart_lib=chart_lib)
        else:
            content = _RENDERERS[fmt](instance_report)
        out = root / f"index.{_EXTENSIONS[fmt]}"
        out.write_text(content, encoding="utf-8")
        written.append((fmt, out))
        print(f"[export] {fmt.upper():4} → {out}", file=sys.stderr)

    if drilldown:
        total = len(project_reports)
        for i, pr in enumerate(project_reports, 1):
            slug = pr["slug"]
            print(f"[{i}/{total}] Rendering drilldown: {slug}...",
                  file=sys.stderr)
            try:
                dash_html = render_html(pr, variant="dashboard",
                                         nav_sibling=f"{slug}_detail.html",
                                         chart_lib=chart_lib)
                det_html  = render_html(pr, variant="detail",
                                         nav_sibling=f"{slug}_dashboard.html",
                                         chart_lib=chart_lib)
            except (ValueError, KeyError, RuntimeError) as exc:
                print(f"[warn] {slug}: HTML render failed ({exc})",
                      file=sys.stderr)
                continue
            (root / "projects" / f"{slug}_dashboard.html").write_text(
                dash_html, encoding="utf-8")
            (root / "projects" / f"{slug}_detail.html").write_text(
                det_html, encoding="utf-8")
            drilldown_slugs.add(slug)
        print(f"[export] per-project drilldowns → {root / 'projects'}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Instance-mode renderers
# ---------------------------------------------------------------------------
#
# Each of the five renderers dispatches into one of these from the
# existing ``render_text`` / ``render_json`` / ``render_csv`` / ``render_md``
# / ``render_html`` entry points when ``report["mode"] == "instance"``. None
# of the session- or project-scope codepaths are touched.

def _render_instance_text(report: dict) -> str:
    """Terse ASCII summary for stdout: header cards, top 10 projects by
    cost, aggregated models, date range. Always emitted to stdout by the
    instance dispatcher (mirrors ``render_text`` for the other modes)."""
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    totals = report.get("totals", {})
    projects = report.get("projects", [])
    models = report.get("models", {})
    tz_label = report.get("tz_label", "UTC")
    generated = _fmt_generated_at(report)

    p("=" * 78)
    p(f"  Claude Code — all-projects instance dashboard")
    p("=" * 78)
    p(f"  Generated : {generated}")
    p(f"  Scanning  : {report.get('projects_dir', '?')}")
    p(f"  Timezone  : {tz_label}")
    p(f"  Projects  : {report.get('project_count', 0)}")
    p(f"  Sessions  : {report.get('session_count', 0)}")
    p(f"  Turns     : {totals.get('turns', 0):,}")
    p(f"  Cost (USD): ${float(totals.get('cost', 0.0)):.4f}")
    p(f"  Input     : {totals.get('input', 0):,} new / "
      f"{totals.get('cache_read', 0):,} cache_read")
    p(f"  Output    : {totals.get('output', 0):,}")
    p(f"  Cache wr  : {totals.get('cache_write', 0):,} "
      f"(5m {totals.get('cache_write_5m', 0):,}, "
      f"1h {totals.get('cache_write_1h', 0):,})")
    p("")
    if projects:
        p(f"Top projects by cost (showing up to 10 of {len(projects)}):")
        p(f"  {'#':>2}  {'Slug':<42}  {'Sessions':>8}  {'Turns':>6}  {'Cost $':>10}")
        p(f"  " + "-" * 74)
        for i, proj in enumerate(projects[:10], 1):
            slug = proj["slug"]
            if len(slug) > 42:
                slug = slug[:39] + "..."
            p(f"  {i:>2}  {slug:<42}  "
              f"{proj.get('session_count', 0):>8}  "
              f"{proj.get('turn_count', 0):>6}  "
              f"${proj.get('cost_usd', 0.0):>9.4f}")
        p("")
    if models:
        p("Models used (aggregated):")
        for name, info in sorted(models.items(),
                                  key=lambda kv: -int(kv[1].get("turns", 0))):
            turns = info.get("turns", 0)
            cost = float(info.get("cost_usd", 0.0))
            p(f"  {name:<44}  {turns:>6} turns  ${cost:>9.4f}")
        p("")
    return out.getvalue()


def _render_instance_json(report: dict) -> str:
    """Serialise the full instance report as indented JSON.

    Per-turn records are never retained at instance scope so the JSON
    stays bounded even for users with hundreds of sessions — only
    per-session summaries, per-project summaries, and cross-project
    aggregates appear.
    """
    export = {k: v for k, v in report.items()
              if not k.startswith("_")}  # drop transient _drilldown_slugs etc.
    # Convert time_of_day epoch lists to human-readable timestamps
    if "time_of_day" in export:
        export["time_of_day"] = _tod_for_json(export["time_of_day"])
    return json.dumps(export, indent=2, default=str)


def _render_instance_csv(report: dict) -> str:
    """One row per session across all projects, with a ``project_slug``
    column. Per-turn rows would explode at instance scale; per-session
    rows give a CSV that's pivotable in Excel without being unwieldy."""
    out = io.StringIO()
    w = csv_mod.writer(out)
    w.writerow([
        "project_slug", "session_id", "first_ts", "last_ts",
        "duration_seconds", "turn_count",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
        "total_tokens", "cost_usd",
    ])
    for proj in report.get("projects", []):
        slug = proj["slug"]
        for s in proj.get("sessions", []):
            st = s.get("subtotal", {}) or {}
            w.writerow([
                slug, s.get("session_id", ""),
                s.get("first_ts", ""), s.get("last_ts", ""),
                s.get("duration_seconds", 0),
                s.get("turn_count", 0),
                st.get("input", 0), st.get("output", 0),
                st.get("cache_read", 0), st.get("cache_write", 0),
                st.get("cache_write_5m", 0), st.get("cache_write_1h", 0),
                st.get("total", 0),
                f"{float(st.get('cost', 0.0)):.6f}",
            ])

    # Instance-level summary row and projects-breakdown section
    totals = report.get("totals", {}) or {}
    w.writerow([])
    w.writerow(["# INSTANCE TOTALS"])
    w.writerow(["project_count", "session_count", "turn_count",
                 "input", "output", "cache_read", "cache_write",
                 "cost_usd"])
    w.writerow([
        report.get("project_count", 0),
        report.get("session_count", 0),
        totals.get("turns", 0),
        totals.get("input", 0), totals.get("output", 0),
        totals.get("cache_read", 0), totals.get("cache_write", 0),
        f"{float(totals.get('cost', 0.0)):.6f}",
    ])
    w.writerow([])
    w.writerow(["# PROJECTS BREAKDOWN (sorted by cost desc)"])
    w.writerow(["project_slug", "friendly_path", "sessions",
                 "turns", "first_ts", "last_ts", "cost_usd"])
    for proj in report.get("projects", []):
        w.writerow([
            proj["slug"],
            proj.get("friendly_path", ""),
            proj.get("session_count", 0),
            proj.get("turn_count", 0),
            proj.get("first_ts", ""),
            proj.get("last_ts", ""),
            f"{float(proj.get('cost_usd', 0.0)):.6f}",
        ])
    return out.getvalue()


def _render_instance_md(report: dict) -> str:
    """GitHub-flavored Markdown for instance scope: summary cards, projects
    breakdown, aggregated models, weekly/hour-of-day sections."""
    out = io.StringIO()

    def p(*args, **kw):
        print(*args, **kw, file=out)

    totals = report.get("totals", {})
    projects = report.get("projects", [])
    models = report.get("models", {})
    tz_label = report.get("tz_label", "UTC")
    generated = _fmt_generated_at(report)

    p(f"# Session Metrics — all projects")
    p()
    p(f"Generated: {generated}  |  Mode: instance  |  "
      f"Scanning: `{report.get('projects_dir', '?')}`")
    p()

    # Summary cards
    p("## Summary")
    p()
    p("| Metric | Value |")
    p("|--------|-------|")
    p(f"| Projects | {report.get('project_count', 0)} |")
    p(f"| Sessions | {report.get('session_count', 0)} |")
    p(f"| Total turns | {totals.get('turns', 0):,} |")
    p(f"| Total cost | ${float(totals.get('cost', 0.0)):.4f} |")
    p(f"| Input tokens (new) | {totals.get('input', 0):,} |")
    p(f"| Output tokens | {totals.get('output', 0):,} |")
    p(f"| Cache read tokens | {totals.get('cache_read', 0):,} |")
    p(f"| Cache write tokens | {totals.get('cache_write', 0):,} |")
    if totals.get("cache_write_1h", 0) > 0:
        pct_1h = 100 * totals["cache_write_1h"] / max(1, totals["cache_write"])
        p(f"| Cache TTL mix (1h share of writes) | {pct_1h:.1f}% |")
    if projects:
        top = projects[0]
        p(f"| Top project by cost | `{top['slug']}` "
          f"(${top.get('cost_usd', 0.0):.4f}) |")
    if models:
        top_model = max(models.items(),
                        key=lambda kv: float(kv[1].get("cost_usd", 0.0)))[0]
        p(f"| Top model by cost | `{top_model}` |")
    p()

    # Projects breakdown — sorted by cost desc (already sorted by builder)
    p("## Projects breakdown")
    p()
    p(f"| # | Project | Friendly path | Sessions | Turns | "
      f"First | Last | Cost $ |")
    p("|--:|---------|---------------|---------:|------:|"
      "-------|------|-------:|")
    for i, proj in enumerate(projects, 1):
        p(f"| {i} | `{proj['slug']}` | `{proj.get('friendly_path', '')}` "
          f"| {proj.get('session_count', 0):,} "
          f"| {proj.get('turn_count', 0):,} "
          f"| {proj.get('first_ts', '')} | {proj.get('last_ts', '')} "
          f"| ${proj.get('cost_usd', 0.0):.4f} |")
    p()

    # Models table (aggregated)
    if models:
        p("## Models (aggregated)")
        p()
        p("| Model | Turns | Input | Output | CacheRd | CacheWr | Cost $ |")
        p("|-------|------:|------:|-------:|--------:|--------:|-------:|")
        for name, info in sorted(models.items(),
                                  key=lambda kv: -float(kv[1].get("cost_usd", 0.0))):
            p(f"| `{name}` | {int(info.get('turns', 0)):,} "
              f"| {int(info.get('input_tokens', 0)):,} "
              f"| {int(info.get('output_tokens', 0)):,} "
              f"| {int(info.get('cache_read_tokens', 0)):,} "
              f"| {int(info.get('cache_write_tokens', 0)):,} "
              f"| ${float(info.get('cost_usd', 0.0)):.4f} |")
        p()

    # Time-of-day (aggregated)
    tod = report.get("time_of_day", {})
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

    # 5-hour session blocks (aggregated)
    blocks = report.get("session_blocks", []) or []
    summary = report.get("block_summary", {}) or {}
    if blocks:
        p(f"## 5-hour session blocks (aggregated, {tz_label})")
        p()
        p(f"- Trailing 7 days: **{summary.get('trailing_7', 0)}** blocks")
        p(f"- Trailing 14 days: **{summary.get('trailing_14', 0)}** blocks")
        p(f"- Trailing 30 days: **{summary.get('trailing_30', 0)}** blocks")
        p(f"- All time: **{summary.get('total', len(blocks))}** blocks")
        p()

    # Per-project sub-sections with per-session subtotals
    p("## Per-project session subtotals")
    p()
    for proj in projects:
        p(f"### `{proj['slug']}`")
        p()
        p(f"`{proj.get('friendly_path', '')}` &nbsp;·&nbsp; "
          f"{proj.get('session_count', 0)} sessions &nbsp;·&nbsp; "
          f"{proj.get('turn_count', 0):,} turns &nbsp;·&nbsp; "
          f"**${proj.get('cost_usd', 0.0):.4f}**")
        p()
        sessions = proj.get("sessions", [])
        if sessions:
            p("| # | Session | First | Last | Turns | Input | Output | "
              "CacheRd | CacheWr | Cost $ |")
            p("|--:|---------|-------|------|------:|------:|-------:|"
              "--------:|--------:|-------:|")
            for i, s in enumerate(sessions, 1):
                st = s.get("subtotal", {}) or {}
                p(f"| {i} | `{s.get('session_id', '')[:8]}…` "
                  f"| {s.get('first_ts', '')} | {s.get('last_ts', '')} "
                  f"| {s.get('turn_count', 0):,} "
                  f"| {int(st.get('input', 0)):,} "
                  f"| {int(st.get('output', 0)):,} "
                  f"| {int(st.get('cache_read', 0)):,} "
                  f"| {int(st.get('cache_write', 0)):,} "
                  f"| ${float(st.get('cost', 0.0)):.4f} |")
            p()
    return out.getvalue()


def _render_instance_html(report: dict, chart_lib: str = "highcharts") -> str:
    """Full instance dashboard HTML.

    Reuses the same visual language (dark theme, cards, tables) as the
    session/project renderer but:
      - suppresses the per-turn drawer CSS/JS (no per-turn data at this
        scope — users drill down into ``projects/<slug>.html`` for that)
      - replaces the timeline-of-turns chart with a **daily cost**
        timeline stacked by the top 10 projects (via the existing chart
        renderers, whose contract is a list of turn-ish dicts)
      - replaces the session timeline table with a **projects breakdown**
        table sorted by cost descending; each row links to the
        corresponding drilldown HTML when present in ``_drilldown_slugs``
    """
    totals = report.get("totals", {}) or {}
    projects = report.get("projects", []) or []
    models = report.get("models", {}) or {}
    tz_label = report.get("tz_label", "UTC")
    tz_offset = report.get("tz_offset_hours", 0.0)
    generated = _fmt_generated_at(report)
    projects_dir = html_mod.escape(str(report.get("projects_dir", "?")))
    drilldown_slugs = report.get("_drilldown_slugs") or set()

    # ---- Chart: synthesise turn-ish dicts from per-day buckets -------------
    # The existing CHART_RENDERERS all expect ``list[dict]`` where each dict
    # carries ``timestamp``, ``cost_usd``, ``total_tokens``, and a ``model``
    # key. We reduce the daily buckets to one synthetic "turn" per day so
    # the same renderer contract applies without any chart-lib rework.
    daily = report.get("daily") or []
    synth_turns: list[dict] = []
    for d in daily:
        synth_turns.append({
            "timestamp":     f"{d['date']}T12:00:00Z",
            "timestamp_fmt": d["date"],
            "cost_usd":      float(d.get("cost", 0.0)),
            "total_tokens":  int(d.get("tokens", 0)),
            # v1.14.1: pipe real per-day token buckets through to the
            # chart renderer. Prior to this change all four series were
            # hardcoded to 0, producing a flatlined stacked-bar chart
            # where only the Cost $ line carried real data.
            "input_tokens":       int(d.get("input", 0)),
            "output_tokens":      int(d.get("output", 0)),
            "cache_read_tokens":  int(d.get("cache_read", 0)),
            "cache_write_tokens": int(d.get("cache_write", 0)),
            "model":         "instance",
            "index":         0,
        })
    # Instance page shows a daily cost rail (not the Highcharts 3D chart and
    # not the per-session chartrail — each is wrong at instance scope).
    daily_cost_rail_html   = _build_daily_cost_rail_html(daily)
    daily_cost_rail_script = _daily_cost_rail_script() if daily_cost_rail_html else ""

    # ---- Summary cards -----------------------------------------------------
    top_project = projects[0] if projects else None
    top_model_name = ""
    if models:
        top_model_name = max(models.items(),
                              key=lambda kv: float(kv[1].get("cost_usd", 0.0)))[0]

    active_days = len({d["date"] for d in daily}) if daily else 0

    cards = [
        (f"${float(totals.get('cost', 0.0)):.2f}", "Total cost"),
        (f"{totals.get('turns', 0):,}",            "Total turns"),
        (f"{report.get('project_count', 0):,}",    "Projects"),
        (f"{report.get('session_count', 0):,}",    "Sessions"),
        (f"{active_days:,}",                        "Active days"),
        (f"{totals.get('input', 0):,}",             "Input tokens (new)"),
        (f"{totals.get('output', 0):,}",            "Output tokens"),
        (f"{totals.get('cache_read', 0):,}",        "Cache read"),
        (f"{totals.get('cache_write', 0):,}",       "Cache write"),
    ]
    if top_project:
        cards.append((f"`{top_project['slug'][:18]}…`"
                       if len(top_project["slug"]) > 18
                       else f"`{top_project['slug']}`",
                      "Top project by cost"))
    if top_model_name:
        cards.append((f"{top_model_name[:20]}…"
                       if len(top_model_name) > 20 else top_model_name,
                      "Top model by cost"))

    cards_html_parts = []
    for idx, (val, lbl) in enumerate(cards):
        safe_val = html_mod.escape(val)
        safe_lbl = html_mod.escape(lbl)
        # First card is "Total cost" — elevate to .featured
        kpi_cls = "kpi featured cat-tokens" if idx == 0 else "kpi cat-tokens"
        cards_html_parts.append(
            f'<div class="{kpi_cls}"><div class="kpi-label">{safe_lbl}</div>'
            f'<div class="kpi-val">{safe_val}</div></div>'
        )
    summary_cards_html = (
        f'<div class="kpi-grid">{"".join(cards_html_parts)}</div>'
    )

    # ---- Reused insights helpers ------------------------------------------
    # Each of these already handles the "empty" case gracefully by returning
    # "" when the underlying data is absent — so we can drop them in without
    # additional conditionals.
    rollup_html = _build_weekly_rollup_html(report.get("weekly_rollup", {}))
    blocks_html = _build_session_blocks_html(
        report.get("session_blocks", []),
        report.get("block_summary", {}),
        tz_label, tz_offset,
    )
    tod_section = report.get("time_of_day", {}) or {}
    hod_html    = _build_hour_of_day_html(tod_section, tz_label, tz_offset)
    punchcard_html = _build_punchcard_html(tod_section, tz_label, tz_offset)
    heatmap_html = _build_tod_heatmap_html(tod_section, tz_label, tz_offset)

    insights_html = rollup_html + blocks_html + hod_html + punchcard_html + heatmap_html

    # Phase-A instance-level sections (v1.6.0).
    inst_by_skill_html = _build_by_skill_html(report.get("by_skill", []) or [])
    inst_by_subagent_type_html = _build_by_subagent_type_html(
        report.get("by_subagent_type", []) or [])
    inst_cache_breaks_html = _build_cache_breaks_html(
        report.get("cache_breaks", []) or [],
        int(report.get("cache_break_threshold", _CACHE_BREAK_DEFAULT_THRESHOLD)),
    )

    # ---- Projects breakdown table -----------------------------------------
    proj_rows_html_parts = []
    for i, proj in enumerate(projects, 1):
        slug = proj["slug"]
        slug_safe = html_mod.escape(slug)
        friendly = html_mod.escape(proj.get("friendly_path", ""))
        if slug in drilldown_slugs:
            name_cell = (
                f'<a class="drilldown" data-sm-nav href="projects/{slug_safe}_dashboard.html">'
                f'<code>{slug_safe}</code></a>'
            )
        else:
            name_cell = f'<code>{slug_safe}</code>'
        proj_rows_html_parts.append(
            f'<tr>'
            f'<td class="num">{i}</td>'
            f'<td>{name_cell}</td>'
            f'<td class="muted mono">{friendly}</td>'
            f'<td class="num">{proj.get("session_count", 0):,}</td>'
            f'<td class="num">{proj.get("turn_count", 0):,}</td>'
            f'<td class="ts">{html_mod.escape(proj.get("first_ts", ""))}</td>'
            f'<td class="ts">{html_mod.escape(proj.get("last_ts", ""))}</td>'
            f'<td class="cost">${float(proj.get("cost_usd", 0.0)):.4f}</td>'
            f'</tr>'
        )
    projects_table_html = (
        f'<section class="section">'
        f'<div class="section-title"><h2>Projects breakdown</h2>'
        f'<span class="hint">sorted by cost descending · click project to open drilldown</span></div>'
        f'<table class="timeline-table">'
        f'<thead><tr>'
        f'<th class="num">#</th><th>Project</th><th>Path</th>'
        f'<th class="num">Sessions</th><th class="num">Turns</th>'
        f'<th>First</th><th>Last</th><th class="num">Cost $</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(proj_rows_html_parts)}</tbody>'
        f'</table>'
        f'</section>'
    )

    # ---- Models table (aggregated) ----------------------------------------
    if models:
        model_rows_html_parts = []
        for name, info in sorted(models.items(),
                                  key=lambda kv: -float(kv[1].get("cost_usd", 0.0))):
            r = info.get("rates") or _pricing_for(name)
            model_rows_html_parts.append(
                f'<tr>'
                f'<td><code>{html_mod.escape(name)}</code></td>'
                f'<td class="num">{int(info.get("turns", 0)):,}</td>'
                f'<td class="num">{int(info.get("input_tokens", 0)):,}</td>'
                f'<td class="num">{int(info.get("output_tokens", 0)):,}</td>'
                f'<td class="num">{int(info.get("cache_read_tokens", 0)):,}</td>'
                f'<td class="num">{int(info.get("cache_write_tokens", 0)):,}</td>'
                f'<td class="num">${r["input"]:.2f}</td>'
                f'<td class="num">${r["output"]:.2f}</td>'
                f'<td class="num">${r["cache_read"]:.2f}</td>'
                f'<td class="num">${r["cache_write"]:.2f}</td>'
                f'<td class="cost">${float(info.get("cost_usd", 0.0)):.4f}</td>'
                f'</tr>'
            )
        models_table_html = (
            f'<section class="section">'
            f'<div class="section-title"><h2>Models (aggregated)</h2></div>'
            f'<table class="models-table">'
            f'<thead><tr>'
            f'<th>Model</th><th class="num">Turns</th>'
            f'<th class="num">Input</th><th class="num">Output</th>'
            f'<th class="num">CacheRd</th><th class="num">CacheWr</th>'
            f'<th class="num">$/M in</th><th class="num">$/M out</th>'
            f'<th class="num">$/M rd</th><th class="num">$/M wr</th>'
            f'<th class="num">Cost $</th>'
            f'</tr></thead>'
            f'<tbody>{"".join(model_rows_html_parts)}</tbody>'
            f'</table>'
            f'</section>'
        )
    else:
        models_table_html = ""

    page_title = "Session Metrics — all projects"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="chart-lib" content="{chart_lib}">
<title>{page_title}</title>
{_theme_css()}
{_theme_bootstrap_head_js()}
</head>
<body class="theme-console">
<div class="shell">
<header class="topbar">
  <div class="brand"><span class="dot"></span><span>session-metrics</span></div>
  <nav class="nav"><span class="navlink current">Instance</span>{_theme_picker_markup()}</nav>
</header>
<header class="page-header">
  <h1>{page_title}</h1>
  <p class="meta">Generated {generated} &nbsp;·&nbsp; Scanning: <code>{projects_dir}</code>
   &nbsp;·&nbsp; {report.get("project_count", 0)} projects,
   {report.get("session_count", 0)} sessions,
   {totals.get("turns", 0):,} turns</p>
</header>
{summary_cards_html}
{daily_cost_rail_html}
{inst_cache_breaks_html}
{inst_by_skill_html}
{inst_by_subagent_type_html}
{projects_table_html}
{insights_html}
{models_table_html}
<footer class="foot">
  <span class="muted">session-metrics (instance) · {generated}</span>
</footer>
</div>
{daily_cost_rail_script}
{_theme_bootstrap_body_js()}
</body>
</html>"""


# === instance mode: end =====================================================


def _dispatch(report: dict, formats: list[str],
               single_page: bool = False,
               chart_lib: str = "highcharts",
               redact_user_prompts: bool = False) -> None:
    # Always render text to stdout
    print(render_text(report))

    is_compare = report.get("mode") == "compare"

    for fmt in formats:
        if fmt == "text":
            continue   # already printed
        if fmt == "html" and is_compare:
            # Compare HTML is always single-page — the report is compact
            # enough to read at a glance, and splitting dashboard/detail
            # would fragment the story (summary cards and per-turn table
            # are read together). ``--single-page`` / ``--chart-lib`` are
            # silently ignored for compare output.
            smc = sys.modules["session_metrics_compare"]
            content = smc.render_compare_html(
                report, redact_user_prompts=redact_user_prompts,
            )
            path = _write_output(fmt, content, report)
            print(f"[export] HTML (compare) → {path}", file=sys.stderr)
            continue
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
    p.add_argument("--all-projects", action="store_true",
                   help="Instance-wide dashboard: aggregate every project under the "
                        "projects directory into one report. Writes HTML/MD/CSV/JSON "
                        "and (unless --no-project-drilldown) a per-project HTML "
                        "drilldown for each project into a dated subfolder under "
                        "exports/session-metrics/instance/.")
    p.add_argument("--no-project-drilldown", action="store_true",
                   help="With --all-projects: skip the per-project HTML drilldown "
                        "pass. Fast path for CI / quick-glance runs. The instance "
                        "HTML still renders, but project rows are plain text "
                        "without hyperlinks.")
    p.add_argument("--projects-dir", metavar="PATH",
                   help="Override the Claude Code projects directory (normally "
                        "~/.claude/projects or $CLAUDE_PROJECTS_DIR). Highest "
                        "precedence. Makes it trivial to script multi-instance "
                        "dashboards: run --all-projects once per path.")
    p.add_argument("--output", "-o", nargs="+", metavar="FMT",
                   choices=["text", "json", "csv", "md", "html"],
                   help="Export formats in addition to stdout text. "
                        "One or more of: json csv md html. "
                        "Written to exports/session-metrics/ in the project root.")
    p.add_argument("--include-subagents", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Tally spawned subagent JSONL files (default: on). "
                        "Pass --no-include-subagents to skip for faster runs.")
    p.add_argument("--cache-break-threshold", type=int,
                   default=_CACHE_BREAK_DEFAULT_THRESHOLD, metavar="TOKENS",
                   help=(f"Turns whose input + cache_creation exceed TOKENS are "
                         f"flagged as cache-break events (default: "
                         f"{_CACHE_BREAK_DEFAULT_THRESHOLD:,}). Matches Anthropic "
                         f"session-report's convention."))
    p.add_argument("--no-subagent-attribution", action="store_true",
                   help="Disable Phase-B subagent → parent-prompt token "
                        "attribution. By default, subagent token usage rolls "
                        "up onto the user prompt that spawned the subagent "
                        "chain via additional ``attributed_subagent_*`` "
                        "fields (no double-counting).")
    p.add_argument("--sort-prompts-by", choices=["total", "self"],
                   default=None, metavar="MODE",
                   help="How to rank top-prompts: 'total' (parent + attributed "
                        "subagent cost — bubbles cheap prompts that spawned "
                        "expensive subagents) or 'self' (parent only — pre-"
                        "Phase-B behaviour). Default: 'total' for HTML/MD "
                        "outputs, 'self' for CSV/JSON to keep machine "
                        "consumers stable.")
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
    p.add_argument("--allow-unverified-charts", action="store_true",
                   help="Downgrade vendor-chart SHA-256 verification failures "
                        "(missing manifest entry, missing file, hash mismatch) "
                        "from hard errors to stderr warnings. Default: fail "
                        "loudly so tampered or corrupted installs are caught.")
    p.add_argument("--strict-tz", action="store_true",
                   help="When --tz / --peak-tz cannot be resolved (e.g. on "
                        "Windows without the 'tzdata' pip package), raise "
                        "instead of warning and falling back to UTC. Default: "
                        "warn and fall back so reports still render. See "
                        "references/platform-notes.md.")
    # --- Compare-mode flags ------------------------------------------------
    # ``--compare`` is the single entrypoint: any other compare-mode flag is
    # a no-op without it. Kept out of the ``--project-cost`` / single-session
    # code paths so natural-language prompts ("session cost?") never fall
    # into this branch — dispatch only happens when the user explicitly
    # passes two specifiers via ``--compare``.
    # The four primary-mode flags are mutually exclusive at the CLI: passing
    # two of them together is caught here rather than silently last-wins.
    _mode = p.add_mutually_exclusive_group()
    _mode.add_argument("--compare", nargs=2, metavar=("A", "B"),
                   help="Run a model-compare report over two sessions. Each "
                        "arg may be a .jsonl path, a session UUID, or a "
                        "'last-<family>' / 'all-<family>' magic token. "
                        "Supports Mode 1 (controlled session pair) and "
                        "Mode 2 (observational project aggregate). "
                        "See references/model-compare.md.")
    p.add_argument("--pair-by", choices=["fingerprint", "ordinal"],
                   default="fingerprint",
                   help="Turn-pairing strategy for --compare. 'fingerprint' "
                        "(default) hashes the first 200 chars of each user "
                        "prompt; 'ordinal' pairs by turn index.")
    p.add_argument("--compare-min-turns", type=int, default=5, metavar="N",
                   help="Minimum user-prompt turns required for a 'last-<family>' "
                        "resolver match. Default 5; lower when deliberately "
                        "comparing short sessions.")
    p.add_argument("--compare-scope", choices=["auto", "session", "project"],
                   default="auto",
                   help="Force a compare-mode scope. 'auto' (default) picks "
                        "'controlled' for session pairs and 'observational' "
                        "for project aggregates. 'session' refuses "
                        "'all-<family>' args. 'project' forces observational "
                        "mode even when both args are single sessions.")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Auto-accept confirmation prompts for expensive "
                        "compare paths (Phase 3: 'all-<family>' rollups, "
                        "count-tokens API mode, multi-trial runs). Accepted "
                        "now for CLI-shape stability.")
    # --- Compare capture-protocol helper (Phase 4) -----------------------
    _mode.add_argument("--compare-prep", nargs="*", metavar="MODEL",
                   default=None,
                   help="Emit the capture protocol + canonical prompt suite "
                        "to stdout. Takes 0-2 positional model IDs; defaults "
                        "to 'claude-opus-4-6 claude-opus-4-7'. Pipe to a file "
                        "for easy copy-paste into two fresh Claude Code "
                        "sessions.")
    p.add_argument("--compare-prompts", metavar="DIR",
                   help="Override the compare-suite prompt directory (default: "
                        "references/model-compare/prompts next to this script). "
                        "Used by --compare for predicate eval and by "
                        "--compare-prep for the prompt list.")
    p.add_argument("--compare-list-prompts", action="store_true",
                   help="Print which prompts will run on the next --compare-run "
                        "(built-in suite + any user extras from "
                        "~/.session-metrics/prompts/) and the total inference-"
                        "call count. No inference is performed. Respects "
                        "--compare-prompts if given.")
    p.add_argument("--compare-add-prompt", metavar="TEXT",
                   help="Add a custom prompt to ~/.session-metrics/prompts/ and "
                        "print the file path and remove command. The prompt runs "
                        "automatically on every subsequent --compare-run with no "
                        "flags required. Supports plain-text prompts — no YAML "
                        "or predicate needed.")
    p.add_argument("--compare-remove-prompt", metavar="NAME",
                   help="Remove a user prompt from ~/.session-metrics/prompts/ "
                        "by its name (as shown by --compare-list-prompts). "
                        "Cannot remove built-in prompts.")
    p.add_argument("--allow-suite-mismatch", action="store_true",
                   help="Proceed with a --compare even when the two sessions "
                        "ran different compare-suite versions. Without this "
                        "flag the compare refuses (ratios would conflate "
                        "suite shift with model shift).")
    p.add_argument("--compare-effort", nargs="*", metavar="LEVEL",
                   default=None,
                   help="Annotate the compare report with the reasoning "
                        "effort level each side was captured at. Purely "
                        "cosmetic — does not re-run anything — this flag "
                        "surfaces the effort used during capture on the "
                        "text, MD, CSV, HTML, and analysis.md outputs. "
                        "Takes 0, 1, or 2 positional levels from "
                        "{low, medium, high, xhigh, max}. With 1 value "
                        "both sides share that label; with 2 values the "
                        "first applies to side A, the second to side B. "
                        "--compare-run already infers this from "
                        "--compare-run-effort, so you rarely need to "
                        "pass this flag manually unless you're running "
                        "--compare on JSONLs captured outside the "
                        "orchestrator.")
    # --- Phase 6 / 7 — HTML compare + Insights card ----------------------
    p.add_argument("--redact-user-prompts", action="store_true",
                   help="In the compare HTML report, replace freeform "
                        "user-prompt fingerprints with '[redacted]' so the "
                        "file is safe to share. Sentinel-tagged suite "
                        "prompts (canonical, non-PII) stay visible.")
    p.add_argument("--no-model-compare-insight", action="store_true",
                   help="Suppress the Model-compare insight card on the "
                        "single-session / project dashboards. Use when the "
                        "hint is noisy (e.g. a project with many historical "
                        "families but no interest in running a benchmark).")
    # --- Phase 8 — count_tokens API mode --------------------------------
    _mode.add_argument("--count-tokens-only", action="store_true",
                   help="Compare input-token counts between two models using "
                        "the /v1/messages/count_tokens API — no inference, no "
                        "cost (other than request rate). Requires "
                        "ANTHROPIC_API_KEY. Pair with --compare-models to "
                        "choose the pair (defaults: claude-opus-4-6 vs "
                        "claude-opus-4-7). Output tokens and total cost are "
                        "NOT measured — run --compare for that.")
    p.add_argument("--compare-models", nargs="*", metavar="MODEL",
                   default=None,
                   help="Model pair for --count-tokens-only. Takes 0-2 "
                        "positional model IDs; defaults to 'claude-opus-4-6 "
                        "claude-opus-4-7'. A single model is accepted for "
                        "input-token measurement without ratios.")
    # --- Phase 10 — Automated headless capture ---------------------------
    _mode.add_argument("--compare-run", nargs="*", metavar="MODEL",
                   default=None,
                   help="Fully automated compare: spawns two 'claude -p' "
                        "(headless) sessions, feeds each the canonical "
                        "10-prompt suite, then runs --compare on the result. "
                        "Takes 0-2 positional model IDs; defaults to "
                        "'claude-opus-4-6[1m] claude-opus-4-7[1m]' because "
                        "that matches Claude Code's shipping default (1M-"
                        "context Opus). Pass 'claude-opus-4-6 claude-opus-4-7' "
                        "to compare the 200k-context variants instead; mixed "
                        "tiers are accepted and fire the existing context-"
                        "tier-mismatch advisory on the report. Runs 2 × N "
                        "inference calls against your subscription quota — "
                        "confirmation gate requires --yes on non-TTY.")
    p.add_argument("--compare-run-scratch-dir", metavar="DIR", default=None,
                   help="Scratch directory for --compare-run captures. "
                        "Defaults to a fresh mkdtemp under $TMPDIR. The "
                        "directory becomes the cwd for every 'claude -p' "
                        "subprocess, which determines the project slug "
                        "Claude Code writes session JSONLs under.")
    p.add_argument("--compare-run-allowed-tools", metavar="TOOLS",
                   default=None,
                   help="--allowedTools value passed to each 'claude -p' "
                        "subprocess in --compare-run. Default: "
                        "'Bash,Read,Write,Edit,Glob,Grep'. Identical on both "
                        "sides so the tool-call ratio stays comparable.")
    p.add_argument("--compare-run-permission-mode", metavar="MODE",
                   default=None,
                   help="--permission-mode value for every --compare-run "
                        "subprocess (default: 'bypassPermissions' so the "
                        "headless calls don't stall waiting for human "
                        "approval). Pass an empty string to omit the flag.")
    p.add_argument("--compare-run-max-budget-usd", type=float, default=None,
                   metavar="USD",
                   help="Per-subprocess --max-budget-usd ceiling for "
                        "--compare-run. Not set by default. Threaded to each "
                        "'claude -p' invocation unchanged.")
    p.add_argument("--compare-run-per-call-timeout", type=float, default=None,
                   metavar="SECONDS",
                   help="Wall-clock timeout for each 'claude -p' subprocess "
                        "in --compare-run. Default 900s (15 min); the "
                        "tool-heavy prompt is the usual slowest.")
    p.add_argument("--compare-run-effort", nargs="*", metavar="LEVEL",
                   default=None,
                   help="Reasoning effort level threaded as 'claude -p "
                        "--effort <level>' to each --compare-run subprocess. "
                        "Takes 0, 1, or 2 positional levels from "
                        "{low, medium, high, xhigh, max}. With 0 (flag "
                        "absent or given with no arguments) the flag is "
                        "omitted entirely, so each model uses Claude Code's "
                        "per-model default (opus-4-6 → high, opus-4-7 → "
                        "xhigh). With 1 value both sides pin to that level. "
                        "With 2 values the first applies to side A, the "
                        "second to side B. Useful when you want to hold "
                        "effort constant across a version comparison "
                        "instead of letting each model fall back to its own "
                        "default.")
    p.add_argument("--no-compare-run-extras", action="store_true",
                   help="Skip the per-session HTML/JSON dashboards and the "
                        "analysis.md companion that --compare-run normally "
                        "emits alongside the compare report. Extras only fire "
                        "when --compare-run is combined with --output (the "
                        "text-only stdout path stays file-free regardless). "
                        "Use this flag to restore the pre-v1.7.0 minimal "
                        "single-artefact output.")
    p.add_argument("--compare-run-prompt-steering", metavar="VARIANT",
                   default=None,
                   help="Wrap each of the 10 canonical prompts with prompt-"
                        "steering text before feeding them to 'claude -p'. "
                        "VARIANT must be one of: concise, think-step-by-step, "
                        "ultrathink, no-tools. Applied symmetrically to both "
                        "sides so the A/B comparison stays clean. Default: "
                        "unset (no wrapper, identical to baseline behaviour). "
                        "IFEval pass rates may differ from baseline under "
                        "steering by design — predicate breakage is the "
                        "measurement, not a regression. For multi-variant "
                        "sweeps with auto-rendered comparison articles use "
                        "the benchmark-effort-prompt skill instead.")
    p.add_argument("--compare-run-prompt-steering-position",
                   metavar="POSITION", default="prefix",
                   choices=["prefix", "append", "both"],
                   help="Where to inject the steering text relative to the "
                        "prompt body when --compare-run-prompt-steering is "
                        "set. 'prefix' prepends; 'append' appends; 'both' "
                        "sandwiches the prompt between the steering prefix "
                        "and suffix. Default: prefix. Ignored when "
                        "--compare-run-prompt-steering is absent.")
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


def _load_compare_module():
    """Lazy-load the sibling ``session_metrics_compare`` module.

    Split out of ``main()`` so the import cost is paid only when the
    user actually runs compare mode — everyday single-session reports
    don't touch it. Also registers this script as ``session_metrics``
    in ``sys.modules`` before the compare module executes, because the
    compare module's one-way coupling helper (``_main()``) looks up
    that name. When this file is executed directly its ``__name__`` is
    ``"__main__"``, so the registration is non-redundant.
    """
    if "session_metrics_compare" in sys.modules:
        return sys.modules["session_metrics_compare"]
    sys.modules.setdefault("session_metrics", sys.modules[__name__])
    import importlib.util
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "session_metrics_compare", here / "session_metrics_compare.py")
    if spec is None or spec.loader is None:
        print("[error] could not locate session_metrics_compare.py alongside "
              "session-metrics.py", file=sys.stderr)
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["session_metrics_compare"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    args = _build_parser().parse_args()
    slug = args.slug or _env_slug() or _cwd_to_slug()
    _validate_slug(slug)
    formats: list[str] = args.output or []
    tz_offset, tz_label = _resolve_tz(args.tz, args.utc_offset,
                                      strict=bool(args.strict_tz))
    peak = _build_peak(args.peak_hours, args.peak_tz,
                       strict=bool(args.strict_tz))
    chart_lib: str = args.chart_lib
    # Flip the module-level gate so _read_vendor_files knows whether to
    # raise or warn on verification failures. Set before any chart code runs.
    global _ALLOW_UNVERIFIED_CHARTS
    _ALLOW_UNVERIFIED_CHARTS = bool(args.allow_unverified_charts)
    _maybe_warn_chart_license(chart_lib, formats)

    if args.list:
        _list_sessions(slug)
        return

    if args.compare_add_prompt:
        smc = _load_compare_module()
        _extras_dir = smc._EXTRAS_DIR
        _extras_dir.mkdir(parents=True, exist_ok=True)
        _slug = re.sub(r"[^a-z0-9]+", "_", args.compare_add_prompt.lower())[:40].strip("_") + "_user"
        _dest = _extras_dir / f"{_slug}.md"
        if _dest.exists():
            print(f"[warn] prompt '{_slug}' already exists at {_dest}")
            print("Edit it directly or delete it and re-run with a different prompt.")
        else:
            _dest.write_text(args.compare_add_prompt.strip() + "\n", encoding="utf-8")
            print(f"Added prompt to {_dest}")
            print("Will run automatically on every --compare-run "
                  "(ratio/token data only; no pass/fail scoring)")
            print(f"Preview: session-metrics --compare-list-prompts")
            print(f"Remove:  session-metrics --compare-remove-prompt {_slug}")
        return

    if args.compare_remove_prompt:
        smc = _load_compare_module()
        _name = args.compare_remove_prompt.removesuffix(".md")
        _extras_suite = smc._load_prompt_suite(smc._EXTRAS_DIR)
        _entry = _extras_suite.get(_name)
        if _entry is None:
            print(f"[error] no user prompt named '{_name}' in {smc._EXTRAS_DIR}.",
                  file=sys.stderr)
            print("Run: session-metrics --compare-list-prompts  to see available names.",
                  file=sys.stderr)
            sys.exit(1)
        _entry["path"].unlink()
        print(f"Removed {_entry['path']}")
        return

    if args.compare_list_prompts:
        smc = _load_compare_module()
        _suite_dir = Path(args.compare_prompts).expanduser() if args.compare_prompts else None
        try:
            _suite = smc._load_prompt_suite(_suite_dir)
        except smc.PromptSuiteError as exc:
            print(f"[error] prompt suite: {exc}", file=sys.stderr)
            sys.exit(1)
        smc._run_compare_list_prompts(_suite)
        return

    if args.compare_prep is not None:
        smc = _load_compare_module()
        suite_dir = Path(args.compare_prompts).expanduser() if args.compare_prompts else None
        smc._run_compare_prep(args.compare_prep, suite_dir=suite_dir)
        return

    if args.count_tokens_only:
        smc = _load_compare_module()
        suite_dir = Path(args.compare_prompts).expanduser() if args.compare_prompts else None
        smc._run_count_tokens_only(
            args.compare_models,
            suite_dir=suite_dir,
            assume_yes=args.yes,
        )
        return

    if args.compare_run is not None:
        smc = _load_compare_module()
        suite_dir = Path(args.compare_prompts).expanduser() if args.compare_prompts else None
        scratch_dir = Path(args.compare_run_scratch_dir).expanduser() \
            if args.compare_run_scratch_dir else None
        # Resolve 0/1/2 positional model IDs to an (A, B) pair. Default is
        # the 1M-context Opus tier because that is what Claude Code ships
        # as the default Opus routing — comparing 200k vs 200k is a
        # deliberate opt-out, not a realistic baseline.
        _default_a = "claude-opus-4-6[1m]"
        _default_b = "claude-opus-4-7[1m]"
        _models = list(args.compare_run)
        if len(_models) == 0:
            model_a, model_b = _default_a, _default_b
        elif len(_models) == 1:
            model_a, model_b = _models[0], _default_b
        elif len(_models) == 2:
            model_a, model_b = _models[0], _models[1]
        else:
            print("[error] --compare-run takes 0, 1, or 2 model IDs; "
                  f"got {len(_models)}", file=sys.stderr)
            sys.exit(1)
        # Allow empty string to mean "omit --permission-mode"; None means "use default".
        if args.compare_run_permission_mode is None:
            permission_mode = "bypassPermissions"
        elif args.compare_run_permission_mode == "":
            permission_mode = None
        else:
            permission_mode = args.compare_run_permission_mode
        allowed_tools = args.compare_run_allowed_tools \
            or "Bash,Read,Write,Edit,Glob,Grep"
        timeout = args.compare_run_per_call_timeout or 900.0
        # Resolve 0/1/2 positional effort values. None or empty list means
        # "let each model use its Claude Code default" (Opus 4.6 → high,
        # Opus 4.7 → xhigh). One value pins both sides; two values map
        # A then B. The orchestrator validates the level itself, so we
        # only enforce arity here.
        _efforts = list(args.compare_run_effort or [])
        if len(_efforts) == 0:
            effort_a, effort_b = None, None
        elif len(_efforts) == 1:
            effort_a = effort_b = _efforts[0]
        elif len(_efforts) == 2:
            effort_a, effort_b = _efforts[0], _efforts[1]
        else:
            print("[error] --compare-run-effort takes 0, 1, or 2 levels; "
                  f"got {len(_efforts)}", file=sys.stderr)
            sys.exit(1)
        try:
            _touch_compare_state_marker(_cwd_to_slug(str(scratch_dir.resolve()))
                                        if scratch_dir else slug)
        except (OSError, AttributeError):
            pass
        # --compare-run defaults to md + html artefact generation so the
        # user always gets the analysis.md scaffold + dashboard HTML
        # pair alongside the text report. Passing an explicit --output
        # list overrides this (empty list stays empty after override
        # only via the not-yet-exposed opt-out; see SKILL.md for the
        # rationale). --no-compare-run-extras is the escape hatch when
        # the user wants the text-only behaviour back.
        compare_run_formats = formats or ["md", "html"]
        _maybe_warn_chart_license(chart_lib, compare_run_formats)
        smc._run_compare_run(
            model_a, model_b,
            scratch_dir=scratch_dir,
            suite_dir=suite_dir,
            assume_yes=args.yes,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            max_budget_usd=args.compare_run_max_budget_usd,
            per_call_timeout=timeout,
            formats=compare_run_formats,
            single_page=args.single_page,
            chart_lib=chart_lib,
            redact_user_prompts=args.redact_user_prompts,
            tz_offset=tz_offset,
            tz_label=tz_label,
            use_cache=not args.no_cache,
            include_subagents=args.include_subagents,
            pair_by=args.pair_by,
            min_turns=args.compare_min_turns,
            allow_suite_mismatch=args.allow_suite_mismatch,
            compare_run_extras=not args.no_compare_run_extras,
            effort_a=effort_a,
            effort_b=effort_b,
            steering_variant=args.compare_run_prompt_steering,
            steering_position=args.compare_run_prompt_steering_position,
        )
        return

    if args.compare:
        smc = _load_compare_module()
        suite_dir = Path(args.compare_prompts).expanduser() if args.compare_prompts else None
        # Resolve 0/1/2 positional effort annotations for --compare. This is
        # cosmetic: it doesn't re-run inference, it just lets the user
        # surface the effort level the JSONLs were captured at on the
        # text/MD/HTML/CSV/analysis.md outputs. Same 0/1/2 arity as
        # --compare-run-effort so the two feel symmetric.
        _compare_efforts = list(args.compare_effort or [])
        if len(_compare_efforts) == 0:
            effort_a_compare = effort_b_compare = None
        elif len(_compare_efforts) == 1:
            effort_a_compare = effort_b_compare = _compare_efforts[0]
        elif len(_compare_efforts) == 2:
            effort_a_compare, effort_b_compare = _compare_efforts[0], _compare_efforts[1]
        else:
            print("[error] --compare-effort takes 0, 1, or 2 levels; "
                  f"got {len(_compare_efforts)}", file=sys.stderr)
            sys.exit(1)
        # State-marker file: Phase 7's dashboard insight card only fires
        # after the user has successfully run --compare once in this project.
        # Dropping the marker here (before the run, as a best-effort) means
        # that even if the compare crashes mid-way we still remember the
        # user attempted one — the whole point is to suppress spam on
        # projects where nobody's interested in a benchmark.
        try:
            _touch_compare_state_marker(slug)
        except OSError:
            pass
        smc._run_compare(
            args.compare[0], args.compare[1],
            slug=slug,
            pair_by=args.pair_by,
            compare_scope=args.compare_scope,
            min_turns=args.compare_min_turns,
            formats=formats,
            tz_offset=tz_offset,
            tz_label=tz_label,
            include_subagents=args.include_subagents,
            use_cache=not args.no_cache,
            single_page=args.single_page,
            chart_lib=chart_lib,
            assume_yes=args.yes,
            prompt_suite_dir=suite_dir,
            allow_suite_mismatch=args.allow_suite_mismatch,
            redact_user_prompts=args.redact_user_prompts,
            effort_a=effort_a_compare,
            effort_b=effort_b_compare,
        )
        return

    if args.all_projects:
        # Apply the --projects-dir override (if any) before any discovery
        # call. ``_projects_dir()`` reads this module-level var first, so
        # setting it here cascades through _list_all_projects, _load_session,
        # and every downstream helper without threading an arg through them.
        if args.projects_dir:
            global _PROJECTS_DIR_OVERRIDE
            _PROJECTS_DIR_OVERRIDE = Path(args.projects_dir).expanduser()
        _run_all_projects(
            formats, tz_offset, tz_label,
            peak=peak, single_page=args.single_page,
            use_cache=not args.no_cache, chart_lib=chart_lib,
            include_subagents=args.include_subagents,
            drilldown=not args.no_project_drilldown,
            suppress_model_compare_insight=args.no_model_compare_insight,
            cache_break_threshold=args.cache_break_threshold,
            subagent_attribution=not args.no_subagent_attribution,
            sort_prompts_by=args.sort_prompts_by,
        )
        return

    if args.project_cost:
        print(f"Slug : {slug}", file=sys.stderr)
        print(f"TZ   : {tz_label} (UTC{'+' if tz_offset >= 0 else '-'}{abs(tz_offset):g})", file=sys.stderr)
        print(file=sys.stderr)
        _run_project_cost(
            slug, args.include_subagents, formats, tz_offset, tz_label,
            peak=peak, single_page=args.single_page,
            use_cache=not args.no_cache, chart_lib=chart_lib,
            suppress_model_compare_insight=args.no_model_compare_insight,
            cache_break_threshold=args.cache_break_threshold,
            subagent_attribution=not args.no_subagent_attribution,
            sort_prompts_by=args.sort_prompts_by,
        )
        return

    jsonl_path, resolved_slug = _resolve_session(args)
    print(f"Slug    : {resolved_slug}", file=sys.stderr)
    print(f"TZ      : {tz_label} (UTC{'+' if tz_offset >= 0 else '-'}{abs(tz_offset):g})", file=sys.stderr)
    _run_single_session(
        jsonl_path, resolved_slug, args.include_subagents, formats,
        tz_offset, tz_label, peak=peak, single_page=args.single_page,
        use_cache=not args.no_cache, chart_lib=chart_lib,
        suppress_model_compare_insight=args.no_model_compare_insight,
        cache_break_threshold=args.cache_break_threshold,
        subagent_attribution=not args.no_subagent_attribution,
        sort_prompts_by=args.sort_prompts_by,
    )


if __name__ == "__main__":
    main()
