"""Drift guard for the policy-ASK "block forever" contract.

A policy ASK is a human-in-the-loop gate: the agent must block until a
human answers and must NEVER proceed on its own. Every wait-for-a-human
budget on the path is therefore pinned to ``INT_MAX`` seconds (~68 years),
effectively infinite — the policy's ``ask_timeout`` is the only real cap,
and every plumbing layer (hook subprocess timeout, client httpx budget,
server-side mirror) is held at the same value so no layer caps the wait
before the policy does.

This was the cost-policy bug: a finite window (originally 30s) let the
gate lapse and the agent ran sub-agent tool calls unapproved. If any one
of these constants is reverted to a finite value, this test fails loudly
and points at the layer that would cap the wait first.

The deliberately-short timeouts are intentionally NOT covered here, because
they are not policy-ASK gates: the ``AskUserQuestion`` hook (10s, TUI
fallthrough), connect timeouts (30s), and the Claude session-rotation
budget (70s). See POLICIES.md §7, §13.
"""

import omnigent.claude_native_hook as claude_native_hook
import omnigent.codex_native_app_server as codex_native_app_server
import omnigent.codex_native_forwarder as codex_native_forwarder
import omnigent.codex_native_hook as codex_native_hook
import omnigent.runner.app as runner_app
import omnigent.runner.pending_approvals as pending_approvals
import omnigent.runner.tool_dispatch as tool_dispatch
import omnigent.runtime.harnesses._scaffold as scaffold
import omnigent.server.routes.sessions as sessions
from omnigent.spec.types import DEFAULT_ASK_TIMEOUT

# INT_MAX (2**31 - 1). Chosen over ``math.inf`` because it must stay a
# valid JSON integer for Claude Code's command-hook ``timeout`` setting
# while also working as an asyncio/httpx timeout.
INT_MAX = 2_147_483_647


def test_policy_ask_default_timeout_is_infinite() -> None:
    """The spec-wide default ASK timeout blocks effectively forever."""
    assert DEFAULT_ASK_TIMEOUT == INT_MAX


def test_native_ask_plumbing_timeouts_are_infinite() -> None:
    """Every native-path wait-for-a-human budget is pinned to INT_MAX.

    If any single layer regresses to a finite value it becomes the cap
    that fires first, and a gated tool call runs before the human answers.
    """
    assert claude_native_hook._PERMISSION_TIMEOUT_S == INT_MAX
    assert claude_native_hook._EVALUATE_POLICY_TIMEOUT_S == INT_MAX
    assert sessions._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S == INT_MAX

    assert codex_native_hook._EVALUATE_POLICY_TIMEOUT_S == INT_MAX
    assert codex_native_app_server._POLICY_HOOK_TIMEOUT_SECONDS == INT_MAX
    assert codex_native_forwarder._CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS == INT_MAX
    assert sessions._CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S == INT_MAX


def test_sdk_policy_eval_timeout_is_infinite() -> None:
    """The SDK (non-native) round-trip gate also blocks forever."""
    assert scaffold._POLICY_EVAL_TIMEOUT_S == INT_MAX


def test_native_ask_lockstep_ordering_invariants() -> None:
    """The plumbing budgets never cap before the layer they wrap.

    Equality at INT_MAX satisfies these today; the explicit ``>=`` checks
    document the ordering the design depends on so a future change that
    keeps them finite-but-mismatched still fails here.
    """
    # Codex: the subprocess timeout codex reads from hooks.json must
    # outlast the hook's own AP request budget, or codex kills the hook
    # mid-park and the tool runs before the verdict arrives.
    assert (
        codex_native_app_server._POLICY_HOOK_TIMEOUT_SECONDS
        >= codex_native_hook._EVALUATE_POLICY_TIMEOUT_S
    )
    # Codex: the client long-poll budget must not undercut the server's
    # wait, so the server's own resolution wins over a client cut.
    assert (
        codex_native_forwarder._CODEX_ELICITATION_REQUEST_TIMEOUT_SECONDS
        >= sessions._CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S
    )
    # Claude: client httpx budget and server-side mirror stay in lockstep.
    assert (
        claude_native_hook._PERMISSION_TIMEOUT_S
        == sessions._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S
    )


def test_runner_ask_gate_delivery_timeouts_are_infinite() -> None:
    """Runner→server delivery POSTs that PARK behind a human ASK wait forever.

    These are the message/event-delivery clients whose connection a
    server-side ASK gate parks on. A short read timeout here severed the
    parked gate before the human answered, then fail-closed to DENY and
    retried — the regression that produced both the auto-resolved card AND
    the duplicate approval cards:

    * ``pending_approvals._DEFAULT_WAIT_SECONDS`` — the relay/MCP approval
      park default (was 120s → auto-refuse at 2 min).
    * ``runner.app`` policy-eval + sub-agent wake-notice delivery POSTs
      (were 30s; the wake POST retried each timeout → duplicate cards).
    * ``runner.tool_dispatch`` message-send POSTs to a child/target session
      (were 30s).

    They must hold the READ budget at INT_MAX so the deliverer waits for the
    real verdict, while keeping a fast CONNECT so an unreachable server still
    fails out promptly.
    """
    assert pending_approvals._DEFAULT_WAIT_SECONDS == INT_MAX

    assert runner_app._ASK_GATE_DELIVERY_READ_TIMEOUT_S == INT_MAX
    assert runner_app._ASK_GATE_DELIVERY_TIMEOUT.read == INT_MAX
    assert runner_app._ASK_GATE_DELIVERY_TIMEOUT.connect == 30.0

    assert tool_dispatch._ASK_GATE_DELIVERY_READ_TIMEOUT_S == INT_MAX
    assert tool_dispatch._ASK_GATE_DELIVERY_TIMEOUT.read == INT_MAX
    assert tool_dispatch._ASK_GATE_DELIVERY_TIMEOUT.connect == 30.0
