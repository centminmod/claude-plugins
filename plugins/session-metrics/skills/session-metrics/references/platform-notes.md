# Platform notes — Windows, timezone data, and path quirks

`session-metrics` is stdlib-only Python 3.11+ and runs on macOS, Linux,
and Windows. This document captures the small number of platform-specific
wrinkles that are worth knowing before you hit them.

## Windows — IANA timezone names require `tzdata`

On macOS and Linux, Python's `zoneinfo` module reads the system tzdb at
`/usr/share/zoneinfo`. On Windows there is no system tzdb, so
`ZoneInfo("America/Los_Angeles")` raises `ZoneInfoNotFoundError` unless
the `tzdata` pip package is installed.

### How session-metrics behaves

**Default (no `--strict-tz`):** the parse emits a `[warn]` line to
`stderr` and falls back to UTC:

```
[warn] ZoneInfo not found for tz 'America/Los_Angeles'. On Windows,
install the 'tzdata' package (pip install tzdata) for IANA tz support.
Falling back to UTC.
```

The report still renders, but the hour-of-day / punchcard / time-of-day
buckets are computed in UTC — which may not be what you wanted.

**With `--strict-tz`:** the parse raises a hard error and exits with
code 2 instead of silently falling back. Use this when a silent UTC
fallback would be a correctness problem (e.g. CI pipelines, dashboards).

```bash
# Explicit error on Windows without tzdata:
python session-metrics.py --tz Europe/Berlin --strict-tz
```

### Fix

```bash
pip install tzdata
```

`tzdata` is a pure-Python IANA-tz wheel maintained by the Python core
team. After installing, IANA names resolve the same way they do on
macOS/Linux. No other config needed.

### Why this isn't a declared dependency

The plugin's `plugin.json` advertises "stdlib-only Python" and the
skill payload ships no `requirements.txt` / `pyproject.toml`. Declaring
`tzdata` would break that claim and require a different install path
on macOS/Linux (where the wheel would be redundant). The current
approach — cross-platform stdlib-only by default, with an actionable
error for Windows users who opt into IANA names — preserves the
zero-dependency posture.

## Fallback paths that do NOT need tzdata

These paths use `datetime.now().astimezone()` (reads the OS tz via
platform-native APIs) and work identically on all platforms with no
extra packages:

- Default timezone (no `--tz` / `--utc-offset` flag): uses system
  local tz.
- `--utc-offset` flag: takes a plain float (`-8`, `5.5`). No tz
  name resolution is performed; DST is not applied.

If you need DST-accurate bucketing on Windows without installing
`tzdata`, the system-local fallback still works — but only for the
local machine's timezone, not arbitrary IANA names.

## Timezone contract across outputs

See the `_resolve_tz` docstring in `scripts/session-metrics.py` for the
full contract. Summary:

- **Static exports** (text / JSON / CSV / MD, plus Highcharts-rendered
  PNGs): bucket every event against a single scalar offset captured
  once at parse time. This is intentional — consumers expect one tz
  label per report, not per-event astimezone() jitter.
- **HTML client-side charts** (uPlot / Chart.js): bucket using the
  same fixed offset, for consistency with the static path. (Earlier
  internal docstrings mentioned per-event `Intl.DateTimeFormat` —
  that design was not implemented; the static and client paths agree
  by design.)

The behaviour-lock regression test for this is
`test_hour_of_day_dst_boundary_uses_fixed_offset` in
`tests/test_session_metrics.py`.

## Path and filesystem notes

- JSONL discovery under `~/.claude/projects/` uses the project-slug
  derived from the current working directory. Windows uses
  `%USERPROFILE%\.claude\projects\` — no code change needed, but the
  slug derivation (replacing path separators with dashes) produces
  the same string on all platforms.
- Cache directory is `~/.claude/projects/<slug>/.session-metrics-cache/`
  on every platform. Cache blobs are gzipped JSON, platform-independent.
