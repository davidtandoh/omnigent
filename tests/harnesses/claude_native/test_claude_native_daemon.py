"""Tests for the daemon-routed remote-claude launch helpers.

Covers the client side of HOST_BY_DEFAULT: acquiring a daemon-spawned
runner (reuse-online / launch / clear-stale) and the
``_prepare_claude_terminal_via_daemon`` orchestration that persists the
user's pass-through args on the session so the runner applies them. See
designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.host import daemon_launch
from omnigent.native import native_terminal

pytestmark = pytest.mark.asyncio


async def test_native_wrappers_share_daemon_terminal_helpers() -> None:
    """
    Claude and Codex wrappers use the same daemon terminal helpers.

    Codex used to import moved private helpers from ``claude_native``,
    which made module import fail during default server startup. These
    helpers are now owned by ``native_terminal`` and re-exported under
    existing wrapper-local names for focused monkeypatch tests.
    """
    assert claude_native._attach_url is native_terminal.terminal_attach_url
    assert codex_native._attach_url is native_terminal.terminal_attach_url
    assert claude_native._bind_session_runner is native_terminal.bind_session_runner
    assert codex_native._bind_session_runner is native_terminal.bind_session_runner
    assert (
        claude_native._DAEMON_HOST_ONLINE_TIMEOUT_S
        == codex_native._DAEMON_HOST_ONLINE_TIMEOUT_S
        == native_terminal.DAEMON_HOST_ONLINE_TIMEOUT_S
    )


async def test_launch_or_reuse_daemon_runner_reuses_online_runner() -> None:
    """
    An already-bound, still-online runner is reused — no new launch.

    Resuming into a live session must not spawn a second runner. If the
    helper POSTed to ``/runners`` here it would 400 ("already bound") or
    spawn a duplicate; reuse keeps the existing one.
    """
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the reuse-path requests."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={"runner_id": "runner_live"})
        if request.method == "GET" and path == "/v1/runners/runner_live/status":
            return httpx.Response(200, json={"runner_id": "runner_live", "online": True})
        if request.method == "POST" and path.endswith("/runners"):
            posted.append(path)
            return httpx.Response(200, json={"runner_id": "unexpected"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/w"
        )

    assert runner_id == "runner_live"
    # No launch POST fired — proves the online runner was reused, not respawned.
    assert posted == []


async def test_launch_environment_rejects_online_runner_reuse() -> None:
    """A launch environment cannot silently miss an already-running runner."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={"runner_id": "runner_live"})
        if request.url.path == "/v1/runners/runner_live/status":
            return httpx.Response(200, json={"runner_id": "runner_live", "online": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        with pytest.raises(click.ClickException, match="only when a new native runner starts"):
            await daemon_launch.launch_or_reuse_daemon_runner(
                client,
                host_id="host_1",
                session_id="conv_a",
                workspace="/w",
                native_env={"FM_TASK_ID": "task-123"},
            )


async def test_launch_or_reuse_daemon_runner_launches_when_unbound() -> None:
    """
    A session with no runner triggers a launch on the host endpoint.

    The launch request must carry the session id + workspace so the
    daemon spawns the runner in the right place and binds it.
    """
    launched: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the launch-path requests."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/hosts/host_1/runners":
            launched["body"] = json.loads(request.content)
            return httpx.Response(200, json={"runner_id": "runner_new", "status": "launching"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client,
            host_id="host_1",
            session_id="conv_a",
            workspace="/work",
            native_env={"FM_TASK_ID": "task-123", "CLAUDE_ACCOUNT": ""},
        )

    assert runner_id == "runner_new"
    assert launched["body"] == {
        "session_id": "conv_a",
        "workspace": "/work",
        "native_env": {"FM_TASK_ID": "task-123", "CLAUDE_ACCOUNT": ""},
    }


async def test_launch_or_reuse_daemon_runner_clears_stale_binding() -> None:
    """
    A bound-but-offline runner is cleared before launching a fresh one.

    The launch endpoint binds atomically only when ``runner_id IS
    NULL``; without clearing the dead binding first it would 400. The
    clear must therefore precede the launch.
    """
    events: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the stale-binding-path requests, recording order."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={"runner_id": "runner_dead"})
        if request.method == "GET" and path == "/v1/runners/runner_dead/status":
            return httpx.Response(200, json={"runner_id": "runner_dead", "online": False})
        if request.method == "PATCH" and path == "/v1/sessions/conv_a":
            events.append(("patch", json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/hosts/host_1/runners":
            events.append(("launch", None))
            return httpx.Response(200, json={"runner_id": "runner_fresh"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/w"
        )

    assert runner_id == "runner_fresh"
    # The stale binding was cleared (runner_id="") strictly before the
    # launch — the ordering the atomic NULL-bind requires.
    assert ("patch", {"runner_id": ""}) in events
    assert events.index(("patch", {"runner_id": ""})) < events.index(("launch", None))


async def test_launch_or_reuse_daemon_runner_fresh_skips_session_get() -> None:
    """
    ``fresh=True`` skips the ``GET /v1/sessions/{id}`` check entirely.

    A session created in the same startup can't have a runner bound yet,
    so the read is always empty and only adds latency (~2-3s). The fresh
    path goes straight to ``POST /v1/hosts/{id}/runners``.
    """
    gets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any GET to sessions and serve the launch endpoint."""
        if request.method == "GET" and "/sessions/" in request.url.path:
            gets.append(request.url.path)
            return httpx.Response(200, json={})
        if request.method == "POST" and request.url.path == "/v1/hosts/host_1/runners":
            return httpx.Response(200, json={"runner_id": "runner_new"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/w", fresh=True
        )

    assert runner_id == "runner_new"
    # No GET to /v1/sessions — the binding check was skipped.
    assert gets == []


async def test_create_claude_session_persists_terminal_launch_args() -> None:
    """
    The daemon-flow create persists pass-through args and omits the
    bridge-id label.

    ``omnigent claude --server X <flags>`` must carry the flags to a
    daemon-spawned runner: they're written to the session's
    ``terminal_launch_args`` at create so the runner applies them when it
    auto-launches the terminal. The bridge-id label is omitted
    (``bridge_id=None``) so the bridge dir keys by session id — the
    convention the runner's auto-create uses. A missing
    ``terminal_launch_args`` would silently drop the user's flags; a
    present bridge-id label would split the bridge dir between the
    harness and the terminal.
    """
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the multipart create body."""
        if request.method == "POST" and request.url.path == "/v1/sessions":
            captured["body"] = request.content
            return httpx.Response(201, json={"session_id": "conv_new"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        session_id = await claude_native._create_claude_session(
            client,
            b"bundle",
            bridge_id=None,
            terminal_launch_args=["--dangerously-skip-permissions"],
        )

    assert session_id == "conv_new"
    body = captured["body"]
    assert b'"terminal_launch_args"' in body
    assert b"--dangerously-skip-permissions" in body
    # bridge_id=None → the bridge-id label must NOT be written.
    assert BRIDGE_ID_LABEL_KEY.encode() not in body


@pytest.mark.parametrize(
    ("account_effort", "claude_args", "expected"),
    [
        ("low", (), "low"),
        ("low", ("--effort", "xhigh"), "xhigh"),
        (None, (), None),
        ("invalid", (), None),
    ],
)
async def test_create_claude_session_uses_selected_account_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    account_effort: str | None,
    claude_args: tuple[str, ...],
    expected: str | None,
) -> None:
    """Session effort follows the selected account unless explicitly overridden."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"effortLevel": "high"}), encoding="utf-8")
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)
    account = tmp_path / "work-account"
    account.mkdir()
    if account_effort is not None:
        (account / "settings.json").write_text(json.dumps({"effortLevel": account_effort}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-account"))
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the multipart create body."""
        captured["body"] = request.content
        return httpx.Response(201, json={"session_id": "conv_new"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        await claude_native._create_claude_session(
            client,
            b"bundle",
            bridge_id=None,
            claude_args=claude_args,
            launch_env={"CLAUDE_CONFIG_DIR": str(account)},
        )

    if expected is None:
        assert b'"reasoning_effort"' not in captured["body"]
    else:
        assert f'"reasoning_effort": "{expected}"'.encode() in captured["body"]


async def test_create_claude_session_records_only_the_account_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(201, json={"session_id": "conv_new"})

    account_dir = str(tmp_path / "accounts" / "work")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        await claude_native._create_claude_session(
            client,
            b"bundle",
            bridge_id=None,
            launch_env={"CLAUDE_CONFIG_DIR": account_dir},
            account="work",
        )

    assert b'"account": "work"' in captured["body"]
    assert account_dir.encode() not in captured["body"]


def _session_lookup(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    """Answer ``_resumed_session_account``'s session read with *response*."""
    real_client = httpx.Client

    def _client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(lambda request: response), **kwargs)

    monkeypatch.setattr(claude_native.httpx, "Client", _client)


@pytest.mark.parametrize(
    ("recorded", "requested", "expected"),
    [("work", None, "work"), ("work", "work", "work"), (None, None, None)],
)
def test_resume_keeps_the_recorded_account(
    monkeypatch: pytest.MonkeyPatch,
    recorded: str | None,
    requested: str | None,
    expected: str | None,
) -> None:
    _session_lookup(monkeypatch, httpx.Response(200, json={"id": "conv_a", "account": recorded}))
    assert (
        claude_native._resumed_session_account(
            base_url="https://e.com", headers={}, session_id="conv_a", requested=requested
        )
        == expected
    )


@pytest.mark.parametrize(("recorded", "current"), [("work", "'work'"), (None, "default")])
def test_resume_on_another_account_is_refused_and_points_at_new_session(
    monkeypatch: pytest.MonkeyPatch, recorded: str | None, current: str
) -> None:
    """Resuming with a different --account never re-homes the session silently."""
    _session_lookup(monkeypatch, httpx.Response(200, json={"id": "conv_a", "account": recorded}))
    with pytest.raises(click.ClickException) as caught:
        claude_native._resumed_session_account(
            base_url="https://e.com", headers={}, session_id="conv_a", requested="client-a"
        )
    assert current in caught.value.message
    assert "Start a new session" in caught.value.message


def test_unreadable_session_refuses_only_an_explicit_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session_lookup(monkeypatch, httpx.Response(500, json={"detail": "boom"}))
    assert (
        claude_native._resumed_session_account(
            base_url="https://e.com", headers={}, session_id="conv_a", requested=None
        )
        is None
    )
    with pytest.raises(click.ClickException, match="check its Claude account"):
        claude_native._resumed_session_account(
            base_url="https://e.com", headers={}, session_id="conv_a", requested="work"
        )


def _install_daemon_seam_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prepared: claude_native.PreparedClaudeTerminal,
    attach_outcome: claude_native._AttachOutcome,
    captured: dict[str, Any],
    ensured: list[str],
) -> None:
    """
    Patch the high-level seams of ``_run_with_remote_server``.

    Stubs the daemon-ensure, host identity, session-resolve, cwd-align,
    bundle, launch-state, the per-daemon prepare (capturing its kwargs),
    and the attach (returning *attach_outcome*) so the orchestration can
    run in-process without a server, daemon, or runner.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param prepared: Prepared terminal the fake prepare returns.
    :param attach_outcome: Outcome the fake attach returns.
    :param captured: Dict the fake prepare populates with its kwargs.
    :param ensured: List the daemon-ensure stub appends the server url
        to.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda server_url=None, **k: {})
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(
        "omnigent.cli._ensure_host_daemon",
        lambda url: ensured.append(url),
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda *a, **k: SimpleNamespace(host_id="host_1", name="h"),
    )
    monkeypatch.setattr(
        claude_native,
        "_resolve_session_id_for_resume",
        lambda **k: k.get("session_id"),
    )
    monkeypatch.setattr(
        claude_native, "_align_working_directory_with_session", lambda *a, **k: None
    )
    monkeypatch.setattr(claude_native, "_record_launch_for_fresh_session", lambda sid: None)
    monkeypatch.setattr(claude_native, "_resumed_session_account", lambda **k: None)

    async def _fake_prepare(**kwargs: Any) -> claude_native.PreparedClaudeTerminal:
        """Capture prepare kwargs and return the canned terminal."""
        captured.update(kwargs)
        return prepared

    async def _fake_attach(**kwargs: Any) -> claude_native._AttachOutcome:
        """Return the canned attach outcome."""
        del kwargs
        return attach_outcome

    monkeypatch.setattr(claude_native, "_prepare_claude_terminal_via_daemon", _fake_prepare)
    monkeypatch.setattr(claude_native, "_attach_with_transcript_forwarder", _fake_attach)


@pytest.mark.parametrize("session_id", [None, "conv_existing"])
@pytest.mark.parametrize("prompt", [None, "Reply with READY.\nThen wait."])
def test_run_with_remote_server_routes_through_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str | None,
    prompt: str | None,
) -> None:
    """
    Remote launches prepare through the daemon and queue a prompt once.

    Proves the CLI no longer spawns the runner itself: it routes the launch
    through ``_prepare_claude_terminal_via_daemon`` with this host's id,
    the cwd as workspace, and the user's ``claude_args`` (so the runner
    can apply them). Daemon start is handled by ``_ensure_backend`` in
    ``cli_native.py`` before ``_run_with_remote_server`` is called — not
    inside ``_run_with_remote_server`` itself. Attach recovery must not
    resubmit the explicit prompt on either fresh launch or resume.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id=session_id or "conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=session_id is not None,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=ensured,
    )

    messages: list[dict[str, Any]] = []

    def handle_message(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/sessions/{prepared.session_id}/events"
        messages.append(json.loads(request.content))
        return httpx.Response(202, json={})

    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client",
        lambda base_url, **kwargs: httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handle_message), **kwargs
        ),
    )

    async def attach_with_recovery(**kwargs: Any) -> claude_native._AttachOutcome:
        assert len(messages) == (0 if prompt is None else 1)
        await kwargs["recover"]()
        await kwargs["recover"]()
        assert len(messages) == (0 if prompt is None else 1)
        return claude_native._AttachOutcome.EXITED

    monkeypatch.setattr(claude_native, "_attach_with_transcript_forwarder", attach_with_recovery)

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id=session_id,
        resume_picker=False,
        claude_args=("--dangerously-skip-permissions",),
        prompt=prompt,
    )

    # Daemon start is now _ensure_backend's responsibility (called from
    # cli_native before _run_with_remote_server); not asserted here.
    # The launch was routed through the daemon prepare with this host,
    # the cwd workspace, and the user's args.
    assert captured["host_id"] == "host_1"
    assert captured["workspace"] == str(tmp_path.resolve())
    assert captured["claude_args"] == ("--dangerously-skip-permissions",)
    assert captured["session_id"] == session_id
    assert "prompt" not in captured
    assert messages == (
        [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        ]
        if prompt is not None
        else []
    )


def test_run_with_remote_server_unsent_prompt_stops_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unsent remote prompt fails with the session id and skips the attach."""
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured={},
        ensured=[],
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client",
        lambda base_url, **kwargs: httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(refuse), **kwargs
        ),
    )
    attached: list[dict[str, Any]] = []

    async def record_attach(**kwargs: Any) -> claude_native._AttachOutcome:
        attached.append(kwargs)
        return claude_native._AttachOutcome.EXITED

    monkeypatch.setattr(claude_native, "_attach_with_transcript_forwarder", record_attach)

    with pytest.raises(click.ClickException, match="Session conv_new did not receive it"):
        claude_native._run_with_remote_server(
            "https://example.com",
            spec_path,
            session_id=None,
            resume_picker=False,
            claude_args=(),
            prompt="hello",
        )

    assert attached == []


def test_run_with_remote_server_detach_prints_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A tmux detach on a resumed session prints the copyable resume command.

    The daemon owns the runner, so a detach leaves it running for the web
    UI; the user gets a ``--resume`` command to reattach. This pins the
    detach-hint UX preserved from the pre-daemon flow.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_existing",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=True,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.DETACHED,
        captured=captured,
        ensured=ensured,
    )

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id="conv_existing",
        resume_picker=False,
        claude_args=(),
    )

    err = capsys.readouterr().err
    assert "Detached. Agent still running at https://example.com/c/conv_existing" in err
    # Exact resume command: server + session only — a --profile part
    # here would tell the user to run a flag that no longer exists.
    assert (
        "Resume with: omnigent claude --server https://example.com --resume conv_existing"
    ) in err


def test_run_with_remote_server_unreachable_server_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An unreachable Omnigent server fails with an actionable message, not a
    raw httpx traceback.

    The daemon flow's first server contact is the session create; if the
    server isn't reachable that surfaces as ``httpx.ConnectError``. The
    wrapper must turn it into a ``ClickException`` naming the URL so the
    user knows what to check, rather than dumping a connection-pool
    stack trace.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=ensured,
    )

    async def _boom(**kwargs: Any) -> claude_native.PreparedClaudeTerminal:
        """Simulate the server being unreachable at create."""
        del kwargs
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(claude_native, "_prepare_claude_terminal_via_daemon", _boom)

    with pytest.raises(click.ClickException) as exc_info:
        claude_native._run_with_remote_server(
            "https://unreachable.example",
            spec_path,
            session_id=None,
            resume_picker=False,
            claude_args=(),
        )

    assert "Could not reach the omnigent server at https://unreachable.example" in str(
        exc_info.value
    )


@pytest.mark.parametrize("background", [False, True])
def test_remote_resume_relocates_the_selected_account_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, background: bool
) -> None:
    from omnigent.harnesses.claude_native.state import read_launch_state, write_launch_state

    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    current = tmp_path / "current"
    current.mkdir()
    old = tmp_path / "removed"
    account_dir = tmp_path / "work-account"
    projects = account_dir / "projects"
    source = projects / "old-project" / "claude-session-abc.jsonl"
    source.parent.mkdir(parents=True)
    original = json.dumps({"type": "user", "cwd": str(old), "sessionId": "claude-session-abc"})
    source.write_text(original + "\n")
    if background:
        sessions = account_dir / "sessions"
        sessions.mkdir()
        (sessions / f"{os.getppid()}.json").write_text(
            json.dumps({"sessionId": "claude-session-abc", "kind": "bg"})
        )
    monkeypatch.chdir(current)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-account"))
    write_launch_state("conv_existing", str(old))
    align = claude_native._align_working_directory_with_session
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_existing",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    captured: dict[str, Any] = {}
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=[],
    )
    monkeypatch.setattr(claude_native, "_align_working_directory_with_session", align)
    monkeypatch.setattr(
        claude_native, "_fetch_external_session_id_for_redirect", lambda **kw: "claude-session-abc"
    )
    monkeypatch.setattr(claude_native, "_prompt_resume_workspace_action", lambda **kw: "move")
    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id="conv_existing",
        resume_picker=False,
        claude_args=(),
        launch_env={"CLAUDE_CONFIG_DIR": str(account_dir)},
    )
    assert captured["launch_env"] == {"CLAUDE_CONFIG_DIR": str(account_dir)}
    assert captured["workspace"] == str(current.resolve())
    assert read_launch_state("conv_existing").working_directory == str(current.resolve())
    target = (
        projects
        / claude_native._sanitize_claude_project_name(str(current.resolve()))
        / source.name
    )
    if background:
        assert source.read_text() == original + "\n"
        assert not target.exists()
    else:
        assert not source.exists()
        assert json.loads(target.read_text())["cwd"] == str(current.resolve())
    assert not (tmp_path / "other-account").exists()


def test_remote_resume_reads_local_settings_from_the_recorded_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local reads use the session's account folder; only the name reaches the server."""
    account_dir = tmp_path / "accounts" / "work"
    (tmp_path / "config.yaml").write_text(
        "providers:\n  claude-work:\n    kind: subscription\n    cli: claude\n"
        f"    config_dir: {account_dir}\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    captured: dict[str, Any] = {}
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=claude_native.PreparedClaudeTerminal(
            session_id="conv_existing",
            terminal_id=claude_native.claude_terminal_resource_id(),
            bridge_dir=tmp_path / "bridge",
            reattached=False,
        ),
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=[],
    )
    monkeypatch.setattr(claude_native, "_resumed_session_account", lambda **k: "work")

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id="conv_existing",
        resume_picker=False,
        claude_args=(),
    )

    assert captured["settings_env"] == {"CLAUDE_CONFIG_DIR": str(account_dir)}
    assert captured["launch_env"] is None
