"""Mic capture with pause-based live transcription, and audio playback.

Backends: on Linux, ``arecord`` / ``pw-play`` / ``aplay`` (alsa-utils); on
Windows and macOS, ``sounddevice`` (bundles PortAudio).

The voice detector compares each 30 ms frame to a rolling estimate of the
room's background level, so steady noise like a fan is ignored. Recording is
split at natural pauses and each segment is transcribed in the background while
the user keeps talking; the turn ends after a longer silence. A line typed
while listening cancels the recording and is returned instead.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import BinaryIO, Protocol

RATE = 16000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 16-bit mono

WARMUP_MS = 300
PREROLL_MS = 300
ONSET_MS = 60
PAUSE_SPLIT_MS = 600
MIN_SEGMENT_MS = 2000
MIN_VOICED_MS = 250
END_SILENCE_MS = int(os.getenv("BAYMON_END_SILENCE_MS", "1500"))
NO_SPEECH_TIMEOUT_MS = 8000
MAX_RECORDING_MS = 120_000

FLOOR_WINDOW_MS = 4000
SPEECH_RATIO = float(os.getenv("BAYMON_MIC_RATIO", "1.5"))
ABSOLUTE_MIN = 100.0
FIXED_THRESHOLD = float(os.getenv("BAYMON_MIC_THRESHOLD", "0")) or None


class AudioError(Exception):
    pass


class NothingHeard(AudioError):
    pass


@dataclass
class Utterance:
    text: str
    typed: bool = False


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def _rms(frame: bytes) -> float:
    samples = array("h", frame)
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


class VoiceDetector:
    """Classifies 30 ms frames as speech/non-speech against a rolling noise floor."""

    def __init__(self) -> None:
        self.levels: deque[float] = deque(maxlen=FLOOR_WINDOW_MS // FRAME_MS)
        self.last_raw = False
        self.level = self.floor = self.threshold = 0.0

    @property
    def warmed_up(self) -> bool:
        return len(self.levels) * FRAME_MS >= WARMUP_MS

    def update(self, frame: bytes) -> bool:
        self.level = _rms(frame)
        self.levels.append(self.level)
        self.floor = sorted(self.levels)[len(self.levels) // 10]
        self.threshold = FIXED_THRESHOLD or max(self.floor * SPEECH_RATIO, ABSOLUTE_MIN)
        raw = self.level > self.threshold
        voiced, self.last_raw = raw and self.last_raw, raw
        return voiced and self.warmed_up


def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


# --------------------------------------------------------------------------
# Mic sources
# --------------------------------------------------------------------------


class MicSource(Protocol):
    def read(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class _ArecordSource:
    def __init__(self) -> None:
        if not shutil.which("arecord"):
            raise AudioError("Mic recording needs `arecord` (install alsa-utils).")
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def read(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        data = self.proc.stdout.read(n)
        if len(data) < n:
            raise AudioError("Microphone stopped (arecord exited). Is a mic connected?")
        return data

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait()


class _SoundDeviceSource:
    def __init__(self) -> None:
        try:
            import sounddevice as sd  # type: ignore[import-not-found]
        except (ImportError, OSError) as exc:
            raise AudioError(f"Audio isn't available: {exc}") from exc
        try:
            self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME_BYTES // 2)
            self.stream.start()
        except Exception as exc:
            raise AudioError(f"Couldn't open the microphone: {exc}") from exc

    def read(self, n: int) -> bytes:
        data, _overflowed = self.stream.read(n // 2)
        return bytes(data)

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


def open_mic() -> MicSource:
    return _ArecordSource() if sys.platform.startswith("linux") else _SoundDeviceSource()


class _StreamSource:
    """Wraps a raw PCM stream (tests, files)."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream

    def read(self, n: int) -> bytes:
        data = self.stream.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Keyboard while listening
# --------------------------------------------------------------------------


def _typed_line() -> str | None:
    """Return a line if the user typed something and pressed Enter, without blocking."""
    if not sys.stdin.isatty():
        return None
    if sys.platform == "win32":
        import msvcrt

        if not msvcrt.kbhit():  # type: ignore[attr-defined]
            return None
        return input().strip()
    import select

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.readline().strip() if ready else None


# --------------------------------------------------------------------------
# Listening
# --------------------------------------------------------------------------


