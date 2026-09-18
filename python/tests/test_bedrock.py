"""Offline tests: a fake client stands in for bedrock-runtime, so nothing touches the network."""

from __future__ import annotations

from typing import Any

from aws_ai.bedrock import invoke_text_model


class FakeConverseClient:
    """Records the request it was given and replays a canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_request: dict[str, Any] | None = None

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.last_request = kwargs
        return self.response


def _reply(text: str = "hello", stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 11, "outputTokens": 3},
    }


def test_returns_text_and_token_counts() -> None:
    client = FakeConverseClient(_reply("four"))

    result = invoke_text_model(client, "amazon.nova-lite-v1:0", "2+2?")

    assert result.text == "four"
    assert (result.input_tokens, result.output_tokens) == (11, 3)
    assert not result.blocked_by_guardrail


def test_guardrail_config_is_sent_only_when_asked() -> None:
    client = FakeConverseClient(_reply())

    invoke_text_model(client, "m", "hi")
    assert client.last_request is not None
    assert "guardrailConfig" not in client.last_request

    invoke_text_model(client, "m", "hi", guardrail_id="gr-123", guardrail_version="1")
    assert client.last_request["guardrailConfig"] == {
        "guardrailIdentifier": "gr-123",
        "guardrailVersion": "1",
    }


def test_system_prompt_and_inference_config_are_passed_through() -> None:
    client = FakeConverseClient(_reply())

    invoke_text_model(client, "m", "hi", system="Be terse.", max_tokens=64, temperature=0.0)

    assert client.last_request is not None
    assert client.last_request["system"] == [{"text": "Be terse."}]
    assert client.last_request["inferenceConfig"] == {"maxTokens": 64, "temperature": 0.0}


def test_guardrail_intervention_is_visible_to_the_caller() -> None:
    client = FakeConverseClient(_reply("blocked", stop_reason="guardrail_intervened"))

    result = invoke_text_model(client, "m", "pick me a stock", guardrail_id="gr-123")

    assert result.blocked_by_guardrail


def test_multiple_content_blocks_are_joined() -> None:
    client = FakeConverseClient(
        {
            "output": {"message": {"content": [{"text": "part one "}, {"text": "part two"}]}},
            "stopReason": "end_turn",
            "usage": {},
        }
    )

    assert invoke_text_model(client, "m", "hi").text == "part one part two"
