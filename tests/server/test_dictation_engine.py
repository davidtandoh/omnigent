"""Unit tests for the dictation engine layer (no route, no WebSocket).

Everything here runs without the ``dictation`` extra except the last
test, which exercises the real sherpa-onnx engine end-to-end and skips
itself unless the extra and a model are installed (developer machines).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.server import dictation


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient dictation env configuration."""
    monkeypatch.delenv(dictation.ENGINE_ENV, raising=False)
    monkeypatch.delenv(dictation.MODEL_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.PUNCT_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.MAX_STREAMS_ENV, raising=False)
    monkeypatch.delenv(dictation.THREADS_ENV, raising=False)
    monkeypatch.delenv(dictation.PRELOAD_ENV, raising=False)


def _touch_asr_files(model_dir: Path) -> None:
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (model_dir / name).touch()


def test_availability_fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake engine is always available, extra or not."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    assert dictation.engine_availability() == (True, None)


def test_availability_extra_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the sherpa-onnx package the probe says extra_not_installed."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: None)
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_EXTRA_NOT_INSTALLED,
    )


def test_availability_models_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the package but an empty model dir the probe says models_missing."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_MODELS_MISSING,
    )


def test_availability_with_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A populated model dir plus the package reports available."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    _touch_asr_files(tmp_path)
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (True, None)


def test_pick_model_file_prefers_int8(tmp_path: Path) -> None:
    """int8 quantizations win over float exports of the same stem."""
    (tmp_path / "encoder.onnx").touch()
    (tmp_path / "encoder.int8.onnx").touch()
    picked = dictation._pick_model_file(tmp_path, "encoder")
    assert picked is not None and picked.name == "encoder.int8.onnx"


def test_max_streams_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad or non-positive values fall back to the default."""
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "5")
    assert dictation.max_streams() == 5
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "0")
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "lots")
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS


def test_num_threads_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decode-thread setting defaults to 4 and rejects bad values."""
    assert dictation.num_threads() == dictation.DEFAULT_THREADS == 4
    monkeypatch.setenv(dictation.THREADS_ENV, "2")
    assert dictation.num_threads() == 2
    for bad in ("0", "-1", "many", ""):
        monkeypatch.setenv(dictation.THREADS_ENV, bad)
        assert dictation.num_threads() == dictation.DEFAULT_THREADS


