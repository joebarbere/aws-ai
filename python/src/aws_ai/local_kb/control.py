"""The control plane: knowledge bases, data sources, ingestion jobs, and the document ledger.

This is the state Bedrock keeps out of sight, kept here in SQLite. Every method returns the same
shape the matching ``bedrock-agent`` operation returns (checked against the botocore model and
local-kb/fixtures/), so the CLI and the runtime server can hand results straight back.

The data plane — the chunks themselves — lives only in OpenSearch. The one place the two meet
is deletion: a data source with ``dataDeletionPolicy: DELETE`` takes its vectors with it, which
the caller does through the ``purge`` callback.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aws_ai.local_kb import config
from aws_ai.local_kb.errors import (
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)
from aws_ai.local_kb.ids import resource_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id   TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_sources (
    id    TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
    body  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id         TEXT PRIMARY KEY,
    kb_id      TEXT NOT NULL,
    ds_id      TEXT NOT NULL,
    status     TEXT NOT NULL,
    started_at TEXT NOT NULL,
    body       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    ds_id         TEXT NOT NULL,
    uri           TEXT NOT NULL,
    etag          TEXT NOT NULL,
    metadata_etag TEXT,
    embedding_prefix TEXT NOT NULL DEFAULT '',
    chunk_ids     TEXT NOT NULL,
    PRIMARY KEY (ds_id, uri)
);
CREATE TABLE IF NOT EXISTS sessions (
    id    TEXT PRIMARY KEY,
    turns TEXT NOT NULL
);
"""

_NAME = re.compile(r"([0-9a-zA-Z][_-]?){1,100}")
_TIMESTAMPS = ("createdAt", "updatedAt", "startedAt")
ACTIVE_JOB_STATUSES = ("STARTING", "IN_PROGRESS", "STOPPING")
STATISTICS = (
    "numberOfDocumentsScanned",
    "numberOfMetadataDocumentsScanned",
    "numberOfNewDocumentsIndexed",
    "numberOfModifiedDocumentsIndexed",
    "numberOfMetadataDocumentsModified",
    "numberOfDocumentsDeleted",
    "numberOfDocumentsFailed",
    "numberOfDocumentsSkipped",
)


@dataclass(frozen=True)
class DocumentRecord:
    """What the last sync saw of one source file: the ledger entry incremental sync diffs.

    ``metadata_etag`` is the ETag of the file's ``.metadata.json`` sidecar, or None without one.
    ``embedding_prefix`` is the text its ``includeForEmbedding`` attributes put in front of each
    chunk: a sidecar change that alters it needs re-embedding, any other is a metadata rewrite.
    """

    uri: str
    etag: str
    metadata_etag: str | None
    chunk_ids: tuple[str, ...]
    embedding_prefix: str = ""


def open_control_plane(state_dir: Path | None = None) -> ControlPlane:
    """Open (creating if needed) the SQLite store under ``state_dir``."""
    directory = state_dir or config.default_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "control.db", timeout=30)
    # WAL lets the server's request threads read while its ingestion worker writes.
    connection.execute("PRAGMA journal_mode=WAL")
    return ControlPlane(connection)


