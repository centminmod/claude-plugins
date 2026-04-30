"""General-purpose datetime utility for session-metrics."""
from datetime import datetime


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
