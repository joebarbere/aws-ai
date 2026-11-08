"""Embeddings from LM Studio, shaped like Titan Text Embeddings V2's.

The request Bedrock sent Titan in Phase 0 was ``{"inputText", "dimensions": 1024,
"embeddingTypes": ["float"]}`` and the vectors came back unit-length. Qwen3-Embedding-0.6B also
returns unit vectors at 1024, but LM Studio ignores ``dimensions``, so 512 and 256 are made here
the Matryoshka way: truncate, then re-normalize.
"""

from __future__ import annotations

import json
import math
import urllib.request
from collections.abc import Callable
from functools import cache

from aws_ai.local_kb import config

BATCH_SIZE = 32


def model_id(model_arn: str) -> str:
    """``arn:aws:bedrock:…::foundation-model/kb-embed`` → ``kb-embed``, the LM Studio identifier."""
    return model_arn.rsplit("/", 1)[-1]


def lm_studio_embedder(
    model: str, dimensions: int, endpoint: str = config.LM_STUDIO_ENDPOINT
) -> Callable[[list[str]], list[list[float]]]:
    def embed(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            body = json.dumps({"model": model, "input": texts[start : start + BATCH_SIZE]})
            request = urllib.request.Request(  # noqa: S310 - local LM Studio
                f"{endpoint}/embeddings",
                data=body.encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
                data = sorted(json.loads(response.read())["data"], key=lambda d: d["index"])
            vectors += [truncate(d["embedding"], dimensions) for d in data]
        return vectors

    return embed


def truncate(vector: list[float], dimensions: int) -> list[float]:
    """Keep the first ``dimensions`` values and scale back to unit length."""
    if len(vector) < dimensions:
        raise ValueError(f"model returned {len(vector)} dimensions; the index needs {dimensions}")
    head = vector[:dimensions]
    norm = math.sqrt(sum(v * v for v in head)) or 1.0
    return [v / norm for v in head]


@cache
def qwen_token_counter() -> Callable[[str], int]:
    """Token counts from the embedding model's own tokenizer (downloaded once from Hugging
    Face, then cached). LM Studio's API reports no usage for embeddings, so it can't count."""
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_pretrained("Qwen/Qwen3-Embedding-0.6B")
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def lm_studio_chat(
    model: str = "kb-llm", endpoint: str = config.LM_STUDIO_ENDPOINT
) -> Callable[[str, str, dict[str, object]], str]:
    """A chat call shaped like Bedrock's Converse: the prompt template as the system message,
    the user's query as the one user message, Bedrock's inference settings mapped across.

    ``reasoning_effort: "none"`` goes on every request: Qwen3.5 thinks otherwise, and it is the
    only switch LM Studio honours (PLAN.md, Phase 2).
    """

    def chat(system: str, user: str, inference: dict[str, object]) -> str:
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": inference.get("temperature", 0.0),
            "top_p": inference.get("topP", 1.0),
            "max_tokens": inference.get("maxTokens", 2048),
            "reasoning_effort": "none",
        }
        if inference.get("stopSequences"):
            body["stop"] = inference["stopSequences"]
        request = urllib.request.Request(  # noqa: S310 - local LM Studio
            f"{endpoint}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310
            reply = json.loads(response.read())["choices"][0]["message"].get("content") or ""
        return str(reply)

    return chat
