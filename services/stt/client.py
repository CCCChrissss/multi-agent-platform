"""Thin client for the STT model, routed through the LiteLLM gateway.

Callers pass a local audio path and get back a transcript, without knowing
how or where the model actually runs. The STT service (services/stt/server.py)
exposes an OpenAI-compatible /v1/audio/transcriptions route so it's just
another provider in gateway/config.yaml (model_name: breeze-asr) -- this
gives STT calls the same unified routing/fallback/logging as LLM calls.
"""

from __future__ import annotations

from pathlib import Path

import openai

from gateway.client import transcribe as gateway_transcribe
from services.errors import ToolDependencyError, ToolInputError

MODEL = "breeze-asr"


def transcribe(audio_path: str) -> str:
    # audio_path is a local path on the caller's machine, read here before
    # upload -- gateway.client.transcribe's own open(audio_path) would raise
    # a raw, unclassified FileNotFoundError past this point, which is
    # actually a ToolInputError (retrying with a corrected path would help),
    # not the ToolDependencyError guarded_tool's fallback would assume.
    if not Path(audio_path).exists():
        raise ToolInputError(
            f"audio file not found: {audio_path}。"
            "請確認路徑正確；可先呼叫 check_audio_format 確認格式與檔案是否存在。"
        )
    try:
        return gateway_transcribe(MODEL, audio_path)
    except openai.APIStatusError as exc:
        # The STT backend (services/stt/server.py) already turns its own
        # ToolInputError/ToolDependencyError into HTTPException(400/503,
        # detail=...) -- that detail text survives inside exc's message even
        # after crossing the LiteLLM proxy + openai SDK hop, just buried in
        # wrapper noise. Use the status code (not exception type alone) to
        # tell "bad input, retry won't help without a different file" (4xx)
        # from "backend itself is unhealthy" (5xx) -- both are still openai.
        # APIStatusError, so a bare except-type check can't distinguish them.
        if exc.status_code < 500:
            raise ToolInputError(
                f"STT 服務拒絕這次轉錄請求（{exc.status_code}）：{exc}。"
                "通常代表音檔本身有問題——換一個檔案，或先用 check_audio_format 確認格式。"
            ) from exc
        raise ToolDependencyError(
            f"STT 服務回傳錯誤（{exc.status_code}）：{exc}。"
            "可能是 services.stt.server 沒啟動或轉錄過程出錯——確認 STT 服務是否在跑，稍後重試。"
        ) from exc
    except openai.APIError as exc:
        # No response at all -- LiteLLM gateway itself unreachable/timed out.
        raise ToolDependencyError(
            f"無法連線到 LiteLLM gateway 呼叫 {MODEL}：{exc}。"
            "確認 litellm gateway（見 Procfile 的 litellm: 這一行）是否在跑，稍後重試。"
        ) from exc
