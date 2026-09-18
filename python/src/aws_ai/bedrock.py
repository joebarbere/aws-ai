"""Bedrock runtime calls, using the Converse API.

Converse is the model-agnostic entry point: the same request shape works for Anthropic, Amazon
Nova, Meta, and Mistral models, where InvokeModel requires each vendor's own JSON body. That
difference is worth knowing for the exam and worth having in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelReply:
    """What came back from a Converse call."""

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int

    @property
    def blocked_by_guardrail(self) -> bool:
        """True when a guardrail intervened instead of the model finishing normally."""
        return self.stop_reason == "guardrail_intervened"


def invoke_text_model(
    client: Any,
    model_id: str,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    guardrail_id: str | None = None,
    guardrail_version: str = "DRAFT",
) -> ModelReply:
    """Send one prompt to a Bedrock text model and return the reply.

    Args:
        client: A ``bedrock-runtime`` client.
        model_id: Model or inference-profile id, e.g. ``amazon.nova-lite-v1:0``.
        prompt: The user turn.
        system: Optional system instruction.
        max_tokens: Cap on generated tokens.
        temperature: Sampling temperature.
        guardrail_id: Guardrail to apply; the id output by the bedrock Terraform module.
        guardrail_version: Published version, or ``DRAFT`` while iterating.

    Returns:
        A ModelReply. Check ``blocked_by_guardrail`` before trusting ``text``.
    """
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }

    if system is not None:
        request["system"] = [{"text": system}]

    if guardrail_id is not None:
        request["guardrailConfig"] = {
            "guardrailIdentifier": guardrail_id,
            "guardrailVersion": guardrail_version,
        }

    response = client.converse(**request)

    blocks = response["output"]["message"]["content"]
    text = "".join(block.get("text", "") for block in blocks)
    usage = response.get("usage", {})

    return ModelReply(
        text=text,
        stop_reason=response.get("stopReason", "unknown"),
        input_tokens=int(usage.get("inputTokens", 0)),
        output_tokens=int(usage.get("outputTokens", 0)),
    )
