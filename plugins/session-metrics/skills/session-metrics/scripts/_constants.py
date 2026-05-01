"""Shared literals consumed at function-def time.

Constants imported here cannot be replaced with `_sm()._NAME` lookups because
they are referenced as default-argument values (`def fn(x: int = _NAME)`),
which Python evaluates at def-time — before `_sm()` (the orchestrator
back-reference) is wired up. Keep this leaf dependency-free so it can be the
first one `session-metrics.py` loads.
"""

# Cache-break threshold: any single turn with
# ``input_tokens + cache_write_tokens > _CACHE_BREAK_DEFAULT_THRESHOLD`` is
# flagged. Matches the Anthropic session-report default. Override via
# ``--cache-break-threshold`` on the CLI; runtime reads in function bodies
# go through ``_sm()._CACHE_BREAK_DEFAULT_THRESHOLD`` so tests can monkeypatch.
_CACHE_BREAK_DEFAULT_THRESHOLD = 100_000
