"""Exactly one attachment can claim a live custodian without foreign signals."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.runner.custody import CustodyError, SessionAttachment, SessionCustody


def context() -> dict[str, object]:
    return {
        "session_id": "conv_synthetic",
        "runner_id": "runner_synthetic",
        "harness": "codex-native",
        "host_id": "host_synthetic",
        "endpoint": "http://127.0.0.1:8123",
        "workspace": "/tmp",
        "config_digest": "a" * 64,
        "environment_digest": "b" * 64,
        "owner_runtime_generation": "sha256:" + "c" * 64,
        "config_files": [],
    }


def test_concurrent_and_repeated_adoption_are_exclusive(tmp_path: Path) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        first = SessionAttachment(tmp_path, "conv_synthetic", generation="host_generation_1")
        second = SessionAttachment(tmp_path, "conv_synthetic", generation="host_generation_2")
        with first:
            record = first.adopt(
                host_id="host_synthetic", endpoint="http://127.0.0.1:8123", config_digest="a" * 64
            )
            assert record.owner.pid == os.getpid()
            assert (
                first.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                ).owner_generation
                == record.owner_generation
            )
            with pytest.raises(CustodyError, match="owner_busy"):
                with second:
                    second.adopt(
                        host_id="host_synthetic",
                        endpoint="http://127.0.0.1:8123",
                        config_digest="a" * 64,
                    )
        with second:
            adopted = second.adopt(
                host_id="host_synthetic", endpoint="http://127.0.0.1:8123", config_digest="a" * 64
            )
            assert adopted.owner_generation == record.owner_generation
        assert owner.still_owner()


@pytest.mark.parametrize(
    "field,value,category",
    [
        ("host_id", "foreign", "scope_mismatch"),
        ("endpoint", "http://127.0.0.1:9999", "scope_mismatch"),
        ("config_digest", "d" * 64, "config_mismatch"),
    ],
)
def test_adoption_refuses_drift_without_mutation(
    tmp_path: Path, field: str, value: str, category: str
) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        before = owner.record_path.read_bytes()
        kwargs = {
            "host_id": "host_synthetic",
            "endpoint": "http://127.0.0.1:8123",
            "config_digest": "a" * 64,
            field: value,
        }
        with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
            with pytest.raises(CustodyError, match=category):
                attachment.adopt(**kwargs)
        assert owner.record_path.read_bytes() == before
        assert owner.still_owner()


def test_reused_pid_refuses_and_preserves_record(tmp_path: Path) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        record = json.loads(owner.record_path.read_bytes())
        record["owner"]["started_at"] += 1
        owner.record_path.write_text(json.dumps(record))
        before = owner.record_path.read_bytes()
        with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
            with pytest.raises(CustodyError, match="record_mismatch"):
                attachment.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                )
        assert owner.record_path.read_bytes() == before


def test_duplicate_custodian_cannot_launch_native_resources(tmp_path: Path) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        with pytest.raises(CustodyError, match="owner_busy"):
            with SessionCustody.create(tmp_path, context()):
                raise AssertionError("duplicate native launch was admitted")
        assert owner.still_owner()


def test_watchdog_preserves_custody_after_parent_death_and_ends_on_claim_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from omnigent.runner import _entry

    monkeypatch.setattr(_entry, "_parent_is_orphaned", lambda pid: True)
    observations = iter([True, True, False])
    shutdown = Mock()
    hard_exit = Mock()
    _entry._run_parent_death_killer(
        1,
        shutdown,
        owner_alive=lambda: next(observations),
        poll_interval_s=0,
        grace_s=0,
        exit_fn=hard_exit,
    )
    shutdown.assert_called_once()
    hard_exit.assert_called_once()


def test_foreign_socket_refuses_adoption_without_touching_owner(tmp_path: Path) -> None:
    import socket
    import tempfile

    with (
        SessionCustody.create(tmp_path, context()) as owner,
        tempfile.TemporaryDirectory(dir="/tmp", prefix="omg-sock-") as socket_dir,
    ):
        socket_path = Path(socket_dir) / "native.sock"
        with socket.socket(socket.AF_UNIX) as original:
            original.bind(str(socket_path))
            owner.publish_terminal("codex", "main", socket_path, os.getpid(), os.getpid())
            socket_path.rename(Path(socket_dir) / "original.sock")
            with socket.socket(socket.AF_UNIX) as foreign:
                foreign.bind(str(socket_path))
                with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
                    with pytest.raises(CustodyError, match="foreign_socket"):
                        attachment.adopt(
                            host_id="host_synthetic",
                            endpoint="http://127.0.0.1:8123",
                            config_digest="a" * 64,
                        )
                assert owner.still_owner()


def test_end_record_is_retained_and_no_longer_adoptable(tmp_path: Path) -> None:
    owner = SessionCustody.create(tmp_path, context())
    path = owner.record_path
    owner.finish()
    assert json.loads(path.read_bytes())["state"] == "ended"
    with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
        with pytest.raises(CustodyError, match="stale_attachment"):
            attachment.adopt(
                host_id="host_synthetic", endpoint="http://127.0.0.1:8123", config_digest="a" * 64
            )


def test_custody_watchdog_thread_observes_the_live_owner(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with SessionCustody.create(tmp_path, context()) as owner:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(owner.still_owner).result(timeout=2)


@pytest.mark.parametrize("field", ["pid_ns", "boot_id"])
def test_foreign_attachment_identity_refuses_without_overwriting(
    tmp_path: Path, field: str
) -> None:
    from omnigent.native.owner_claim import ProcessIdentity

    with SessionCustody.create(tmp_path, context()) as owner:
        path = owner.directory / "attachment.json"
        process = ProcessIdentity.capture(os.getpid()).as_dict()
        process[field] = "foreign"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": "old",
                    "process": process,
                    "epoch": 1,
                    "state": "attached",
                    "owner_generation": owner.record.owner_generation,
                }
            )
        )
        before = path.read_bytes()
        with SessionAttachment(tmp_path, "conv_synthetic", generation="replacement") as attachment:
            with pytest.raises(CustodyError, match="record_mismatch"):
                attachment.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                )
        assert path.read_bytes() == before


@pytest.mark.parametrize("drift", ["config", "lock"])
def test_custodian_does_not_overwrite_changed_authority(tmp_path: Path, drift: str) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        if drift == "config":
            record = json.loads(owner.record_path.read_bytes())
            record["config_digest"] = "d" * 64
            owner.record_path.write_text(json.dumps(record))
        else:
            lock = owner.directory / "owner.lock"
            lock.rename(owner.directory / "old-owner.lock")
            lock.touch()
        before = owner.record_path.read_bytes()
        assert not owner.still_owner()
        with pytest.raises(CustodyError, match="record_mismatch"):
            owner.publish_helper("helper", os.getpid())
        assert owner.record_path.read_bytes() == before


def test_end_session_requires_exact_scope_and_generation_then_retains_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from omnigent.runner import custody

    with SessionCustody.create(tmp_path, context()) as owner:
        process = Mock()
        process.terminate.side_effect = owner.finish
        monkeypatch.setattr(custody, "CustodyProcess", lambda record: process)
        for kwargs in (
            {"owner_generation": "foreign"},
            {"host_id": "foreign"},
            {"endpoint": "http://127.0.0.1:9999"},
            {"config_digest": "d" * 64},
        ):
            expected = {
                "owner_generation": owner.record.owner_generation,
                "host_id": "host_synthetic",
                "endpoint": "http://127.0.0.1:8123",
                "config_digest": "a" * 64,
            }
            expected.update(kwargs)
            with pytest.raises(CustodyError):
                custody.end_session(tmp_path, "conv_synthetic", **expected)
            process.terminate.assert_not_called()
        result = custody.end_session(
            tmp_path,
            "conv_synthetic",
            owner_generation=owner.record.owner_generation,
            host_id="host_synthetic",
            endpoint="http://127.0.0.1:8123",
            config_digest="a" * 64,
        )
        assert result["state"] == "ended"
        assert result["schema_version"] == 1
        process.terminate.assert_called_once()
        assert json.loads(owner.record_path.read_bytes())["state"] == "ended"
        assert (
            custody.end_session(
                tmp_path,
                "conv_synthetic",
                owner_generation=owner.record.owner_generation,
                host_id="host_synthetic",
                endpoint="http://127.0.0.1:8123",
                config_digest="a" * 64,
            )
            == result
        )
        process.terminate.assert_called_once()


def test_scanning_refuses_symlink_custody_namespace_without_mutation(tmp_path: Path) -> None:
    from omnigent.runner.custody import custody_records

    source = tmp_path / "source"
    source.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with SessionCustody.create(source, context()) as owner:
        before = owner.record_path.read_bytes()
        (foreign / "custody").symlink_to(source / "custody", target_is_directory=True)
        with pytest.raises(CustodyError, match="record_mismatch"):
            custody_records(foreign)
        assert owner.record_path.read_bytes() == before


def test_adoption_qualifies_the_retained_policy_bridge_config(tmp_path: Path) -> None:
    with SessionCustody.create(tmp_path, context()) as owner:
        resource = tmp_path / "policy-bridge.json"
        resource.write_text('{"endpoint":"http://127.0.0.1:8124"}\n')
        owner.publish_resource("tool_policy_bridge", resource)
        resource.write_text('{"endpoint":"http://127.0.0.1:9999"}\n')
        before = owner.record_path.read_bytes()
        with SessionAttachment(tmp_path, "conv_synthetic", generation="replacement") as attachment:
            with pytest.raises(CustodyError, match="config_mismatch"):
                attachment.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                )
        assert owner.record_path.read_bytes() == before


def test_attachment_claim_is_exclusive_across_processes(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = """
import json, sys
from pathlib import Path
from omnigent.runner.custody import CustodyError, SessionAttachment
try:
    with SessionAttachment(Path(sys.argv[1]), 'conv_synthetic', generation=sys.argv[2]) as claim:
        scope = dict(
            host_id='host_synthetic', endpoint='http://127.0.0.1:8123', config_digest='a' * 64
        )
        record = claim.adopt(**scope)
        repeated = claim.adopt(**scope)
        assert repeated.owner == record.owner
        receipt = {'owner': record.owner.as_dict(), 'generation': record.owner_generation}
        print(json.dumps(receipt), flush=True)
        sys.stdin.readline()
