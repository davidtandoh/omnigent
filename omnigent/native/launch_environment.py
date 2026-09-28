"""Explicit per-launch environment for native terminal wrappers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping

NATIVE_LAUNCH_ENV_VAR = "OMNIGENT_NATIVE_LAUNCH_ENV"

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_ENTRIES = 64
_MAX_KEY_BYTES = 128
_MAX_VALUE_BYTES = 32 * 1024
_MAX_TOTAL_BYTES = 64 * 1024


class NativeLaunchEnvironmentError(ValueError):
    """An explicit native-launch environment does not satisfy the contract."""


def parse_native_launch_environment(entries: Iterable[str]) -> dict[str, str]:
    """Parse repeatable ``KEY=VALUE`` options without reading ambient values."""
    parsed: dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        if "=" not in entry:
            raise NativeLaunchEnvironmentError(f"--env entry {index} must use KEY=VALUE syntax")
        key, value = entry.split("=", 1)
        if key in parsed:
            raise NativeLaunchEnvironmentError(f"--env repeats variable {key!r}")
        parsed[key] = value
    return validate_native_launch_environment(parsed)


def validate_native_launch_environment(
    values: Mapping[str, object] | None,
) -> dict[str, str]:
    """Validate and copy a launch environment without including values in errors."""
    if values is None:
        return {}
    if len(values) > _MAX_ENTRIES:
        raise NativeLaunchEnvironmentError(
            f"native launch environment has more than {_MAX_ENTRIES} variables"
        )

    validated: dict[str, str] = {}
    total_bytes = 0
    for index, (raw_key, raw_value) in enumerate(values.items(), start=1):
        if not isinstance(raw_key, str) or not _ENV_NAME_RE.fullmatch(raw_key):
            raise NativeLaunchEnvironmentError(
                f"native launch environment variable {index} has an invalid name"
            )
        if len(raw_key.encode("utf-8")) > _MAX_KEY_BYTES:
            raise NativeLaunchEnvironmentError(
                f"native launch environment variable {raw_key!r} has a name longer "
                f"than {_MAX_KEY_BYTES} bytes"
            )
        if not isinstance(raw_value, str):
            raise NativeLaunchEnvironmentError(
                f"native launch environment variable {raw_key!r} must have a string value"
            )
        if "\x00" in raw_value:
            raise NativeLaunchEnvironmentError(
                f"native launch environment variable {raw_key!r} contains a NUL byte"
            )
        value_bytes = len(raw_value.encode("utf-8"))
        if value_bytes > _MAX_VALUE_BYTES:
            raise NativeLaunchEnvironmentError(
                f"native launch environment variable {raw_key!r} exceeds {_MAX_VALUE_BYTES} bytes"
            )
        total_bytes += len(raw_key.encode("utf-8")) + value_bytes
        if total_bytes > _MAX_TOTAL_BYTES:
            raise NativeLaunchEnvironmentError(
                f"native launch environment exceeds {_MAX_TOTAL_BYTES} bytes"
            )
        validated[raw_key] = raw_value
    return validated


def encode_native_launch_environment(values: Mapping[str, object] | None) -> str:
    """Return a canonical runner envelope for a validated launch environment."""
    return json.dumps(
        validate_native_launch_environment(values),
        sort_keys=True,
        separators=(",", ":"),
    )


_installed_native_launch_environment: dict[str, str] = {}


def install_native_launch_environment_from_process() -> None:
    """Consume the daemon envelope before any terminal child can inherit it."""
    raw = os.environ.pop(NATIVE_LAUNCH_ENV_VAR, None)
    if raw is None:
        values: object = {}
    else:
        try:
            values = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise NativeLaunchEnvironmentError(
                "native launch environment envelope is not valid JSON"
            ) from exc
    if not isinstance(values, dict):
        raise NativeLaunchEnvironmentError(
            "native launch environment envelope must contain an object"
        )
    global _installed_native_launch_environment
    _installed_native_launch_environment = validate_native_launch_environment(values)


def native_launch_environment() -> dict[str, str]:
    """Return a copy of the explicit environment installed for this runner."""
    return dict(_installed_native_launch_environment)
