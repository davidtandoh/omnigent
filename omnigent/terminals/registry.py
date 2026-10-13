"""Per-AP-process registry of conversation-scoped tmux terminals.

Replaces the legacy OSC 633 / pexpect ``TerminalManagerRegistry``
documented in ``designs/PERSISTENT_TERMINAL_RESEARCH.md``. The legacy
class kept ``dict[conv_id, TerminalManager]`` where ``TerminalManager``
owned ``Shell`` (pexpect-based) instances keyed by ``shell_name``.

Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.2, this rewrite swaps to:

- One terminal abstraction (``inner.terminal.TerminalInstance``) for
  the whole project — tmux-based.
- Three-level keying: ``(conversation_id, terminal_name, session_key)``.
  Multiple session keys per terminal name allow independent tmux
  sessions of the same configured terminal (e.g. ``bash:s1`` and
  ``bash:s2`` running in parallel).
- No idle reaper: terminals are explicit-launch only and the LLM is
  expected to ``sys_terminal_close`` when done. Omnigent shutdown still
  closes everything; per-conversation cleanup runs from the workflow's
  finally block.

The registry is constructed once at Omnigent startup
(``omnigent.runtime._globals.init``) and accessed via
``omnigent.runtime.get_terminal_registry()`` from tools and the
workflow.

**Locking.** A ``threading.Lock`` (not ``asyncio.Lock``) protects the
map. Tool invocations run on background threads via
``asyncio.to_thread`` (see ``runtime/workflow.py:1787``); each thread
spins up its own ``asyncio.run`` loop to drive the registry's async
methods. An ``asyncio.Lock`` would be bound to whichever loop created
it and would silently fail to synchronize concurrent invocations from
different threads. The threading lock is held only for short map
mutations (no tmux I/O underneath); slow tmux subprocess calls happen
outside the lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import psutil

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import OSEnvironment
from omnigent.inner.terminal import TerminalInstance, create_terminal_instance

logger = logging.getLogger(__name__)

# Per-instance close timeout. Bounds the cleanup window so a wedged
# tmux session can't block Omnigent shutdown or workflow finalization
# indefinitely. 5s mirrors the legacy ``_RELEASE_GRACE_S`` in
# ``runtime/harnesses/process_manager.py`` — long enough for a
# well-behaved tmux to flush and exit, short enough that cleanup
# doesn't stall.
_CLOSE_TIMEOUT_S = 5.0


def conversation_link_for_id(
    conversation_id: str,
    *,
    base_url: str | None = None,
) -> str:
    """
    Build the web UI link for a conversation.

    :param conversation_id: Conversation/session id, e.g.
        ``"conv_abc123"``.
    :param base_url: Optional Omnigent server base URL, e.g.
        ``"http://127.0.0.1:6767"``. When provided, the returned
        link is absolute.
    :returns: Web UI link, e.g. ``"/c/conv_abc123"`` or
        ``"http://127.0.0.1:6767/c/conv_abc123"``.
    """
    if base_url is None or not base_url.strip():
        return f"/c/{quote(conversation_id, safe='')}"
    # Delegate to the shared builder so workspace-hosted servers get the
    # API→UI mount swap (``/api/2.0/omnigent`` → ``/omnigent``) and the
    # ``?o=<org>`` selector — keeping the terminal status-bar link in
    # lockstep with the CLI's ``Web UI:`` link instead of pointing at the
    # JSON API mount.
    from omnigent.conversation_browser import conversation_url

    return conversation_url(base_url.strip(), conversation_id)


@dataclass(frozen=True)
class TerminalListEntry:
    """One entry returned by :meth:`TerminalRegistry.list_for_conversation`.

    :param terminal_name: The terminal's spec name, e.g. ``"bash"``.
    :param session_key: The per-launch session key, e.g. ``"s1"``.
    :param instance: The live :class:`TerminalInstance`. Callers can
        read ``.running``, ``.command``, ``.socket_path``, etc., or
        invoke ``send`` / ``read`` directly when wrapping in tools.
    """

    terminal_name: str
    session_key: str
    instance: TerminalInstance


class TerminalExitedDuringLaunch(RuntimeError):
    """Retain a dead launch's captured evidence after its private server is closed."""

    def __init__(self, instance: TerminalInstance) -> None:
        self.instance = instance
        super().__init__(
            f"terminal {instance.name}:{instance.session_key} exited before it became available "
            f"(exit status {instance.last_exit_status()})"
        )


