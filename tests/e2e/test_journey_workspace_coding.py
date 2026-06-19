"""E2E test: "terminal coding session" user journey.

These tests exercise real tmux terminal interaction (launch, send,
read, file creation via terminal). The terminal tools dispatch to
a live tmux subprocess whose output is non-deterministic and cannot
be meaningfully driven by a mock LLM.

Skipped unconditionally: terminal/tmux workspace coding tests
require a real LLM to drive the terminal tools and interpret
shell output. A mock LLM cannot exercise this code path.

Usage (with real LLM)::

    pytest tests/e2e/test_journey_workspace_coding.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "workspace coding tests require real tmux interaction and a real "
        "LLM to drive terminal tools; not feasible under mock LLM"
    ),
)


def test_terminal_coding_session_journey() -> None:
    """Placeholder -- skipped; requires real tmux + real LLM."""
