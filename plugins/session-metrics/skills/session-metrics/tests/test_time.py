"""Time-bucketing tests — time-of-day, hour-of-day, weekday×hour matrix, 5-hour session blocks.

Split out of test_session_metrics.py in v1.41.9 (Tier 4 of the
post-audit improvement plan; sibling-file pattern established in v1.41.8).

Run with: uv run python -m pytest tests/test_time.py -v
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


def _build_fixture_report():
    sid, turns, user_ts = sm._load_session(_FIXTURE, include_subagents=False)
    return sm._build_report("session", "test-slug", [(sid, turns, user_ts)])


# === BODY (extracted from test_session_metrics.py) ============================

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


def _raise_zoneinfo_missing(*_):
    raise sm.ZoneInfoNotFoundError("simulated windows-without-tzdata")


def test_resolve_tz_missing_warns_and_falls_back(monkeypatch, capsys):
    """Default: ZoneInfo miss warns to stderr and returns (0.0, 'UTC')."""
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    monkeypatch.setattr(sys.modules["_tz"], "ZoneInfo", _raise_zoneinfo_missing)
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
    monkeypatch.setattr(sys.modules["_tz"], "ZoneInfo", _raise_zoneinfo_missing)
    with pytest.raises(SystemExit) as excinfo:
        sm._resolve_tz("Europe/Berlin", None, strict=True)
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "[error]" in err
    assert "tzdata" in err


def test_build_peak_missing_warns_and_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    monkeypatch.setattr(sys.modules["_tz"], "ZoneInfo", _raise_zoneinfo_missing)
    p = sm._build_peak((9, 17), "America/Los_Angeles")
    assert p is not None
    assert p["tz_label"] == "UTC"
    assert p["tz_offset_hours"] == 0.0
    err = capsys.readouterr().err
    assert "[warn]" in err
    assert "tzdata" in err


def test_build_peak_strict_raises_on_missing(monkeypatch, capsys):
    monkeypatch.setattr(sm, "ZoneInfo", _raise_zoneinfo_missing)
    monkeypatch.setattr(sys.modules["_tz"], "ZoneInfo", _raise_zoneinfo_missing)
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


def test_weekly_rollup_boundary_inclusivity():
    """Half-open ``[start, end)`` boundary math at ``now-7d`` and ``now-14d``.

    Trailing window is ``[now-7d, now)``; prior window is
    ``[now-14d, now-7d)``. A turn timestamped at the cutoff must land in
    *one* window, not zero or two — and the cutoff is the lower bound,
    not the upper. Without this regression test an off-by-one swap of
    inclusive/exclusive on either edge would silently double-count or
    drop a turn straddling the seam.
    """
    from datetime import datetime as _dt, timezone as _tz
    now = _epoch(2026, 4, 17, 12, 0)

    def _iso(epoch: int) -> str:
        return _dt.fromtimestamp(epoch, tz=_tz.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    epochs = {
        "at_now":         now,                 # exclusive upper of trailing
        "trailing_start": now - 7  * 86400,    # inclusive lower of trailing
        "seam_minus_1s":  now - 7  * 86400 - 1,  # last second of prior
        "prior_start":    now - 14 * 86400,    # inclusive lower of prior
        "before_prior":   now - 14 * 86400 - 1,  # outside both windows
    }

    def _mk_turn(epoch: int) -> dict:
        return {
            "timestamp":          _iso(epoch),
            "input_tokens":       1,
            "output_tokens":      1,
            "cache_read_tokens":  0,
            "cache_write_tokens": 0,
            "cost_usd":           0.001,
        }

    sessions_out = [{
        "turns": [_mk_turn(e) for e in epochs.values()],
    }]
    sessions_raw = [("sid", [], [])]
    ro = sm._build_weekly_rollup(sessions_out, sessions_raw, [], now_epoch=now)
    # Trailing catches at_now-exclusive and trailing_start-inclusive — so
    # one turn (trailing_start). Prior catches seam_minus_1s and
    # prior_start-inclusive — so two turns. before_prior and at_now fall
    # outside both windows.
    assert ro["trailing_7d"]["turns"] == 1
    assert ro["prior_7d"]["turns"] == 2


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
    blobs = list((tmp_path / "parse").glob("*.pkl"))
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
    # New key was written and the old blob was pruned — only 1 blob remains.
    assert len(after) == 1
    assert before.isdisjoint(after)


def test_cached_parse_invalidates_on_size(tmp_path, monkeypatch):
    """Same mtime + different content size must invalidate the cache.

    Regression for the atomic-replace gap: tools that preserve ``mtime_ns``
    while rewriting content (``cp -p``, ``rsync --inplace``, restore-from-
    backup) used to silently serve a stale pickle blob keyed only on
    mtime. Including ``st_size`` in the cache key makes any change in
    file size mint a fresh blob.
    """
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "fixed_mtime.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"))
    sm._cached_parse_jsonl(src, use_cache=True)
    # Snapshot the original blob set + the original ns timestamps so we can pin them.
    before = {p.name for p in (tmp_path / "parse").iterdir()}
    import os
    stat_before = src.stat()
    # Truncate the file so size shrinks but mtime is restored to the original.
    truncated = "\n".join(src.read_text(encoding="utf-8").splitlines()[:1]) + "\n"
    src.write_text(truncated, encoding="utf-8")
    # Use ``ns=`` to preserve nanosecond precision — float-second utime
    # round-trips lose precision on macOS APFS.
    os.utime(src, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    # mtime must match (the test premise — atomic-replace preserves mtime).
    assert src.stat().st_mtime_ns == stat_before.st_mtime_ns
    sm._cached_parse_jsonl(src, use_cache=True)
    after = {p.name for p in (tmp_path / "parse").iterdir()}
    # New key was minted (size component differs); old blob pruned.
    assert before.isdisjoint(after)
    assert len(after) == 1


def test_cached_parse_invalidates_on_script_version(tmp_path, monkeypatch):
    """Bumping ``_SCRIPT_VERSION`` must invalidate every existing blob.

    The version embeds in the cache filename so a parser-shape change
    forces a cold rebuild — without this guard, an upgrade that changes
    the parsed entry shape would silently feed stale dicts to downstream
    code.
    """
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "version_bump.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"))
    sm._cached_parse_jsonl(src, use_cache=True)
    before = {p.name for p in (tmp_path / "parse").iterdir()}
    assert len(before) == 1
    # Simulate a version bump.
    monkeypatch.setattr(sm, "_SCRIPT_VERSION", sm._SCRIPT_VERSION + "+test")
    sm._cached_parse_jsonl(src, use_cache=True)
    after = {p.name for p in (tmp_path / "parse").iterdir()}
    # New key was minted (version component differs); old blob pruned.
    assert before.isdisjoint(after)
    assert len(after) == 1


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
    size = 3
    key_a = sm._parse_cache_key(p_a, mtime_ns, size)
    key_b = sm._parse_cache_key(p_b, mtime_ns, size)
    assert key_a != key_b
    # Keys must still be deterministic for the same path.
    assert key_a == sm._parse_cache_key(p_a, mtime_ns, size)


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
    blobs = list((tmp_path / "parse").glob("*.pkl"))
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
    same source file all succeed, the final blob is a valid pickle blob,
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
    blobs = list(cache_dir.glob("*.pkl"))
    assert len(blobs) == 1, f"expected one blob, saw {[b.name for b in blobs]}"
    orphans = list(cache_dir.glob("*.tmp"))
    assert orphans == [], f"orphan tmp files: {[o.name for o in orphans]}"
    # Reading the cached blob must succeed and return a usable list.
    parsed = sm._cached_parse_jsonl(src, use_cache=True)
    assert isinstance(parsed, list)


def test_cached_parse_prunes_stale_mtime(tmp_path, monkeypatch):
    """After an mtime bump, the old blob is deleted on the next cache write."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # Prime the cache.
    sm._cached_parse_jsonl(src, use_cache=True)
    assert len(list((tmp_path / "parse").glob("*.pkl"))) == 1
    # Bump mtime to force a cache miss on the next call.
    import os
    stat = src.stat()
    os.utime(src, (stat.st_atime, stat.st_mtime + 2))
    sm._cached_parse_jsonl(src, use_cache=True)
    # Prune must have run: only the new blob remains.
    blobs = list((tmp_path / "parse").glob("*.pkl"))
    assert len(blobs) == 1