def test_preload_disabled_by_default() -> None:
    """Without the flag nothing loads at startup."""

    def _must_not_load() -> dictation.DictationEngine:
        raise AssertionError("preload ran without the flag")

    assert dictation.preload_engine_in_background(_must_not_load) is None


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_preload_loads_engine_in_background(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """A truthy flag loads the engine on a daemon thread."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    monkeypatch.setenv(dictation.PRELOAD_ENV, flag)
    loaded: list[dictation.DictationEngine] = []

    def _resolve() -> dictation.DictationEngine:
        engine = dictation.FakeDictationEngine()
        loaded.append(engine)
        return engine

    thread = dictation.preload_engine_in_background(_resolve)
    assert thread is not None and thread.daemon
    thread.join(timeout=5)
    assert len(loaded) == 1


def test_preload_skips_unavailable_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable engine is not preloaded (no model, no thread)."""
    monkeypatch.setenv(dictation.ENGINE_ENV, "does-not-exist")
    monkeypatch.setenv(dictation.PRELOAD_ENV, "1")
    assert dictation.preload_engine_in_background(dictation.FakeDictationEngine) is None


def test_preload_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed preload leaves the first take to retry the load."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    monkeypatch.setenv(dictation.PRELOAD_ENV, "1")

    def _broken() -> dictation.DictationEngine:
        raise RuntimeError("weights corrupt")

    thread = dictation.preload_engine_in_background(_broken)
    assert thread is not None
    thread.join(timeout=5)
    assert "dictation preload failed" in caplog.text


class _StubRecognizer:
    """Recognizer double whose transcript the test sets directly."""

    def __init__(self) -> None:
        self.text = ""

    def is_ready(self, stream: object) -> bool:
        return False

    def get_result(self, stream: object) -> str:
        return self.text

    def is_endpoint(self, stream: object) -> bool:
        return False


class _CountingPunct:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def add_punctuation_with_case(self, text: str) -> str:
        self.calls.append(text)
        return text.capitalize() + "."


class _StubWaveStream:
    def accept_waveform(self, sample_rate: int, samples: object) -> None:
        pass


def test_sherpa_stream_skips_repunctuating_unchanged_partial() -> None:
    """An unchanged raw partial reuses the cached punctuated text."""
    pytest.importorskip("numpy")
    import threading

    engine = object.__new__(dictation.SherpaDictationEngine)
    recognizer = _StubRecognizer()
    punct = _CountingPunct()
    engine._recognizer = recognizer
    engine._punct = punct
    engine._lock = threading.Lock()
    stream = dictation._SherpaStream(engine, _StubWaveStream())
    chunk = b"\x00\x00" * 160

    recognizer.text = "hello world"
    assert stream.feed_pcm16(chunk).partial == "Hello world."
    assert stream.feed_pcm16(chunk).partial == "Hello world."
    assert punct.calls == ["hello world"]

    recognizer.text = "hello world again"
    assert stream.feed_pcm16(chunk).partial == "Hello world again."
    assert punct.calls == ["hello world", "hello world again"]

    # Empty partial needs no model call and is not stale-cached.
    recognizer.text = ""
    assert stream.feed_pcm16(chunk).partial == ""
    assert len(punct.calls) == 2


def test_get_engine_is_a_singleton_and_failure_caches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One engine per process; a failed load leaves the slot empty for retry."""
    caplog.set_level("INFO", logger=dictation.__name__)
    monkeypatch.setattr(dictation, "_engine", None)
    # Unavailable (empty model dir) → raises and caches nothing.
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    with pytest.raises(RuntimeError):
        dictation.get_engine()
    assert dictation._engine is None
    # Becomes available (fake engine) → loads once, then reuses.
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    first = dictation.get_engine()
    assert isinstance(first, dictation.FakeDictationEngine)
    assert "dictation engine 'fake' loaded in" in caplog.text
    assert dictation.get_engine() is first


def test_get_engine_rejects_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unregistered engine name is unavailable and raises on load."""
    monkeypatch.setattr(dictation, "_engine", None)
    monkeypatch.setenv(dictation.ENGINE_ENV, "does-not-exist")
    assert dictation.engine_availability() == (False, dictation.REASON_UNKNOWN_ENGINE)
    with pytest.raises(RuntimeError):
        dictation.get_engine()


def test_register_engine_is_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered engine is selected by name with no core edits.

    Mirrors what adding Whisper looks like: one register_engine call, then
    OMNIGENT_DICTATION_ENGINE picks it up.
    """
    monkeypatch.setattr(dictation, "_engine", None)
    monkeypatch.setitem(
        dictation._ENGINE_REGISTRY,
        "probe-engine",
        dictation._EngineEntry(
            factory=dictation.FakeDictationEngine,
            available=lambda: (True, None),
        ),
    )
    monkeypatch.setenv(dictation.ENGINE_ENV, "probe-engine")
    assert dictation.engine_availability() == (True, None)
    assert isinstance(dictation.get_engine(), dictation.FakeDictationEngine)


def test_fake_stream_reveals_script_by_bytes() -> None:
    """One script word per 100 ms of audio; sentence finalizes when done."""
    word = b"\x00" * (dictation.SAMPLE_RATE * 2 // 10)
    words = dictation.FAKE_SCRIPT.split()
    stream = dictation.FakeDictationEngine().create_stream()

    update = stream.feed_pcm16(word * 2)
    assert update.partial == " ".join(words[:2])
    assert update.finalized is None

    update = stream.feed_pcm16(word * (len(words) - 2))
    assert update.partial == ""
    assert update.finalized == dictation.FAKE_SCRIPT

    # After the script completes, the stream stays quiet.
    assert stream.feed_pcm16(word).partial == ""
    assert stream.finish() == ""


def test_fake_stream_finish_returns_tail() -> None:
    """finish() mid-script returns the revealed words."""
    word = b"\x00" * (dictation.SAMPLE_RATE * 2 // 10)
    words = dictation.FAKE_SCRIPT.split()
    stream = dictation.FakeDictationEngine().create_stream()
    stream.feed_pcm16(word * 3)
    assert stream.finish() == " ".join(words[:3])


def test_sherpa_engine_transcribes_test_wav() -> None:
    """Real-model smoke test; skips unless the extra + models are installed.

    Hermetic on CI (always skipped there); on a developer machine with
    models fetched via ``scripts/fetch-dictation-models.sh`` it exercises
    the true engine: PCM in → partial/finalized text out.
    """
    pytest.importorskip("sherpa_onnx")
    asr_dir = dictation._asr_dir()
    if dictation._asr_files(asr_dir) is None:
        pytest.skip(f"no dictation ASR model in {asr_dir}")
    wavs = sorted(asr_dir.glob("test_wavs/*.wav"))
    if not wavs:
        pytest.skip("model dir has no test_wavs to decode")

    import wave

    engine = dictation.SherpaDictationEngine(asr_dir, dictation._punct_dir())
    stream = engine.create_stream()
    with wave.open(str(wavs[0])) as wav:
        assert wav.getframerate() == dictation.SAMPLE_RATE
        pcm = wav.readframes(wav.getnframes())

    texts: list[str] = []
    chunk = dictation.SAMPLE_RATE * 2 // 10  # 100 ms
    for i in range(0, len(pcm), chunk):
        update = stream.feed_pcm16(pcm[i : i + chunk])
        if update.finalized:
            texts.append(update.finalized)
    tail = stream.finish()
    if tail:
        texts.append(tail)
    transcript = " ".join(texts)
    assert len(transcript.split()) >= 3, transcript
