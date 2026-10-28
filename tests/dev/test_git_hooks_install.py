"""The local lint gate must stay self-installing.

CI runs the same hooks, so a clone that silently stops installing them goes
red on push instead of on commit. These pin the two places that wire it up.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pre_commit_config_installs_the_commit_and_push_hooks() -> None:
    """A plain ``pre-commit install`` has to wire the push gate as well."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())

    assert set(config["default_install_hook_types"]) == {"pre-commit", "pre-push"}


def test_hooks_run_at_both_stages() -> None:
    """No hook opts out of the push stage, or the push gate would be weaker than CI."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    assert "default_stages" not in config

    narrowed = [
        hook["id"]
        for repo in config["repos"]
        for hook in repo["hooks"]
        if "stages" in hook and "pre-push" not in hook["stages"]
    ]
    assert narrowed == [], f"hooks that skip the push stage: {narrowed}"


def test_just_ensure_installs_the_hooks(tmp_path: Path) -> None:
    just = shutil.which("just")
    if just is None:
        pytest.skip("requires just")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    for name in ("uv", "bundle", "omnidev", "cargo"):
        shim = commands / name
        shim.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['HOOK_TEST_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        shim.chmod(0o755)
    subprocess.run(
        [just, "--justfile", str(_REPO_ROOT / "justfile"), "ensure"],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "PATH": f"{commands}{os.pathsep}{os.defpath}",
            "HOOK_TEST_LOG": str(log),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls.count(["run", "--no-sync", "pre-commit", "install"]) == 1