def test_cached_parse_prunes_stale_version(tmp_path, monkeypatch):
    """After a _SCRIPT_VERSION bump, the old blob is deleted on the next cache write."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # Write cache under old version.
    monkeypatch.setattr(sm, "_SCRIPT_VERSION", "1.1.0-test-old")
    sm._cached_parse_jsonl(src, use_cache=True)
    assert len(list((tmp_path / "parse").glob("*.pkl"))) == 1
    # Bump version to force a cache miss.
    monkeypatch.setattr(sm, "_SCRIPT_VERSION", "1.1.0-test-new")
    sm._cached_parse_jsonl(src, use_cache=True)
    # Only the new-version blob must survive.
    blobs = list((tmp_path / "parse").glob("*.pkl"))
    assert len(blobs) == 1
    assert "1.1.0-test-new" in blobs[0].name


def test_cached_parse_prune_does_not_touch_other_jsonls(tmp_path, monkeypatch):
    """Pruning blobs for source A must not delete blobs for source B."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src_a = tmp_path / "session_a.jsonl"
    src_b = tmp_path / "session_b.jsonl"
    content = _FIXTURE.read_text(encoding="utf-8")
    src_a.write_text(content, encoding="utf-8")
    src_b.write_text(content, encoding="utf-8")
    # Prime both caches.
    sm._cached_parse_jsonl(src_a, use_cache=True)
    sm._cached_parse_jsonl(src_b, use_cache=True)
    assert len(list((tmp_path / "parse").glob("*.pkl"))) == 2
    # Touch A to cause a prune on A's blobs only.
    import os
    stat = src_a.stat()
    os.utime(src_a, (stat.st_atime, stat.st_mtime + 2))
    sm._cached_parse_jsonl(src_a, use_cache=True)
    blobs = list((tmp_path / "parse").glob("*.pkl"))
    # Still 2 blobs: A (new) + B (untouched).
    assert len(blobs) == 2
    # B's blob must still be intact and loadable.
    entries_b = sm._cached_parse_jsonl(src_b, use_cache=True)
    assert isinstance(entries_b, list)


