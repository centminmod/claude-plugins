"""JSON rendering helpers for session-metrics."""
import json
import sys
from datetime import datetime, timezone


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


# Fields redacted from JSON exports under ``--redact-user-prompts``. These
# carry freeform user / assistant text that may contain PII; ``slash_command``
# and tool input previews are canonical / structured and stay visible so the
# export remains useful for cost analysis.
_REDACTED_TURN_FIELDS = (
    "prompt_text", "prompt_snippet",
    "assistant_text", "assistant_snippet",
)
_REDACTED_PLACEHOLDER = "[redacted]"


def _redact_turns_for_json(sessions: list[dict]) -> list[dict]:
    """Return a shallow copy of ``sessions`` with freeform prompt/assistant
    text replaced by ``[redacted]`` on every turn. Empty fields stay empty so
    downstream filters (``if t.get("prompt_text"):``) keep their meaning."""
    out = []
    for s in sessions:
        new_turns = []
        for t in s.get("turns", []):
            redacted = {**t}
            for fld in _REDACTED_TURN_FIELDS:
                if redacted.get(fld):
                    redacted[fld] = _REDACTED_PLACEHOLDER
            new_turns.append(redacted)
        out.append({**s, "turns": new_turns})
    return out


def render_json(report: dict, *, redact_user_prompts: bool = False) -> str:
    """Render the full report as indented JSON.

    Internal ``epoch_secs`` lists in ``time_of_day`` sections are converted to
    ISO-8601 ``utc_timestamps`` for human readability.  The transform uses a
    shallow copy of the report — session turns, subtotals, and model dicts are
    shared by reference to avoid copying ~thousands of turn record dicts.

    ``redact_user_prompts`` masks ``prompt_text`` / ``prompt_snippet`` and
    ``assistant_text`` / ``assistant_snippet`` on every turn with
    ``[redacted]`` so the JSON is safe to share publicly. Tool inputs,
    slash-command names, and structured cost / token fields stay visible.
    Instance-scope JSON has no per-turn records, so the flag is a no-op
    there.
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
        sessions = export["sessions"]
        if redact_user_prompts:
            sessions = _redact_turns_for_json(sessions)
        export["sessions"] = [
            {**s, "time_of_day": _tod_for_json(s["time_of_day"])}
            if "time_of_day" in s else s
            for s in sessions
        ]
    return json.dumps(export, indent=2)


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
