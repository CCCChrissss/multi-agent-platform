"""FastAPI service hosting Breeze-ASR-25 inference.

Runs as a persistent process (like the LiteLLM gateway) so the model loads
once and stays resident in GPU/MPS memory, instead of every workflow run
loading its own copy in-process. The actual model logic still lives in
services/stt/breeze_asr.py -- this module is only the HTTP transport
around it.

Exposes an OpenAI-compatible /v1/audio/transcriptions route (rather than a
bespoke schema) so LiteLLM can front this service like any other provider in
gateway/config.yaml -- `model`/`response_format` are accepted because the
OpenAI client always sends them, but this service only ever runs one model.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile

from services.errors import ToolDependencyError, ToolInputError
from services.stt.breeze_asr import transcribe as asr_transcribe
from services.stt.temp_audio import temporary_audio_path

app = FastAPI()


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile, model: str = Form(...), response_format: str = Form("json")) -> dict:
    suffix = Path(file.filename or "audio.wav").suffix
    with temporary_audio_path(suffix) as audio_path:
        audio_path.write_bytes(await file.read())
        try:
            result = asr_transcribe(str(audio_path))
        except ToolInputError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        except ToolDependencyError as exc:
            raise HTTPException(503, detail=str(exc)) from exc
    return {"text": result["text"]}
