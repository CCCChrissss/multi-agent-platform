"""Manual smoke test for gateway/client.py's chat_with_tools() -- run with:
    uv run python -m gateway.client_smoke_test

No live services needed -- the underlying OpenAI SDK call is mocked.
Regression test for a real bug: chat_with_tools() grew a `response_format`
parameter (docs/generic-agent-runtime-plan.md P2's `from: model`) but the
body's _client.chat.completions.create(...) call never forwarded it, so
every llm/tsmc_judge.py / llm/exclusion_judge.py structured-output call
silently ran unconstrained -- caught by /code-review, not by
llm/tsmc_judge_smoke_test.py / llm/exclusion_judge_smoke_test.py, because
those mock chat_with_tools() itself and only ever checked the Python-level
kwarg, never gateway/client.py's own forwarding.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_response() -> MagicMock:
    response = MagicMock()
    response.choices[0].message.model_dump.return_value = {}
    response.model = "fake-model"
    return response


def scenario_response_format_forwarded_when_given() -> None:
    import gateway.client as m

    with patch.object(m._client.chat.completions, "create", return_value=_fake_response()) as create:
        m.chat_with_tools("model", [{"role": "user", "content": "hi"}], [], response_format={"type": "json_schema"})

    assert create.call_args.kwargs.get("response_format") == {"type": "json_schema"}, create.call_args.kwargs
    print("[response_format_forwarded] OK -- chat_with_tools() passes response_format through to the SDK call")


def scenario_response_format_omitted_when_none() -> None:
    """Not just "doesn't crash" -- a provider that 400s on an explicit
    response_format=None (unlikely but unverified) must never see the key
    at all when the caller didn't ask for structured output."""
    import gateway.client as m

    with patch.object(m._client.chat.completions, "create", return_value=_fake_response()) as create:
        m.chat_with_tools("model", [{"role": "user", "content": "hi"}], [])

    assert "response_format" not in create.call_args.kwargs, create.call_args.kwargs
    print("[response_format_omitted] OK -- no response_format key sent when the caller didn't pass one")


def main() -> None:
    scenario_response_format_forwarded_when_given()
    scenario_response_format_omitted_when_none()
    print("\nAll gateway.client smoke tests passed.")


if __name__ == "__main__":
    main()
