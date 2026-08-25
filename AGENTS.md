# Multi-Agent Platform repository instructions

## Project goal

This repository builds a reusable internal multi-agent platform. The current
`stt -> check -> notified` workflows are demonstration scenarios used to
validate the platform; they are not the platform's product boundary.

When changing the code, keep platform capabilities (gateways, orchestration,
service discovery, policies, generic agent execution) separate from scenario
rules (for example, TSMC matching or insurance exclusions). Prefer declarative
workflow and policy changes over adding scenario-specific branches to shared
infrastructure.

## Working agreement

- Use Traditional Chinese for user-facing communication. The primary user is
  a university student and intern, so explain the cause, implementation choice,
  validation method, and common pitfalls without lowering engineering quality.
- Before editing files, changing project structure, installing packages, or
  running integration tests, present a scoped plan and wait for confirmation.
- Inspect the existing implementation before proposing a rewrite. Prefer the
  smallest maintainable change that follows the current architecture.
- Never claim a command or test passed unless it was actually run. Report
  skipped checks and their prerequisites explicitly.
- Do not commit secrets, `.env`, credentials, tokens, generated agent YAML, or
  local tool logs.
- This repository's writable Git remote is
  `https://github.com/CCCChrissss/multi-agent-platform.git`. Treat
  `donydony228/agent-architecture` as historical upstream context only; never
  push branches, commits, tags, or issues to that repository.

## Sources of truth

- `AGENTS.md`: project goals and contributor/agent rules. `CLAUDE.md` is only a
  legacy pointer to this file.
- `pyproject.toml` and `uv.lock`: Python dependencies. Use `uv`; do not add or
  install a dependency without explaining why and receiving confirmation.
- `.python-version`: supported local Python baseline. CI and local development
  target Python 3.11.
- `workflows/definitions/*.yaml`: workflow steps, prompts, schemas, model
  aliases, and output mappings.
- `gateway/config.yaml`: LiteLLM provider/model alias definitions.
- `mcp_servers/policy.yaml`: MCP server declarations, tool authorization, and
  memory grants.
- `docs/harness-engineering-principles.md`: required design checklist before
  changing an agent or tool loop.

## Provider and Codex boundaries

Codex is the development agent for this repository; it is not automatically a
runtime model provider. Runtime code talks to LiteLLM through an
OpenAI-compatible interface, while each workflow selects an alias declared in
`gateway/config.yaml`.

Do not replace Claude, Gemini, Ollama, or embedding aliases merely to make the
repository usable with Codex. A runtime model/provider change is a product
change: update the gateway/workflow documentation together, verify structured
output and tool calling, and run the relevant evals before recommending it.

## Development and validation

- Prefer commands that work in PowerShell as well as CI. Put Windows-specific
  setup guidance in `docs/windows-setup.md`.
- Run dependency-free compatibility checks after changing source, docs,
  workflow aliases, or project configuration:

  ```powershell
  python scripts/static_compat_check.py
  python -m services.stt.temp_audio_smoke_test
  ```

- After dependencies are installed, use the existing `uv run python -m ...`
  smoke tests documented in `docs/testing.md`.
- Tests requiring PostgreSQL, Ollama, LiteLLM, model downloads, API keys, or
  external notification services are integration tests. State these
  prerequisites and obtain confirmation before starting or installing them.
- Keep `workflows/simple_pipeline.py` compatible with its parity guard; do not
  casually modify the intentionally frozen synchronous path.

## Safety and repository hygiene

- Load credentials through environment variables and document them in
  `.env.example`; never hard-code real values.
- Preserve fail-closed policy behavior in MCP and memory authorization.
- Use atomic writes for UI-authored YAML through `demo/spec_writer.py`; do not
  bypass its validation path.
- Before pushing, inspect `git status`, the staged diff, the branch, and the
  remote. Use a normal fast-forward push to `origin/main`; never force-push.
