"""Dependency-local smoke test for Breeze ASR audio preprocessing.

This test deliberately replaces the heavy Hugging Face pipeline, so it does
not download or load Breeze-ASR-25.

Run from the repository root:
    python -m services.stt.breeze_asr_smoke_test
"""

from __future__ import annotations

import math
import struct
import wave

from services.stt import breeze_asr
from services.stt.temp_audio import temporary_audio_path


class _CapturingPipeline:
    def __init__(self) -> None:
        self.audio: dict | None = None
        self.return_timestamps: bool | None = None

    def __call__(self, audio: dict, *, return_timestamps: bool) -> dict:
        self.audio = audio
        self.return_timestamps = return_timestamps
        return {"text": "smoke test"}


def _write_8khz_test_wave(path: str) -> None:
    sample_rate = 8_000
    samples = [
        int(8_000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        for index in range(sample_rate // 10)
    ]
    with wave.open(path, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def main() -> None:
    original_pipeline = breeze_asr._asr
    capturing_pipeline = _CapturingPipeline()
    breeze_asr._asr = capturing_pipeline
    try:
        with temporary_audio_path(".wav") as path:
            _write_8khz_test_wave(str(path))
            result = breeze_asr.transcribe(str(path))
    finally:
        breeze_asr._asr = original_pipeline

    assert result == {"text": "smoke test"}
    assert capturing_pipeline.return_timestamps is True
    assert capturing_pipeline.audio is not None
    assert capturing_pipeline.audio["sampling_rate"] == breeze_asr.TARGET_SAMPLE_RATE
    assert capturing_pipeline.audio["array"].ndim == 1
    assert 1_590 <= len(capturing_pipeline.audio["array"]) <= 1_610
    print("[OK] audio is loaded as mono 16 kHz without FFmpeg")


if __name__ == "__main__":
    main()
