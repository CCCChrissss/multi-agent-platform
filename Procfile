# Windows: use scripts/dev.ps1 services; Ollama is managed separately.
# Manual commands assume uv sync --locked has already completed.
ollama: ollama serve
litellm: uv run --no-sync litellm --config gateway/config.yaml --host 127.0.0.1 --port 4000
stt: uv run --no-sync uvicorn services.stt.server:app --port 8001
notified: uv run --no-sync uvicorn services.notified.server:app --port 8002
agents: uv run --no-sync python -m agents.server
