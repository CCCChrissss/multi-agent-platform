from __future__ import annotations

import time
import warnings
from pathlib import Path

import librosa
import torch
from transformers import pipeline

from services.errors import ToolDependencyError, ToolInputError

warnings.filterwarnings("ignore", category=UserWarning, module="transformers")

MODEL_ID = "MediaTek-Research/Breeze-ASR-25"
TARGET_SAMPLE_RATE = 16_000

_asr = None


def _get_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _load_pipeline():
    global _asr
    if _asr is None:
        device = _get_device()
        print(f"[asr] loading {MODEL_ID} on device={device} ...", flush=True)
        t0 = time.time()
        try:
            _asr = pipeline(
                "automatic-speech-recognition",
                model=MODEL_ID,
                device=device,
                dtype=torch.float16 if device != "cpu" else torch.float32,
            )
        except Exception as exc:
            raise ToolDependencyError(
                f"無法載入 ASR 模型 {MODEL_ID}（device={device}）：{exc}。"
                "可能是 HuggingFace Hub 網路不通、模型權限不足，或裝置初始化失敗——"
                "檢查本機網路與 HF cache 後稍後重試。"
            ) from exc
        print(f"[asr] model loaded in {time.time() - t0:.1f}s", flush=True)
    return _asr


def _load_audio(audio_path: str) -> dict:
    """Load mono 16 kHz audio without requiring a system FFmpeg install."""

    try:
        waveform, sample_rate = librosa.load(
            audio_path,
            sr=TARGET_SAMPLE_RATE,
            mono=True,
        )
    except Exception as exc:
        raise ToolInputError(
            f"無法讀取音檔 {audio_path}：{exc}。"
            "請確認檔案不是空檔、損壞檔或不支援的編碼格式。"
        ) from exc
    return {"array": waveform, "sampling_rate": sample_rate}


def transcribe(audio_path: str) -> dict:
    if not Path(audio_path).exists():
        raise ToolInputError(
            f"audio file not found: {audio_path}。"
            "請確認路徑正確；可先呼叫 check_audio_format 確認格式與檔案是否存在。"
        )
    audio = _load_audio(audio_path)
    asr = _load_pipeline()
    t0 = time.time()
    try:
        result = asr(audio, return_timestamps=True)
    except Exception as exc:
        raise ToolInputError(
            f"轉錄 {audio_path} 失敗：{exc}。"
            "通常是音檔本身有問題（壞檔、不支援的編碼、空檔案）——"
            "換一個檔案，或先用 check_audio_format 確認格式。"
        ) from exc
    print(f"[asr] inference done in {time.time() - t0:.1f}s", flush=True)
    return result
