"""Read-only runtime admission receipt, independent of trace delivery."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from omnigent.native.state_files import read_regular_file

CLIENT_ENV_NAMES = frozenset(
    {
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_LOCAL_SERVER_PORT",
        "OMNIGENT_WEB_BASE_PATH",
    }
)


class LaunchContractError(ValueError):
    """A safe, stable refusal category with no captured output or secrets."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def runtime_identity() -> dict[str, object]:
    """Digest the actual executable and loaded package sources, not a version label."""
    import omnigent

    executable = Path(sys.executable).absolute().parent / "omnigent"
    root = Path(omnigent.__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        launcher = read_regular_file(executable, limit=65536)
        if (
            not os.access(executable, os.X_OK)
            or str(Path(sys.executable).parent).encode() not in launcher.splitlines()[0]
        ):
            raise LaunchContractError("runtime_mismatch")
        digest.update(launcher)
        files = sorted(root.rglob("*.py"))
        if not files or len(files) > 10000:
            raise LaunchContractError("runtime_unverified")
        total = 0
        for path in files:
            content = read_regular_file(path, limit=4 * 1024 * 1024)
            total += len(content)
            if total > 128 * 1024 * 1024:
                raise LaunchContractError("runtime_unverified")
            digest.update(str(path.relative_to(root)).encode() + b"\0" + content + b"\0")
    except (OSError, ValueError, IndexError) as exc:
        if isinstance(exc, LaunchContractError):
            raise
        raise LaunchContractError("runtime_unverified") from exc
    from omnigent.update_check import _read_installed_wheel_info

    installed = _read_installed_wheel_info()
    return {
        "executable": str(executable),
        "python": sys.executable,
        "generation": "sha256:" + digest.hexdigest(),
        "package_version": importlib.metadata.version("omnigent"),
        "source_commit": installed.commit_sha
        if installed is not None and not installed.is_editable
        else None,
        "state_schema": 1,
    }


def launch_contract(endpoint: str, *, probe: bool = True) -> dict[str, object]:
    """Publish the measured runtime and endpoint without starting any service."""
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid endpoint")
        _ = parsed.port
    except ValueError as exc:
        raise LaunchContractError("invalid_endpoint") from exc
    endpoint = endpoint.rstrip("/")
    availability = "unverified"
    if probe:
        try:
            response = httpx.get(
                endpoint + "/health", timeout=3.0, trust_env=False, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise LaunchContractError("endpoint_unavailable") from exc
        if response.status_code in {401, 403}:
            raise LaunchContractError("auth_denied")
        if response.status_code != 200:
            raise LaunchContractError("endpoint_unavailable")
        try:
            if response.json() != {"status": "ok"}:
                raise ValueError("foreign endpoint")
        except ValueError as exc:
            raise LaunchContractError("endpoint_mismatch") from exc
        availability = "ready"
    from omnigent.harness_plugins import harness_capabilities
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS

    capabilities = harness_capabilities()
    return {
        "schema_version": 1,
        "runtime": runtime_identity(),
        "endpoint": endpoint,
        "availability": availability,
        "allowed_client_environment": sorted(CLIENT_ENV_NAMES),
        "client_environment": {
            name: os.environ[name] for name in sorted(CLIENT_ENV_NAMES) if name in os.environ
        },
        "native_environment": {
            "interface": "--env KEY=VALUE",
            "max_entries": 64,
            "max_total_bytes": 65536,
            "ambient_forwarding": False,
        },
        "harnesses": [
            {
                "id": agent.harness,
                "command": agent.key,
                "capabilities": capabilities[agent.harness].as_dict()
                if agent.harness in capabilities
                else {},
                "durable_custody": False,
            }
            for agent in NATIVE_CODING_AGENTS
        ],
        "telemetry": {"owner": "external", "admission": "external_policy"},
    }
