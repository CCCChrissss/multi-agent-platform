ollama: ollama serve
litellm: uv run litellm --config gateway/config.yaml --port 4000
stt: uv run uvicorn services.stt.server:app --port 8001
notified: uv run uvicorn services.notified.server:app --port 8002
agents: uv run uvicorn agents.runtime:app --port 8003