class ControlPlane:
    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = connection
        self.db.executescript(_SCHEMA)
        self.clock = clock

    # ---------------------------------------------------------- knowledge bases

    def create_knowledge_base(
        self,
        *,
        name: str,
        knowledge_base_configuration: dict[str, Any],
        storage_configuration: dict[str, Any],
        role_arn: str = f"arn:aws:iam::{config.ACCOUNT_ID}:role/local-kb",
        description: str | None = None,
    ) -> dict[str, Any]:
        _check_name(name)
        _check_vector_configuration(knowledge_base_configuration)
        _check_storage(storage_configuration)
        kb_id = resource_id()
        now = self._now()
        body: dict[str, Any] = {
            "knowledgeBaseId": kb_id,
            "name": name,
            "knowledgeBaseArn": (
                f"arn:aws:bedrock:{config.REGION}:{config.ACCOUNT_ID}:knowledge-base/{kb_id}"
            ),
            "roleArn": role_arn,
            "knowledgeBaseConfiguration": knowledge_base_configuration,
            "storageConfiguration": storage_configuration,
            "status": "ACTIVE",
            "createdAt": now,
            "updatedAt": now,
        }
        if description is not None:
            body["description"] = description
        with self.db:
            self.db.execute(
                "INSERT INTO knowledge_bases (id, body) VALUES (?, ?)", (kb_id, json.dumps(body))
            )
        return _out(body)

    def get_knowledge_base(self, knowledge_base_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT body FROM knowledge_bases WHERE id = ?", (knowledge_base_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundException(f"Knowledge base {knowledge_base_id} does not exist.")
        return _out(json.loads(row[0]))

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        bodies = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM knowledge_bases")]
        return [_summary(b, ("knowledgeBaseId", "name", "description", "status")) for b in bodies]

    def delete_knowledge_base(
        self,
        knowledge_base_id: str,
        purge: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Delete the knowledge base and its data sources, each by its own deletion policy."""
        self.get_knowledge_base(knowledge_base_id)
        self._refuse_if_syncing(knowledge_base_id)
        for summary in self.list_data_sources(knowledge_base_id):
            self.delete_data_source(knowledge_base_id, summary["dataSourceId"], purge)
        with self.db:
            self.db.execute("DELETE FROM ingestion_jobs WHERE kb_id = ?", (knowledge_base_id,))
            self.db.execute("DELETE FROM knowledge_bases WHERE id = ?", (knowledge_base_id,))
        return {"knowledgeBaseId": knowledge_base_id, "status": "DELETING"}

    # ------------------------------------------------------------- data sources

    def create_data_source(
        self,
        *,
        knowledge_base_id: str,
        name: str,
        data_source_configuration: dict[str, Any],
        vector_ingestion_configuration: dict[str, Any] | None = None,
        data_deletion_policy: str = "DELETE",
        description: str | None = None,
    ) -> dict[str, Any]:
        self.get_knowledge_base(knowledge_base_id)
        _check_name(name)
        _check_s3_source(data_source_configuration)
        if vector_ingestion_configuration is not None:
            _check_vector_ingestion(vector_ingestion_configuration)
        if data_deletion_policy not in ("DELETE", "RETAIN"):
            raise ValidationException("dataDeletionPolicy must be DELETE or RETAIN.")
        ds_id = resource_id()
        now = self._now()
        body: dict[str, Any] = {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": ds_id,
            "name": name,
            "status": "AVAILABLE",
            "dataSourceConfiguration": data_source_configuration,
            "dataDeletionPolicy": data_deletion_policy,
            "createdAt": now,
            "updatedAt": now,
        }
        # Bedrock omits the key entirely when chunking is left at its default (Phase 0), and
        # offers no way to change it later: chunking is fixed at creation.
        if vector_ingestion_configuration is not None:
            body["vectorIngestionConfiguration"] = vector_ingestion_configuration
        if description is not None:
            body["description"] = description
        with self.db:
            self.db.execute(
                "INSERT INTO data_sources (id, kb_id, body) VALUES (?, ?, ?)",
                (ds_id, knowledge_base_id, json.dumps(body)),
            )
        return _out(body)

    def get_data_source(self, knowledge_base_id: str, data_source_id: str) -> dict[str, Any]:
        self.get_knowledge_base(knowledge_base_id)
        row = self.db.execute(
            "SELECT body FROM data_sources WHERE id = ? AND kb_id = ?",
            (data_source_id, knowledge_base_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundException(
                f"Data source {data_source_id} does not exist in knowledge base "
                f"{knowledge_base_id}."
            )
        return _out(json.loads(row[0]))

    def list_data_sources(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        self.get_knowledge_base(knowledge_base_id)
        rows = self.db.execute(
            "SELECT body FROM data_sources WHERE kb_id = ?", (knowledge_base_id,)
        )
        keys = ("knowledgeBaseId", "dataSourceId", "name", "description", "status")
        return [_summary(json.loads(r[0]), keys) for r in rows]

    def delete_data_source(
        self,
        knowledge_base_id: str,
        data_source_id: str,
        purge: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Delete the data source. Under ``DELETE`` its vectors go too, through ``purge``;
        under ``RETAIN`` they stay in the index, orphaned, exactly as on AWS."""
        data_source = self.get_data_source(knowledge_base_id, data_source_id)
        self._refuse_if_syncing(knowledge_base_id, data_source_id)
        if data_source["dataDeletionPolicy"] == "DELETE" and purge is not None:
            purge(data_source)
        with self.db:
            self.db.execute("DELETE FROM documents WHERE ds_id = ?", (data_source_id,))
            self.db.execute("DELETE FROM ingestion_jobs WHERE ds_id = ?", (data_source_id,))
            self.db.execute("DELETE FROM data_sources WHERE id = ?", (data_source_id,))
        return {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
            "status": "DELETING",
        }

    # ----------------------------------------------------------- ingestion jobs

    def start_ingestion_job(
        self, knowledge_base_id: str, data_source_id: str, description: str | None = None
    ) -> dict[str, Any]:
        """Queue a sync. The worker (Phase 4) picks up STARTING jobs; this only records one.

        Bedrock allows one running job per knowledge base, so a second start is a conflict.
        """
        self.get_data_source(knowledge_base_id, data_source_id)
        self._refuse_if_syncing(knowledge_base_id)
        job_id = resource_id()
        now = self._now()
        body: dict[str, Any] = {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
            "ingestionJobId": job_id,
            "status": "STARTING",
            "statistics": dict.fromkeys(STATISTICS, 0),
            "startedAt": now,
            "updatedAt": now,
        }
        if description is not None:
            body["description"] = description
        with self.db:
            self.db.execute(
                "INSERT INTO ingestion_jobs (id, kb_id, ds_id, status, started_at, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, knowledge_base_id, data_source_id, "STARTING", now, json.dumps(body)),
            )
        return _out(body)

    def get_ingestion_job(
        self, knowledge_base_id: str, data_source_id: str, ingestion_job_id: str
    ) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT body FROM ingestion_jobs WHERE id = ? AND kb_id = ? AND ds_id = ?",
            (ingestion_job_id, knowledge_base_id, data_source_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundException(f"Ingestion job {ingestion_job_id} does not exist.")
        return _out(json.loads(row[0]))

    def list_ingestion_jobs(
        self,
        knowledge_base_id: str,
        data_source_id: str,
        *,
        statuses: Iterable[str] | None = None,
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """Summaries, newest first unless ``ascending``; ``statuses`` is the STATUS filter."""
        self.get_data_source(knowledge_base_id, data_source_id)
        rows = self.db.execute(
            "SELECT body, status FROM ingestion_jobs WHERE kb_id = ? AND ds_id = ? "
            f"ORDER BY started_at {'ASC' if ascending else 'DESC'}",
            (knowledge_base_id, data_source_id),
        )
        wanted = set(statuses) if statuses is not None else None
        return [
            _out(json.loads(body)) for body, status in rows if wanted is None or status in wanted
        ]

    def stop_ingestion_job(
        self, knowledge_base_id: str, data_source_id: str, ingestion_job_id: str
    ) -> dict[str, Any]:
        job = self.get_ingestion_job(knowledge_base_id, data_source_id, ingestion_job_id)
        if job["status"] not in ("STARTING", "IN_PROGRESS"):
            raise ConflictException(f"Ingestion job {ingestion_job_id} is {job['status']}.")
        return self.update_ingestion_job(ingestion_job_id, status="STOPPING")

    def update_ingestion_job(
        self,
        ingestion_job_id: str,
        *,
        status: str | None = None,
        statistics: dict[str, int] | None = None,
        failure_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        """For the worker: move a job along and record its counters."""
        row = self.db.execute(
            "SELECT body FROM ingestion_jobs WHERE id = ?", (ingestion_job_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundException(f"Ingestion job {ingestion_job_id} does not exist.")
        body = json.loads(row[0])
        if status is not None:
            body["status"] = status
        if statistics is not None:
            body["statistics"] = {**body["statistics"], **statistics}
        if failure_reasons:
            body["failureReasons"] = [*body.get("failureReasons", []), *failure_reasons]
        body["updatedAt"] = self._now()
        with self.db:
            self.db.execute(
                "UPDATE ingestion_jobs SET status = ?, body = ? WHERE id = ?",
                (body["status"], json.dumps(body), ingestion_job_id),
            )
        return _out(body)

    def pending_jobs(self) -> list[dict[str, Any]]:
        """STARTING jobs across every knowledge base, oldest first: the worker's queue."""
        rows = self.db.execute(
            "SELECT body FROM ingestion_jobs WHERE status = 'STARTING' ORDER BY started_at"
        )
        return [_out(json.loads(r[0])) for r in rows]

    def _refuse_if_syncing(self, knowledge_base_id: str, data_source_id: str | None = None) -> None:
        query = "SELECT id FROM ingestion_jobs WHERE kb_id = ? AND status IN (?, ?, ?)"
        args: tuple[str, ...] = (knowledge_base_id, *ACTIVE_JOB_STATUSES)
        if data_source_id is not None:
            query += " AND ds_id = ?"
            args = (*args, data_source_id)
        if (row := self.db.execute(query, args).fetchone()) is not None:
            raise ConflictException(
                f"Ingestion job {row[0]} is still running for knowledge base {knowledge_base_id}."
            )

    # ------------------------------------------------------------ document ledger

    def documents(self, data_source_id: str) -> dict[str, DocumentRecord]:
        rows = self.db.execute(
            "SELECT uri, etag, metadata_etag, chunk_ids, embedding_prefix FROM documents "
            "WHERE ds_id = ?",
            (data_source_id,),
        )
        return {
            uri: DocumentRecord(uri, etag, metadata_etag, tuple(json.loads(chunk_ids)), prefix)
            for uri, etag, metadata_etag, chunk_ids, prefix in rows
        }

    def put_document(self, data_source_id: str, record: DocumentRecord) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO documents "
                "(ds_id, uri, etag, metadata_etag, chunk_ids, embedding_prefix) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    data_source_id,
                    record.uri,
                    record.etag,
                    record.metadata_etag,
                    json.dumps(list(record.chunk_ids)),
                    record.embedding_prefix,
                ),
            )

    def remove_document(self, data_source_id: str, uri: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM documents WHERE ds_id = ? AND uri = ?", (data_source_id, uri)
            )

    # ------------------------------------------------------------------ sessions

    def create_session(self) -> str:
        """A RetrieveAndGenerate session. Bedrock issues the id (a UUID, per Phase 0);
        callers can only pass back one it gave them."""
        session_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO sessions (id, turns) VALUES (?, '[]')", (session_id,))
        return session_id

    def session_turns(self, session_id: str) -> list[tuple[str, str]]:
        row = self.db.execute("SELECT turns FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ResourceNotFoundException(f"Session {session_id} does not exist.")
        return [(user, bot) for user, bot in json.loads(row[0])]

    def append_turn(self, session_id: str, user: str, bot: str) -> None:
        turns = [*self.session_turns(session_id), (user, bot)]
        with self.db:
            self.db.execute(
                "UPDATE sessions SET turns = ? WHERE id = ?", (json.dumps(turns), session_id)
            )

    def _now(self) -> str:
        return self.clock().isoformat()


# ------------------------------------------------------------------ helpers


def _out(body: dict[str, Any]) -> dict[str, Any]:
    """Timestamps come back as datetimes, as boto3 returns them."""
    return {k: datetime.fromisoformat(v) if k in _TIMESTAMPS else v for k, v in body.items()}


def _summary(body: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return _out({k: body[k] for k in (*keys, "updatedAt") if k in body})


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValidationException(
            f"Name {name!r} must match ([0-9a-zA-Z][_-]?){{1,100}}: letters and digits, "
            "with single - or _ between them."
        )


def _check_vector_configuration(configuration: dict[str, Any]) -> None:
    vector = configuration.get("vectorKnowledgeBaseConfiguration", {})
    if configuration.get("type") != "VECTOR" or not vector.get("embeddingModelArn"):
        raise ValidationException(
            "local-kb supports VECTOR knowledge bases with an embeddingModelArn only."
        )


def _check_storage(configuration: dict[str, Any]) -> None:
    cluster = configuration.get("opensearchManagedClusterConfiguration", {})
    if configuration.get("type") != "OPENSEARCH_MANAGED_CLUSTER":
        raise ValidationException("local-kb supports OPENSEARCH_MANAGED_CLUSTER storage only.")
    missing = [k for k in ("domainEndpoint", "domainArn", "vectorIndexName") if k not in cluster]
    fields = cluster.get("fieldMapping", {})
    missing += [k for k in ("vectorField", "textField", "metadataField") if k not in fields]
    if missing:
        raise ValidationException(f"storageConfiguration is missing {', '.join(missing)}.")


def _check_s3_source(configuration: dict[str, Any]) -> None:
    s3 = configuration.get("s3Configuration", {})
    if configuration.get("type") != "S3" or not s3.get("bucketArn"):
        raise ValidationException("local-kb supports S3 data sources with a bucketArn only.")
    if len(s3.get("inclusionPrefixes", [])) > 1:
        raise ValidationException("inclusionPrefixes accepts at most one prefix.")


def _check_vector_ingestion(configuration: dict[str, Any]) -> None:
    unsupported = set(configuration) - {"chunkingConfiguration"}
    if unsupported:
        raise ValidationException(
            f"local-kb supports chunkingConfiguration only, not {', '.join(sorted(unsupported))}."
        )
    _check_chunking(configuration.get("chunkingConfiguration", {}))


def _check_chunking(chunking: dict[str, Any]) -> None:
    """The documented ranges for each chunking strategy (botocore bedrock-agent model)."""
    strategy = chunking.get("chunkingStrategy")

    def require(block: str, key: str, low: int, high: int | None = None) -> None:
        value = chunking.get(block, {}).get(key)
        if not isinstance(value, int) or value < low or (high is not None and value > high):
            span = f"{low}..{high}" if high is not None else f">= {low}"
            raise ValidationException(f"{block}.{key} must be an integer in {span}.")

    if strategy == "FIXED_SIZE":
        require("fixedSizeChunkingConfiguration", "maxTokens", 1, 8192)
        require("fixedSizeChunkingConfiguration", "overlapPercentage", 1, 99)
    elif strategy == "HIERARCHICAL":
        levels = chunking.get("hierarchicalChunkingConfiguration", {}).get("levelConfigurations")
        if not isinstance(levels, list) or len(levels) != 2:
            raise ValidationException(
                "hierarchicalChunkingConfiguration.levelConfigurations needs exactly two "
                "levels: parent, then child."
            )
        for level in levels:
            tokens = level.get("maxTokens")
            if not isinstance(tokens, int) or not 1 <= tokens <= 8192:
                raise ValidationException("Each level's maxTokens must be an integer in 1..8192.")
        require("hierarchicalChunkingConfiguration", "overlapTokens", 1)
    elif strategy == "SEMANTIC":
        require("semanticChunkingConfiguration", "maxTokens", 1, 8192)
        require("semanticChunkingConfiguration", "bufferSize", 0, 1)
        require("semanticChunkingConfiguration", "breakpointPercentileThreshold", 50, 99)
    elif strategy != "NONE":
        raise ValidationException(
            "chunkingStrategy must be FIXED_SIZE, HIERARCHICAL, SEMANTIC or NONE."
        )
