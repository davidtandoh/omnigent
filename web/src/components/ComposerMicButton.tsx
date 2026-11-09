"use client";

import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { DictationBusyError, DictationSession } from "@/lib/dictation";
import { isElectronShell, isIOSShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { ArrowUpIcon, Loader2Icon, MicIcon } from "lucide-react";
import {
  type ForwardedRef,
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

// Local-only types; speech-input.tsx already augments Window globally.
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  addEventListener: (type: string, listener: (event: Event) => void) => void;
  removeEventListener: (type: string, listener: (event: Event) => void) => void;
}

interface SpeechRecognitionEventLike extends Event {
  results: {
    readonly length: number;
    [index: number]: {
      readonly length: number;
      [index: number]: { transcript: string };
      isFinal: boolean;
    };
  };
  resultIndex: number;
}

interface SpeechRecognitionErrorEventLike extends Event {
  error: string;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

const getRecognitionCtor = (): SpeechRecognitionCtor | null => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
};

const getDefaultDictationLang = (): string => {
  if (typeof navigator === "undefined") return "en-US";
  try {
    return navigator.language || "en-US";
  } catch {
    return "en-US";
  }
};

// FFT bin ranges per bar, weighted toward voice frequencies (~100Hz–3kHz).
const BAR_BINS: readonly (readonly [number, number])[] = [
  [1, 3],
  [3, 6],
  [6, 10],
  [10, 16],
];

const BAR_BASELINE = 0.2;

export interface ComposerMicButtonProps {
  onTranscript: (text: string) => void;
  className?: string;
  /**
   * Streaming partial transcripts (server dictation only): called with the
   * revisable in-progress utterance as it forms, and with "" when the take
   * ends without finalizing it. Utterances that do finalize arrive via
   * onTranscript, which supersedes the pending interim. When absent, the
   * server path still works but only finals are inserted — the same
   * behavior the Web Speech path has always had.
   */
  onInterim?: (text: string) => void;
  disabled?: boolean;
  lang?: string;
  /** Bind the global ⌘⌥V dictation hotkey to this mic. Enable on exactly one
   *  mounted mic (the primary composer) so two don't fight for the device. */
  enableHotkey?: boolean;
  /** Fired when dictation begins. The parent should snapshot the composer text
   *  here so {@link onVoiceDiscard} can revert to it. */
  onVoiceStart?: () => void;
  /** Fired when Esc ends dictation. The parent should restore the text it
   *  snapshotted in {@link onVoiceStart}, discarding what was dictated. */
  onVoiceDiscard?: () => void;
  /** Fired after a mic click or Enter has flushed the tail into the composer.
   *  The parent sends that draft once. Esc does not call this, and neither
   *  does the ⌘⌥V hotkey (that chord only ends the take and keeps the text). */
  onVoiceSend?: () => void;
}

export interface ComposerMicButtonHandle {
  /** End the current take, preserving dictated text without flushing a trailing
   *  utterance into the just-cleared composer. No-op when idle. */
  endTake: () => void;
  /** Send from the composer's Send control during a live take: flush the last
   *  words, then send once via onVoiceSend. Returns false when no take is
   *  recording, so the caller sends the draft itself. */
  sendTake: () => boolean;
}

/** getUserMedia permission failures, distinct from transport failures. */
const isPermissionError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "NotAllowedError" || error.name === "SecurityError");

