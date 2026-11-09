"""Streaming dictation route: the transcription WebSocket.

This module hosts the server-side speech-to-text surface behind the
composer mic button (``designs/server-dictation.md``):

- ``WS /v1/dictation/stream`` — one connection per dictation take.

Availability is advertised as ``dictation_available`` on ``GET /v1/info``
(the web UI's boot-time capability probe); there is no separate probe
endpoint.

Wire protocol on the WebSocket
------------------------------

- **Client → server, binary frames**: raw 16 kHz mono s16le PCM. The
  browser worklet downsamples from the capture rate before sending.
- **Client → server, text frames**: JSON control messages.
  ``{"type": "stop"}`` asks the server to flush trailing audio and
  finish the take. Unknown shapes are ignored so future control
  messages don't break older servers.
- **Server → client, text frames**: JSON events.
    - ``{"type": "ready"}`` — sent once after the engine is ready;
      the client may start streaming audio.
    - ``{"type": "partial", "text": ...}`` — revisable in-progress
      utterance, throttled server-side.
    - ``{"type": "final", "text": ...}`` — an utterance completed by
      endpoint detection (a pause). The client appends it and clears
      its partial region.
    - ``{"type": "stopped", "text": ...}`` — reply to ``stop``: the
      flushed tail utterance (possibly empty). The server closes the
      socket after sending it.
    - ``{"type": "error", "message": ...}`` — fatal; the server closes.

Auth
----

Dictation is not session-scoped — the new-chat composer dictates before
any session exists — so the check is identity-level only, matching
``GET /v1/harnesses``: when an auth provider is configured the caller
must be authenticated (the WebSocket handshake carries identity via the
ingress/dev proxy exactly like the terminal-attach socket); in
single-user/dev mode the route is open.

Capacity
--------

Decoding is CPU-bound, so concurrent takes are capped (default 2,
``OMNIGENT_DICTATION_MAX_STREAMS``). Over-cap connections are accepted
and immediately closed with code 1013 (try again later) so the client
can distinguish "busy" from "broken".

Timing
------

Each take logs one ``dictation_take`` summary: ready latency, audio
seconds received versus decode seconds (real-time factor), how far
decode trailed real time when ``stop`` arrived, and the tail flush time.
The ``relay`` field identifies streams forwarded to a remote worker. For
relayed takes, decode timings measure relay operations; the worker's own
``dictation_take`` log carries the actual decode time and real-time factor.
With ``OMNIGENT_DICTATION_PRELOAD`` set, the engine loads when the router
is built instead of on the first take.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException
from starlette import status

from omnigent.debug_logging import debug_event
from omnigent.server.auth import AuthProvider
from omnigent.server.dictation import (
    SAMPLE_RATE,
    DictationEngine,
    DictationStreamHandle,
    _RemoteStream,
    get_engine,
    max_streams,
    preload_engine_in_background,
)

_logger = logging.getLogger(__name__)

_WS_CLOSE_TRY_AGAIN_LATER: Final[int] = 1013
_WS_CLOSE_INTERNAL_ERROR: Final[int] = 1011

#: Minimum interval between partial-transcript pushes. Keeps the socket
#: chatty enough for live text without a frame per audio chunk.
_PARTIAL_INTERVAL_S: Final[float] = 0.15
_PCM_BYTES_PER_SECOND: Final[int] = SAMPLE_RATE * 2


@dataclass
class _TakeTiming:
    """Per-take latency counters, logged once when the take ends."""

    accepted_at: float
    relay: bool = False
    ready_s: float | None = None
    audio_bytes: int = 0
    decode_s: float = 0.0
    first_audio_at: float | None = None
    stop_lag_s: float | None = None
    finish_s: float | None = None

    @property
    def audio_s(self) -> float:
        return self.audio_bytes / _PCM_BYTES_PER_SECOND

    def note_stop(self, now: float) -> None:
        """Record how far decode trailed real-time audio when stop arrived.

        Frames arrive in order, so by the time ``stop`` is read every frame
        has decoded; positive lag means decode fell behind the speaker.
        """
        if self.first_audio_at is not None:
            self.stop_lag_s = max(0.0, now - self.first_audio_at - self.audio_s)

    def log(self) -> None:
        audio_s = self.audio_s
        rtf = self.decode_s / audio_s if audio_s else None
        fields = {
            "relay": self.relay,
            "ready_s": _round(self.ready_s),
            "audio_s": round(audio_s, 3),
            "decode_s": round(self.decode_s, 3),
            "rtf": _round(rtf),
            "stop_lag_s": _round(self.stop_lag_s),
            "finish_s": _round(self.finish_s),
        }
        _logger.info(
            "dictation take: %s",
            " ".join(f"{key}={value}" for key, value in fields.items()),
            extra=debug_event("dictation_take", **fields),
        )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def create_dictation_router(
    *,
    auth_provider: AuthProvider | None = None,
    engine_provider: Callable[[], DictationEngine] | None = None,
) -> APIRouter:
    """Build the router carrying the dictation stream route.

    Wired into the FastAPI app under the ``/v1`` prefix in
    :func:`omnigent.server.app.create_app`.

    :param auth_provider: Optional provider used to authenticate the
        WebSocket handshake. ``None`` preserves single-user/dev
        behavior (open).
    :param engine_provider: Engine factory override for tests. Defaults
        to :func:`omnigent.server.dictation.get_engine`, which resolves
        the configured engine and loads models on first use.
    :returns: An :class:`APIRouter` carrying the stream route.
    """
    router = APIRouter()
    resolve_engine = engine_provider or get_engine
    # Router-scoped so each app (and each test app) gets its own cap.
    slots = asyncio.Semaphore(max_streams())
    preload_engine_in_background(resolve_engine)

    @router.websocket("/dictation/stream")
    async def dictation_stream(websocket: WebSocket) -> None:
        """Transcribe one dictation take (see module docstring)."""
        if auth_provider is not None and auth_provider.get_user_id(websocket) is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="authentication required",
            )
        await websocket.accept()
        timing = _TakeTiming(accepted_at=time.monotonic())
        _logger.info(
            "dictation stream connected",
            extra=debug_event("dictation_stream", phase="connected"),
        )

        if slots.locked():
            await websocket.close(
                code=_WS_CLOSE_TRY_AGAIN_LATER,
                reason="dictation is at capacity; try again shortly",
            )
            return

        async with slots:
            # Engine construction loads model weights — seconds on first
            # use. Run it off-loop; later takes reuse the shared engine.
            try:
                engine = await asyncio.to_thread(resolve_engine)
                handle: DictationStreamHandle = await asyncio.to_thread(engine.create_stream)
                timing.relay = isinstance(handle, _RemoteStream)
            except Exception:
                _logger.exception(
                    "dictation engine failed to initialize",
                    extra=debug_event("dictation_stream", phase="error", stage="engine_init"),
                )
                with contextlib.suppress(RuntimeError):
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": "dictation engine unavailable"})
                    )
                    await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
                return
            # Release the take on every exit — normal stop, abrupt browser
            # disconnect, or a crash mid-send. For the in-process engines
            # close() just frees the recognizer stream, so a best-effort
            # close on the way out is enough.
            try:
                await websocket.send_text(json.dumps({"type": "ready"}))
                timing.ready_s = time.monotonic() - timing.accepted_at
                await _pump_dictation(websocket, handle, timing)
            finally:
                timing.log()
                _logger.info(
                    "dictation stream disconnected",
                    extra=debug_event("dictation_stream", phase="disconnected"),
                )
                # An abrupt disconnect tears the ASGI task down via
                # cancellation, which would cancel this close mid-await and
                # leak the take (the remote engine holds a worker slot until
                # close). Shield it so cleanup always completes.
                with anyio.CancelScope(shield=True):
                    try:
                        await asyncio.to_thread(handle.close)
                    except Exception:  # noqa: BLE001 - fall back to a direct close
                        # During teardown the loop's thread-pool executor may
                        # already be shutting down, so offloading raises rather
                        # than running close() — which would leak the take.
                        # close() is a quick, non-blocking free for every
                        # engine, so fall back to a direct call on the loop.
                        with contextlib.suppress(Exception):
                            handle.close()

    return router


async def _pump_dictation(
    websocket: WebSocket, handle: DictationStreamHandle, timing: _TakeTiming
) -> None:
    """Shuttle audio in and transcript events out until stop/disconnect.

    :param websocket: The accepted browser-facing WebSocket.
    :param handle: The per-connection recognizer stream.
    :param timing: The take's latency counters, updated in place.
    """
    last_partial_sent = ""
    last_partial_at = 0.0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

            data = message.get("bytes")
            if data is not None:
                started = time.monotonic()
                if timing.first_audio_at is None:
                    timing.first_audio_at = started - len(data) / _PCM_BYTES_PER_SECOND
                update = await asyncio.to_thread(handle.feed_pcm16, data)
                timing.decode_s += time.monotonic() - started
                timing.audio_bytes += len(data)
                if update.finalized:
                    await websocket.send_text(
                        json.dumps({"type": "final", "text": update.finalized})
                    )
                    last_partial_sent = ""
                    last_partial_at = 0.0
                now = time.monotonic()
                if (
                    update.partial != last_partial_sent
                    and now - last_partial_at >= _PARTIAL_INTERVAL_S
                ):
                    await websocket.send_text(
                        json.dumps({"type": "partial", "text": update.partial})
                    )
                    last_partial_sent = update.partial
                    last_partial_at = now
                continue

            text_frame = message.get("text")
            if text_frame is None:
                continue
            try:
                control = json.loads(text_frame)
            except ValueError:
                continue
            if isinstance(control, dict) and control.get("type") == "stop":
                started = time.monotonic()
                timing.note_stop(started)
                tail = await asyncio.to_thread(handle.finish)
                timing.finish_s = time.monotonic() - started
                await websocket.send_text(json.dumps({"type": "stopped", "text": tail}))
                await websocket.close()
                return
            # Unknown control messages are ignored for forward compat.
    except WebSocketDisconnect:
        return
    except Exception:
        _logger.exception(
            "dictation stream failed",
            extra=debug_event("dictation_stream", phase="error", stage="pump"),
        )
        with contextlib.suppress(RuntimeError):
            await websocket.send_text(json.dumps({"type": "error", "message": "dictation failed"}))
            await websocket.close(code=_WS_CLOSE_INTERNAL_ERROR)
