"""Opt-in e2e for the v3 cost advisor (per-turn brain-model selection) on polly.

Skipped unconditionally: cost advisor tests require real model
inference (the judge is a live LLM call), real subprocess
``omnigent run`` invocations against a local server, and the
dev-box Claude provider with configured tier models. These cannot
be driven by a mock LLM because the test spawns ``omnigent run``
as a subprocess (not via the session API), so the mock server is
not in the request path.

Usage (with real LLM, opt-in)::

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \\
        tests/e2e/test_polly_cost_advisor_e2e.py -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "polly cost-advisor e2e requires real LLM judge calls, real "
        "subprocess ``omnigent run`` invocations, and the dev-box Claude "
        "provider with configured tier models; not feasible under mock LLM. "
        "Opt in with OMNIGENT_E2E_POLLY=1."
    ),
)


def test_advise_mode_sizes_trivial_cheap_and_hard_expensive() -> None:
    """Placeholder -- skipped; requires real polly + real LLM judge."""


def test_optimize_mode_runs_turn_on_verdict_model() -> None:
    """Placeholder -- skipped; requires real polly + real LLM judge."""


def test_run_model_flag_is_spec_default_not_session_pin() -> None:
    """Placeholder -- skipped; requires real polly + real LLM judge."""