function ComposerMicButtonImpl(
  {
    onTranscript,
    className,
    onInterim,
    disabled,
    lang = getDefaultDictationLang(),
    enableHotkey = false,
    onVoiceStart,
    onVoiceDiscard,
    onVoiceSend,
  }: ComposerMicButtonProps,
  ref: ForwardedRef<ComposerMicButtonHandle>,
) {
  // Web Speech is primary on desktop browsers that have the constructor
  // (Chrome/Safari); with no constructor (Firefox), or on the surfaces in
  // prefersServer, takes use server dictation when GET /v1/info advertises it.
  // A constructor is no guarantee of a backend — Electron and plain
  // Chromium error at runtime with "network" — so a failed Web Speech
  // take falls back to the server per take (see handleError). Per-take,
  // not sticky: a transient blip in real Chrome must not permanently
  // downgrade the page to the local model.
  const [Ctor] = useState(getRecognitionCtor);
  const serverInfo = useServerInfo();
  const serverAvailable = serverInfo !== "loading" && serverInfo.dictation_available;
  // Mirrored into a ref so the mount-time recognition handlers (closed
  // over [Ctor, lang]) see the current probe result.
  const serverAvailableRef = useRef(serverAvailable);
  serverAvailableRef.current = serverAvailable;
  // When the server can dictate, these surfaces skip Web Speech. Electron has
  // the constructor but no backend, so a take fails with "network" first. On
  // touch devices and the iOS app, Safari's engine gives no partials or punctuation.
  const coarsePointer = useIsCoarsePointer();
  const prefersServer = isElectronShell() || isIOSShell() || coarsePointer;
  const [isListening, setIsListening] = useState(false);
  // Server-path only: the take has started but audio isn't flowing yet (the
  // first take cold-loads the model, up to ~40s). Shows a spinner so the click
  // isn't a dead-looking button until it flips to listening.
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const sessionRef = useRef<DictationSession | null>(null);
  const startAbortRef = useRef<AbortController | null>(null);
  // Refs so handlers aren't re-attached on every parent re-render.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const onInterimRef = useRef(onInterim);
  onInterimRef.current = onInterim;
  // Synced refs so the mount-time listeners call the latest callbacks.
  const onVoiceStartRef = useRef(onVoiceStart);
  onVoiceStartRef.current = onVoiceStart;
  const onVoiceDiscardRef = useRef(onVoiceDiscard);
  onVoiceDiscardRef.current = onVoiceDiscard;
  const onVoiceSendRef = useRef(onVoiceSend);
  onVoiceSendRef.current = onVoiceSend;
  // Set by the Esc handler so late results after a discard don't repopulate the
  // composer the parent just reverted. Cleared on the next start.
  const discardingRef = useRef(false);
  // Set by endTake when a server take is still mid-handshake (session not yet
  // attached): toggleServer sees it once start resolves and discards the
  // just-opened session instead of recording into the cleared composer.
  const endPendingRef = useRef(false);
  // Set once a finish has sent, so a sentence that arrives after the composer
  // was cleared is not written back into the empty field. Distinct from
  // discardingRef: a send must not restore the pre-take snapshot.
  const sentRef = useRef(false);
  // Web Speech emits the final result and then "end". A send waits for end so
  // that result is already in the draft.
  const pendingSendRef = useRef(false);
  // The stop in progress should send. The mic click and Enter set this; the
  // ⌘⌥V hotkey does not, so the chord still only ends the take.
  const sendOnStopRef = useRef(false);
  const stoppingRef = useRef(false);
  // Synced prop ref so the recognition result handler (closure over the
  // mount-time effect) can drop late events when the composer goes
  // disabled mid-utterance.
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  // Click guard: true between toggle() and the matching start/end event.
  // Prevents rapid double-clicks from calling recognition.start() twice,
  // which throws InvalidStateError in Chrome.
  const transitionRef = useRef(false);
  // Server-take guard, deliberately separate from transitionRef: a failed
  // Web Speech attempt fires a late "end" event that resets transitionRef,
  // which must not unlock a second server take mid-handshake.
  const serverBusyRef = useRef(false);
  // Lets the mount-time Web Speech error handler start the fallback take
  // without closing over toggleServer's identity.
  const toggleServerRef = useRef<() => Promise<void>>(async () => {});
  // Latest server partial for the current take, so a crash can preserve what
  // was spoken instead of blanking it. Set on each partial, cleared on final,
  // and reset when a take starts so a stale partial can't resurrect.
  const interimRef = useRef("");

  // Surface a failure both in the button (tooltip + red tint) and as a toast,
  // so a failed take is an explained error rather than a silent flash back to
  // the idle mic. Stable identity: only setError/showToast, both stable.
  const reportError = useCallback((message: string) => {
    setError(message);
    showToast(message);
  }, []);

  // Written via .style.transform from rAF — avoids 60Hz React re-renders.
  const barRefs = useRef<(HTMLSpanElement | null)[]>(BAR_BINS.map(() => null));

  useEffect(() => {
    if (!Ctor) return;

    const recognition = new Ctor();
    // Keep listening until the user clicks stop — no auto-stop on silence.
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = lang;

    // A dead recognizer keeps firing start/end/error after the take has
    // fallen back to the server; those stale events must not clobber the
    // server session's isListening/transition state.
    const serverTakeOwnsState = () => sessionRef.current !== null || serverBusyRef.current;

    const handleStart = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      discardingRef.current = false;
      sentRef.current = false;
      pendingSendRef.current = false;
      setError(null);
      setIsListening(true);
      // Snapshot point: let the parent record the text so Esc can revert to it.
      onVoiceStartRef.current?.();
    };
    const handleEnd = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      setIsListening(false);
      if (pendingSendRef.current && !discardingRef.current && !disabledRef.current) {
        pendingSendRef.current = false;
        sentRef.current = true;
        onVoiceSendRef.current?.();
      } else {
        pendingSendRef.current = false;
      }
    };
    const handleError = (event: Event) => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      const err = (event as SpeechRecognitionErrorEventLike).error;
      // An unavailable or blocked speech service can still use client mic
      // capture with server transcription. getUserMedia checks mic permission.
      if (
        (err === "network" || err === "service-not-allowed") &&
        serverAvailableRef.current &&
        !disabledRef.current
      ) {
        setIsListening(false);
        void toggleServerRef.current();
        return;
      }
      // "no-speech" / "aborted" are routine (silence timeout, user stop).
      if (err === "not-allowed") {
        reportError("Microphone access denied. Allow access and try again.");
      } else if (err && err !== "no-speech" && err !== "aborted") {
        reportError("Voice input isn't available on this device.");
      }
      setIsListening(false);
    };
    const handleResult = (event: Event) => {
      // Drop late events that arrive after the composer went disabled, or after
      // an Esc discard the parent has already reverted.
      if (disabledRef.current || discardingRef.current || sentRef.current) return;
      const speechEvent = event as SpeechRecognitionEventLike;
      let finalTranscript = "";
      for (let i = speechEvent.resultIndex; i < speechEvent.results.length; i += 1) {
        const result = speechEvent.results[i];
        if (result.isFinal) {
          finalTranscript += result[0]?.transcript ?? "";
        }
      }
      const trimmed = finalTranscript.trim();
      if (trimmed) onTranscriptRef.current(trimmed);
    };

    recognition.addEventListener("start", handleStart);
    recognition.addEventListener("end", handleEnd);
    recognition.addEventListener("error", handleError);
    recognition.addEventListener("result", handleResult);
    recognitionRef.current = recognition;

    return () => {
      recognition.removeEventListener("start", handleStart);
      recognition.removeEventListener("end", handleEnd);
      recognition.removeEventListener("error", handleError);
      recognition.removeEventListener("result", handleResult);
      recognition.stop();
      recognitionRef.current = null;
    };
  }, [Ctor, lang, reportError]);

  // Auto-stop if the composer goes disabled mid-dictation. Stops the
  // recognizer; the disabledRef guard in handleResult catches any final
  // events still queued before the end event fires. A server session is
  // cancelled outright (no tail flush) — the take is moot once the
  // composer can't accept text.
  useEffect(() => {
    if (!disabled || (!isListening && !connecting)) return;
    pendingSendRef.current = false;
    sendOnStopRef.current = false;
    if (startAbortRef.current) {
      endPendingRef.current = true;
      discardingRef.current = true;
      startAbortRef.current.abort();
      return;
    }
    if (sessionRef.current) {
      sessionRef.current.cancel();
      sessionRef.current = null;
      setIsListening(false);
      onInterimRef.current?.("");
      return;
    }
    try {
      recognitionRef.current?.stop();
    } catch {
      // .stop() on an already-stopped recognizer can throw in some
      // browsers; safe to ignore — the end event will reconcile state.
    }
  }, [disabled, isListening, connecting]);

  // Release the mic if the component unmounts mid-take (e.g. the
  // new-chat dialog closes while dictating).
  useEffect(
    () => () => {
      discardingRef.current = true;
      endPendingRef.current = true;
      startAbortRef.current?.abort();
      sessionRef.current?.cancel();
      sessionRef.current = null;
    },
    [],
  );

  // Second getUserMedia stream just for visualization — Web Speech API
  // hides its audio buffer. Chrome batches the permission to one prompt.
  useEffect(() => {
    if (!isListening) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let audioCtx: AudioContext | null = null;
    let rafId: number | null = null;
    // Snapshot so cleanup doesn't read a stale .current (exhaustive-deps).
    const bars = barRefs.current;

    const start = async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        // Mic permission denied just for the visualization stream — leave
        // the bars at baseline. The speech recognition error handler will
        // surface the user-facing message if it also fails.
        return;
      }
      if (cancelled) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      audioCtx = new AudioContext();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 64;
      // Built-in temporal smoothing so bars don't jitter frame-to-frame.
      analyser.smoothingTimeConstant = 0.75;
      source.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        analyser.getByteFrequencyData(data);
        for (let i = 0; i < BAR_BINS.length; i += 1) {
          const [lo, hi] = BAR_BINS[i];
          let sum = 0;
          for (let j = lo; j < hi; j += 1) sum += data[j];
          const avg = sum / (hi - lo) / 255;
          // 1.6× headroom for quiet speech; clamp at 1 to fit the button.
          const scale = Math.max(BAR_BASELINE, Math.min(1, avg * 1.6));
          const el = bars[i];
          if (el) el.style.transform = `scaleY(${scale})`;
        }
        rafId = requestAnimationFrame(tick);
      };
      rafId = requestAnimationFrame(tick);
    };

    start();

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (stream) {
        for (const track of stream.getTracks()) track.stop();
      }
      if (audioCtx && audioCtx.state !== "closed") {
        audioCtx.close();
      }
      // Reset for the next session.
      for (const el of bars) {
        if (el) el.style.transform = `scaleY(${BAR_BASELINE})`;
      }
    };
  }, [isListening]);

  // Server-dictation toggle. Start resolves only once the mic + socket
  // handshake are up, so isListening flips exactly when audio flows.
  const toggleServer = useCallback(async () => {
    if (serverBusyRef.current) {
      return;
    }
    serverBusyRef.current = true;
    const session = sessionRef.current;
    if (session) {
      sessionRef.current = null;
      stoppingRef.current = true;
      const tail = (await session.stop()).trim() || interimRef.current.trim();
      interimRef.current = "";
      stoppingRef.current = false;
      const send = sendOnStopRef.current;
      sendOnStopRef.current = false;
      const drop = disabledRef.current || discardingRef.current;
      if (!drop) {
        if (tail) onTranscriptRef.current(tail);
        else onInterimRef.current?.("");
      }
      setIsListening(false);
      serverBusyRef.current = false;
      // After the tail is in the draft. A later partial/final is dropped so
      // it cannot land in the composer the parent is about to clear.
      if (send && !drop) {
        sentRef.current = true;
        onVoiceSendRef.current?.();
      }
      return;
    }
    try {
      // Snapshot point: let the parent record the text so Esc can revert to it.
      discardingRef.current = false;
      endPendingRef.current = false;
      sentRef.current = false;
      pendingSendRef.current = false;
      interimRef.current = "";
      setConnecting(true);
      onVoiceStartRef.current?.();
      const startAbort = new AbortController();
      startAbortRef.current = startAbort;
      const next = await DictationSession.start(
        {
          onPartial: (text) => {
            // Drop late partials after an Esc discard — they'd repopulate the
            // composer the parent just reverted.
            if (!disabledRef.current && !discardingRef.current && !sentRef.current) {
              interimRef.current = text;
              onInterimRef.current?.(text);
            }
          },
          onFinal: (text) => {
            interimRef.current = "";
            const trimmed = text.trim();
            if (trimmed && !disabledRef.current && !discardingRef.current && !sentRef.current) {
              onTranscriptRef.current(trimmed);
            }
          },
          onError: () => {
            sessionRef.current = null;
            // Preserve anything spoken but not yet finalized: pin the pending
            // partial as a final rather than blanking it, so a crash mid-take
            // doesn't discard the user's words.
            const pending = interimRef.current.trim();
            interimRef.current = "";
            if (pending && !disabledRef.current && !discardingRef.current && !sentRef.current) {
              onTranscriptRef.current(pending);
            } else {
              onInterimRef.current?.("");
            }
            reportError("Voice input failed. Please try again.");
            setIsListening(false);
          },
        },
        startAbort.signal,
      );
      startAbortRef.current = null;
      if (endPendingRef.current) {
        // The take was ended (e.g. a Send tap) while the socket was still
        // connecting. Discard the session we just opened instead of attaching
        // it, so it can't record into the composer the parent already cleared.
        endPendingRef.current = false;
        next.cancel();
        setConnecting(false);
        serverBusyRef.current = false;
        return;
      }
      sessionRef.current = next;
      setError(null);
      setIsListening(true);
    } catch (startError) {
      // Stay silent if the user already ended this take mid-handshake (a Send
      // tap set endPendingRef); the toast would warn about a take they dropped.
      if (!endPendingRef.current) {
        reportError(
          startError instanceof DictationBusyError
            ? "Voice input is busy. Please try again shortly."
            : isPermissionError(startError)
              ? "Microphone access denied. Allow access and try again."
              : "Voice input isn't available on this device.",
        );
      }
      setIsListening(false);
    }
    // Reached only by the start path (the stop branch returns earlier). Clear
    // the handshake spinner and reconcile endPendingRef on success and failure.
    startAbortRef.current = null;
    setConnecting(false);
    serverBusyRef.current = false;
    endPendingRef.current = false;
  }, [reportError]);
  toggleServerRef.current = toggleServer;

  // ``send`` is true for a mic click or Enter while a take is live: flush,
  // then ask the parent to send once. The hotkey calls this with false, so
  // ⌘⌥V still only starts or stops the take.
  const toggle = useCallback(
    (send = false) => {
      // An active (or starting) server take is owned by the server path,
      // whichever mode started it.
      if (sessionRef.current || serverBusyRef.current) {
        if (sessionRef.current) sendOnStopRef.current = send;
        else if (stoppingRef.current && send) sendOnStopRef.current = true;
        void toggleServer();
        return;
      }
      if (!Ctor || (serverAvailable && prefersServer)) {
        if (serverAvailable) void toggleServer();
        return;
      }
      // Guard against rapid clicks landing before start/end event fires.
      if (transitionRef.current) return;
      const recognition = recognitionRef.current;
      if (!recognition) return;
      transitionRef.current = true;
      try {
        if (isListening) {
          if (send) pendingSendRef.current = true;
          recognition.stop();
        } else {
          // Clear the last take's send flag now, so a Send tap before "start"
          // fires still ends this take.
          sentRef.current = false;
          recognition.start();
        }
      } catch {
        // InvalidStateError from a double-call — drop the guard so the
        // user can try again, and let the next event reconcile state.
        transitionRef.current = false;
        pendingSendRef.current = false;
      }
    },
    [isListening, Ctor, serverAvailable, prefersServer, toggleServer],
  );

  // End an in-progress take without flushing a trailing utterance into the
  // composer the parent just cleared on send. discardingRef drops a result that
  // lands after teardown; the next take clears it (like the Esc discard).
  const endTake = useCallback(() => {
    // A voice send already ended this take before the parent's send called here.
    if (sentRef.current) return;
    const session = sessionRef.current;
    if (session) {
      sessionRef.current = null;
      serverBusyRef.current = false;
      discardingRef.current = true;
      session.cancel();
      setIsListening(false);
      return;
    }
    // No session yet: the take is mid-handshake or mid-stop. discardingRef drops
    // the late result or trailing tail in either case. endPendingRef only matters
    // on the start path, which cancels the resolved session and clears the
    // connecting spinner (so the button isn't idle-looking while it still rejects
    // taps); the stop path returns before reading it, so the flag is inert there.
    if (serverBusyRef.current) {
      endPendingRef.current = true;
      discardingRef.current = true;
      startAbortRef.current?.abort();
      return;
    }
    if (!isListening && !transitionRef.current) return;
    try {
      // abort() discards buffered audio; discardingRef drops a final that still
      // fires before the abort lands.
      discardingRef.current = true;
      recognitionRef.current?.abort();
    } catch {
      // Already stopping — the end event will reconcile state.
    }
  }, [isListening]);

  const sendTake = useCallback((): boolean => {
    if (disabledRef.current || sentRef.current) return false;
    if (stoppingRef.current) {
      sendOnStopRef.current = true;
      return true;
    }
    if (isListening && transitionRef.current) {
      pendingSendRef.current = true;
      return true;
    }
    if (pendingSendRef.current) return true;
    const recording =
      sessionRef.current !== null ||
      (isListening && !transitionRef.current && !serverBusyRef.current);
    if (!recording) return false;
    toggle(true);
    return true;
  }, [isListening, toggle]);
  useImperativeHandle(ref, () => ({ endTake, sendTake }), [endTake, sendTake]);

  // ⌘⌥V starts a take or ends it and keeps the text. A click or Enter sends.
  // Inert unless dictation can run and the composer is enabled.
  useVoiceDictationHotkey(toggle, enableHotkey && (Boolean(Ctor) || serverAvailable) && !disabled);

  // Enter, including Cmd/Ctrl+Enter, flushes and sends. Shift/Alt+Enter stay
  // newlines, and an IME confirmation is not a send. Esc cancels the take.
  // Capture phase, so neither key reaches the composer's stop handler.
  useEffect(() => {
    if (!isListening) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        e.stopPropagation();
        toggle(true);
      } else if (e.key === "Escape" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // Cancel: flag the discard so trailing results are dropped, revert the
        // composer, then tear the take down immediately (no tail flush, no send).
        e.preventDefault();
        e.stopPropagation();
        discardingRef.current = true;
        pendingSendRef.current = false;
        sendOnStopRef.current = false;
        onVoiceDiscardRef.current?.();
        const session = sessionRef.current;
        if (session) {
          sessionRef.current = null;
          serverBusyRef.current = false;
          session.cancel();
          setIsListening(false);
        } else {
          try {
            recognitionRef.current?.stop();
          } catch {
            // Already stopping — the end event will reconcile state.
          }
        }
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isListening, toggle]);

  if (!Ctor && !serverAvailable) return null;

  // Stable accessible name with aria-pressed signals toggle state to
  // screen readers. Error text takes over the tooltip when set.
  const a11yLabel = "Voice dictation";
  const tooltip =
    error ?? (connecting ? "Starting voice input…" : isListening ? "Send dictation" : a11yLabel);

  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      disabled={disabled}
      onClick={() => toggle(isListening || sessionRef.current !== null)}
      aria-pressed={isListening}
      aria-busy={connecting}
      aria-label={a11yLabel}
      data-server-dictation-available={serverAvailable}
      title={tooltip}
      className={cn(
        "size-9 md:size-8",
        isListening && "bg-muted/60 text-foreground",
        error && "text-destructive",
        className,
      )}
    >
      {connecting ? (
        <Loader2Icon className="size-4 animate-spin" data-icon-size="16" aria-hidden />
      ) : isListening ? (
        // Bars fade out and a send arrow fades in on hover OR keyboard focus,
        // so the listening control reads as send rather than as Interrupt.
        <span className="relative flex size-4 items-center justify-center" aria-hidden>
          <span className="flex h-full items-center gap-[2px] transition-opacity group-hover/button:opacity-0 group-focus-visible/button:opacity-0">
            {BAR_BINS.map(([lo, hi], i) => (
              <span
                key={`${lo}-${hi}`}
                ref={(el) => {
                  barRefs.current[i] = el;
                }}
                className="block h-3 w-[2px] origin-center rounded-full bg-current"
                style={{ transform: `scaleY(${BAR_BASELINE})` }}
              />
            ))}
          </span>
          <ArrowUpIcon className="absolute size-3 opacity-0 transition-opacity group-hover/button:opacity-100 group-focus-visible/button:opacity-100" />
        </span>
      ) : (
        <MicIcon className="size-4" data-icon-size="16" />
      )}
    </Button>
  );
}

export const ComposerMicButton = forwardRef(ComposerMicButtonImpl);
ComposerMicButton.displayName = "ComposerMicButton";
