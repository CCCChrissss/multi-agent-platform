ollama: ollama serve
litellm: uv run litellm --config gateway/config.yaml --port 4000
stt: uv run uvicorn services.stt.server:app --port 8001
notified: uv run uvicorn services.notified.server:app --port 8002
stt-agent: uv run uvicorn agents.stt.server:app --port 8003
check-agent: uv run uvicorn agents.check.server:app --port 8004
notified-agent: uv run uvicorn agents.notified.server:app --port 8005