def test_cached_parse_prune_failure_is_non_fatal(tmp_path, monkeypatch):
    """An OSError during prune must not propagate — entries are still returned."""
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: tmp_path / "parse")
    src = tmp_path / "mini.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    # Prime cache, then bump mtime to trigger a cache miss + write + prune.
    sm._cached_parse_jsonl(src, use_cache=True)
    import os
    stat = src.stat()
    os.utime(src, (stat.st_atime, stat.st_mtime + 2))
    # Make the parse dir read-only so glob() raises OSError.
    cache_dir = tmp_path / "parse"
    cache_dir.chmod(0o500)
    try:
        result = sm._cached_parse_jsonl(src, use_cache=True)
        # Must still return parsed entries — prune failure is non-fatal.
        assert isinstance(result, list)
        assert len(result) > 0
    finally:
        cache_dir.chmod(0o700)


# ─── _prune_cache_global ─────────────────────────────────────────────────────

def test_prune_cache_global_creates_sentinel(tmp_path, monkeypatch):
    """First run writes the sentinel file."""
    cache_dir = tmp_path / "parse"
    cache_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    sm._prune_cache_global(cache_dir)
    assert (cache_dir / ".prune_last_run").exists()


def test_prune_cache_global_skips_within_24h(tmp_path, monkeypatch):
    """Fresh sentinel → prune body is skipped; orphaned blobs are untouched."""
    cache_dir = tmp_path / "parse"
    cache_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    orphan = cache_dir / "deadbeef__aabbccdd__123456789__1.1.0.pkl"
    orphan.write_bytes(b"")
    (cache_dir / ".prune_last_run").touch()  # mark as recently run
    sm._prune_cache_global(cache_dir)
    assert orphan.exists()


def test_prune_cache_global_deletes_orphaned_blob(tmp_path, monkeypatch):
    """Blob whose stem matches no live JSONL is deleted."""
    cache_dir = tmp_path / "parse"
    cache_dir.mkdir()
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    orphan = cache_dir / "deadbeef__aabbccdd__123456789__1.1.0.pkl"
    orphan.write_bytes(b"")
    sm._prune_cache_global(cache_dir)
    assert not orphan.exists()


def test_prune_cache_global_keeps_fresh_blob(tmp_path, monkeypatch):
    """Blob younger than 30 days is kept regardless of JSONL age."""
    cache_dir = tmp_path / "parse"
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: cache_dir)
    projects_dir = tmp_path / "projects"
    slug_dir = projects_dir / "myproject"
    slug_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    src = slug_dir / "session.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    sm._cached_parse_jsonl(src, use_cache=True)
    sm._prune_cache_global(cache_dir)
    assert len(list(cache_dir.glob("*.pkl"))) == 1


def test_prune_cache_global_keeps_old_blob_for_active_session(tmp_path, monkeypatch):
    """Blob > 30 d old but JSONL mtime < 60 d → session still active → keep."""
    import os as _os
    cache_dir = tmp_path / "parse"
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: cache_dir)
    projects_dir = tmp_path / "projects"
    slug_dir = projects_dir / "myproject"
    slug_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    src = slug_dir / "session.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    sm._cached_parse_jsonl(src, use_cache=True)
    blob = next(cache_dir.glob("*.pkl"))
    past = blob.stat().st_mtime - 35 * 86400
    _os.utime(blob, (past, past))  # blob is 35 d old
    sm._prune_cache_global(cache_dir)
    assert blob.exists()  # JSONL is fresh, so keep


def test_prune_cache_global_deletes_inactive_session_blob(tmp_path, monkeypatch):
    """Blob > 30 d old AND JSONL mtime > 60 d → session inactive → delete."""
    import os as _os
    cache_dir = tmp_path / "parse"
    monkeypatch.setattr(sm, "_parse_cache_dir", lambda: cache_dir)
    projects_dir = tmp_path / "projects"
    slug_dir = projects_dir / "myproject"
    slug_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sm, "_PROJECTS_DIR_OVERRIDE", None)
    src = slug_dir / "session.jsonl"
    src.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    sm._cached_parse_jsonl(src, use_cache=True)
    blob = next(cache_dir.glob("*.pkl"))
    anchor = blob.stat().st_mtime
    _os.utime(blob, (anchor - 35 * 86400, anchor - 35 * 86400))  # blob 35 d old
    _os.utime(src,  (anchor - 65 * 86400, anchor - 65 * 86400))  # JSONL 65 d old
    sm._prune_cache_global(cache_dir)
    assert not blob.exists()


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
    assert 'class="chartrail-card"' in html   # chartrail replaces Highcharts 3D in detail
    assert "Highcharts"             not in html
    assert 'id="session-blocks"'    not in html
    assert 'class="cards"'          not in html
    assert 'href="dashboard.html"'  in html


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

