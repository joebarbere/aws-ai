"""Lex V2 runtime: send text to a bot and read back what it understood.

The useful part for study is not the reply text but the *interpretation* — which intent Lex
matched, how confident it was, and which slots it filled. That is what the confidence threshold
in the conversational Terraform module arbitrates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Every Lex V2 bot gets this draft alias automatically; a published alias replaces it in
# production.
DRAFT_ALIAS_ID = "TSTALIASID"


@dataclass(frozen=True)
class BotReply:
    """What the bot said, and what it thought you meant."""

    messages: list[str]
    intent: str | None
    intent_confidence: float
    slots: dict[str, str]
    dialog_action: str

    @property
    def fell_back(self) -> bool:
        """True when Lex could not match a real intent and used the fallback."""
        return self.intent == "FallbackIntent"


def recognize_text(
    client: Any,
    bot_id: str,
    text: str,
    *,
    session_id: str = "study-session",
    bot_alias_id: str = DRAFT_ALIAS_ID,
    locale_id: str = "en_US",
) -> BotReply:
    """Send one utterance to a Lex V2 bot.

    Args:
        client: A ``lexv2-runtime`` client.
        bot_id: Bot id from the ``lex_bot_id`` Terraform output.
        text: What the user said.
        session_id: Conversation key. Reuse it across turns to keep filled slots.
        bot_alias_id: Defaults to the draft alias, which is what a freshly built bot has.
        locale_id: Must match a built locale, or Lex rejects the call.

    Returns:
        The reply, with the interpretation attached.
    """
    response = client.recognize_text(
        botId=bot_id,
        botAliasId=bot_alias_id,
        localeId=locale_id,
        sessionId=session_id,
        text=text,
    )

    interpretations = response.get("interpretations", [])
    top = interpretations[0] if interpretations else {}
    intent = top.get("intent", {})

    slots = {
        name: (value or {}).get("value", {}).get("interpretedValue", "")
        for name, value in (intent.get("slots") or {}).items()
    }

    return BotReply(
        messages=[m.get("content", "") for m in response.get("messages", [])],
        intent=intent.get("name"),
        intent_confidence=float(top.get("nluConfidence", {}).get("score", 0.0)),
        slots=slots,
        dialog_action=response.get("sessionState", {}).get("dialogAction", {}).get("type", ""),
    )
