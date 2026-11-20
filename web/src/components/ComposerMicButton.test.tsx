// Tests for ComposerMicButton — Web Speech API voice dictation plus the
// server-dictation fallback.
//
// Web Speech mode: the button toggles a SpeechRecognition session; final
// transcripts are emitted via onTranscript. It renders nothing when the
// browser has no SpeechRecognition constructor AND the server offers no
// dictation. Electron's Web Speech has no backend, so Electron uses server
// dictation or, on macOS, the macOS Dictation fallback (bridge mocked here).
//
// Server mode: when there is no SpeechRecognition constructor but the
// /v1/info capability probe reports dictation_available, the button drives a
// DictationSession instead (mocked here — the real transport needs a mic,
// an AudioWorklet, and a WebSocket; the full loop runs in the Playwright
// e2e test against the server's fake engine).

import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { createRef, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { useApproveHotkey } from "@/hooks/useApproveHotkey";
import type { AnyBlock } from "@/lib/blocks";
import { CapabilitiesContext } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import type { DictationSessionEvents } from "@/lib/dictation";
import { isMacPlatform } from "@/lib/hotkeys";
import { AskUserQuestionForm } from "./blocks/AskUserQuestionForm";
import { useChatStore } from "@/store/chatStore";
import { ComposerMicButton, type ComposerMicButtonHandle } from "./ComposerMicButton";

/** Hovers the mic button and returns the tooltip it opens. */
async function hoverMicTooltip(button: HTMLElement): Promise<HTMLElement> {
  fireEvent.pointerMove(button, { pointerType: "mouse" });
  return screen.findByRole("tooltip");
}

// Controllable DictationSession stand-in for the server-mode tests. The
// factory reads the mutable spies at call time, so each test installs its
// own behavior in beforeEach.
interface SessionStub {
  stop: () => Promise<string>;
  cancel: () => void;
}
let sessionStartMock: Mock<
  (events: DictationSessionEvents, signal?: AbortSignal) => Promise<SessionStub>
>;
let sessionStopMock: Mock<() => Promise<string>>;
let sessionCancelMock: Mock<() => void>;
let sessionEvents: DictationSessionEvents | null;

vi.mock("@/lib/dictation", () => {
  class DictationBusyError extends Error {}
  return {
    DictationBusyError,
    DictationSession: {
      start: (events: DictationSessionEvents, signal?: AbortSignal) =>
        sessionStartMock(events, signal),
    },
  };
});

// Spy on the toast so tests can assert a failure is actually surfaced to the
// user, not just tucked into the button tooltip.
const showToastMock = vi.fn();
vi.mock("@/components/ui/toast", () => ({ showToast: (msg: unknown) => showToastMock(msg) }));

function installDictationSession() {
  sessionEvents = null;
  sessionStopMock = vi.fn(async () => "");
  sessionCancelMock = vi.fn();
  sessionStartMock = vi.fn(async (events: DictationSessionEvents) => {
    sessionEvents = events;
    return { stop: sessionStopMock, cancel: sessionCancelMock };
  });
}

/** Captured event handlers keyed by event type, fed by the fake recognition. */
let handlers: Record<string, (event: unknown) => void>;
let constructSpy: Mock<() => void>;
let startSpy: ReturnType<typeof vi.fn>;
let stopSpy: ReturnType<typeof vi.fn>;
let abortSpy: ReturnType<typeof vi.fn>;
let recognitionLang: string | undefined;
/** Original navigator.mediaDevices descriptor, restored after each test. */
let originalMediaDevices: PropertyDescriptor | undefined;
/** Original navigator.language descriptor, restored after each test. */
let originalLanguage: PropertyDescriptor | undefined;

function installSpeechRecognition() {
  handlers = {};
  constructSpy = vi.fn<() => void>();
  startSpy = vi.fn();
  stopSpy = vi.fn();
  abortSpy = vi.fn();
  recognitionLang = undefined;
  // A class (not an arrow fn) so `new Ctor()` is constructable — the component
  // does `new Ctor()` in its mount effect.
  class FakeRecognition {
    constructor() {
      constructSpy();
    }
    private language = "en-US";
    get lang() {
      return this.language;
    }
    set lang(value: string) {
      this.language = value;
      recognitionLang = value;
    }
    continuous = false;
    interimResults = false;
    start = startSpy;
    stop = stopSpy;
    abort = abortSpy;
    addEventListener(type: string, handler: (event: unknown) => void) {
      handlers[type] = handler;
    }
    removeEventListener() {}
  }
  vi.stubGlobal("SpeechRecognition", FakeRecognition);
}

/** Build a SpeechRecognition `result` event carrying one final transcript. */
function resultEvent(transcript: string) {
  return {
    resultIndex: 0,
    results: { length: 1, 0: { length: 1, isFinal: true, 0: { transcript } } },
  };
}

beforeEach(() => {
  installSpeechRecognition();
  installDictationSession();
  // The visualizer's getUserMedia is best-effort; reject so no AudioContext
  // (unavailable in jsdom) is ever constructed. Capture the original descriptor
  // first so afterEach can restore it — otherwise this navigator stub leaks.
  originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  originalLanguage = Object.getOwnPropertyDescriptor(navigator, "language");
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockRejectedValue(new Error("no mic")) },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  // Restore navigator.mediaDevices so the stub never leaks to other test files.
  if (originalMediaDevices) {
    Object.defineProperty(navigator, "mediaDevices", originalMediaDevices);
  } else {
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
  if (originalLanguage) {
    Object.defineProperty(navigator, "language", originalLanguage);
  } else {
    delete (navigator as { language?: unknown }).language;
  }
});

describe("ComposerMicButton", () => {
  it("renders nothing when the browser has no SpeechRecognition support", () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    const { container } = render(<ComposerMicButton onTranscript={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an idle, un-pressed dictation button when supported", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("allows a caller to override sizing without changing dictation state classes", () => {
    render(<ComposerMicButton className="size-8 md:size-7" onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    expect(button).toHaveClass("size-8", "md:size-7");
    expect(button).not.toHaveClass("size-9", "md:size-8");
    expect(button).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(button);
    act(() => handlers.start?.({}));

    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveClass("bg-muted/60");
    expect(button).not.toHaveClass("focus-visible:text-destructive");
  });

  it("defaults Web Speech to the browser language", () => {
    Object.defineProperty(navigator, "language", { configurable: true, value: "ja-JP" });

    render(<ComposerMicButton onTranscript={vi.fn()} />);

    expect(recognitionLang).toBe("ja-JP");
  });

  it("prefers an explicit language over the browser language", () => {
    Object.defineProperty(navigator, "language", { configurable: true, value: "ja-JP" });

    render(<ComposerMicButton onTranscript={vi.fn()} lang="en-GB" />);

    expect(recognitionLang).toBe("en-GB");
  });

  it("falls back to en-US when the browser language is empty", () => {
    Object.defineProperty(navigator, "language", { configurable: true, value: "" });

    render(<ComposerMicButton onTranscript={vi.fn()} />);

    expect(recognitionLang).toBe("en-US");
  });

  it("starts recognition on click and reflects the recording state", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    expect(startSpy).toHaveBeenCalledTimes(1);

    // The recognizer's "start" event flips the pressed state.
    act(() => handlers.start?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");
  });

  it("sends when the mic is clicked again while recording", async () => {
    const onVoiceSend = vi.fn();
    const onVoiceDiscard = vi.fn();
    render(
      <ComposerMicButton
        onTranscript={vi.fn()}
        onVoiceSend={onVoiceSend}
        onVoiceDiscard={onVoiceDiscard}
      />,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    act(() => handlers.start?.({}));
    expect(await hoverMicTooltip(button)).toHaveTextContent("Send dictation");
    fireEvent.click(button);
    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(onVoiceSend).not.toHaveBeenCalled();
    // The final result lands before end, and the send waits for end.
    act(() => handlers.result?.(resultEvent("hello there")));
    act(() => handlers.end?.({}));
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).not.toHaveBeenCalled();
  });

  it("delivers the trimmed final transcript via onTranscript", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    act(() => handlers.result?.(resultEvent("  hello world  ")));
    expect(onTranscript).toHaveBeenCalledWith("hello world");
  });

  it("does not emit a transcript while the composer is disabled", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} disabled />);
    // The button is disabled, but a late recognition result must still be
    // dropped by the disabled guard rather than reaching the callback.
    act(() => handlers.result?.(resultEvent("late words")));
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("surfaces a permission-denied error in the tooltip and a toast", async () => {
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    act(() => handlers.error?.({ error: "not-allowed" }));
    const message = "Microphone access denied. Allow access and try again.";
    expect(await hoverMicTooltip(button)).toHaveTextContent(message);
    expect(showToastMock).toHaveBeenCalledWith(message);
    expect(sessionStartMock).not.toHaveBeenCalled();
  });

  it("ignores routine no-speech/aborted errors (no tooltip change, no toast)", async () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    act(() => handlers.error?.({ error: "no-speech" }));
    expect(await hoverMicTooltip(button)).toHaveTextContent("Voice dictation");
    expect(showToastMock).not.toHaveBeenCalled();
  });

  it("shows the dictation chord in its tooltip only while the hotkey is enabled", async () => {
    const { unmount } = render(<ComposerMicButton onTranscript={vi.fn()} enableHotkey />);
    const tooltip = await hoverMicTooltip(screen.getByRole("button", { name: "Voice dictation" }));
    const keys = Array.from(tooltip.querySelectorAll<HTMLElement>('[data-slot="kbd"]'));
    expect(keys.map((key) => key.textContent)).toEqual([MOD_KEY, ALT_KEY, "V"]);
    unmount();

    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const plain = await hoverMicTooltip(screen.getByRole("button", { name: "Voice dictation" }));
    expect(plain.querySelector('[data-slot="kbd"]')).toBeNull();
  });

  it("snapshots via onVoiceStart when dictation begins", () => {
    const onVoiceStart = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceStart={onVoiceStart} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

    act(() => handlers.start?.({}));
    expect(onVoiceStart).toHaveBeenCalledTimes(1);
  });

  it("Enter while listening stops dictation and sends (no discard)", () => {
    const onVoiceDiscard = vi.fn();
    const onVoiceSend = vi.fn();
    const onTranscript = vi.fn();
    render(
      <ComposerMicButton
        onTranscript={onTranscript}
        onVoiceDiscard={onVoiceDiscard}
        onVoiceSend={onVoiceSend}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const e = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true);
    act(() => handlers.result?.(resultEvent("  hello world  ")));
    expect(onVoiceSend).not.toHaveBeenCalled();
    act(() => handlers.end?.({}));
    expect(onTranscript).toHaveBeenCalledWith("hello world");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);

    onTranscript.mockClear();
    act(() => handlers.result?.(resultEvent("late words")));
    expect(onTranscript).not.toHaveBeenCalled();
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("Cmd or Ctrl+Enter while listening sends and does not leave the mic running", () => {
    const onVoiceSend = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceSend={onVoiceSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const e = new KeyboardEvent("keydown", {
      key: "Enter",
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(e);
    });
    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
    act(() => handlers.end?.({}));
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("Enter during IME composition does not stop or send", () => {
    const onVoiceSend = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceSend={onVoiceSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const composing = new KeyboardEvent("keydown", {
      key: "Enter",
      isComposing: true,
      bubbles: true,
      cancelable: true,
    });
    const processing = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    Object.defineProperty(processing, "keyCode", { value: 229 });
    act(() => {
      window.dispatchEvent(composing);
      window.dispatchEvent(processing);
    });

    expect(stopSpy).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();
    expect(composing.defaultPrevented).toBe(false);
    expect(processing.defaultPrevented).toBe(false);
  });

  it("Shift+Enter and Alt+Enter while listening insert a newline and do not send", () => {
    const onVoiceSend = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceSend={onVoiceSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const shift = new KeyboardEvent("keydown", {
      key: "Enter",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    const alt = new KeyboardEvent("keydown", {
      key: "Enter",
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(shift);
      window.dispatchEvent(alt);
    });
    expect(stopSpy).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();
    expect(shift.defaultPrevented).toBe(false);
    expect(alt.defaultPrevented).toBe(false);
  });

  it("Esc while listening stops dictation and discards the text", () => {
    const onVoiceDiscard = vi.fn();
    const onVoiceSend = vi.fn();
    render(
      <ComposerMicButton
        onTranscript={vi.fn()}
        onVoiceDiscard={onVoiceDiscard}
        onVoiceSend={onVoiceSend}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
    act(() => handlers.end?.({}));
    expect(onVoiceSend).not.toHaveBeenCalled();
  });

  it("Esc while listening discards dictation without declining a pending approval", () => {
    const { submitApproval } = useChatStore.getState();
    const submit = vi.fn();
    useChatStore.setState({
      blocks: [{ type: "elicitation", elicitationId: "elic_1", status: "pending" }] as AnyBlock[],
      submitApproval: submit,
    });
    try {
      // The approval hotkey binds first, as the app shell does.
      renderHook(() => useApproveHotkey());
      const onVoiceDiscard = vi.fn();
      render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={onVoiceDiscard} />);
      const mic = screen.getByRole("button", { name: "Voice dictation" });
      fireEvent.click(mic);
      act(() => handlers.start?.({}));
      mic.focus();
      act(() => {
        mic.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
        );
      });
      expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
      expect(submit).not.toHaveBeenCalled();
    } finally {
      useChatStore.setState({ blocks: [], submitApproval });
    }
  });

  it("drops a late transcript that arrives after an Esc discard", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} onVoiceDiscard={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    // A trailing final result races in before the recognizer's end event.
    act(() => handlers.result?.(resultEvent("trailing words")));

    expect(onTranscript).not.toHaveBeenCalled();
  });

  it.each(["Enter", "Escape"])(
    "keeps %s for dictation over a question that mounted first",
    (keyName) => {
      const onSubmit = vi.fn();
      const onReject = vi.fn();
      render(
        <AskUserQuestionForm
          elicitationId="elic_mic"
          questions={[
            { id: "pick", question: "Pick one", multiSelect: false, options: [{ label: "A" }] },
          ]}
          onSubmit={onSubmit}
          onReject={onReject}
          keyboardActive
        />,
      );
      render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
      act(() => handlers.start?.({}));

      fireEvent.keyDown(document.body, { key: keyName });

      expect(stopSpy).toHaveBeenCalledTimes(1);
      expect(onSubmit).not.toHaveBeenCalled();
      expect(onReject).not.toHaveBeenCalled();
    },
  );

  describe("restarts a take the browser ends on its own", () => {
    let now: number;
    beforeEach(() => {
      now = 1_000_000;
      vi.spyOn(Date, "now").mockImplementation(() => now);
    });
    afterEach(() => {
      vi.restoreAllMocks();
    });

    /** Click the mic and let the recognizer report it started. */
    function startTake(props: Partial<Parameters<typeof ComposerMicButton>[0]> = {}) {
      const view = render(<ComposerMicButton onTranscript={vi.fn()} {...props} />);
      const button = screen.getByRole("button", { name: "Voice dictation" });
      fireEvent.click(button);
      act(() => handlers.start?.({}));
      return { ...view, button };
    }

    it("restarts after a pause and keeps listening as one take", () => {
      const onTranscript = vi.fn();
      const onVoiceStart = vi.fn();
      const { button } = startTake({ onTranscript, onVoiceStart });

      now += 2_000;
      act(() => handlers.result?.(resultEvent("first phrase")));
      now += 1_500;
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(2);
      expect(button).toHaveAttribute("aria-pressed", "true");

      now += 100;
      act(() => handlers.start?.({}));
      now += 2_000;
      act(() => handlers.result?.(resultEvent("second phrase")));

      expect(onTranscript.mock.calls).toEqual([["first phrase"], ["second phrase"]]);
      // A restart continues the take, so the Esc snapshot is taken only once.
      expect(onVoiceStart).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "true");
    });

    it("restarts through a no-speech timeout without surfacing an error", async () => {
      const { button } = startTake();

      now += 8_000;
      act(() => handlers.error?.({ error: "no-speech" }));
      expect(button).toHaveAttribute("aria-pressed", "true");
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(2);
      expect(button).toHaveAttribute("aria-pressed", "true");
      expect(await hoverMicTooltip(button)).toHaveTextContent("Send dictation");
      expect(showToastMock).not.toHaveBeenCalled();
    });

    it("does not restart after the user stops", () => {
      const { button } = startTake();

      fireEvent.click(button);
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("does not restart after an Esc discard", () => {
      const onVoiceDiscard = vi.fn();
      const { button } = startTake({ onVoiceDiscard });

      act(() => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      act(() => handlers.end?.({}));

      expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("ends the take when the user stops while a restart is starting", () => {
      const onVoiceStart = vi.fn();
      const { button } = startTake({ onVoiceStart });
      now += 3_000;
      act(() => handlers.end?.({}));
      expect(startSpy).toHaveBeenCalledTimes(2);

      fireEvent.click(button);
      // The restarted recognizer reports start after the stop, then ends.
      act(() => handlers.start?.({}));
      act(() => handlers.end?.({}));

      expect(onVoiceStart).toHaveBeenCalledTimes(1);
      expect(startSpy).toHaveBeenCalledTimes(2);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("does not restart once the composer is disabled", () => {
      const { button, rerender } = startTake();

      rerender(<ComposerMicButton onTranscript={vi.fn()} disabled />);
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("stops after a minute without recognized speech", () => {
      const { button } = startTake();
      now += 5_000;
      act(() => handlers.result?.(resultEvent("hello")));

      now += 59_000;
      act(() => handlers.end?.({}));
      expect(startSpy).toHaveBeenCalledTimes(2);
      act(() => handlers.start?.({}));

      now += 1_000;
      act(() => handlers.end?.({}));
      expect(startSpy).toHaveBeenCalledTimes(2);
      expect(button).toHaveAttribute("aria-pressed", "false");
      expect(showToastMock).not.toHaveBeenCalled();
    });

    it("gives up with an error when the recognizer keeps ending immediately", () => {
      const { button } = startTake();

      for (let restart = 0; restart < 3; restart += 1) {
        now += 200;
        act(() => handlers.end?.({}));
        act(() => handlers.start?.({}));
      }

      // Two quick restarts are tolerated; the third quick end ends the take.
      expect(startSpy).toHaveBeenCalledTimes(3);
      expect(button).toHaveAttribute("aria-pressed", "false");
      expect(showToastMock).toHaveBeenCalledWith(
        "Voice input stopped unexpectedly. Please try again.",
      );
    });

    it("does not restart a take that never reported start", () => {
      render(<ComposerMicButton onTranscript={vi.fn()} />);
      const button = screen.getByRole("button", { name: "Voice dictation" });
      fireEvent.click(button);

      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("ends the take when the stop lands before the restart reports start", () => {
      const { button } = startTake();
      now += 3_000;
      act(() => handlers.end?.({}));
      expect(startSpy).toHaveBeenCalledTimes(2);

      fireEvent.click(button);
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(2);
      expect(button).toHaveAttribute("aria-pressed", "false");
    });

    it("does not restart while the page is hidden", () => {
      const { button } = startTake();
      const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);

      now += 3_000;
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
      expect(showToastMock).not.toHaveBeenCalled();
      hidden.mockRestore();
    });

    it("ends the take on a fatal error instead of restarting", () => {
      const { button } = startTake();

      now += 3_000;
      act(() => handlers.error?.({ error: "audio-capture" }));
      act(() => handlers.end?.({}));

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "false");
      expect(showToastMock).toHaveBeenCalledWith("Voice input isn't available on this device.");
    });
  });

  it("does not intercept Enter/Esc when not listening", () => {
    const onVoiceDiscard = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={onVoiceDiscard} />);
    // Never started listening.
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(enter);
      window.dispatchEvent(esc);
    });

    expect(stopSpy).not.toHaveBeenCalled();
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(enter.defaultPrevented).toBe(false);
    expect(esc.defaultPrevented).toBe(false);
  });
});

/** ServerInfo with dictation on; the other capabilities are irrelevant here. */
const DICTATION_INFO: ServerInfo = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  enabled_connections: [],
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: "test",
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: {},
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: true,
};

const NO_DICTATION_INFO: ServerInfo = {
  ...DICTATION_INFO,
  dictation_available: false,
};

function renderServerMode(
  props: Partial<React.ComponentProps<typeof ComposerMicButton>> = {},
  info: ServerInfo = DICTATION_INFO,
) {
  // No SpeechRecognition constructor → the component must pick server mode.
  vi.stubGlobal("SpeechRecognition", undefined);
  vi.stubGlobal("webkitSpeechRecognition", undefined);
  return render(
    <CapabilitiesContext.Provider value={info}>
      <ComposerMicButton onTranscript={vi.fn()} {...props} />
    </CapabilitiesContext.Provider>,
  );
}

/** Pose as a native shell of the given kind; returns the restore function. */
function installShell(kind: "electron" | "ios"): () => void {
  const shellWindow = window as unknown as { omnigentDesktop?: unknown; omnigentNative?: unknown };
  if (kind === "electron") shellWindow.omnigentDesktop = { kind };
  else shellWindow.omnigentNative = { kind };
  return () => {
    delete shellWindow.omnigentDesktop;
    delete shellWindow.omnigentNative;
  };
}

/** Make `(pointer: coarse)` match, as on a phone; returns the restore function. */
function installCoarsePointer(): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query === "(pointer: coarse)",
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

async function clickMic() {
  // toggle() kicks off the async DictationSession.start; flush it inside act.
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  });
}

/**
 * Run `body` with the Electron preload bridge present, as the desktop shell
 * exposes it; `bridge` adds shell methods (the macOS shell's startSystemDictation).
 */
async function inElectronShell(
  body: () => Promise<void> | void,
  bridge: Record<string, unknown> = {},
) {
  const w = window as unknown as Record<string, unknown>;
  const previous = w.omnigentDesktop;
  w.omnigentDesktop = { kind: "electron", ...bridge };
  try {
    await body();
  } finally {
    if (previous === undefined) delete w.omnigentDesktop;
    else w.omnigentDesktop = previous;
  }
}

/** A message box plus its mic, wired the way the desktop composers wire them. */
function MacDesktopComposer({
  info,
  enableHotkey = false,
  withInput = true,
}: {
  info: ServerInfo | "loading";
  enableHotkey?: boolean;
  withInput?: boolean;
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  return (
    <CapabilitiesContext.Provider value={info}>
      <textarea aria-label="Message" ref={inputRef} />
      <ComposerMicButton
        onTranscript={vi.fn()}
        enableHotkey={enableHotkey}
        inputRef={withInput ? inputRef : undefined}
      />
    </CapabilitiesContext.Provider>
  );
}

/** The platform's dictation chord: ⌘⌥V on macOS, Ctrl+Alt+V elsewhere. */
function dictationChord() {
  const mac = isMacPlatform();
  return new KeyboardEvent("keydown", {
    code: "KeyV",
    altKey: true,
    metaKey: mac,
    ctrlKey: !mac,
    bubbles: true,
    cancelable: true,
  });
}

describe("ComposerMicButton (server dictation)", () => {
  it("renders the button when the server advertises dictation", () => {
    renderServerMode();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeInTheDocument();
  });

  it("renders nothing when neither Web Speech nor the server can help", () => {
    const { container } = renderServerMode({}, NO_DICTATION_INFO);
    expect(container).toBeEmptyDOMElement();
  });

  it("starts a session on click and reflects the recording state", async () => {
    renderServerMode();
    await clickMic();
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows a connecting state during the handshake, then flips to listening", async () => {
    // Hold the handshake open so the connecting window is observable (the real
    // first take cold-loads the model for up to ~40s).
    let resolveStart!: (s: SessionStub) => void;
    sessionStartMock = vi.fn((events: DictationSessionEvents) => {
      sessionEvents = events;
      return new Promise<SessionStub>((resolve) => {
        resolveStart = resolve;
      });
    });
    renderServerMode();
    const button = screen.getByRole("button", { name: "Voice dictation" });

    await act(async () => {
      fireEvent.click(button);
    });
    // Handshake in flight: busy, not yet pressed (no audio is flowing).
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(await hoverMicTooltip(button)).toHaveTextContent("Starting voice input…");

    await act(async () => {
      resolveStart({ stop: sessionStopMock, cancel: sessionCancelMock });
    });
    // Ready: the take is live and the spinner is gone.
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).toHaveAttribute("aria-pressed", "true");
  });

  it("routes partials to onInterim and finals to onTranscript", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    renderServerMode({ onTranscript, onInterim });
    await clickMic();

    act(() => sessionEvents?.onPartial("hello wor"));
    expect(onInterim).toHaveBeenCalledWith("hello wor");
    expect(onTranscript).not.toHaveBeenCalled();

    act(() => sessionEvents?.onFinal("Hello, world."));
    expect(onTranscript).toHaveBeenCalledWith("Hello, world.");
  });

  it("stop click flushes the tail into onTranscript and sends once", async () => {
    const onTranscript = vi.fn();
    const onVoiceSend = vi.fn();
    const onVoiceDiscard = vi.fn();
    sessionStopMock = vi.fn(async () => "tail words");
    renderServerMode({ onTranscript, onVoiceSend, onVoiceDiscard });
    await clickMic();
    await clickMic();

    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledWith("tail words");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    onTranscript.mockClear();
    act(() => {
      sessionEvents?.onPartial("late partial");
      sessionEvents?.onFinal("late final");
    });
    expect(onTranscript).not.toHaveBeenCalled();
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("drops a pending stop and late transcripts after unmount", async () => {
    let resolveStop!: (tail: string) => void;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceSend = vi.fn();
    const view = renderServerMode({ onTranscript, onInterim, onVoiceSend });
    await clickMic();
    await clickMic();
    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => {
      sessionEvents?.onPartial("late partial");
      sessionEvents?.onFinal("late final");
      resolveStop("tail from the previous conversation");
    });
    expect(onTranscript).not.toHaveBeenCalled();
    expect(onInterim).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();
  });

  it.each(["send", "unmount", "disable"])("aborts pending startup on %s", async (action) => {
    let signal!: AbortSignal;
    sessionStartMock = vi.fn((_events, startSignal) => {
      signal = startSignal!;
      return new Promise<SessionStub>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")));
      });
    });
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    const ref = createRef<ComposerMicButtonHandle>();
    const view = render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton ref={ref} onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    await clickMic();
    expect(signal.aborted).toBe(false);
    await act(async () => {
      if (action === "send") ref.current?.endTake();
      else if (action === "disable") {
        view.rerender(
          <CapabilitiesContext.Provider value={DICTATION_INFO}>
            <ComposerMicButton ref={ref} onTranscript={vi.fn()} disabled />
          </CapabilitiesContext.Provider>,
        );
      } else view.unmount();
    });
    expect(signal.aborted).toBe(true);
    expect(showToastMock).not.toHaveBeenCalled();
    if (action !== "unmount") {
      expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
        "aria-busy",
        "false",
      );
    }
  });

  it("stop with an empty tail clears the interim region and still sends", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceSend = vi.fn();
    renderServerMode({ onTranscript, onInterim, onVoiceSend });
    await clickMic();
    await clickMic();

    expect(onTranscript).not.toHaveBeenCalled();
    expect(onInterim).toHaveBeenCalledWith("");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("surfaces mic permission denial in the tooltip and a toast", async () => {
    sessionStartMock = vi.fn(async () => {
      throw new DOMException("denied", "NotAllowedError");
    });
    renderServerMode();
    await clickMic();
    const message = "Microphone access denied. Allow access and try again.";
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(await hoverMicTooltip(button)).toHaveTextContent(message);
    expect(showToastMock).toHaveBeenCalledWith(message);
  });

  it("a mid-take transport error resets state and surfaces the failure", async () => {
    const onInterim = vi.fn();
    renderServerMode({ onInterim });
    await clickMic();

    act(() => sessionEvents?.onError("dictation failed"));
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(await hoverMicTooltip(button)).toHaveTextContent(
      "Voice input failed. Please try again.",
    );
    expect(showToastMock).toHaveBeenCalledWith("Voice input failed. Please try again.");
    // No partial was in flight, so the interim region is just cleared.
    expect(onInterim).toHaveBeenCalledWith("");
  });

  it("preserves an in-flight partial when a take crashes", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    renderServerMode({ onTranscript, onInterim });
    await clickMic();

    // The user has spoken; a partial is showing but hasn't finalized.
    act(() => sessionEvents?.onPartial("half a sentence"));
    onInterim.mockClear();

    act(() => sessionEvents?.onError("dictation failed"));
    // The partial is pinned as a final rather than blanked, so the words survive.
    expect(onTranscript).toHaveBeenCalledWith("half a sentence");
    expect(onInterim).not.toHaveBeenCalledWith("");
  });

  it.each(["network", "service-not-allowed"])("falls back on %s", async (error) => {
    // The browser exposes Web Speech but its service rejects the request.
    // Server transcription can still use the client's microphone.
    const onInterim = vi.fn();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} onInterim={onInterim} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    expect(startSpy).toHaveBeenCalledTimes(1);
    await act(async () => handlers.error?.({ error }));

    // The take restarted on the server path, with no error tooltip for
    // the silent switch, and partials flow.
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(await hoverMicTooltip(button)).toHaveTextContent("Send dictation");
    act(() => sessionEvents?.onPartial("via server"));
    expect(onInterim).toHaveBeenCalledWith("via server");

    // Stale events from the dead recognizer must not clobber the live
    // server take's state (Chrome fires "end" after a failed start).
    act(() => handlers.end?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");

    // The fallback is per take, not sticky: after stopping, the next
    // take tries Web Speech again (a transient Chrome blip must not
    // permanently downgrade the page to the server model).
    await clickMic(); // stop the server take
    await clickMic(); // next take
    expect(startSpy).toHaveBeenCalledTimes(2);
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
  });

  it("reports a busy server distinctly from a broken one", async () => {
    const { DictationBusyError } = await import("@/lib/dictation");
    sessionStartMock = vi.fn(async () => {
      throw new DictationBusyError("at capacity");
    });
    renderServerMode();
    await clickMic();
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(await hoverMicTooltip(button)).toHaveTextContent(
      "Voice input is busy. Please try again shortly.",
    );
  });

  it.each(["network", "service-not-allowed"])("reports %s without a server", async (error) => {
    render(
      <CapabilitiesContext.Provider value={NO_DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);
    await act(async () => handlers.error?.({ error }));
    expect(sessionStartMock).not.toHaveBeenCalled();
    expect(await hoverMicTooltip(button)).toHaveTextContent(
      "Voice input isn't available on this device.",
    );
    expect(showToastMock).toHaveBeenCalledWith("Voice input isn't available on this device.");
  });

  it("cancels the session when the composer goes disabled mid-take", async () => {
    const { rerender } = renderServerMode();
    await clickMic();

    rerender(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} disabled />
      </CapabilitiesContext.Provider>,
    );
    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
  });

  it("fires onVoiceStart when a server take begins", async () => {
    const onVoiceStart = vi.fn();
    renderServerMode({ onVoiceStart });
    await clickMic();
    expect(onVoiceStart).toHaveBeenCalledTimes(1);
  });

  it.each([
    // Electron has the constructor but no backend: Web Speech fails with "network".
    { surface: "Electron", install: () => installShell("electron") },
    // The iOS app and touch browsers have Web Speech, but without partials or punctuation.
    { surface: "the iOS app", install: () => installShell("ios") },
    { surface: "a touch device", install: installCoarsePointer },
  ])("goes straight to server dictation in $surface", async ({ install }) => {
    const restore = install();
    try {
      render(
        <CapabilitiesContext.Provider value={DICTATION_INFO}>
          <ComposerMicButton onTranscript={vi.fn()} />
        </CapabilitiesContext.Provider>,
      );
      await clickMic();

      // Server path taken directly; the Web Speech recognizer never started.
      expect(sessionStartMock).toHaveBeenCalledTimes(1);
      expect(startSpy).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    } finally {
      restore();
    }
  });

  it("in Electron without server dictation or macOS Dictation, offers no mic and an inert hotkey", async () => {
    // Electron's Web Speech has no backend, so without the server (or the macOS
    // shell's Dictation) no path can work — a mic there could only fail.
    await inElectronShell(async () => {
      render(
        <CapabilitiesContext.Provider value={NO_DICTATION_INFO}>
          <ComposerMicButton onTranscript={vi.fn()} enableHotkey />
        </CapabilitiesContext.Provider>,
      );
      expect(screen.queryByRole("button", { name: "Voice dictation" })).toBeNull();
      const chord = dictationChord();
      await act(async () => {
        window.dispatchEvent(chord);
      });
      // The hook preventDefaults a chord it handles, so an unbound hotkey
      // leaves the event untouched.
      expect(chord.defaultPrevented).toBe(false);
      expect(startSpy).not.toHaveBeenCalled();
      expect(sessionStartMock).not.toHaveBeenCalled();
      expect(showToastMock).not.toHaveBeenCalled();
    });
  });

  it("in Electron, offers the mic and hotkey only once the server advertises dictation", async () => {
    await inElectronShell(async () => {
      const { rerender } = render(
        <CapabilitiesContext.Provider value="loading">
          <ComposerMicButton onTranscript={vi.fn()} enableHotkey />
        </CapabilitiesContext.Provider>,
      );
      // Nothing can be offered while the capability probe is in flight.
      expect(screen.queryByRole("button", { name: "Voice dictation" })).toBeNull();
      const loadingChord = dictationChord();
      await act(async () => {
        window.dispatchEvent(loadingChord);
      });
      expect(loadingChord.defaultPrevented).toBe(false);
      expect(sessionStartMock).not.toHaveBeenCalled();

      rerender(
        <CapabilitiesContext.Provider value={DICTATION_INFO}>
          <ComposerMicButton onTranscript={vi.fn()} enableHotkey />
        </CapabilitiesContext.Provider>,
      );
      expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
      const chord = dictationChord();
      await act(async () => {
        window.dispatchEvent(chord);
      });
      expect(chord.defaultPrevented).toBe(true);
      expect(sessionStartMock).toHaveBeenCalledTimes(1);
      expect(constructSpy).not.toHaveBeenCalled();
      expect(startSpy).not.toHaveBeenCalled();
    });
  });

  it("on macOS without server dictation, hands the take to macOS Dictation in the message box", async () => {
    // macOS Dictation types into the field focused when it starts, so record
    // the focus at the moment the shell is asked.
    const focusedAtStart: (Element | null)[] = [];
    const startSystemDictation = vi.fn(async () => {
      focusedAtStart.push(document.activeElement);
      return true;
    });
    await inElectronShell(
      async () => {
        render(<MacDesktopComposer info={NO_DICTATION_INFO} enableHotkey />);
        const message = screen.getByRole("textbox", { name: "Message" });
        const mic = screen.getByRole("button", { name: "Voice dictation" });
        expect(message).not.toHaveFocus();

        await clickMic();
        expect(focusedAtStart).toEqual([message]);

        // The hotkey works from anywhere, so focus starts elsewhere this time.
        mic.focus();
        const chord = dictationChord();
        await act(async () => {
          window.dispatchEvent(chord);
        });
        expect(chord.defaultPrevented).toBe(true);
        expect(focusedAtStart).toEqual([message, message]);

        // macOS owns the take: no in-app take, listening state, or error.
        expect(sessionStartMock).not.toHaveBeenCalled();
        expect(startSpy).not.toHaveBeenCalled();
        expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
          "aria-pressed",
          "false",
        );
        expect(showToastMock).not.toHaveBeenCalled();
      },
      { startSystemDictation },
    );
  });

  it("on macOS, prefers server dictation over macOS Dictation", async () => {
    const startSystemDictation = vi.fn(async () => true);
    await inElectronShell(
      async () => {
        render(<MacDesktopComposer info={DICTATION_INFO} />);
        await clickMic();
        expect(sessionStartMock).toHaveBeenCalledTimes(1);
        expect(startSystemDictation).not.toHaveBeenCalled();
      },
      { startSystemDictation },
    );
  });

  it("on macOS, offers the fallback only once the server reports no dictation", async () => {
    const startSystemDictation = vi.fn(async () => true);
    await inElectronShell(
      () => {
        const { rerender } = render(<MacDesktopComposer info="loading" />);
        // The server may still turn out to dictate; offer nothing until it answers.
        expect(screen.queryByRole("button", { name: "Voice dictation" })).toBeNull();
        rerender(<MacDesktopComposer info={NO_DICTATION_INFO} />);
        expect(screen.getByRole("button", { name: "Voice dictation" })).toBeInTheDocument();
      },
      { startSystemDictation },
    );
  });

  it("on macOS, reports when the shell can't start macOS Dictation", async () => {
    const startSystemDictation = vi.fn(async () => false);
    await inElectronShell(
      async () => {
        render(<MacDesktopComposer info={NO_DICTATION_INFO} />);
        await clickMic();
        expect(showToastMock).toHaveBeenCalledWith("Voice input isn't available on this device.");
      },
      { startSystemDictation },
    );
  });

  it("on macOS, offers no fallback without a message box to type into", async () => {
    await inElectronShell(
      () => {
        render(<MacDesktopComposer info={NO_DICTATION_INFO} withInput={false} />);
        expect(screen.queryByRole("button", { name: "Voice dictation" })).toBeNull();
      },
      { startSystemDictation: vi.fn(async () => true) },
    );
  });

  it("keeps Web Speech on a touch device when the server cannot dictate", async () => {
    const restore = installCoarsePointer();
    try {
      render(
        <CapabilitiesContext.Provider value={NO_DICTATION_INFO}>
          <ComposerMicButton onTranscript={vi.fn()} />
        </CapabilitiesContext.Provider>,
      );
      await clickMic();

      expect(startSpy).toHaveBeenCalledTimes(1);
      expect(sessionStartMock).not.toHaveBeenCalled();
    } finally {
      restore();
    }
  });

  it("Enter while listening ends the server take via stop and sends the tail", async () => {
    const onTranscript = vi.fn();
    const onVoiceDiscard = vi.fn();
    const onVoiceSend = vi.fn();
    sessionStopMock = vi.fn(async () => "tail words");
    renderServerMode({ onTranscript, onVoiceDiscard, onVoiceSend });
    await clickMic();

    const e = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await act(async () => {
      window.dispatchEvent(e);
    });

    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(sessionCancelMock).not.toHaveBeenCalled();
    expect(onTranscript).toHaveBeenCalledWith("tail words");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true);
  });

  it("Shift+Enter while a server take is live does not stop or send", async () => {
    const onVoiceSend = vi.fn();
    renderServerMode({ onVoiceSend });
    await clickMic();

    const e = new KeyboardEvent("keydown", {
      key: "Enter",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      window.dispatchEvent(e);
    });
    expect(sessionStopMock).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("Esc while listening cancels the server take and discards, dropping late results", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceDiscard = vi.fn();
    const onVoiceSend = vi.fn();
    renderServerMode({ onTranscript, onInterim, onVoiceDiscard, onVoiceSend });
    await clickMic();

    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
    expect(sessionStopMock).not.toHaveBeenCalled();
    expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);

    // A partial/final racing in after the cancel must not repopulate the
    // composer the parent just reverted.
    onInterim.mockClear();
    onTranscript.mockClear();
    act(() => {
      sessionEvents?.onPartial("late partial");
      sessionEvents?.onFinal("late final");
    });
    expect(onInterim).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();
  });
});

// The imperative endTake handle lets a parent end the take when it sends the
// draft. On touch devices Send is a button tap that never reaches the
// Enter-commit handler, so without this the mic keeps recording after send.
describe("ComposerMicButton (endTake)", () => {
  it("aborts a Web Speech take and keeps the dictated text (no discard)", () => {
    const onTranscript = vi.fn();
    const onVoiceDiscard = vi.fn();
    const ref = createRef<ComposerMicButtonHandle>();
    render(
      <ComposerMicButton ref={ref} onTranscript={onTranscript} onVoiceDiscard={onVoiceDiscard} />,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);
    act(() => handlers.start?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");

    act(() => ref.current?.endTake());

    // Unlike Esc, a send keeps what was dictated. abort() (not stop()) is used
    // so a trailing final can't flush into the composer the parent just cleared.
    expect(abortSpy).toHaveBeenCalledTimes(1);
    expect(stopSpy).not.toHaveBeenCalled();
    expect(onVoiceDiscard).not.toHaveBeenCalled();

    // Real abort() fires "end"; the fake doesn't, so drive it to confirm the
    // button settles back to idle.
    act(() => handlers.end?.({}));
    expect(button).toHaveAttribute("aria-pressed", "false");

    // A final the engine had already queued before abort() must be dropped, not
    // flushed into the composer the parent just cleared on send.
    act(() => handlers.result?.(resultEvent("trailing words")));
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("is a no-op when no take is in progress", () => {
    const ref = createRef<ComposerMicButtonHandle>();
    render(<ComposerMicButton ref={ref} onTranscript={vi.fn()} />);

    act(() => ref.current?.endTake());

    expect(abortSpy).not.toHaveBeenCalled();
    expect(stopSpy).not.toHaveBeenCalled();
  });

  it("cancels a server take without flushing the tail into the sent composer", async () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    const onTranscript = vi.fn();
    // A non-empty tail would surface if endTake wrongly flushed like a stop.
    sessionStopMock = vi.fn(async () => "tail words");
    const ref = createRef<ComposerMicButtonHandle>();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton ref={ref} onTranscript={onTranscript} />
      </CapabilitiesContext.Provider>,
    );
    await clickMic();
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "true");

    await act(async () => {
      ref.current?.endTake();
    });

    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
    expect(sessionStopMock).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalledWith("tail words");
    expect(button).toHaveAttribute("aria-pressed", "false");

    // A final the session had already queued before cancel() must not flush
    // into the composer the parent just cleared on send.
    act(() => {
      sessionEvents?.onFinal("late final");
    });
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("discards a server take ended mid-handshake instead of letting it go live", async () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    // Hold the handshake open so the send (endTake) lands while start is still
    // pending with no session attached yet.
    let resolveStart!: (s: SessionStub) => void;
    sessionStartMock = vi.fn((events: DictationSessionEvents) => {
      sessionEvents = events;
      return new Promise<SessionStub>((resolve) => {
        resolveStart = resolve;
      });
    });
    const ref = createRef<ComposerMicButtonHandle>();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton ref={ref} onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    await act(async () => {
      fireEvent.click(button);
    });
    // Handshake in flight when the parent sends.
    expect(button).toHaveAttribute("aria-busy", "true");
    act(() => ref.current?.endTake());

    // The take is marked for teardown, but the handshake is still in flight, so
    // the button keeps showing busy instead of looking idle yet rejecting taps.
    expect(button).toHaveAttribute("aria-busy", "true");

    // Resolving the handshake must discard the session, not attach a live take.
    await act(async () => {
      resolveStart({ stop: sessionStopMock, cancel: sessionCancelMock });
    });
    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
    expect(sessionStopMock).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("stays silent when the handshake fails after the take is ended mid-send", async () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    // Hold the handshake open so the send (endTake) lands while start is still
    // pending, then fail it. The failure is for a take the user already ended,
    // so it must clear the spinner without warning about a dropped take.
    let rejectStart!: (reason: unknown) => void;
    sessionStartMock = vi.fn((events: DictationSessionEvents) => {
      sessionEvents = events;
      return new Promise<SessionStub>((_resolve, reject) => {
        rejectStart = reject;
      });
    });
    const ref = createRef<ComposerMicButtonHandle>();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton ref={ref} onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    await act(async () => {
      fireEvent.click(button);
    });
    expect(button).toHaveAttribute("aria-busy", "true");
    act(() => ref.current?.endTake());

    await act(async () => {
      rejectStart(new Error("socket refused"));
    });
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(await hoverMicTooltip(button)).toHaveTextContent("Voice dictation");
    expect(showToastMock).not.toHaveBeenCalled();
  });

  it("drops the tail from an in-flight server stop ended on send", async () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    const onTranscript = vi.fn();
    // Hold session.stop() open so the send (endTake) lands while the stop is
    // still pending and no session is attached — its tail must not flush.
    let resolveStop!: (tail: string) => void;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const ref = createRef<ComposerMicButtonHandle>();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton ref={ref} onTranscript={onTranscript} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });
    await clickMic();
    expect(button).toHaveAttribute("aria-pressed", "true");

    // Tap the mic again to end the take: this begins the stop, but its promise
    // stays pending, so the session is detached and serverBusyRef stays set.
    await act(async () => {
      fireEvent.click(button);
    });
    expect(sessionStopMock).toHaveBeenCalledTimes(1);

    // The parent's Send lands while the stop is still in flight.
    act(() => ref.current?.endTake());

    // Resolving the stop with a trailing tail must not repopulate the composer.
    await act(async () => {
      resolveStop("tail words");
    });
    expect(onTranscript).not.toHaveBeenCalledWith("tail words");
    expect(button).toHaveAttribute("aria-pressed", "false");
  });
});

// The composer's Send control calls sendTake during a live take. The mic then
// flushes the take (stop, wait for the tail) and sends once via onVoiceSend, so
// words spoken after the last partial are not lost.
describe("ComposerMicButton (sendTake)", () => {
  it.each([false, true])("preserves a partial when stop has no tail (send: %s)", async (send) => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceSend = vi.fn();
    const ref = createRef<ComposerMicButtonHandle>();
    renderServerMode({ ref, onTranscript, onInterim, onVoiceSend, enableHotkey: true });
    await clickMic();
    act(() => sessionEvents?.onPartial("pending words"));
    onInterim.mockClear();
    await act(async () => {
      if (send) ref.current?.sendTake();
      else
        fireEvent.keyDown(window, {
          code: "KeyV",
          ctrlKey: !isMacPlatform(),
          metaKey: isMacPlatform(),
          altKey: true,
        });
    });
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith("pending words");
    expect(onInterim).not.toHaveBeenCalled();
    expect(onVoiceSend).toHaveBeenCalledTimes(send ? 1 : 0);
  });

  it("accepts Send during a keep-text stop and waits for the tail once", async () => {
    let resolveStop!: (tail: string) => void;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const onTranscript = vi.fn();
    const onVoiceSend = vi.fn();
    const ref = createRef<ComposerMicButtonHandle>();
    renderServerMode({ ref, onTranscript, onVoiceSend, enableHotkey: true });
    await clickMic();
    await act(async () => {
      fireEvent.keyDown(window, {
        code: "KeyV",
        ctrlKey: !isMacPlatform(),
        metaKey: isMacPlatform(),
        altKey: true,
      });
    });
    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    act(() => {
      expect(ref.current?.sendTake()).toBe(true);
      expect(ref.current?.sendTake()).toBe(true);
    });
    expect(onVoiceSend).not.toHaveBeenCalled();
    await act(async () => resolveStop("last words"));
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith("last words");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onTranscript.mock.invocationCallOrder[0]).toBeLessThan(
      onVoiceSend.mock.invocationCallOrder[0],
    );
    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(sessionCancelMock).not.toHaveBeenCalled();
  });

  it("flushes a server take sent before its final partial, then sends once", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceSend = vi.fn();
    let resolveStop!: (tail: string) => void;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const ref = createRef<ComposerMicButtonHandle>();
    renderServerMode({ ref, onTranscript, onInterim, onVoiceSend });
    await clickMic();
    act(() => sessionEvents?.onPartial("send the last"));

    let taken = false;
    await act(async () => {
      taken = ref.current?.sendTake() ?? false;
    });

    // The mic owns the send: it stops (flushes) instead of cancelling.
    expect(taken).toBe(true);
    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(sessionCancelMock).not.toHaveBeenCalled();
    expect(onVoiceSend).not.toHaveBeenCalled();

    // A second tap while the tail is in flight waits for the same send.
    let retaken = false;
    act(() => {
      retaken = ref.current?.sendTake() ?? false;
    });
    expect(retaken).toBe(true);
    expect(sessionStopMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveStop("send the last words");
    });
    expect(onTranscript).toHaveBeenCalledWith("send the last words");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onTranscript.mock.invocationCallOrder[0]).toBeLessThan(
      onVoiceSend.mock.invocationCallOrder[0],
    );
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    // The send already ended the take; the parent's endTake is a no-op.
    act(() => ref.current?.endTake());
    expect(sessionCancelMock).not.toHaveBeenCalled();
  });

  it("accepts Send during a Web Speech keep-text stop but not startup", () => {
    const onTranscript = vi.fn();
    const ref = createRef<ComposerMicButtonHandle>();
    const onVoiceSend = vi.fn(() => ref.current?.endTake());
    render(
      <ComposerMicButton
        ref={ref}
        enableHotkey
        onTranscript={onTranscript}
        onVoiceSend={onVoiceSend}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    expect(ref.current?.sendTake()).toBe(false);
    act(() => handlers.start?.({}));
    act(() => handlers.result?.(resultEvent("First sentence.")));

    fireEvent.keyDown(window, {
      code: "KeyV",
      ctrlKey: !isMacPlatform(),
      metaKey: isMacPlatform(),
      altKey: true,
    });
    expect(stopSpy).toHaveBeenCalledTimes(1);
    act(() => {
      expect(ref.current?.sendTake()).toBe(true);
      expect(ref.current?.sendTake()).toBe(true);
    });
    expect(onVoiceSend).not.toHaveBeenCalled();
    expect(stopSpy).toHaveBeenCalledTimes(1);
    act(() => handlers.result?.(resultEvent("Last words.")));
    act(() => handlers.end?.({}));

    expect(onTranscript.mock.calls).toEqual([["First sentence."], ["Last words."]]);
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
    expect(onTranscript.mock.invocationCallOrder[1]).toBeLessThan(
      onVoiceSend.mock.invocationCallOrder[0],
    );
    expect(abortSpy).not.toHaveBeenCalled();
    act(() => handlers.end?.({}));
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("stops a Web Speech take so its final lands before the single send", () => {
    const onTranscript = vi.fn();
    const onVoiceSend = vi.fn();
    const ref = createRef<ComposerMicButtonHandle>();
    render(<ComposerMicButton ref={ref} onTranscript={onTranscript} onVoiceSend={onVoiceSend} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    let taken = false;
    act(() => {
      taken = ref.current?.sendTake() ?? false;
    });

    expect(taken).toBe(true);
    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(abortSpy).not.toHaveBeenCalled();
    act(() => handlers.result?.(resultEvent("last words")));
    act(() => handlers.end?.({}));
    expect(onTranscript).toHaveBeenCalledWith("last words");
    expect(onVoiceSend).toHaveBeenCalledTimes(1);
  });

  it("leaves the send to the caller when no take is recording", async () => {
    const ref = createRef<ComposerMicButtonHandle>();
    let resolveStart!: (s: SessionStub) => void;
    sessionStartMock = vi.fn((events: DictationSessionEvents) => {
      sessionEvents = events;
      return new Promise<SessionStub>((resolve) => {
        resolveStart = resolve;
      });
    });
    renderServerMode({ ref });
    expect(ref.current?.sendTake()).toBe(false);

    // Mid-handshake nothing has been spoken, so the caller sends directly.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    });
    expect(ref.current?.sendTake()).toBe(false);
    await act(async () => {
      resolveStart({ stop: sessionStopMock, cancel: sessionCancelMock });
    });
    expect(sessionStopMock).not.toHaveBeenCalled();
  });
});
