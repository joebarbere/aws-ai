"""The ingestion job: list → diff → parse → chunk → embed → write, with Bedrock's counters.

How each case is counted follows local-kb/fixtures/ingestion-job-*.json, not the docs alone:

- New file: ingest it. Counts as ``NewDocumentsIndexed``.
- Content ETag changed: delete the old chunks, then ingest. ``ModifiedDocumentsIndexed``.
- Only the sidecar changed, same embedding prefix: rewrite the attributes in place under the
  same ``_id``, with no embedding call, and ``id`` becomes the ``_id``.
  ``MetadataDocumentsModified`` only.
- The sidecar change alters the embedding prefix: delete and re-ingest.
  ``MetadataDocumentsModified``.
- Unchanged: nothing, and no counter. ``DocumentsSkipped`` is *not* "unchanged".
- Gone from the bucket: delete its chunks. ``DocumentsDeleted``.
- Unreadable or unsupported: the reason goes in ``failureReasons`` and the job continues.
  ``DocumentsFailed``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from aws_ai.local_kb import index
from aws_ai.local_kb.chunking import Chunk, CountTokens, Embed, chunk
from aws_ai.local_kb.control import STATISTICS, ControlPlane, DocumentRecord
from aws_ai.local_kb.ids import chunk_id
from aws_ai.local_kb.opensearch import OpenSearch
from aws_ai.local_kb.parse import (
    ParseError,
    Sidecar,
    is_sidecar,
    parse_document,
    parse_sidecar,
    sidecar_key,
)


@dataclass(frozen=True)
class Services:
    """Everything a job talks to. ``embedder`` builds an embed function for a knowledge base's
    model ARN and dimensions."""

    s3: Any
    opensearch: OpenSearch
    embedder: Callable[[str, int], Embed]
    count_tokens: CountTokens
    # (system, user, inference settings) -> the model's reply; only RetrieveAndGenerate needs it.
    chat: Callable[[str, str, dict[str, Any]], str] | None = None


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
        vector = knowledge_base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
        dims = (
            vector.get("embeddingModelConfiguration", {})
            .get("bedrockEmbeddingModelConfiguration", {})
            .get("dimensions", 1024)
        )
        return cls(
            cluster["vectorIndexName"],
            fields["vectorField"],
            fields["textField"],
            fields["metadataField"],
            dims,
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


def run_pending(cp: ControlPlane, services: Services) -> list[dict[str, Any]]:
    """Run every STARTING job, oldest first. Returns the finished jobs."""
    return [run_job(cp, job, services) for job in cp.pending_jobs()]


def run_job(cp: ControlPlane, job: dict[str, Any], services: Services) -> dict[str, Any]:
    kb_id, ds_id, job_id = job["knowledgeBaseId"], job["dataSourceId"], job["ingestionJobId"]
    knowledge_base = cp.get_knowledge_base(kb_id)
    data_source = cp.get_data_source(kb_id, ds_id)
    target = Target.of(knowledge_base)
    model = knowledge_base["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    stats = dict.fromkeys(STATISTICS, 0)
    failures: list[str] = []
    cp.update_ingestion_job(job_id, status="IN_PROGRESS")
    try:
        index.ensure_index(
            services.opensearch,
            target.index,
            index.index_body(
                target.dimensions, target.vector_field, target.text_field, target.metadata_field
            ),
        )
        sync = _Sync(
            cp,
            services,
            target,
            data_source,
            services.embedder(model["embeddingModelArn"], target.dimensions),
            stats,
            failures,
        )
        stopped = sync.run(lambda: cp.get_ingestion_job(kb_id, ds_id, job_id)["status"])
    except Exception as exc:  # an infrastructure failure: the job fails, as on AWS
        return cp.update_ingestion_job(
            job_id, status="FAILED", statistics=stats, failure_reasons=[*failures, str(exc)]
        )
    return cp.update_ingestion_job(
        job_id,
        status="STOPPED" if stopped else "COMPLETE",
        statistics=stats,
        failure_reasons=failures,
    )


def has_changes(cp: ControlPlane, s3: Any, data_source: dict[str, Any]) -> bool:
    """Whether the bucket differs from what the last sync saw: the ETag poll's question. The
    same comparison a job makes, without parsing or writing anything."""
    s3_config = data_source["dataSourceConfiguration"]["s3Configuration"]
    bucket = s3_config["bucketArn"].split(":::", 1)[1]
    prefix = (s3_config.get("inclusionPrefixes") or [""])[0]
    seen: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            seen[obj["Key"]] = obj["ETag"].strip('"')
    ledger = cp.documents(data_source["dataSourceId"])
    current = {
        f"s3://{bucket}/{key}": (etag, seen.get(sidecar_key(key)))
        for key, etag in seen.items()
        if not is_sidecar(key) and not key.endswith("/")
    }
    known = {uri: (r.etag, r.metadata_etag) for uri, r in ledger.items()}
    return current != known


class _Sync:
    def __init__(
        self,
        cp: ControlPlane,
        services: Services,
        target: Target,
        data_source: dict[str, Any],
        embed: Embed,
        stats: dict[str, int],
        failures: list[str],
    ) -> None:
        s3_config = data_source["dataSourceConfiguration"]["s3Configuration"]
        self.cp, self.services, self.target, self.embed = cp, services, target, embed
        self.stats, self.failures = stats, failures
        self.ds_id = data_source["dataSourceId"]
        self.bucket = s3_config["bucketArn"].split(":::", 1)[1]
        self.prefix = (s3_config.get("inclusionPrefixes") or [""])[0]
        self.chunking = data_source.get("vectorIngestionConfiguration", {}).get(
            "chunkingConfiguration"
        )

    def run(self, status: Callable[[], str]) -> bool:
        """Sync the data source. Returns True if the job was stopped part-way."""
        documents, sidecars = self._list()
        self.stats["numberOfDocumentsScanned"] = len(documents)
        self.stats["numberOfMetadataDocumentsScanned"] = len(sidecars)
        ledger = self.cp.documents(self.ds_id)

        for key, etag in sorted(documents.items()):
            if status() == "STOPPING":
                return True
            uri = f"s3://{self.bucket}/{key}"
            meta_etag = sidecars.get(sidecar_key(key))
            previous = ledger.get(uri)
            if previous and previous.etag == etag and previous.metadata_etag == meta_etag:
                continue
            try:
                self._sync_document(key, uri, etag, meta_etag, previous)
            except ParseError as exc:
                self.stats["numberOfDocumentsFailed"] += 1
                self.failures.append(str(exc))

        for uri, record in ledger.items():
            if uri.removeprefix(f"s3://{self.bucket}/") not in documents:
                self._delete(record.chunk_ids)
                self.cp.remove_document(self.ds_id, uri)
                self.stats["numberOfDocumentsDeleted"] += 1
        return False

    def _list(self) -> tuple[dict[str, str], dict[str, str]]:
        documents: dict[str, str] = {}
        sidecars: dict[str, str] = {}
        paginator = self.services.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key, etag = obj["Key"], obj["ETag"].strip('"')
                if key.endswith("/"):
                    continue
                (sidecars if is_sidecar(key) else documents)[key] = etag
        return documents, sidecars

    def _sync_document(
        self,
        key: str,
        uri: str,
        etag: str,
        meta_etag: str | None,
        previous: DocumentRecord | None,
    ) -> None:
        sidecar = self._sidecar(key) if meta_etag else Sidecar()
        prefix = sidecar.embedding_prefix()
        if previous and previous.etag == etag and previous.embedding_prefix == prefix:
            self._rewrite_metadata(previous.chunk_ids, sidecar)
            chunk_ids = previous.chunk_ids
            self.stats["numberOfMetadataDocumentsModified"] += 1
        else:
            chunk_ids = self._ingest(key, uri, sidecar)
            if previous:
                self._delete(previous.chunk_ids)
            counter = (
                "numberOfNewDocumentsIndexed"
                if previous is None
                else "numberOfModifiedDocumentsIndexed"
                if previous.etag != etag
                else "numberOfMetadataDocumentsModified"
            )
            self.stats[counter] += 1
        self.cp.put_document(self.ds_id, DocumentRecord(uri, etag, meta_etag, chunk_ids, prefix))

    def _sidecar(self, key: str) -> Sidecar:
        body = self.services.s3.get_object(Bucket=self.bucket, Key=sidecar_key(key))["Body"]
        return parse_sidecar(sidecar_key(key), body.read())

    def _ingest(self, key: str, uri: str, sidecar: Sidecar) -> tuple[str, ...]:
        data = self.services.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        parsed = parse_document(key, data)
        chunks = chunk(parsed.text, self.chunking, self.services.count_tokens, self.embed)
        prefix = sidecar.embedding_prefix()
        vectors = self.embed([prefix + c.text for c in chunks])
        pages = PurePosixPath(key).suffix.lower() == ".pdf" and (
            (self.chunking or {}).get("chunkingStrategy") != "NONE"
        )
        actions: list[dict[str, Any]] = []
        ids = []
        for piece, vector in zip(chunks, vectors, strict=True):
            _id = chunk_id()
            ids.append(_id)
            page = parsed.page_at(piece.start) if pages else None
            actions += [
                {"index": {"_index": self.target.index, "_id": _id}},
                self._source(piece, vector, uri, page, sidecar),
            ]
        self.services.opensearch.bulk(actions)
        return tuple(ids)

    def _source(
        self, piece: Chunk, vector: list[float], uri: str, page: int | None, sidecar: Sidecar
    ) -> dict[str, Any]:
        """One stored chunk, shaped like local-kb/fixtures/index-documents.json."""
        metadata: dict[str, Any] = {"source": uri}
        if piece.parent_text is not None:
            metadata["parentText"] = piece.parent_text
        source: dict[str, Any] = {
            self.target.vector_field: vector,
            self.target.text_field: piece.text,
            self.target.metadata_field: json.dumps(metadata, separators=(",", ":")),
            index.SOURCE_URI: uri,
            index.DATA_SOURCE_ID: self.ds_id,
            index.MODALITY: "TEXT",
            "id": str(uuid.uuid4()),
        }
        if page is not None:
            source[index.PAGE_NUMBER] = page
        source.update({name: a.value for name, a in sidecar.attributes.items()})
        return source

    def _rewrite_metadata(self, chunk_ids: tuple[str, ...], sidecar: Sidecar) -> None:
        """Read the chunks back, vectors and all, swap their attributes, and write them under
        the same ``_id`` — what the docs describe, and what Phase 0 showed (including ``id``
        being overwritten with the ``_id``)."""
        own = self.target.own_fields()
        actions: list[dict[str, Any]] = []
        for doc in index.get_chunks(self.services.opensearch, self.target.index, list(chunk_ids)):
            source = {k: v for k, v in doc["_source"].items() if k in own}
            source.update({name: a.value for name, a in sidecar.attributes.items()})
            source["id"] = doc["_id"]
            actions += [{"index": {"_index": self.target.index, "_id": doc["_id"]}}, source]
        self.services.opensearch.bulk(actions)

    def _delete(self, chunk_ids: tuple[str, ...]) -> None:
        self.services.opensearch.bulk(
            [{"delete": {"_index": self.target.index, "_id": _id}} for _id in chunk_ids]
        )
