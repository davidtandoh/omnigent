"""Contract tests for explicit native-wrapper launch variables."""

from __future__ import annotations

import json

import pytest

from omnigent.native import launch_environment
from omnigent.native.launch_environment import (
    NATIVE_LAUNCH_ENV_VAR,
    NativeLaunchEnvironmentError,
    encode_native_launch_environment,
    install_native_launch_environment_from_process,
    native_launch_environment,
    parse_native_launch_environment,
    validate_native_launch_environment,
)


def test_parse_native_launch_environment_preserves_empty_and_equals() -> None:
    assert parse_native_launch_environment(
        ["FM_HOME=/tmp/fm", "CLAUDE_ACCOUNT=", "SETTING=a=b"]
    ) == {
        "FM_HOME": "/tmp/fm",
        "CLAUDE_ACCOUNT": "",
        "SETTING": "a=b",
    }


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (["MISSING_EQUALS"], "KEY=VALUE"),
        (["INVALID-NAME=value"], "invalid name"),
        (["FM_TASK_ID=one", "FM_TASK_ID=two"], "repeats variable"),
    ],
)
def test_parse_native_launch_environment_rejects_malformed_or_duplicate_entries(
    entries: list[str], message: str
) -> None:
    with pytest.raises(NativeLaunchEnvironmentError, match=message):
        parse_native_launch_environment(entries)


@pytest.mark.parametrize(
    "values",
    [
        {"FM_TASK_ID": 7},
        {"FM_TASK_ID": "contains\0nul"},
        {f"KEY_{index}": "value" for index in range(65)},
    ],
)
def test_validate_native_launch_environment_rejects_unsafe_values(
    values: dict[str, object],
) -> None:
    secret = "sentinel-secret-value"
    values = {**values, "SECRET_SENTINEL": secret}
    with pytest.raises(NativeLaunchEnvironmentError) as exc_info:
        validate_native_launch_environment(values)
    assert secret not in str(exc_info.value)


def test_runner_envelope_is_canonical_consumed_and_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"FM_TASK_ID": "task-123", "CLEAR_OVERRIDE": ""}
    encoded = encode_native_launch_environment(values)
    assert encoded == json.dumps(values, sort_keys=True, separators=(",", ":"))

    monkeypatch.setenv(NATIVE_LAUNCH_ENV_VAR, encoded)
    monkeypatch.setattr(launch_environment, "_installed_native_launch_environment", {})
    install_native_launch_environment_from_process()

    assert NATIVE_LAUNCH_ENV_VAR not in launch_environment.os.environ
    assert native_launch_environment() == values


def test_no_ambient_environment_is_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNAPPROVED_SECRET", "must-not-cross-boundary")
    monkeypatch.delenv(NATIVE_LAUNCH_ENV_VAR, raising=False)
    monkeypatch.setattr(launch_environment, "_installed_native_launch_environment", {})

    install_native_launch_environment_from_process()

    assert native_launch_environment() == {}
