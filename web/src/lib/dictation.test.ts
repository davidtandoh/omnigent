import { afterEach, describe, expect, it, vi } from "vitest";
import { DictationSession, parseDictationEvent } from "./dictation";

describe("parseDictationEvent", () => {
  it("parses the transcript event shapes", () => {
    expect(parseDictationEvent('{"type":"ready"}')).toEqual({ type: "ready" });
    expect(parseDictationEvent('{"type":"partial","text":"hel"}')).toEqual({
      type: "partial",
      text: "hel",
    });
    expect(parseDictationEvent('{"type":"final","text":"hello."}')).toEqual({
      type: "final",
      text: "hello.",
    });
    expect(parseDictationEvent('{"type":"stopped","text":""}')).toEqual({
      type: "stopped",
      text: "",
    });
    expect(parseDictationEvent('{"type":"error","message":"boom"}')).toEqual({
      type: "error",
      message: "boom",
    });
  });

  it("returns null for malformed or unknown frames", () => {
    expect(parseDictationEvent("not json")).toBeNull();
    expect(parseDictationEvent("42")).toBeNull();
    expect(parseDictationEvent("null")).toBeNull();
    expect(parseDictationEvent('{"type":"future-thing"}')).toBeNull();
    // Known types with a missing/mistyped payload are dropped, not crashed on.
    expect(parseDictationEvent('{"type":"partial"}')).toBeNull();
    expect(parseDictationEvent('{"type":"partial","text":7}')).toBeNull();
    expect(parseDictationEvent('{"type":"error"}')).toBeNull();
  });
});

describe("DictationSession startup cancellation", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("releases capture and rejects before the server becomes ready", async () => {
    const stop = vi.fn();
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] });
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia } });
    const close = vi.fn();
    vi.stubGlobal(
      "WebSocket",
      class {
        binaryType = "";
        close = close;
      },
    );
    const controller = new AbortController();
    const start = DictationSession.start(
      { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() },
      controller.signal,
    );
    const rejected = expect(start).rejects.toMatchObject({ name: "AbortError" });
    await Promise.resolve();
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(stop).not.toHaveBeenCalled();
    controller.abort();
    expect(stop).toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
    await rejected;
  });

  it("releases a microphone acquired after cancellation without opening a socket", async () => {
    const stop = vi.fn();
    let grant!: (stream: unknown) => void;
    vi.stubGlobal("navigator", {
      mediaDevices: {
        getUserMedia: vi.fn(
          () =>
            new Promise((resolve) => {
              grant = resolve;
            }),
        ),
      },
    });
    const socket = vi.fn();
    vi.stubGlobal("WebSocket", socket);
    const controller = new AbortController();
    const start = DictationSession.start(
      { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() },
      controller.signal,
    );
    const rejected = expect(start).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    grant({ getTracks: () => [{ stop }] });
    await rejected;
    expect(stop).toHaveBeenCalledTimes(1);
    expect(socket).not.toHaveBeenCalled();
  });
});
