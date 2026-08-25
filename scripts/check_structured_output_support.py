"""One-off check for docs/generic-agent-runtime-plan.md P2 completion
criterion #1: does each gateway model (gateway/config.yaml) accept
`response_format={"type": "json_schema", ...}`, both alone and combined with
a `tools` list in the same call (llm/tsmc_judge.py and llm/exclusion_judge.py
both mix tool-calling turns and a final structured-output turn on the same
messages list)? Results below (run 2026-08-14 against a local `litellm
--config gateway/config.yaml` + `ollama serve`):

    [OK] gemini-strong (tools=no)
    [OK] gemini-strong (tools=yes)  -- model replies with the schema-conforming JSON
    [OK] gemini-cheap  (tools=no)
    [OK] gemini-cheap  (tools=yes)  -- model chose to call the dummy tool instead
                                        (content=None, tool_calls=[...]); no error
    [OK] local-qwen    (tools=no)   -- ollama_chat, qwen2.5:3b
    [OK] local-qwen    (tools=yes)

All three models accept response_format+tools together without error, so
llm/tsmc_judge.py and llm/exclusion_judge.py apply it unconditionally --
no text-parsing *fallback* (a second, unstructured code path) needed. Re-run
this if a model in gateway/config.yaml changes.

2026-08-17: gemini-strong/gemini-cheap replaced by claude-haiku across every
workflow-declared step (explicit user request to stop using Gemini). Rerun
against claude-haiku found the API call itself succeeds (no BadRequestError,
unlike a model that flatly rejects response_format), but the tools=yes case
came back as prose *then* the JSON object with no fence around it --
Anthropic has no native json_schema response_format, so `strict: True`
evidently isn't a hard constraint on the whole reply the way it is for
Gemini/local-qwen. This script now also runs the reply through
harness.agent_loop.parse_structured_json() (not just checks for an API
error) specifically to catch that -- parse_structured_json() was fixed to
raw_decode() from the first `{` when a bare parse fails, so this now prints
[OK] for claude-haiku too. Re-run this if a model in gateway/config.yaml
changes again.

Note: this script calls the OpenAI SDK directly against the gateway, not
through gateway/client.py::chat_with_tools() -- which is why it caught real
model support while an earlier version of chat_with_tools() had a bug
silently dropping response_format entirely (found by /code-review, fixed;
see gateway/client_smoke_test.py). harness.agent_loop.parse_structured_json()
still defensively strips a stray ```json fence around otherwise-valid
content -- see its docstring for why that's kept even though the concurrent
run that motivated it turned out to be confounded by that bug.

Run with (needs `ollama serve` + `litellm --config gateway/config.yaml
--port 4000` up, e.g. via `uv run honcho start`):
    uv run python -m scripts.check_structured_output_support
"""

from __future__ import annotations

import json

from openai import BadRequestError, OpenAI

from harness.agent_loop import parse_structured_json

_BASE_URL = "http://localhost:4000"
_API_KEY = "sk-local"

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "schema": {
            "type": "object",
            "properties": {"mentions_tsmc": {"type": "boolean"}},
            "required": ["mentions_tsmc"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

_DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup__query_company_profile",
        "description": "look up a company profile",
        "parameters": {"type": "object", "properties": {"company": {"type": "string"}}, "required": ["company"]},
    },
}

_MESSAGES = [
    {"role": "system", "content": '只回覆最終判斷，格式固定為 {"mentions_tsmc": true} 或 {"mentions_tsmc": false}。'},
    {"role": "user", "content": "台積電今天股價上漲。"},
]


def _try_call(client: OpenAI, model: str, with_tools: bool) -> None:
    label = f"{model} (tools={'yes' if with_tools else 'no'})"
    kwargs: dict = {"model": model, "messages": _MESSAGES, "response_format": _SCHEMA}
    if with_tools:
        kwargs["tools"] = [_DUMMY_TOOL]
        kwargs["tool_choice"] = "auto"
    try:
        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        if message.content:
            # The API call succeeding isn't the whole check -- P2's own
            # llm/tsmc_judge.py/llm/exclusion_judge.py feed this straight
            # into parse_structured_json(), so a reply that isn't actually
            # parseable (e.g. claude-haiku's tools=yes case: prose *then*
            # the JSON object, no fence) is still a real failure, just a
            # silent one until a live judge call hits it.
            try:
                parsed = parse_structured_json(message.content)
                detail = f"content={message.content!r} parsed={parsed!r}"
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[FAIL] {label}: content={message.content!r} -- parse_structured_json() couldn't parse it: {exc}")
                return
        else:
            detail = f"tool_calls={message.tool_calls!r}"
        print(f"[OK]   {label}: {detail}")
    except BadRequestError as exc:
        print(f"[FAIL] {label}: BadRequestError: {exc}")
    except Exception as exc:
        print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")


def main() -> None:
    client = OpenAI(base_url=_BASE_URL, api_key=_API_KEY)
    for model in ["claude-haiku", "local-qwen"]:
        _try_call(client, model, with_tools=False)
        _try_call(client, model, with_tools=True)


if __name__ == "__main__":
    main()
