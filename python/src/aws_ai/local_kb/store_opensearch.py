"""The OpenSearch store: chunks laid out as in local-kb/fixtures/index-documents.json.

Writes: one document per chunk, ``_id`` in Bedrock's ``1%3A0%3A…`` format, the vector, text and
``AMAZON_BEDROCK_METADATA`` fields from the knowledge base's field mapping, and the
``x-amz-bedrock-kb-*`` fields and sidecar attributes at the top level, typed by dynamic mapping.

SEMANTIC is a faiss k-NN query with the filter inside it (efficient filtering, so a selective
filter still returns k results); its score is OpenSearch's own l2 score, ``1/(1+d²)``.

HYBRID adds a ``match`` on the text field through a min-max / arithmetic-mean normalization
pipeline. AWS doesn't document its fusion for OpenSearch — and in Phase 0 HYBRID returned
exactly what SEMANTIC did — so this is an honest stand-in, and it does what hybrid is for: an
exact code like ``ZX-4471`` ranks the chunk that contains it first.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from aws_ai.local_kb import index
from aws_ai.local_kb.filters import to_query
from aws_ai.local_kb.ids import chunk_id
from aws_ai.local_kb.opensearch import OpenSearch
from aws_ai.local_kb.store import Hit, StoredChunk, embedding_dimensions

HYBRID_PIPELINE = "bedrock-kb-hybrid"
# Text first, vector second: the order of the hybrid query's sub-queries. Equal weights,
# OpenSearch's default: AWS doesn't publish its fusion, and a vector-heavy 0.3/0.7 let the
# vector score bury the exact match for "ZX-4471" — the case hybrid search exists for.
HYBRID_WEIGHTS = [0.5, 0.5]
# Min-max normalization over only the requested few results forces the last one to 0, however
# close it was; normalize over a wider candidate pool, then trim.
HYBRID_POOL = 50


@dataclass(frozen=True)
class Target:
    """Where a knowledge base keeps its vectors, read from its storageConfiguration."""

    index: str
    vector_field: str
    text_field: str
    metadata_field: str
    dimensions: int

    @classmethod
    def of(cls, knowledge_base: dict[str, Any]) -> Target:
        cluster = knowledge_base["storageConfiguration"]["opensearchManagedClusterConfiguration"]
        fields = cluster["fieldMapping"]
        return cls(
            cluster["vectorIndexName"],
            fields["vectorField"],
            fields["textField"],
            fields["metadataField"],
            embedding_dimensions(knowledge_base),
        )

    def own_fields(self) -> set[str]:
        """Fields Bedrock writes itself; everything else on a chunk is a sidecar attribute."""
        return {
            self.vector_field,
            self.text_field,
            self.metadata_field,
            index.SOURCE_URI,
            index.DATA_SOURCE_ID,
            index.PAGE_NUMBER,
            index.MODALITY,
            "id",
        }


class OpenSearchStore:
    def __init__(self, client: OpenSearch, target: Target) -> None:
        self.client = client
        self.target = target
        self.dimensions = target.dimensions

    # ------------------------------------------------------------------ writes

    def ensure(self) -> None:
        t = self.target
        index.ensure_index(
            self.client,
            t.index,
            index.index_body(t.dimensions, t.vector_field, t.text_field, t.metadata_field),
        )

    def new_id(self) -> str:
        return chunk_id()

    def write(self, chunks: list[StoredChunk]) -> None:
        actions: list[dict[str, Any]] = []
        for piece in chunks:
            actions += [
                {"index": {"_index": self.target.index, "_id": piece.id}},
                self._source(piece),
            ]
        self.client.bulk(actions)

    def _source(self, piece: StoredChunk) -> dict[str, Any]:
        """One stored chunk, shaped like local-kb/fixtures/index-documents.json."""
        metadata: dict[str, Any] = {"source": piece.source_uri}
        if piece.parent_text is not None:
            metadata["parentText"] = piece.parent_text
        source: dict[str, Any] = {
            self.target.vector_field: piece.vector,
            self.target.text_field: piece.text,
            self.target.metadata_field: json.dumps(metadata, separators=(",", ":")),
            index.SOURCE_URI: piece.source_uri,
            index.DATA_SOURCE_ID: piece.data_source_id,
            index.MODALITY: "TEXT",
            "id": str(uuid.uuid4()),
        }
        if piece.page is not None:
            source[index.PAGE_NUMBER] = piece.page
        source.update(piece.attributes)
        return source

    def rewrite_attributes(self, chunk_ids: list[str], attributes: dict[str, Any]) -> None:
        """Read the chunks back, vectors and all, swap their attributes, and write them under
        the same ``_id`` — what the docs describe, and what Phase 0 showed (including ``id``
        being overwritten with the ``_id``)."""
        own = self.target.own_fields()
        actions: list[dict[str, Any]] = []
        for doc in index.get_chunks(self.client, self.target.index, chunk_ids):
            source = {k: v for k, v in doc["_source"].items() if k in own}
            source.update(attributes)
            source["id"] = doc["_id"]
            actions += [{"index": {"_index": self.target.index, "_id": doc["_id"]}}, source]
        self.client.bulk(actions)

    def delete(self, chunk_ids: list[str]) -> None:
        self.client.bulk(
            [{"delete": {"_index": self.target.index, "_id": _id}} for _id in chunk_ids]
        )

    def delete_data_source(self, data_source_id: str) -> None:
        index.delete_chunks(self.client, self.target.index, data_source_id, None)

    # ------------------------------------------------------------------- reads

    def compile_filter(self, retrieval_filter: dict[str, Any]) -> Any:
        return to_query(retrieval_filter)

    def semantic(self, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]:
        body = {
            "size": k,
            "query": self._knn(vector, k, compiled_filter),
            "_source": {"excludes": [self.target.vector_field]},
        }
        path = f"/{self.client.quote(self.target.index)}/_search"
        return [self._hit(h) for h in self.client.request("POST", path, body)["hits"]["hits"]]

    def hybrid(self, text: str, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]:
        pool = max(k, HYBRID_POOL)
        self._ensure_pipeline()
        lexical: dict[str, Any] = {"match": {self.target.text_field: text}}
        if compiled_filter is not None:
            lexical = {"bool": {"must": [lexical], "filter": [compiled_filter]}}
        body = {
            "size": pool,
            "query": {"hybrid": {"queries": [lexical, self._knn(vector, pool, compiled_filter)]}},
            "_source": {"excludes": [self.target.vector_field]},
        }
        path = f"/{self.client.quote(self.target.index)}/_search?search_pipeline={HYBRID_PIPELINE}"
        hits = self.client.request("POST", path, body)["hits"]["hits"]
        return [self._hit(h) for h in hits[:k]]

    def _knn(self, vector: list[float], k: int, compiled_filter: Any) -> dict[str, Any]:
        knn: dict[str, Any] = {"vector": vector, "k": k}
        if compiled_filter is not None:
            knn["filter"] = compiled_filter
        return {"knn": {self.target.vector_field: knn}}

    def _ensure_pipeline(self) -> None:
        self.client.request(
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

    def _hit(self, hit: dict[str, Any]) -> Hit:
        """``metadata`` is what the chunk stores minus the vector, the text,
        ``AMAZON_BEDROCK_METADATA`` and ``id``, plus ``x-amz-bedrock-kb-chunk-id`` (the
        ``_id``), with every number a float — as in local-kb/fixtures/retrieve-*.json."""
        t = self.target
        source = hit["_source"]
        hidden = {t.vector_field, t.text_field, t.metadata_field, "id"}
        metadata = {k: _floats(v) for k, v in source.items() if k not in hidden}
        metadata[index.CHUNK_ID] = hit["_id"]
        stored = json.loads(source.get(t.metadata_field) or "{}")
        return Hit(
            {
                "content": {"text": source[t.text_field], "type": "TEXT"},
                "location": {"s3Location": {"uri": source[index.SOURCE_URI]}, "type": "S3"},
                "metadata": metadata,
                "score": hit["_score"],
            },
            stored.get("parentText"),
        )


def _floats(value: Any) -> Any:
    """Bedrock returns every number as a float — page numbers included (``1.0``)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value
