"""User-prompt detection helpers for session-metrics."""
from _dt import _parse_iso_dt


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
