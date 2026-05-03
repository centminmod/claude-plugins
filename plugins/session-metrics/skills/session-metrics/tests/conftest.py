"""Shared autouse fixtures for the session-metrics skill test suite.

Lifted out of the eight split ``test_*.py`` modules in v1.41.11. Before
the split (v1.41.7 and earlier) there was a single test file and the
fixtures lived alongside the tests; v1.41.8/v1.41.9 split the monolith
into 8 sibling modules and duplicated the fixtures into each — pytest
autouse fixtures only fire for tests in their declaring module, so the
duplication was load-bearing at the time. Eight copies of the same
five-line block is the threshold where the duplication becomes
maintenance debt, not safety; pytest auto-discovers this conftest.py
and applies its autouse fixtures to every test collected from this
directory tree, replacing the eight copies.

The ``sm`` reference inside ``_clear_pricing_cache`` is fetched lazily
via ``sys.modules["session_metrics"]`` rather than captured at conftest
import time. The reason: each test file under this directory loads the
skill via ``sm = sys.modules.get("session_metrics") or _load_module(...)``
— whichever file pytest collects first creates the canonical instance,
the others reuse it (the v1.41.8 module-aliasing fix). Capturing ``sm``
at conftest import would work in practice (conftest is collected before
any test module) but adds a hidden coupling to that ordering. Resolving
``sm`` at fixture-fire time is order-independent and makes the contract
explicit.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def isolate_projects_dir(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("real_projects_dir"):
        return
    safe = tmp_path / "_autouse_projects"
    safe.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(safe))


@pytest.fixture(autouse=True)
def _clear_pricing_cache():
    sm = sys.modules.get("session_metrics")
    if sm is not None:
        sm._pricing_for.cache_clear()
    yield
