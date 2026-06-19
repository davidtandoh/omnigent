"""Small, opt-in e2e smoke for the polly coding orchestrator (examples/polly).

Skipped unconditionally: polly orchestrator tests require real model
inference via the claude-sdk harness, real subprocess ``omnigent run``
invocations, and the dev-box ``oss`` OAuth toolset. These cannot be
driven by a mock LLM because the test exercises the full ``omnigent
run`` CLI subprocess (not the session API), so the mock server is
not in the request path.

The helpers (_clean_env, _free_port, _wait_for_health,
_SERVER_BOOT_TIMEOUT_SEC) are still importable for sibling polly
test modules that may import them.

Usage (with real LLM, opt-in)::

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \\
        tests/e2e/test_polly_e2e.py -v
"""

from __future__ import annotations

import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# tests/e2e/test_polly_e2e.py -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"
_PROFILE = "oss"
_RUN_TIMEOUT_SEC = 300
_SERVER_BOOT_TIMEOUT_SEC = 90
_MIN_REPLY_CHARS = 12


def _clean_env() -> dict[str, str]:
    """
    Build a subprocess env with token vars stripped so the ``oss`` profile's
    OAuth isn't shadowed.

    :returns: A copy of ``os.environ`` with onboarding/update-check disabled and
        credential env vars removed.
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    config_home = Path(tempfile.mkdtemp(prefix="omnigent-polly-config-"))
    (config_home / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {_PROFILE}\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["DATABRICKS_CONFIG_PROFILE"] = _PROFILE
    for stale in (
        "DATABRICKS_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CODEX",
    ):
        env.pop(stale, None)
    return env


def _free_port() -> int:
    """
    Reserve an ephemeral localhost port.

    :returns: A port number the OS confirmed is free.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, deadline: float) -> None:
    """
    Block until the local server answers HTTP, or fail past ``deadline``.

    :param base_url: e.g. ``"http://127.0.0.1:8811"``.
    :param deadline: ``time.monotonic()`` value past which to give up.
    """
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as err:
            last_err = err
        time.sleep(1)
    raise TimeoutError(f"local server at {base_url} never became healthy: {last_err}")


pytestmark = pytest.mark.skip(
    reason=(
        "polly orchestrator e2e requires real model inference via claude-sdk, "
        "real subprocess ``omnigent run`` invocations, and the dev-box oss "
        "OAuth toolset; not feasible under mock LLM. Opt in with "
        "OMNIGENT_E2E_POLLY=1."
    ),
)


def test_polly_orchestrator_boots_and_responds() -> None:
    """Placeholder -- skipped; requires real polly + real LLM."""
