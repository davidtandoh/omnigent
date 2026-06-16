"""E2E test: cancel-and-recover user journey.

Exercises the full cancel → recover lifecycle:

1. Send a long-running message to an agent.
2. Cancel mid-response.
3. Verify a cancellation marker is appended to the conversation.
4. Send a follow-up with a distinctive codeword.
5. Verify the agent responds normally (codeword echoed back).
6. Verify the session history contains the expected items.

This validates that cancellation does not corrupt conversation state
and that subsequent turns complete cleanly.

Usage::

    pytest tests/e2e/test_journey_cancel_recover.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_until_terminal,
    send_user_message_to_session,
)


def _wait_for_in_progress(
    client: httpx.Client,
    response_id: str,
    timeout: float = 60,
) -> None:
    """Poll until the response transitions to ``in_progress``.

    :param client: HTTP client.
    :param response_id: The response ID to poll.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If not in_progress within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/responses/{response_id}")
        body = resp.json()
        status = body.get("status")
        if status == "in_progress":
            return
        if status in ("completed", "failed", "cancelled"):
            raise AssertionError(f"Response reached terminal state {status} before in_progress")
        time.sleep(0.3)
    raise AssertionError(f"Response {response_id} didn't reach in_progress within {timeout}s")


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all output_text blocks from a response body.

    :param body: The terminal response body.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


@pytest.mark.llm_flaky(reruns=2)
def test_cancel_and_recover_journey(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """Full journey: send -> cancel -> verify marker -> recover -> verify clean state.

    Validates that a cancelled turn does not corrupt the conversation
    and that a follow-up turn with a distinctive codeword completes
    normally, proving the agent can still process new input after
    cancellation.

    **What breaks if wrong:**

    - If cancellation leaves dangling function_call items without
      synthetic outputs, the follow-up turn fails with an LLM 400.
    - If the cancellation marker is missing, the agent has no context
      about the interruption and the history is incomplete.
    - If session state is corrupted, the recovery turn fails or
      produces garbage output.

    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: Name of the registered archer agent.
    :param live_runner_id: Runner id for session binding.
    """
    # ── Step 1: Create a runner-bound session ──────────────────
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # ── Step 2: Send a message that will take a while ──────────
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Write a detailed 2000-word essay about the history "
            "of the Roman Republic, covering all major political "
            "figures and key events from 509 BC to 27 BC."
        ),
    )

    # ── Step 3: Wait for in_progress, then cancel ──────────────
    # The response may complete before we poll — on fast LLMs the
    # 2000-word essay finishes instantly.  When that happens we skip
    # the cancel and still validate recovery below.
    caught = _wait_for_in_progress(http_client, response_id, timeout=60)
    if caught:
        cancel_resp = http_client.post(
            f"/v1/responses/{response_id}/cancel",
        )
        cancel_resp.raise_for_status()
        assert cancel_resp.json()["status"] == "cancelled"

    # ── Step 4: Verify session history so far ─────────────────
    items_resp = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"order": "desc", "limit": 10},
    )
    items_resp.raise_for_status()
    items = items_resp.json()["data"]
    if caught:
        cancellation_items = [
            item
            for item in items
            if item.get("type") == "message"
            and item.get("role") == "user"
            and any("interrupted" in c.get("text", "") for c in item.get("content", []))
        ]
        assert len(cancellation_items) == 1, (
            f"Expected 1 cancellation marker, found {len(cancellation_items)}."
        )

    # ── Step 5: Send a recovery message with a codeword ────────
    codeword = "phoenix-delta-88"
    recovery_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Never mind the essay. Just remember this codeword and "
            f"repeat it back to me: {codeword}"
        ),
    )

    # ── Step 6: Poll until the recovery turn completes ─────────
    recovery_body = poll_until_terminal(
        http_client,
        recovery_id,
        timeout=120,
    )
    assert recovery_body["status"] == "completed", (
        f"Recovery turn failed: status={recovery_body['status']!r}, "
        f"error={recovery_body.get('error')}"
    )

    # ── Step 7: Verify the agent echoed the codeword ───────────
    recovery_text = _extract_all_text(recovery_body)
    assert codeword in recovery_text.lower(), (
        f"Expected the agent to echo back '{codeword}'. Got: {recovery_text[:500]}"
    )

    # ── Step 8: Verify full session history ────────────────────
    # Fetch all items and verify the expected sequence:
    #   1. Original user message (essay request)
    #   2. (Partial) assistant response (may be empty or truncated)
    #   3. Cancellation marker (user message with "interrupted")
    #   4. Recovery user message (codeword request)
    #   5. Assistant response (echoes codeword)
    final_items_resp = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 20},
    )
    final_items_resp.raise_for_status()
    final_items = final_items_resp.json()["data"]

    # Extract user messages to verify ordering.
    user_messages = [
        item
        for item in final_items
        if item.get("type") == "message" and item.get("role") == "user"
    ]
    # We expect at least 3 user-role messages: original, cancel marker, recovery.
    assert len(user_messages) >= 3, (
        f"Expected at least 3 user messages (original + cancel marker + recovery), "
        f"found {len(user_messages)}. Messages: {user_messages}"
    )

    # The cancel marker should appear between the original and recovery messages.
    cancel_marker_indices = [
        i
        for i, item in enumerate(final_items)
        if item.get("type") == "message"
        and item.get("role") == "user"
        and any("interrupted" in c.get("text", "") for c in item.get("content", []))
    ]
    recovery_indices = [
        i
        for i, item in enumerate(final_items)
        if item.get("type") == "message"
        and item.get("role") == "user"
        and any(codeword in c.get("text", "") for c in item.get("content", []))
    ]
    assert cancel_marker_indices, "Cancellation marker not found in final history"
    assert recovery_indices, "Recovery message not found in final history"
    assert cancel_marker_indices[0] < recovery_indices[0], (
        "Cancellation marker should appear before recovery message in history"
    )

    # Verify at least one assistant message exists after the recovery message.
    assistant_messages_after_recovery = [
        item
        for i, item in enumerate(final_items)
        if i > recovery_indices[0]
        and item.get("type") == "message"
        and item.get("role") == "assistant"
    ]
    assert assistant_messages_after_recovery, (
        "Expected at least one assistant message after the recovery message"
    )
