"""Timezone helpers for session-metrics."""
import argparse
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
        "note":            "unofficial — community-reported",
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