except CustodyError as exc:
    print(str(exc), flush=True)
    sys.exit(1)
"""
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "config"),
        OMNIGENT_DATA_DIR=str(tmp_path / "data"),
    )
    with SessionCustody.create(tmp_path, context()) as owner:
        before = owner.record_path.read_bytes()
        first = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), "first"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert first.stdout is not None
            receipt = json.loads(first.stdout.readline())
            assert receipt["owner"] == owner.record.owner.as_dict()
            competing = subprocess.run(
                [sys.executable, "-c", script, str(tmp_path), "second"],
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert competing.returncode == 1, competing.stderr
            assert competing.stdout.strip() == "owner_busy"
            attachment_path = owner.directory / "attachment.json"
            assert json.loads(attachment_path.read_bytes())["epoch"] == 1
            assert first.communicate(input="\n", timeout=10)[1] == ""
            assert first.returncode == 0
            replacement = subprocess.run(
                [sys.executable, "-c", script, str(tmp_path), "replacement"],
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert replacement.returncode == 0, replacement.stderr
            assert json.loads(replacement.stdout) == receipt
            assert json.loads(attachment_path.read_bytes())["epoch"] == 2
            assert owner.record_path.read_bytes() == before
            assert owner.still_owner()
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=10)


def test_adopted_process_reports_verified_intentional_end_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    from omnigent.native.owner_claim import ProcessIdentity
    from omnigent.runner.custody import CustodyProcess

    with SessionCustody.create(tmp_path, context()) as owner:
        process = CustodyProcess(owner.record, record_path=owner.record_path)
        owner.finish()

        def absent(identity: ProcessIdentity) -> None:
            raise psutil.NoSuchProcess(identity.pid)

        monkeypatch.setattr(ProcessIdentity, "require_live", absent)
        assert process.poll() == 0
        assert process.wait(timeout=1) == 0


def test_adopted_process_does_not_accept_a_foreign_end_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    from omnigent.native.owner_claim import ProcessIdentity
    from omnigent.runner.custody import CustodyProcess

    with SessionCustody.create(tmp_path, context()) as owner:
        process = CustodyProcess(owner.record, record_path=owner.record_path)
        record = owner.record.model_dump(mode="json")
        record.update(state="ended", owner_generation="foreign")
        owner.record_path.write_text(json.dumps(record))

        def absent(identity: ProcessIdentity) -> None:
            raise psutil.NoSuchProcess(identity.pid)

        monkeypatch.setattr(ProcessIdentity, "require_live", absent)
        assert process.poll() == 254


def test_public_session_contract_cli_reads_live_private_lease(tmp_path: Path) -> None:
    import subprocess
    import sys

    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "config"),
        OMNIGENT_DATA_DIR=str(tmp_path),
    )
    executable = Path(sys.executable).parent / "omnigent"
    with SessionCustody.create(tmp_path, context()) as owner:
        before = owner.record_path.read_bytes()
        command = [
            str(executable),
            "runtime",
            "session-contract",
            "--session",
            "conv_synthetic",
            "--json",
        ]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["owner"] == owner.record.owner.as_dict()
        assert receipt["owner_generation"] == owner.record.owner_generation
        assert receipt["schema_version"] == 1
        assert receipt["state"] == "starting"
        assert receipt["reattachment"] is None
        assert owner.record_path.read_bytes() == before
        assert owner.still_owner()


@pytest.mark.parametrize("operation", ["close", "cleanup", "shutdown"])
async def test_owned_terminal_close_retracts_receipt_before_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    import socket
    import tempfile

    from omnigent.inner.terminal import TerminalInstance
    from omnigent.runner import custody
    from omnigent.terminals.registry import TerminalRegistry

    class Terminal(TerminalInstance):
        async def close(self) -> None:
            self.socket_path.unlink()
            self.running = False

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="custody-close-") as short:
        socket_path = Path(short) / "tmux.sock"
        with (
            socket.socket(socket.AF_UNIX) as sock,
            SessionCustody.create(tmp_path, context()) as owner,
        ):
            sock.bind(str(socket_path))
            owner.publish_terminal("bash", "helper", socket_path, os.getpid(), os.getpid())
            monkeypatch.setattr(custody, "_current_custody", owner)
            registry = TerminalRegistry()
            terminal = Terminal(
                name="bash",
                session_key="helper",
                socket_path=socket_path,
                private_dir=Path(short),
                running=True,
            )
            registry._by_conversation["conv_synthetic"] = {("bash", "helper"): terminal}
            if operation == "close":
                assert await registry.close("conv_synthetic", "bash", "helper", expected=terminal)
            elif operation == "cleanup":
                await registry.cleanup_conversation("conv_synthetic")
            else:
                await registry.shutdown()
            with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
                record = attachment.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                )
                assert record.terminals == []
            assert not socket_path.exists()


def test_resource_retirement_cannot_remove_successor(tmp_path: Path) -> None:
    resource = tmp_path / "relay.json"
    resource.write_text("first")
    with SessionCustody.create(tmp_path, context()) as owner:
        owner.publish_resource("relay", resource)
        first = owner.record.resources["relay"]
        resource.write_text("successor")
        owner.publish_resource("relay", resource)
        owner.retire_resource("relay", first)
        assert owner.record.resources["relay"] != first
        owner.retire_resource("relay", owner.record.resources["relay"])
        assert not owner.record.resources


async def test_app_server_close_retires_helper_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import sys

    from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
    from omnigent.runner import custody

    with SessionCustody.create(tmp_path, context()) as owner:
        monkeypatch.setattr(custody, "_current_custody", owner)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        app_server = CodexNativeAppServer(
            codex_path=sys.executable,
            socket_path=tmp_path / "app.sock",
            codex_home=tmp_path,
            env={},
            config_overrides=[],
            cwd=tmp_path,
            bridge_dir=tmp_path,
            proc=proc,
        )
        owner.publish_helper("codex_app_server", proc.pid)
        try:
            await app_server.close()
            assert proc.returncode is not None
            with SessionAttachment(tmp_path, "conv_synthetic", generation="new") as attachment:
                record = attachment.adopt(
                    host_id="host_synthetic",
                    endpoint="http://127.0.0.1:8123",
                    config_digest="a" * 64,
                )
                assert record.helpers == {}
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
