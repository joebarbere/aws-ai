"""The four chunking strategies, sized in Titan-equivalent tokens.

Default and FIXED_SIZE chunking pack **whole sentences** up to ``maxTokens``, then start the next
chunk with the trailing sentences that fit in ``overlapPercentage`` of it. That rule, with
300 tokens / 20%, reproduces all 12 chunk boundaries of equipment-inventory.txt in
local-kb/fixtures/ exactly.

Titan's tokenizer is not public, so tokens are counted with the embedding model's own tokenizer
and scaled: across the boundaries that reproduce the fixture, Qwen3 counts 1.11–1.16 tokens per
Titan token. ``QWEN_TOKENS_PER_TITAN_TOKEN`` is the middle of that range.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

QWEN_TOKENS_PER_TITAN_TOKEN = 1.13
DEFAULT_MAX_TOKENS = 300
DEFAULT_OVERLAP_PERCENTAGE = 20

CountTokens = Callable[[str], int]
Embed = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Chunk:
    text: str
    start: int  # offset in the parsed document, for the page number
    parent_text: str | None = None  # HIERARCHICAL: the parent chunk returned at query time


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence boundaries: after ``.``, ``!`` or ``?`` followed by whitespace."""
    spans, start = [], 0
    for match in re.finditer(r"[.!?](?=\s)|$", text):
        if match.end() > start:
            spans.append((start, match.end()))
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
    return spans


def pack(
    text: str,
    spans: Sequence[tuple[int, int]],
    count: CountTokens,
    max_tokens: float,
    overlap_tokens: float,
    offset: int = 0,
) -> list[Chunk]:
    """Greedy sentence packing with sentence-aligned overlap.

    A sentence longer than ``max_tokens`` on its own is split at word boundaries first, so no
    chunk exceeds the limit.
    """
    spans = _split_long(text, spans, count, max_tokens)
    sizes = [count(text[s:e]) for s, e in spans]
    chunks, i = [], 0
    while i < len(spans):
        j = i + 1
        while j < len(spans) and count(text[spans[i][0] : spans[j][1]]) <= max_tokens:
            j += 1
        start, end = spans[i][0], spans[j - 1][1]
        chunks.append(Chunk(text[start:end], offset + start))
        if j >= len(spans):
            break
        k, overlap = j, 0
        while k - 1 > i and overlap + sizes[k - 1] <= overlap_tokens:
            overlap += sizes[k - 1]
            k -= 1
        i = k
    return chunks


def chunk(
    text: str,
    configuration: dict[str, Any] | None,
    count_tokens: CountTokens,
    embed: Embed | None = None,
) -> list[Chunk]:
    """Split a parsed document per a data source's ``chunkingConfiguration`` (None = default)."""
    if not text:
        return []
    scale = QWEN_TOKENS_PER_TITAN_TOKEN
    strategy = (configuration or {}).get("chunkingStrategy", "FIXED_SIZE")
    spans = sentence_spans(text)

    if strategy == "NONE":
        return [Chunk(text, 0)]

    if strategy == "FIXED_SIZE":
        fixed = (configuration or {}).get("fixedSizeChunkingConfiguration", {})
        max_tokens = fixed.get("maxTokens", DEFAULT_MAX_TOKENS)
        overlap = max_tokens * fixed.get("overlapPercentage", DEFAULT_OVERLAP_PERCENTAGE) / 100
        return pack(text, spans, count_tokens, max_tokens * scale, overlap * scale)

    if strategy == "HIERARCHICAL":
        assert configuration is not None
        hierarchical = configuration["hierarchicalChunkingConfiguration"]
        parent_max, child_max = (lvl["maxTokens"] for lvl in hierarchical["levelConfigurations"])
        overlap = hierarchical["overlapTokens"] * scale
        children = []
        for parent in pack(text, spans, count_tokens, parent_max * scale, overlap):
            local = sentence_spans(parent.text)
            for child in pack(
                parent.text, local, count_tokens, child_max * scale, overlap, parent.start
            ):
                children.append(Chunk(child.text, child.start, parent.text))
        return children

    if strategy == "SEMANTIC":
        assert configuration is not None and embed is not None
        semantic = configuration["semanticChunkingConfiguration"]
        return _semantic(text, spans, count_tokens, embed, semantic, scale)

    raise ValueError(f"unknown chunking strategy {strategy}")


def _semantic(
    text: str,
    spans: list[tuple[int, int]],
    count: CountTokens,
    embed: Embed,
    settings: dict[str, Any],
    scale: float,
) -> list[Chunk]:
    """Embed each sentence with ``bufferSize`` neighbours on either side, split where the
    distance between consecutive windows is above the ``breakpointPercentileThreshold``
    percentile, then re-split any group over ``maxTokens``. Costs one embedding per sentence,
    as it does on AWS."""
    if len(spans) < 2:
        return [Chunk(text, 0)]
    buffer = settings["bufferSize"]
    windows = [
        text[spans[max(0, i - buffer)][0] : spans[min(len(spans) - 1, i + buffer)][1]]
        for i in range(len(spans))
    ]
    vectors = embed(windows)
    distances = [1 - _cosine(a, b) for a, b in zip(vectors, vectors[1:], strict=False)]
    cutoff = _percentile(distances, settings["breakpointPercentileThreshold"])
    groups, current = [], [spans[0]]
    for span, distance in zip(spans[1:], distances, strict=True):
        if distance > cutoff:
            groups.append(current)
            current = []
        current.append(span)
    groups.append(current)
    chunks = []
    for group in groups:
        chunks += pack(text, group, count, settings["maxTokens"] * scale, 0)
    return chunks


def _split_long(
    text: str, spans: Sequence[tuple[int, int]], count: CountTokens, max_tokens: float
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in spans:
        if count(text[start:end]) <= max_tokens:
            out.append((start, end))
            continue
        words = [(m.start() + start, m.end() + start) for m in re.finditer(r"\S+", text[start:end])]
        piece_start = words[0][0]
        last_end = words[0][1]
        for w_start, w_end in words[1:]:
            if count(text[piece_start:w_end]) > max_tokens:
                out.append((piece_start, last_end))
                piece_start = w_start
            last_end = w_end
        out.append((piece_start, last_end))
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
