"""Comprehend: sentiment and language detection.

Comprehend provisions nothing. These calls need only credentials and, for the batch/async jobs,
the IAM role and bucket from the foundation Terraform module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Sentiment:
    """A sentiment verdict and the score behind it."""

    label: str
    scores: dict[str, float]

    @property
    def confidence(self) -> float:
        """Score of the winning label."""
        return self.scores.get(self.label.capitalize(), 0.0)


def detect_sentiment(client: Any, text: str, language_code: str = "en") -> Sentiment:
    """Classify one document as POSITIVE, NEGATIVE, NEUTRAL, or MIXED.

    Args:
        client: A ``comprehend`` client.
        text: The document. Comprehend caps a single call at 5,000 UTF-8 bytes.
        language_code: Two-letter language code.

    Returns:
        The label and the full score map, so a caller can see a close call rather than
        only the winner.
    """
    response = client.detect_sentiment(Text=text, LanguageCode=language_code)
    return Sentiment(
        label=response["Sentiment"],
        scores={k: float(v) for k, v in response.get("SentimentScore", {}).items()},
    )


def detect_dominant_language(client: Any, text: str) -> tuple[str, float]:
    """Return the most likely language code and its score.

    Raises:
        ValueError: If Comprehend identifies no language at all.
    """
    languages = client.detect_dominant_language(Text=text).get("Languages", [])
    if not languages:
        raise ValueError("Comprehend returned no candidate languages")

    best = max(languages, key=lambda item: item["Score"])
    return best["LanguageCode"], float(best["Score"])