class TerminalRegistry:
    """The single registry of per-conversation tmux terminal instances.

    All public methods take ``conversation_id`` as the first
    positional argument. State is keyed on the (conversation_id,
    terminal_name, session_key) tuple so distinct conversations can't
    see each other's terminals and the same terminal name can spawn
    multiple sessions in parallel.

    :class:`TerminalInstance` itself maintains a per-instance lock for
    serializing its own tmux ops; the registry lock is purely for
    map-level consistency.
    """

    def __init__(self, *, conversation_link_base_url: str | None = None) -> None:
        """
        Construct an empty registry.

        :param conversation_link_base_url: Optional Omnigent server base URL
            for terminal status links, e.g. ``"http://127.0.0.1:6767"``.
            ``None`` keeps links relative.
        """
        self._conversation_link_base_url = conversation_link_base_url
        # Two-level dict: conversation_id -> (name, key) -> instance.
        # Per-conversation maps make ``cleanup_conversation`` cheap
        # and ``list_for_conversation`` direct.
        self._by_conversation: dict[str, dict[tuple[str, str], TerminalInstance]] = {}
        # Per-instance locks keyed by the full (conv_id, name, session_key)
        # triple. Created at launch, removed at close /
        # cleanup_conversation. Sender / reader / closer tools acquire
        # this around the ``asyncio.run(instance.X())`` call to serialize
        # tmux subprocess invocations on the same instance — without it,
        # two concurrent ``sys_terminal_send`` calls can interleave their
        # per-keystroke tmux commands (a ``send(text=X, keys="Enter")``
        # decomposes to ~2 subprocess calls with a 50ms ``asyncio.sleep``
        # between them — plenty of room for another send to slip in).
        # See ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §9.1.
        self._instance_locks: dict[tuple[str, str, str], threading.Lock] = {}
        # Threading lock — see module docstring for the rationale.
        # Protects both ``_by_conversation`` and ``_instance_locks``.
        self._lock = threading.RLock()
        self.environment_resolver: Callable[[str, OSEnvSpec], OSEnvironment] | None = None

    def conversation_link_for_id(self, conversation_id: str) -> str:
        """
        Build a status-bar conversation link using this registry's base URL.

        :param conversation_id: Conversation/session id, e.g.
            ``"conv_abc123"``.
        :returns: Web UI link, e.g. ``"/c/conv_abc123"`` or
            ``"http://127.0.0.1:6767/c/conv_abc123"``.
        """
        return conversation_link_for_id(
            conversation_id,
            base_url=self._conversation_link_base_url,
        )

    async def launch(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        *,
        parent_os_env: OSEnvSpec | None = None,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
    ) -> TerminalInstance:
        """Launch a terminal session, or return the existing one.

        If the (conversation_id, terminal_name, session_key) triple
        already maps to a running instance, that instance is returned
        without spawning a new tmux session — matches the legacy
        Session ``_terminal_launch`` "already_running" semantics. The
        caller (the ``sys_terminal_launch`` tool) reports the result
        with the correct status by inspecting whether a new instance
        was created.

        :param conversation_id: The owning conversation id, e.g.
            ``"conv_abc123"``.
        :param terminal_name: The terminal's spec name from
            ``AgentSpec.terminals``, e.g. ``"bash"``.
        :param session_key: The per-launch session key, e.g.
            ``"s1"``. Free-form; different keys give independent
            tmux sessions of the same terminal.
        :param spec: The :class:`TerminalEnvSpec` for *terminal_name*.
            Caller looks this up from ``AgentSpec.terminals`` before
            calling.
        :param parent_os_env: The agent's primary
            :class:`OSEnvSpec`. Used by
            :func:`inner.terminal.create_terminal_instance` to
            resolve the terminal's effective os_env when the
            terminal spec doesn't declare one of its own.
        :param cwd_override: Optional cwd override, already vetted
            by the caller against the terminal spec's
            ``allow_cwd_override`` flag.
        :param sandbox_override: Optional sandbox override, already
            vetted against ``allow_sandbox_override``.
        :returns: The (possibly newly created) :class:`TerminalInstance`.
        :raises RuntimeError: If tmux isn't on PATH or the launch
            fails. Inner code surfaces a clear error; the caller
            tool wraps in a JSON error envelope.
        """
        lock_key = (conversation_id, terminal_name, session_key)
        with self._lock:
            instance_lock = self._instance_locks.setdefault(lock_key, threading.Lock())
            if not instance_lock.acquire(blocking=False):
                raise RuntimeError("Session custody refused: owner_busy")
        try:
            key = (terminal_name, session_key)
            from omnigent.runner.custody import CustodyError, current_custody

            custody = current_custody()
            if custody is not None and custody.record.session_id != conversation_id:
                custody = None
            with self._lock:
                existing = self._by_conversation.get(conversation_id, {}).get(key)
            if existing is not None and existing.running:
                self._verify_custody(conversation_id, existing)
                if await existing.is_alive():
                    if custody is not None:
                        from omnigent.runner.custody import _require_terminal_live

                        try:
                            _require_terminal_live(
                                custody.require_terminal_published(
                                    terminal_name, session_key, existing.socket_path
                                )
                            )
                        except CustodyError as exc:
                            raise RuntimeError(f"Session custody refused: {exc}") from exc
                    return existing
            if existing is not None:
                await self.close(conversation_id, terminal_name, session_key, _preserve_lock=True)

            if custody is not None:
                try:
                    custody.require_terminal_available(terminal_name, session_key)
                except CustodyError as exc:
                    raise RuntimeError(f"Session custody refused: {exc}") from exc

            parent_environment = None
            environment_spec = spec.os_env if isinstance(spec.os_env, OSEnvSpec) else parent_os_env
            if (
                environment_spec is not None
                and environment_spec.sandbox is not None
                and any(p.copy_on_write for p in environment_spec.sandbox.write_path_specs)
            ):
                if self.environment_resolver is None:
                    raise RuntimeError(
                        "copy_on_write terminals require a session resource registry"
                    )
                parent_environment = self.environment_resolver(
                    conversation_id, parent_os_env or environment_spec
                )
            shared_environment_args = (
                {"parent_environment": parent_environment} if parent_environment else {}
            )
            created = create_terminal_instance(
                terminal_name,
                session_key,
                spec,
                parent_os_env_spec=parent_os_env,
                **shared_environment_args,
                cwd_override=cwd_override,
                sandbox_override=sandbox_override,
                conversation_link=self.conversation_link_for_id(conversation_id),
            )
            instance = created.instance
            instance.operation_lock = instance_lock
            pending = None
            if custody is not None:
                try:
                    pending = custody.reserve_terminal(
                        terminal_name, session_key, instance.socket_path
                    )
                except (CustodyError, OSError) as exc:
                    category = str(exc) if isinstance(exc, CustodyError) else "unavailable_probe"
                    raise RuntimeError(f"Session custody refused: {category}") from exc
            try:
                instance.lifecycle_trace.transfer_session(conversation_id)
                await instance.launch(cwd=created.cwd)
                if not await instance.is_alive():
                    instance.lifecycle_trace.note_exit()
                    raise TerminalExitedDuringLaunch(instance)
                if custody is not None:

                    def _publish_custody() -> None:
                        pane_pid = instance.pane_pid_sync()
                        if pane_pid is None:
                            raise CustodyError("unavailable_probe")
                        tmux_pid = psutil.Process(pane_pid).ppid()
                        custody.publish_terminal(
                            terminal_name,
                            session_key,
                            instance.socket_path,
                            tmux_pid,
                            pane_pid,
                            pending=pending,
                        )
                        custody.require_terminal_published(
                            terminal_name, session_key, instance.socket_path
                        )

                    await asyncio.to_thread(_publish_custody)
            except (
                RuntimeError,
                OSError,
                ValueError,
                psutil.Error,
                asyncio.CancelledError,
            ) as exc:
                with contextlib.suppress(RuntimeError, OSError, ValueError, psutil.Error):
                    await asyncio.wait_for(
                        self._close_instance(instance, conversation_id), timeout=_CLOSE_TIMEOUT_S
                    )
                if custody is None or isinstance(exc, asyncio.CancelledError):
                    raise
                category = str(exc) if isinstance(exc, CustodyError) else "unavailable_probe"
                raise RuntimeError(f"Session custody refused: {category}") from exc
            with self._lock:
                slot = self._by_conversation.setdefault(conversation_id, {})
                slot[key] = instance
            return instance
        finally:
            with self._lock:
                instance_lock.release()
                if self._instance_locks.get(
                    lock_key
                ) is instance_lock and key not in self._by_conversation.get(conversation_id, {}):
                    self._instance_locks.pop(lock_key)

    def _verify_custody(self, conversation_id: str, instance: TerminalInstance) -> None:
        from omnigent.runner.custody import CustodyError, current_custody

        custody = current_custody()
        if custody is None:
            return
        try:
            if custody.record.session_id == conversation_id:
                custody.require_terminal_published(
                    instance.name, instance.session_key, instance.socket_path
                )
        except (CustodyError, OSError) as exc:
            category = str(exc) if isinstance(exc, CustodyError) else "unavailable_probe"
            raise RuntimeError(f"Session custody refused: {category}") from exc

    def get_instance_lock(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> threading.Lock | None:
        """Return the per-instance lock for a registered terminal.

        Used by the ``sys_terminal_send`` / ``read`` / ``close``
        tools to serialize tmux subprocess invocations against the
        same instance. The lock exists from registration (in
        :meth:`launch`) until the instance is closed (in
        :meth:`close` or :meth:`cleanup_conversation`).

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key from launch.
        :returns: A :class:`threading.Lock` to acquire around
            per-instance ops, or ``None`` if the instance was
            never registered (or has been closed). Callers should
            handle ``None`` by surfacing a "not running" error to
            the LLM rather than skipping the lock — the
            instance-not-found path means the operation can't
            proceed at all.
        """
        with self._lock:
            return self._instance_locks.get((conversation_id, terminal_name, session_key))

    def get(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> TerminalInstance | None:
        """Look up a registered instance.

        Sync because it doesn't touch tmux — just reads the map.
        Returns ``None`` if no instance was ever launched for this
        triple, or if it was already closed.

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key from launch.
        :returns: The :class:`TerminalInstance` or ``None``.
        """
        with self._lock:
            instance = self._by_conversation.get(conversation_id, {}).get(
                (terminal_name, session_key)
            )
            if instance is not None:
                self._verify_custody(conversation_id, instance)
            return instance

    def list_for_conversation(
        self,
        conversation_id: str,
    ) -> list[TerminalListEntry]:
        """Return all terminals owned by *conversation_id*.

        Snapshot semantics — the list reflects state at call time.
        Sync because it just reads the map. The caller (the
        ``sys_terminal_list`` tool) can inspect each
        ``TerminalListEntry.instance.running`` to filter alive vs
        defunct.

        :param conversation_id: Owning conversation id.
        :returns: List of :class:`TerminalListEntry`. Empty list when
            the conversation has no terminals.
        """
        with self._lock:
            slot = dict(self._by_conversation.get(conversation_id, {}))
        for instance in slot.values():
            self._verify_custody(conversation_id, instance)
        return [
            TerminalListEntry(
                terminal_name=name,
                session_key=key,
                instance=instance,
            )
            for (name, key), instance in slot.items()
        ]

    def native_panes(self) -> list[tuple[str, str, Path]]:
        """Return live native-harness CLI panes as ``(conversation_id, name, socket_path)``.

        A "native pane" is a terminal whose name is a native harness short name
        (``claude`` / ``codex`` / ``cursor`` / ...) with session key ``"main"``.
        This is a cheap NAME pre-filter for the native idle reaper
        (:mod:`omnigent.terminals.pane_reaper`); the reaper's wiring additionally
        confirms the resource ROLE is a native harness before reaping, so a user
        terminal that merely shares the name is never reclaimed. Snapshot
        semantics; sync (map read only, no tmux I/O).

        :returns: ``(conversation_id, terminal_name, tmux_socket_path)`` per live
            name-matching native pane.
        """
        from omnigent.terminals.pane_reaper import NATIVE_PANE_TERMINAL_NAMES

        out: list[tuple[str, str, Path]] = []
        with self._lock:
            for conv_id, slot in self._by_conversation.items():
                for (name, key), instance in slot.items():
                    if key == "main" and name in NATIVE_PANE_TERMINAL_NAMES:
                        self._verify_custody(conv_id, instance)
                        out.append((conv_id, name, instance.socket_path))
        return out

    def transfer(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        """Move one terminal registry entry without closing tmux.

        This is used by native Claude ``/clear`` rotation: the Claude
        process and tmux pane keep running, but the Omnigent session that owns
        the terminal resource changes. No tmux I/O occurs in this method.

        :param source_conversation_id: Current owning conversation id,
            e.g. ``"conv_old"``.
        :param target_conversation_id: New owning conversation id,
            e.g. ``"conv_new"``.
        :param terminal_name: Terminal spec name, e.g. ``"claude"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :returns: ``True`` when an entry was moved, ``False`` when the
            source entry did not exist.
        :raises RuntimeError: If the target conversation already has an
            entry with the same terminal name and session key.
        """
        with self._lock:
            return self._transfer_locked(
                source_conversation_id, target_conversation_id, terminal_name, session_key
            )

    def _transfer_locked(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        key = (terminal_name, session_key)
        source_slot = self._by_conversation.get(source_conversation_id)
        if source_slot is None:
            return False
        instance = source_slot.get(key)
        if instance is None:
            return False
        target_slot = self._by_conversation.get(target_conversation_id, {})

        from omnigent.runner.custody import CustodyError, current_custody

        custody = current_custody()
        if key in target_slot:
            raise RuntimeError(
                f"Terminal {terminal_name!r}:{session_key!r} already exists for "
                f"conversation {target_conversation_id!r}"
            )
        # A custody-owned terminal stays within its owner's session scope.
        if custody is not None and custody.record.session_id in {
            source_conversation_id,
            target_conversation_id,
        }:
            try:
                custody.require_terminal_published(
                    terminal_name, session_key, instance.socket_path
                )
                if target_conversation_id != custody.record.session_id:
                    raise CustodyError("scope_mismatch")
            except CustodyError as exc:
                raise RuntimeError(f"Session custody refused: {exc}") from exc
        target_slot = self._by_conversation.setdefault(target_conversation_id, {})
        source_slot.pop(key)
        target_slot[key] = instance
        instance.lifecycle_trace.transfer_session(target_conversation_id)
        lock = self._instance_locks.pop((source_conversation_id, terminal_name, session_key), None)
        self._instance_locks[(target_conversation_id, terminal_name, session_key)] = (
            lock or threading.Lock()
        )
        if not source_slot:
            self._by_conversation.pop(source_conversation_id, None)
        return True

    async def _close_instance(self, instance: TerminalInstance, conversation_id: str) -> None:
        from omnigent.runner.custody import current_custody

        custody = current_custody()
        receipt = (
            next(
                (r for r in custody.record.terminals if r.socket == str(instance.socket_path)),
                None,
            )
            if custody is not None
            else None
        )
        if (
            custody is not None
            and custody.record.session_id == conversation_id
            and receipt is None
        ):
            receipt = custody.reserve_terminal(
                instance.name, instance.session_key, instance.socket_path
            )
        await instance.close()
        if custody is not None and receipt is not None:
            custody.retire_terminal(receipt)

    async def close(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        *,
        expected: TerminalInstance | None = None,
        _preserve_lock: bool = False,
    ) -> bool:
        """Close one terminal and remove it from the registry.

        Idempotent: closing a non-existent or already-closed terminal
        returns ``False`` without raising. This matches the
        ``sys_terminal_close`` tool's contract — the LLM may close
        the same terminal twice without seeing an error.

        :param conversation_id: Owning conversation id.
        :param terminal_name: Terminal spec name.
        :param session_key: Session key.
        :param expected: When given, close only if this exact instance
            still occupies the key. A caller that wants to retract the
            instance IT published must pass it: the key can have been
            reassigned to a successor in the meantime, and closing by key
            alone would terminate that successor instead. Identity is checked
            under the registry lock before close and before removal.
        :returns: ``True`` if a live instance was closed, ``False``
            if no live instance was found (already-closed or
            never-launched), or if *expected* no longer occupies the key.
        """
        key = (terminal_name, session_key)
        with self._lock:
            slot = self._by_conversation.get(conversation_id)
            if slot is None:
                return False
            if expected is not None and slot.get(key) is not expected:
                return False
            instance = slot.get(key)
        if instance is None:
            return False
        await asyncio.wait_for(
            self._close_instance(instance, conversation_id), timeout=_CLOSE_TIMEOUT_S
        )
        with self._lock:
            slot = self._by_conversation.get(conversation_id)
            if slot is not None and slot.get(key) is instance:
                del slot[key]
                if not slot:
                    self._by_conversation.pop(conversation_id, None)
                if not _preserve_lock:
                    self._instance_locks.pop((conversation_id, terminal_name, session_key), None)
        return True

    async def cleanup_conversation(self, conversation_id: str) -> None:
        """Close every terminal owned by *conversation_id*.

        Called from the workflow's ``finally:`` block at workflow
        exit (any of completed / failed / cancelled). Idempotent:
        no-op when the conversation has no terminals.

        Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.4, this is **not**
        checkpointed: tmux kills are idempotent external side
        effects, the workflow doesn't read from terminals after
        exit, and step wrappers exist to checkpoint results the
        workflow consumes — not for fire-and-forget cleanup.

        Each ``instance.close()`` is bounded by ``_CLOSE_TIMEOUT_S``
        via ``asyncio.wait_for`` so a wedged tmux session can't stall
        cleanup. Timeouts and exceptions are logged and swallowed; the
        rest of the cleanup proceeds.

        :param conversation_id: The conversation being torn down.
        """
        with self._lock:
            slot = dict(self._by_conversation.get(conversation_id, {}))
        for (name, key), instance in slot.items():
            try:
                await self.close(conversation_id, name, key, expected=instance)
            except asyncio.TimeoutError:
                logger.warning(
                    "cleanup_conversation: close timed out for %s:%s in conv %s",
                    name,
                    key,
                    conversation_id,
                )
            except Exception:
                # We're in a workflow finally block; raising here would
                # mask the original workflow result. Log and move on.
                logger.exception(
                    "cleanup_conversation: close failed for %s:%s in conv %s",
                    name,
                    key,
                    conversation_id,
                )

    async def shutdown(self) -> None:
        """Tear down every registered terminal across all conversations.

        Called from the FastAPI server's lifespan shutdown handler.
        Iterates every conversation slot and closes each instance,
        bounded by ``_CLOSE_TIMEOUT_S`` per instance. Best-effort —
        a stuck instance shouldn't block the rest of Omnigent shutdown.
        """
        with self._lock:
            conversation_ids = list(self._by_conversation)
        for conversation_id in conversation_ids:
            await self.cleanup_conversation(conversation_id)

    def active_conversation_ids(self) -> list[str]:
        """Return ids of conversations with at least one registered terminal.

        Used by tests. Snapshot semantics — the list reflects state
        at call time.

        :returns: List of conversation ids currently in the registry.
        """
        with self._lock:
            return list(self._by_conversation.keys())
