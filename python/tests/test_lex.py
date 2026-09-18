"""Offline tests for the Lex V2 runtime wrapper."""

from __future__ import annotations

from typing import Any

from aws_ai.lex import DRAFT_ALIAS_ID, recognize_text


class FakeLexClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def recognize_text(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return self.response


def test_matched_intent_with_slots() -> None:
    client = FakeLexClient(
        {
            "messages": [{"content": "Inference is running a trained model."}],
            "interpretations": [
                {
                    "intent": {
                        "name": "CheckExamTopic",
                        "slots": {"Topic": {"value": {"interpretedValue": "inference"}}},
                    },
                    "nluConfidence": {"score": 0.92},
                }
            ],
            "sessionState": {"dialogAction": {"type": "Close"}},
        }
    )

    reply = recognize_text(client, "bot-1", "tell me about inference")

    assert reply.intent == "CheckExamTopic"
    assert reply.intent_confidence == 0.92
    assert reply.slots == {"Topic": "inference"}
    assert reply.messages == ["Inference is running a trained model."]
    assert reply.dialog_action == "Close"
    assert not reply.fell_back


def test_fallback_is_detected() -> None:
    client = FakeLexClient(
        {
            "messages": [{"content": "Sorry, I did not understand."}],
            "interpretations": [
                {"intent": {"name": "FallbackIntent"}, "nluConfidence": {"score": 0.11}}
            ],
        }
    )

    reply = recognize_text(client, "bot-1", "what is the airspeed of a swallow")

    assert reply.fell_back
    assert reply.intent_confidence == 0.11


def test_empty_interpretations_do_not_raise() -> None:
    reply = recognize_text(FakeLexClient({}), "bot-1", "hello")

    assert reply.intent is None
    assert reply.intent_confidence == 0.0
    assert reply.slots == {}
    assert reply.messages == []


def test_unfilled_slot_becomes_empty_string() -> None:
    client = FakeLexClient(
        {
            "interpretations": [
                {
                    "intent": {"name": "CheckExamTopic", "slots": {"Topic": None}},
                    "nluConfidence": {"score": 0.5},
                }
            ]
        }
    )

    assert recognize_text(client, "bot-1", "study something").slots == {"Topic": ""}


def test_draft_alias_and_session_defaults() -> None:
    client = FakeLexClient({})

    recognize_text(client, "bot-1", "hi")

    assert client.last_kwargs is not None
    assert client.last_kwargs["botAliasId"] == DRAFT_ALIAS_ID
    assert client.last_kwargs["localeId"] == "en_US"
    assert client.last_kwargs["sessionId"] == "study-session"
