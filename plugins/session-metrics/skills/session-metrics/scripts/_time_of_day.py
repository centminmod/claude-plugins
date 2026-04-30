"""Time-of-day analysis helpers for session-metrics."""
from datetime import datetime, timezone


_TOD_PERIODS = (
    ("night",     0,  6),   # 00:00–05:59
    ("morning",   6, 12),   # 06:00–11:59
    ("afternoon", 12, 18),  # 12:00–17:59
    ("evening",   18, 24),  # 18:00–23:59
)


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
