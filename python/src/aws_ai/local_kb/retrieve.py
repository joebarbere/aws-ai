"""Retrieve: the query side of a knowledge base, shaped like ``bedrock-agent-runtime``.

The response matches local-kb/fixtures/retrieve-*.json: each result is exactly ``content``,
``location``, ``metadata`` and ``score`` — no ``documentId``, and no ``nextToken``.
``metadata`` is what the chunk stores minus the vector, the text, ``AMAZON_BEDROCK_METADATA``
and ``id``, plus ``x-amz-bedrock-kb-chunk-id`` (the ``_id``), with every number a float.

SEMANTIC is a faiss k-NN query with the filter inside it (efficient filtering, so a selective
filter still returns k results); its score is OpenSearch's own l2 score, ``1/(1+d²)``.

HYBRID adds a ``match`` on the text field through a min-max / arithmetic-mean normalization
pipeline. AWS doesn't document its fusion — and in Phase 0 HYBRID returned exactly what
SEMANTIC did — so this is an honest stand-in, and it does what hybrid is for: an exact code
like ``ZX-4471`` ranks the chunk that contains it first.
"""

from __future__ import annotations

import json
from typing import Any

from aws_ai.local_kb import index
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.filters import to_query
from aws_ai.local_kb.ingest import Services, Target
from aws_ai.local_kb.opensearch import OpenSearch

HYBRID_PIPELINE = "bedrock-kb-hybrid"
# Text first, vector second: the order of the hybrid query's sub-queries. Equal weights,
# OpenSearch's default: AWS doesn't publish its fusion, and a vector-heavy 0.3/0.7 let the
# vector score bury the exact match for "ZX-4471" — the case hybrid search exists for.
HYBRID_WEIGHTS = [0.5, 0.5]
DEFAULT_RESULTS = 5
# Min-max normalization over only the requested few results forces the last one to 0, however
# close it was; normalize over a wider candidate pool, then trim.
HYBRID_POOL = 50
_UNSUPPORTED = ("implicitFilterConfiguration", "rerankingConfiguration")


def retrieve(
    cp: ControlPlane,
    services: Services,
    knowledge_base_id: str,
    retrieval_query: dict[str, Any],
    retrieval_configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    knowledge_base = cp.get_knowledge_base(knowledge_base_id)
    target = Target.of(knowledge_base)
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
    filter_query = to_query(vector_config["filter"]) if "filter" in vector_config else None

    model = knowledge_base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    embed = services.embedder(model["embeddingModelArn"], target.dimensions)
    # Raw query text, as Bedrock sent it to Titan in Phase 0 — no instruction prefix.
    (vector,) = embed([text])

    hierarchical = _is_hierarchical(cp, knowledge_base_id)
    # Children that share a parent collapse into one result, so fetch extra and trim after.
    fetch = min(size * 3, 100) if hierarchical else size
    if search_type == "HYBRID":
        pool = max(fetch, HYBRID_POOL)
        hits = _hybrid(services.opensearch, target, text, vector, pool, filter_query)[:fetch]
    else:
        hits = _semantic(services.opensearch, target, vector, fetch, filter_query)

    results = [_result(hit, target) for hit in hits]
    if hierarchical:
        results = _parents(results, hits, target)
    return {"retrievalResults": results[:size]}


def _knn(target: Target, vector: list[float], k: int, filter_query: Any) -> dict[str, Any]:
    knn: dict[str, Any] = {"vector": vector, "k": k}
    if filter_query is not None:
        knn["filter"] = filter_query
    return {"knn": {target.vector_field: knn}}


def _semantic(
    client: OpenSearch, target: Target, vector: list[float], k: int, filter_query: Any
) -> list[dict[str, Any]]:
    body = {
        "size": k,
        "query": _knn(target, vector, k, filter_query),
        "_source": {"excludes": [target.vector_field]},
    }
    result = client.request("POST", f"/{client.quote(target.index)}/_search", body)
    hits: list[dict[str, Any]] = result["hits"]["hits"]
    return hits


def _hybrid(
    client: OpenSearch,
    target: Target,
    text: str,
    vector: list[float],
    k: int,
    filter_query: Any,
) -> list[dict[str, Any]]:
    _ensure_pipeline(client)
    lexical: dict[str, Any] = {"match": {target.text_field: text}}
    if filter_query is not None:
        lexical = {"bool": {"must": [lexical], "filter": [filter_query]}}
    body = {
        "size": k,
        "query": {"hybrid": {"queries": [lexical, _knn(target, vector, k, filter_query)]}},
        "_source": {"excludes": [target.vector_field]},
    }
    path = f"/{client.quote(target.index)}/_search?search_pipeline={HYBRID_PIPELINE}"
    hits: list[dict[str, Any]] = client.request("POST", path, body)["hits"]["hits"]
    return hits


def _ensure_pipeline(client: OpenSearch) -> None:
    client.request(
        "PUT",
        f"/_search/pipeline/{HYBRID_PIPELINE}",
        {
            "description": "local-kb HYBRID search: min-max normalized, weighted mean",
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": HYBRID_WEIGHTS},
                        },
                    }
                }
            ],
        },
    )


def _result(hit: dict[str, Any], target: Target) -> dict[str, Any]:
    source = hit["_source"]
    hidden = {target.vector_field, target.text_field, target.metadata_field, "id"}
    metadata = {k: _floats(v) for k, v in source.items() if k not in hidden}
    metadata[index.CHUNK_ID] = hit["_id"]
    return {
        "content": {"text": source[target.text_field], "type": "TEXT"},
        "location": {"s3Location": {"uri": source[index.SOURCE_URI]}, "type": "S3"},
        "metadata": metadata,
        "score": hit["_score"],
    }


def _floats(value: Any) -> Any:
    """Bedrock returns every number as a float — page numbers included (``1.0``)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _is_hierarchical(cp: ControlPlane, knowledge_base_id: str) -> bool:
    for summary in cp.list_data_sources(knowledge_base_id):
        ds = cp.get_data_source(knowledge_base_id, summary["dataSourceId"])
        chunking = ds.get("vectorIngestionConfiguration", {}).get("chunkingConfiguration", {})
        if chunking.get("chunkingStrategy") == "HIERARCHICAL":
            return True
    return False


def _parents(
    results: list[dict[str, Any]], hits: list[dict[str, Any]], target: Target
) -> list[dict[str, Any]]:
    """HIERARCHICAL: answer with each child's parent chunk, once per parent, in rank order —
    so fewer than numberOfResults can come back, as on AWS."""
    seen: set[str] = set()
    out = []
    for result, hit in zip(results, hits, strict=True):
        stored = json.loads(hit["_source"].get(target.metadata_field) or "{}")
        parent = stored.get("parentText")
        if parent is None:
            out.append(result)
            continue
        if parent in seen:
            continue
        seen.add(parent)
        out.append({**result, "content": {"text": parent, "type": "TEXT"}})
    return out
