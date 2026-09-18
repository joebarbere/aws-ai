"""Offline tests for the Comprehend wrappers."""

from __future__ import annotations

from typing import Any

import pytest

from aws_ai.comprehend import detect_dominant_language, detect_sentiment


class FakeComprehendClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def detect_sentiment(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return self.response

    def detect_dominant_language(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return self.response


def test_sentiment_label_and_confidence() -> None:
    client = FakeComprehendClient(
        {
            "Sentiment": "POSITIVE",
            "SentimentScore": {
                "Positive": 0.97,
                "Negative": 0.01,
                "Neutral": 0.01,
                "Mixed": 0.01,
            },
        }
    )

    result = detect_sentiment(client, "This study guide is excellent.")

    assert result.label == "POSITIVE"
    assert result.confidence == pytest.approx(0.97)
    assert result.scores["Negative"] == pytest.approx(0.01)


def test_language_code_is_forwarded() -> None:
    client = FakeComprehendClient({"Sentiment": "NEUTRAL", "SentimentScore": {"Neutral": 1.0}})

    detect_sentiment(client, "Bonjour.", language_code="fr")

    assert client.last_kwargs == {"Text": "Bonjour.", "LanguageCode": "fr"}


def test_dominant_language_picks_the_highest_score() -> None:
    client = FakeComprehendClient(
        {"Languages": [{"LanguageCode": "es", "Score": 0.2}, {"LanguageCode": "en", "Score": 0.8}]}
    )

    assert detect_dominant_language(client, "hello there") == ("en", 0.8)


def test_no_candidate_languages_raises() -> None:
    client = FakeComprehendClient({"Languages": []})

    with pytest.raises(ValueError, match="no candidate languages"):
        detect_dominant_language(client, "")
