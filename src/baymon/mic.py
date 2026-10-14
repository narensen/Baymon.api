"""Mic capture with pause-based live transcription.

Audio is read from ``arecord`` as raw 16 kHz mono PCM in 30 ms frames. An
energy-based voice detector compares each frame to a rolling estimate of the
room's background level (so steady noise like a fan is ignored) and splits the
recording at natural pauses; each segment is sent to Whisper in the background
while you keep talking, so text appears phrase by phrase. Recording stops on
its own after a longer silence, or when Enter is pressed. A line typed while
listening (e.g. ``/type``) cancels the recording and is returned instead.
"""

from __future__ import annotations

import io
import math
import os
import select
import shutil
import subprocess
import sys
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from typing import BinaryIO, Callable

from baymon import services
from baymon.services import ServiceError

RATE = 16000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 16-bit mono

WARMUP_MS = 300  # background level sampled before detection starts
PREROLL_MS = 300  # audio kept from before speech onset so first syllables aren't clipped
ONSET_MS = 60  # consecutive voiced audio needed to count as speech
PAUSE_SPLIT_MS = 600  # pause that ends a segment...
MIN_SEGMENT_MS = 2000  # ...once the segment is at least this long (limits Groq requests)
MIN_VOICED_MS = 250  # segments with less voiced audio are treated as noise
END_SILENCE_MS = int(os.getenv("BAYMON_END_SILENCE_MS", "1500"))  # silence that ends the turn
NO_SPEECH_TIMEOUT_MS = 8000
MAX_RECORDING_MS = 120_000

# Background level = 10th percentile of frame loudness over the last ~4 s. Steady
# noise (fans, hum) sets that floor, while speech keeps dipping back down to it
# between syllables. A frame is speech when it's SPEECH_RATIO times louder than it.
FLOOR_WINDOW_MS = 4000
SPEECH_RATIO = float(os.getenv("BAYMON_MIC_RATIO", "1.5"))
ABSOLUTE_MIN = 100.0  # 16-bit RMS; keeps a digitally silent mic from triggering
FIXED_THRESHOLD = float(os.getenv("BAYMON_MIC_THRESHOLD", "0")) or None


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
        # Require two loud frames in a row so isolated noise blips don't count.
        voiced, self.last_raw = raw and self.last_raw, raw
        return voiced and self.warmed_up


def _open_mic() -> subprocess.Popen[bytes]:
    if not shutil.which("arecord"):
        raise ServiceError("Mic recording needs `arecord` (alsa-utils).", "mic_unavailable")
    return subprocess.Popen(
        ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _to_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


@dataclass
class Utterance:
    text: str
    typed: bool = False  # True if the user typed a line instead of speaking


def _transcribe(pcm: bytes, prompt: str) -> str:
    try:
        text = services.listen(_to_wav(pcm), "segment.wav", prompt=prompt or None)
    except ServiceError as exc:
        if exc.code == "transcription_empty":
            return ""
        raise
    # Whisper returns stray punctuation for noise; drop anything without words.
    return text if any(c.isalnum() for c in text) else ""


def _typed_line() -> str | None:
    """Return a line if the user pressed Enter on the terminal, without blocking."""
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.readline().strip() if ready else None


def stream_transcribe(
    on_text: Callable[[str], None],
    source: BinaryIO | None = None,
    no_speech_timeout_ms: int | None = NO_SPEECH_TIMEOUT_MS,
) -> Utterance:
    """Record until the speaker stops, calling ``on_text`` with each transcribed segment.

    ``source`` is a raw 16 kHz mono S16_LE stream; by default the mic via arecord.
    ``no_speech_timeout_ms=None`` waits for speech indefinitely.
    Pressing Enter on an empty line stops recording early; typing a non-empty
    line cancels it and returns that line with ``typed=True``.
    Raises ServiceError if nothing was said.
    """
    proc = None
    if source is None:
        proc = _open_mic()
        assert proc.stdout is not None
        source = proc.stdout

    pool = ThreadPoolExecutor(max_workers=2)
    pending: deque[Future[str]] = deque()
    texts: list[str] = []

    def emit_ready(block: bool = False) -> None:
        while pending and (block or pending[0].done()):
            text = pending.popleft().result().strip()
            if text:
                texts.append(text)
                on_text(text)

    def flush(segment: bytearray, voiced_ms: int) -> None:
        if voiced_ms >= MIN_VOICED_MS:
            # Previous text as the prompt helps Whisper stitch words across splits.
            pending.append(pool.submit(_transcribe, bytes(segment), " ".join(texts)[-200:]))

    detector = VoiceDetector()
    preroll: deque[bytes] = deque(maxlen=PREROLL_MS // FRAME_MS)
    segment = bytearray()
    in_segment = False
    onset_ms = voiced_ms = since_voice_ms = elapsed_ms = 0
    heard_speech = False

    try:
        while elapsed_ms < MAX_RECORDING_MS:
            frame = source.read(FRAME_BYTES)
            if not frame or len(frame) < FRAME_BYTES:
                if proc is not None:
                    raise ServiceError("Microphone stopped (arecord exited). Is a mic connected?", "mic_unavailable")
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
            if (line := _typed_line()) is not None:
                if line:
                    return Utterance(line, typed=True)
                break
            emit_ready()

        if in_segment:
            flush(segment, voiced_ms)
        emit_ready(block=True)
    finally:
        if proc:
            proc.terminate()
            proc.wait()
        pool.shutdown(wait=False, cancel_futures=True)

    transcript = " ".join(texts).strip()
    if not transcript:
        raise ServiceError("Didn't catch that. Try again.", "transcription_empty")
    return Utterance(transcript)


def monitor(seconds: float = 15) -> None:
    """Print live mic levels and speech detection, for tuning in a noisy room."""
    proc = _open_mic()
    assert proc.stdout is not None
    detector = VoiceDetector()
    print("Stay quiet for a few seconds, then talk. Ctrl+C to stop.\n")
    try:
        for _ in range(int(seconds * 1000 / FRAME_MS)):
            frame = proc.stdout.read(FRAME_BYTES)
            if len(frame) < FRAME_BYTES:
                raise ServiceError("Microphone stopped (arecord exited). Is a mic connected?", "mic_unavailable")
            voiced = detector.update(frame)
            bar = "#" * min(int(detector.level / max(detector.threshold, 1) * 20), 60)
            state = "SPEECH" if voiced else "  -   "
            print(
                f"\r{state} level {detector.level:6.0f}  background {detector.floor:6.0f}  "
                f"threshold {detector.threshold:6.0f}  |{bar:<40.40}|",
                end="",
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    finally:
        print()
        proc.terminate()
        proc.wait()