def listen(
    transcribe: Callable[[bytes, str], str],
    on_text: Callable[[str], None],
    source: BinaryIO | None = None,
    no_speech_timeout_ms: int | None = NO_SPEECH_TIMEOUT_MS,
) -> Utterance:
    """Record until the speaker stops, calling ``on_text`` with each transcribed segment.

    ``transcribe(wav_bytes, prompt)`` turns a WAV segment into text.
    """
    mic: MicSource = _StreamSource(source) if source is not None else open_mic()
    pool = ThreadPoolExecutor(max_workers=2)
    pending: deque[Future[str]] = deque()
    texts: list[str] = []

    def emit_ready(block: bool = False) -> None:
        while pending and (block or pending[0].done()):
            text = pending.popleft().result().strip()
            if text and any(c.isalnum() for c in text):  # Whisper returns stray punctuation for noise
                texts.append(text)
                on_text(text)

    def flush(segment: bytearray, voiced_ms: int) -> None:
        if voiced_ms >= MIN_VOICED_MS:
            pending.append(pool.submit(transcribe, to_wav(bytes(segment)), " ".join(texts)[-200:]))

    detector = VoiceDetector()
    preroll: deque[bytes] = deque(maxlen=PREROLL_MS // FRAME_MS)
    segment = bytearray()
    in_segment = False
    onset_ms = voiced_ms = since_voice_ms = elapsed_ms = 0
    heard_speech = False

    try:
        while elapsed_ms < MAX_RECORDING_MS:
            try:
                frame = mic.read(FRAME_BYTES)
            except EOFError:
                break
            elapsed_ms += FRAME_MS
            voiced = detector.update(frame)
            since_voice_ms = 0 if voiced else since_voice_ms + FRAME_MS

            if not in_segment:
                preroll.append(frame)
                onset_ms = onset_ms + FRAME_MS if voiced else 0
                if onset_ms >= ONSET_MS:
                    in_segment = heard_speech = True
                    segment = bytearray(b"".join(preroll))
                    preroll.clear()
                    voiced_ms = onset_ms
            else:
                segment += frame
                voiced_ms += FRAME_MS if voiced else 0
                segment_ms = len(segment) // (FRAME_BYTES // FRAME_MS)
                if since_voice_ms >= PAUSE_SPLIT_MS and segment_ms >= MIN_SEGMENT_MS:
                    flush(segment, voiced_ms)
                    in_segment, onset_ms = False, 0

            if heard_speech and since_voice_ms >= END_SILENCE_MS:
                break
            if not heard_speech and no_speech_timeout_ms and elapsed_ms >= no_speech_timeout_ms:
                break
            if source is None and (line := _typed_line()) is not None:
                if line:
                    return Utterance(line, typed=True)
                break
            emit_ready()

        if in_segment:
            flush(segment, voiced_ms)
        emit_ready(block=True)
    finally:
        mic.close()
        pool.shutdown(wait=False, cancel_futures=True)

    transcript = " ".join(texts).strip()
    if not transcript:
        raise NothingHeard("Didn't catch that.")
    return Utterance(transcript)


def monitor(print_line: Callable[[str], None], seconds: float = 15) -> None:
    """Show live mic levels and speech detection, for tuning in a noisy room."""
    mic = open_mic()
    detector = VoiceDetector()
    try:
        for _ in range(int(seconds * 1000 / FRAME_MS)):
            voiced = detector.update(mic.read(FRAME_BYTES))
            bar = "#" * min(int(detector.level / max(detector.threshold, 1) * 20), 40)
            print_line(f"{'SPEECH' if voiced else '  -   '} level {detector.level:6.0f}  background "
                       f"{detector.floor:6.0f}  threshold {detector.threshold:6.0f}  |{bar:<40}|")
    except KeyboardInterrupt:
        pass
    finally:
        mic.close()


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------


def play(wav: bytes) -> None:
    """Play WAV audio, blocking until done. Ctrl+C stops playback."""
    if sys.platform.startswith("linux"):
        player = next((p for p in ("pw-play", "paplay", "aplay") if shutil.which(p)), None)
        if player is None:
            raise AudioError("No audio player found (install alsa-utils or pipewire).")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
        try:
            subprocess.run([player, f.name], stderr=subprocess.DEVNULL, check=False)
        except KeyboardInterrupt:
            pass
        finally:
            os.unlink(f.name)
        return
    try:
        import sounddevice as sd  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise AudioError(f"Audio isn't available: {exc}") from exc
    with wave.open(io.BytesIO(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
        stream = sd.RawOutputStream(samplerate=w.getframerate(), channels=w.getnchannels(),
                                    dtype={1: "int8", 2: "int16", 4: "int32"}[w.getsampwidth()])
    try:
        with stream:
            stream.write(frames)
    except KeyboardInterrupt:
        stream.abort()
