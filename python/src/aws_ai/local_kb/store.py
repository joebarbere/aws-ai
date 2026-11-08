"""Where a knowledge base keeps its chunks: the seam between ingestion/retrieval and a store.

Bedrock supports several vector stores, and each lays chunks out its own way — compare
local-kb/fixtures/index-documents.json (OpenSearch) with fixtures/rds/index-documents.json
(Aurora PostgreSQL). Ingestion and retrieval deal in the store-neutral records here; each store
serializes them, searches, and shapes Retrieve results the way Bedrock does on that store.

``store_for`` picks the store from the knowledge base's ``storageConfiguration.type``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from aws_ai.local_kb.ingest import Services


@dataclass(frozen=True)
class StoredChunk:
    """One chunk as ingestion produces it, before any store's layout."""

    id: str
    vector: list[float]
    text: str
    source_uri: str
    data_source_id: str
    page: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)  # sidecar metadataAttributes
    parent_text: str | None = None  # HIERARCHICAL chunking only


@dataclass(frozen=True)
class Hit:
    """One search result: a Retrieve ``retrievalResults`` entry, already shaped the way
    Bedrock returns it from this store, plus the parent text for HIERARCHICAL collapsing."""

    result: dict[str, Any]
    parent_text: str | None = None


class VectorStore(Protocol):
    dimensions: int

    def ensure(self) -> None:
        """Create the index or table if it is missing."""

    def new_id(self) -> str:
        """A fresh chunk id, in this store's format."""

    def write(self, chunks: list[StoredChunk]) -> None:
        """Store one document's chunks."""

    def rewrite_attributes(self, chunk_ids: list[str], attributes: dict[str, Any]) -> None:
        """A sidecar-only change: swap the chunks' attributes in place, keeping their ids."""

    def delete(self, chunk_ids: list[str]) -> None: ...

    def delete_data_source(self, data_source_id: str) -> None:
        """Drop every chunk a data source wrote (DeleteDataSource with policy DELETE)."""

    def compile_filter(self, retrieval_filter: dict[str, Any]) -> Any:
        """Validate a ``RetrievalFilter`` and translate it; raises ValidationException."""

    def semantic(self, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]: ...

    def hybrid(self, text: str, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]: ...


def embedding_dimensions(knowledge_base: dict[str, Any]) -> int:
    vector = knowledge_base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    dims: int = (
        vector.get("embeddingModelConfiguration", {})
        .get("bedrockEmbeddingModelConfiguration", {})
        .get("dimensions", 1024)
    )
    return dims


def store_for(knowledge_base: dict[str, Any], services: Services) -> VectorStore:
    storage = knowledge_base["storageConfiguration"]
    if storage["type"] == "OPENSEARCH_MANAGED_CLUSTER":
        from aws_ai.local_kb.store_opensearch import OpenSearchStore, Target

        return OpenSearchStore(services.opensearch, Target.of(knowledge_base))
    raise ValueError(f"no local store for storage type {storage['type']}")
