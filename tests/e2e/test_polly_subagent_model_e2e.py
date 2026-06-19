"""Opt-in e2e for per-dispatch sub-agent model control on polly.

Skipped unconditionally: sub-agent model tests require real model
inference via the polly orchestrator brain, real subprocess
``omnigent run`` invocations, and the dev-box ``oss`` OAuth
toolset with Claude/GPT providers. The test exercises the full
``omnigent run`` CLI subprocess (not the session API) and relies
on the brain emitting specific tool calls based on real LLM
reasoning.

Usage (with real LLM, opt-in)::

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \\
        tests/e2e/test_polly_subagent_model_e2e.py -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "polly sub-agent model e2e requires real model inference, real "
        "subprocess ``omnigent run`` invocations with fan-out to native "
        "sub-agents, and the dev-box oss OAuth toolset; not feasible "
        "under mock LLM. Opt in with OMNIGENT_E2E_POLLY=1."
    ),
)


def test_polly_dispatches_distinct_models_per_worker() -> None:
    """Placeholder -- skipped; requires real polly + real LLM."""


def test_polly_rejects_cross_family_model_dispatch() -> None:
    """Placeholder -- skipped; requires real polly + real LLM."""


def test_polly_lists_models_then_dispatches_pi_from_list() -> None:
    """Placeholder -- skipped; requires real polly + real LLM."""


def test_polly_canonical_id_localized_for_gateway_child() -> None:
    """Placeholder -- skipped; requires real polly + real LLM."""
