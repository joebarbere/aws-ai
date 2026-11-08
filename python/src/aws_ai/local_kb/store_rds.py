"""The RDS store: Aurora PostgreSQL + pgvector, laid out as in local-kb/fixtures/rds/.

Every statement here is one Bedrock ran in Phase 0b (fixtures/rds/postgresql-statements.json),
with its ``$n`` placeholders written as psycopg's ``%s``. Bedrock reaches Aurora through the Data
API; locally the store connects directly, as ``bedrock_user``.

One row per chunk, in the five columns of the knowledge base's field mapping:

- the primary key, a uuid4;
- the vector, sent as a ``'[…]'::vector`` literal;
- the text;
- ``metadata`` (json): ``{"sourceUrl": uri}``;
- ``custom_metadata`` (jsonb): the sidecar attributes, numbers as floats, plus Bedrock's
  ``x-amz-bedrock-kb-*`` fields (the page number stays an integer).

Writes follow Bedrock's own sequence: one upsert per chunk, inside a transaction when a
document has more than one; an edited document's old rows deleted before its new ones go in, as
separate statements; a sidecar-only change read back and upserted under the same id.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aws_ai.local_kb import index
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.store import Hit, StoredChunk, embedding_dimensions

BEDROCK_FIELD_PREFIX = "x-amz-bedrock-kb-"

# Connects to a database by name, as bedrock_user, in autocommit mode.
Connect = Callable[[str], Any]


@dataclass(frozen=True)
class RdsTarget:
    """Where a knowledge base keeps its rows, read from its storageConfiguration."""

    database: str
    table: str
    id: str
    vector: str
    text: str
    metadata: str
    custom: str
    dimensions: int

    @classmethod
    def of(cls, knowledge_base: dict[str, Any]) -> RdsTarget:
        rds = knowledge_base["storageConfiguration"]["rdsConfiguration"]
        fields = rds["fieldMapping"]
        return cls(
            rds["databaseName"],
            rds["tableName"],
            fields["primaryKeyField"],
            fields["vectorField"],
            fields["textField"],
            fields["metadataField"],
            fields["customMetadataField"],
            embedding_dimensions(knowledge_base),
        )

    @property
    def schema_and_name(self) -> tuple[str, str]:
        schema, _, name = self.table.rpartition(".")
        return schema or "public", name


class RdsStore:
    def __init__(self, connect: Connect, target: RdsTarget) -> None:
        self._connect = connect
        self._db: Any = None
        self.target = target
        self.dimensions = target.dimensions
        t = target
        self._insert = (
            f"INSERT INTO {t.table} ({t.id}, {t.vector}, {t.text}, {t.metadata}, {t.custom}) "
            "VALUES (%s, %s::vector, %s, %s, %s) "
            f"ON CONFLICT({t.id}) DO UPDATE SET {t.vector}=EXCLUDED.{t.vector}, "
            f"{t.text}=EXCLUDED.{t.text}, {t.metadata}=EXCLUDED.{t.metadata}, "
            f"{t.custom}=EXCLUDED.{t.custom} RETURNING NULL"
        )

    @property
    def db(self) -> Any:
        if self._db is None:
            self._db = self._connect(self.target.database)
        return self._db

    # -------------------------------------------------------------- validation

    def ensure(self) -> None:
        """Bedrock's checks at CreateKnowledgeBase and at the start of every sync: a ``<=>``
        probe, the column types, and three index lookups. Bedrock never creates the table;
        neither does this. The error wording is local-kb's: the capture saw only passes."""
        t = self.target
        schema, name = t.schema_and_name
        zero = "[" + ",".join(["0"] * t.dimensions) + "]"
        self.db.execute(f"SELECT {t.vector} <=> '{zero}'::vector as score FROM {t.table} LIMIT 1;")
        columns = dict(
            self.db.execute(
                "SELECT attname AS column_name, format_type(atttypid, atttypmod) AS data_type "
                f"FROM pg_attribute WHERE attrelid = '{t.table}'::regclass AND attnum > 0;"
            ).fetchall()
        )
        expected = {
            t.id: {"uuid"},
            t.vector: {f"vector({t.dimensions})"},
            t.text: {"text"},
            t.metadata: {"json", "jsonb"},
            t.custom: {"jsonb"},
        }
        for column, types in expected.items():
            if columns.get(column) not in types:
                raise ValidationException(
                    f"Column {column} of {t.table} must be {' or '.join(sorted(types))}, "
                    f"not {columns.get(column) or 'missing'}."
                )
        where = f"FROM pg_indexes WHERE schemaname= '{schema}' AND tablename = '{name}' AND "
        select = "SELECT indexname AS index_name, indexdef AS index_definition "
        indexes = {
            f"a gin index on to_tsvector('simple', {t.text})": (
                f"(indexdef LIKE '%USING gin (to_tsvector(''simple''::regconfig, {t.text}))%' OR "
                f"indexdef LIKE '%USING gin (to_tsvector(''english''::regconfig, {t.text}))%')"
            ),
            f"an hnsw index on {t.vector} vector_cosine_ops": (
                f"indexdef LIKE '%USING hnsw ({t.vector} vector_cosine_ops)%'"
            ),
            f"a gin index on {t.custom}": f"indexdef LIKE '%USING gin ({t.custom})%'",
        }
        for description, condition in indexes.items():
            if not self.db.execute(select + where + condition).fetchall():
                raise ValidationException(f"{t.table} needs {description}.")

    # ------------------------------------------------------------------ writes

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def write(self, chunks: list[StoredChunk]) -> None:
        self._upsert([self._row(piece) for piece in chunks])

    def _row(self, piece: StoredChunk) -> tuple[Any, ...]:
        """One stored chunk, shaped like local-kb/fixtures/rds/index-documents.json."""
        metadata: dict[str, Any] = {"sourceUrl": piece.source_uri}
        if piece.parent_text is not None:
            # GUESS: Phase 0b used default chunking, so where Bedrock keeps a HIERARCHICAL
            # chunk's parent on this store was not captured. OpenSearch keeps it beside the
            # source in AMAZON_BEDROCK_METADATA; this mirrors that.
            metadata["parentText"] = piece.parent_text
        custom: dict[str, Any] = {k: _floats(v) for k, v in piece.attributes.items()}
        custom[index.SOURCE_URI] = piece.source_uri
        custom[index.DATA_SOURCE_ID] = piece.data_source_id
        custom[index.MODALITY] = "TEXT"
        if piece.page is not None:
            custom[index.PAGE_NUMBER] = piece.page
        return (piece.id, _vector(piece.vector), piece.text, _json(metadata), _jsonb(custom))

    def _upsert(self, rows: list[tuple[Any, ...]]) -> None:
        """One statement per row; several go in one transaction, as Bedrock does."""
        if len(rows) == 1:
            self.db.execute(self._insert, rows[0])
            return
        with self.db.transaction():
            for row in rows:
                self.db.execute(self._insert, row)

    def rewrite_attributes(self, chunk_ids: list[str], attributes: dict[str, Any]) -> None:
        """Read the rows back, vectors and all, swap their sidecar attributes, and upsert them
        under the same id: Bedrock's sequence, which a plain UPDATE would not reproduce."""
        t = self.target
        literals = ", ".join(f"'{uuid.UUID(i)}'" for i in chunk_ids)
        rows = self.db.execute(
            f"SELECT {t.id}, {t.vector}, {t.text}, {t.metadata}, {t.custom} "
            f"FROM {t.table} WHERE {t.id} IN ({literals})"
        ).fetchall()
        new = {k: _floats(v) for k, v in attributes.items()}
        upserts = []
        for row_id, vector, text, metadata, custom in rows:
            own = {k: v for k, v in custom.items() if k.startswith(BEDROCK_FIELD_PREFIX)}
            upserts.append((row_id, vector, text, _json(metadata), _jsonb({**new, **own})))
        if upserts:
            self._upsert(upserts)

    def delete(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        t = self.target
        marks = ", ".join(["%s"] * len(chunk_ids))
        self.db.execute(
            f"DELETE FROM {t.table} WHERE {t.id} IN ({marks}) RETURNING NULL", chunk_ids
        )

    def delete_data_source(self, data_source_id: str) -> None:
        # GUESS: Phase 0b did not delete its data source while the cluster was up.
        t = self.target
        self.db.execute(
            f"DELETE FROM {t.table} WHERE {t.custom} @> %s",
            (_jsonb({index.DATA_SOURCE_ID: data_source_id}),),
        )

    # ------------------------------------------------------------------- reads

    def compile_filter(self, retrieval_filter: dict[str, Any]) -> Any:
        raise NotImplementedError("RDS retrieval arrives in PGVECTOR_PLAN.md phase P4")

    def semantic(self, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]:
        raise NotImplementedError("RDS retrieval arrives in PGVECTOR_PLAN.md phase P4")

    def hybrid(self, text: str, vector: list[float], k: int, compiled_filter: Any) -> list[Hit]:
        raise NotImplementedError("RDS retrieval arrives in PGVECTOR_PLAN.md phase P4")


def _vector(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _floats(value: Any) -> Any:
    """Bedrock stores every attribute number as a float (``3.0``, ``20261015.0``)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _json(value: Any) -> Any:
    from psycopg.types.json import Json

    return Json(value, dumps=_compact)


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value, dumps=_compact)
