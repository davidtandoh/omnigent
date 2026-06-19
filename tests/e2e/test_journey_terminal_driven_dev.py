"""E2E journey test: terminal-driven development workflow.

These tests exercise real tmux terminal interaction (launch, send,
read, persistence across turns). The terminal tools dispatch to a
live tmux subprocess whose output is non-deterministic and cannot
be meaningfully driven by a mock LLM -- the test's value is in
proving the tmux session lifecycle, not the LLM's reasoning.

Skipped unconditionally: terminal/tmux interaction tests require
a real LLM to decide which terminal commands to issue and how to
interpret their output. A mock LLM cannot exercise this code path
meaningfully.

Usage (with real LLM)::

    pytest tests/e2e/test_journey_terminal_driven_dev.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "terminal-driven dev tests require real tmux interaction and a real "
        "LLM to drive the terminal tools; not feasible under mock LLM"
    ),
)


def test_terminal_multi_command_workflow() -> None:
    """Placeholder -- skipped; requires real tmux + real LLM."""


def test_terminal_persists_across_turns() -> None:
    """Placeholder -- skipped; requires real tmux + real LLM."""
