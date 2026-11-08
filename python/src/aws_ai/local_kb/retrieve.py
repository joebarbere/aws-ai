"""Retrieve: the query side of a knowledge base, shaped like ``bedrock-agent-runtime``.

The response matches local-kb/fixtures/retrieve-*.json: each result is exactly ``content``,
``location``, ``metadata`` and ``score`` — no ``documentId``, and no ``nextToken``. What goes in
``metadata`` and ``score``, and how SEMANTIC and HYBRID search, depends on the store; see
store_opensearch.py.
"""

from __future__ import annotations

from typing import Any

from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.store import Hit, store_for
from aws_ai.local_kb.store_opensearch import HYBRID_PIPELINE, HYBRID_POOL, HYBRID_WEIGHTS

__all__ = ["HYBRID_PIPELINE", "HYBRID_POOL", "HYBRID_WEIGHTS", "retrieve"]

DEFAULT_RESULTS = 5
_UNSUPPORTED = ("implicitFilterConfiguration", "rerankingConfiguration")


def retrieve(
    cp: ControlPlane,
    services: Services,
    knowledge_base_id: str,
    retrieval_query: dict[str, Any],
    retrieval_configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    knowledge_base = cp.get_knowledge_base(knowledge_base_id)
    store = store_for(knowledge_base, services)
    text = retrieval_query.get("text")
    if not isinstance(text, str) or not text:
        raise ValidationException("retrievalQuery.text is required.")

    vector_config = (retrieval_configuration or {}).get("vectorSearchConfiguration", {})
    for key in _UNSUPPORTED:
        if key in vector_config:
            raise ValidationException(f"{key} is not supported by local-kb.")
    size = vector_config.get("numberOfResults", DEFAULT_RESULTS)
    if not isinstance(size, int) or not 1 <= size <= 100:
        raise ValidationException("numberOfResults must be an integer in 1..100.")
    search_type = vector_config.get("overrideSearchType", "SEMANTIC")
    if search_type not in ("SEMANTIC", "HYBRID"):
        raise ValidationException("overrideSearchType must be SEMANTIC or HYBRID.")
    compiled = store.compile_filter(vector_config["filter"]) if "filter" in vector_config else None

    model = knowledge_base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    embed = services.embedder(model["embeddingModelArn"], store.dimensions)
    # Raw query text, as Bedrock sent it to Titan in Phase 0 — no instruction prefix.
    (vector,) = embed([text])

    hierarchical = _is_hierarchical(cp, knowledge_base_id)
    # Children that share a parent collapse into one result, so fetch extra and trim after.
    fetch = min(size * 3, 100) if hierarchical else size
    if search_type == "HYBRID":
        hits = store.hybrid(text, vector, fetch, compiled)
    else:
        hits = store.semantic(vector, fetch, compiled)

    results = _parents(hits) if hierarchical else [hit.result for hit in hits]
    return {"retrievalResults": results[:size]}


def _is_hierarchical(cp: ControlPlane, knowledge_base_id: str) -> bool:
    for summary in cp.list_data_sources(knowledge_base_id):
        ds = cp.get_data_source(knowledge_base_id, summary["dataSourceId"])
        chunking = ds.get("vectorIngestionConfiguration", {}).get("chunkingConfiguration", {})
        if chunking.get("chunkingStrategy") == "HIERARCHICAL":
            return True
    return False


def _parents(hits: list[Hit]) -> list[dict[str, Any]]:
    """HIERARCHICAL: answer with each child's parent chunk, once per parent, in rank order —
    so fewer than numberOfResults can come back, as on AWS."""
    seen: set[str] = set()
    out = []
    for hit in hits:
        if hit.parent_text is None:
            out.append(hit.result)
            continue
        if hit.parent_text in seen:
            continue
        seen.add(hit.parent_text)
        out.append({**hit.result, "content": {"text": hit.parent_text, "type": "TEXT"}})
    return out
