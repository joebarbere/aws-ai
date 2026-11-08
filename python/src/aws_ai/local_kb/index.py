"""The vector index a Bedrock knowledge base expects, and the few index operations ingestion
needs.

The body matches local-kb/fixtures/index-mapping.json as created (before dynamic mapping): only
the vector, text and metadata fields are declared. The ``x-amz-bedrock-kb-*`` fields, ``id`` and
sidecar attributes are left to dynamic mapping, as on AWS — that is what makes them appear,
typed, in the Dashboards field list.
"""

from __future__ import annotations

from typing import Any

from aws_ai.local_kb.opensearch import OpenSearch

SOURCE_URI = "x-amz-bedrock-kb-source-uri"
DATA_SOURCE_ID = "x-amz-bedrock-kb-data-source-id"
PAGE_NUMBER = "x-amz-bedrock-kb-document-page-number"
MODALITY = "x-amz-bedrock-kb-source-file-modality"
CHUNK_ID = "x-amz-bedrock-kb-chunk-id"


def index_body(
    dimensions: int, vector_field: str, text_field: str, metadata_field: str
) -> dict[str, Any]:
    """faiss / hnsw / l2 with m=16, ef_construction=512, ef_search=512: the aws-samples values,
    and what python/scripts/create_vector_index.py creates in the cloud."""
    return {
        "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
        "mappings": {
            "properties": {
                vector_field: {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                        "parameters": {"m": 16, "ef_construction": 512},
                    },
                },
                text_field: {"type": "text", "index": True},
                metadata_field: {"type": "text", "index": False},
            }
        },
    }


def ensure_index(client: OpenSearch, name: str, body: dict[str, Any]) -> bool:
    """Create the index unless it exists. Returns True if it was created."""
    if client.exists(f"/{client.quote(name)}"):
        return False
    client.request("PUT", f"/{client.quote(name)}", body)
    return True


def delete_chunks(client: OpenSearch, index: str, data_source_id: str, uri: str | None) -> None:
    """Remove a data source's chunks — all of them, or one source file's."""
    terms: list[dict[str, Any]] = [{"term": {f"{DATA_SOURCE_ID}.keyword": data_source_id}}]
    if uri is not None:
        terms.append({"term": {f"{SOURCE_URI}.keyword": uri}})
    client.request(
        "POST",
        f"/{client.quote(index)}/_delete_by_query?conflicts=proceed",
        {"query": {"bool": {"filter": terms}}},
        ok_statuses=(404,),
    )


def get_chunks(client: OpenSearch, index: str, chunk_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch stored chunks, vectors included, by ``_id``."""
    if not chunk_ids:
        return []
    result = client.request("POST", f"/{client.quote(index)}/_mget", {"ids": chunk_ids})
    return [doc for doc in result["docs"] if doc.get("found")]
