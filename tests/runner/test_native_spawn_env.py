"""Tests for native child-harness spawn environment propagation."""

from __future__ import annotations

import os

import pytest

from omnigent.claude_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    build_claude_native_spawn_env,
)
from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_DIR_ENV_VAR,
    CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    build_codex_native_spawn_env,
)
from omnigent.runner.app import _merge_spawn_env


def test_claude_native_spawn_env_merges_bridge_and_provider_config() -> None:
    """Bridge/session ids survive alongside provider/ucode routing env."""
    provider_env = {
        "HARNESS_CLAUDE_NATIVE_GATEWAY": "true",
        "HARNESS_CLAUDE_NATIVE_ENV_ANTHROPIC_BASE_URL": "https://gw.example/anthropic",
        "HARNESS_CLAUDE_NATIVE_API_KEY_HELPER": "databricks auth token --profile DEFAULT",
        "HARNESS_CLAUDE_NATIVE_MODEL": "databricks-claude-opus-4-8",
    }

    env = _merge_spawn_env(
        provider_env,
        build_claude_native_spawn_env("conv_123", bridge_id="bridge_123"),
    )

    assert env is not None
    assert env["HARNESS_CLAUDE_NATIVE_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_NATIVE_ENV_ANTHROPIC_BASE_URL"] == "https://gw.example/anthropic"
    assert env["HARNESS_CLAUDE_NATIVE_API_KEY_HELPER"] == "databricks auth token --profile DEFAULT"
    assert env["HARNESS_CLAUDE_NATIVE_MODEL"] == "databricks-claude-opus-4-8"
    assert env[REQUEST_SESSION_ID_ENV_VAR] == "conv_123"
    assert BRIDGE_DIR_ENV_VAR in env


def test_codex_native_spawn_env_merges_bridge_and_provider_config() -> None:
    """Bridge/session ids survive alongside Codex provider routing env."""
    provider_env = {
        "HARNESS_CODEX_NATIVE_GATEWAY": "true",
        "HARNESS_CODEX_NATIVE_DATABRICKS_PROFILE": "DEFAULT",
        "HARNESS_CODEX_NATIVE_MODEL": "databricks-gpt-5",
    }

    env = _merge_spawn_env(
        provider_env,
        build_codex_native_spawn_env("conv_123", bridge_id="bridge_123"),
    )

    assert env is not None
    assert env["HARNESS_CODEX_NATIVE_GATEWAY"] == "true"
    assert env["HARNESS_CODEX_NATIVE_DATABRICKS_PROFILE"] == "DEFAULT"
    assert env["HARNESS_CODEX_NATIVE_MODEL"] == "databricks-gpt-5"
    assert env[CODEX_NATIVE_REQUEST_SESSION_ID_ENV_VAR] == "conv_123"
    assert CODEX_NATIVE_BRIDGE_DIR_ENV_VAR in env


def test_claude_native_provider_env_rehydrates_terminal_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child harness env becomes Claude terminal env/apiKeyHelper/model config."""
    from omnigent.runner.app import _claude_native_config_from_env

    monkeypatch.setenv(
        "HARNESS_CLAUDE_NATIVE_API_KEY_HELPER", "databricks auth token --profile DEFAULT"
    )
    monkeypatch.setenv(
        "HARNESS_CLAUDE_NATIVE_ENV_ANTHROPIC_BASE_URL", "https://gw.example/anthropic"
    )
    monkeypatch.setenv("HARNESS_CLAUDE_NATIVE_MODEL", "databricks-claude-opus-4-8")

    config = _claude_native_config_from_env()

    assert config is not None
    assert config.env == {"ANTHROPIC_BASE_URL": "https://gw.example/anthropic"}
    assert config.api_key_helper == "databricks auth token --profile DEFAULT"
    assert config.model == "databricks-claude-opus-4-8"


def test_codex_native_provider_env_rehydrates_launch_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child harness env becomes Codex app-server profile/model/overrides."""
    from omnigent.runner.app import _codex_native_launch_from_env

    monkeypatch.setenv("HARNESS_CODEX_NATIVE_DATABRICKS_PROFILE", "DEFAULT")
    monkeypatch.setenv("HARNESS_CODEX_NATIVE_MODEL", "databricks-gpt-5")
    monkeypatch.setenv(
        "HARNESS_CODEX_NATIVE_CONFIG_OVERRIDES",
        '["model_provider=\\"omnigent_databricks\\""]',
    )

    launch = _codex_native_launch_from_env()

    assert launch is not None
    assert launch.profile == "DEFAULT"
    assert launch.model == "databricks-gpt-5"
    assert launch.config_overrides == ['model_provider="omnigent_databricks"']


def test_native_provider_env_absent_when_not_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HARNESS native routing keys means native CLIs keep their own config."""
    from omnigent.runner.app import _claude_native_config_from_env, _codex_native_launch_from_env

    for key in list(os.environ):
        if key.startswith(("HARNESS_CLAUDE_NATIVE", "HARNESS_CODEX_NATIVE")):
            monkeypatch.delenv(key, raising=False)

    assert _claude_native_config_from_env() is None
    assert _codex_native_launch_from_env() is None
