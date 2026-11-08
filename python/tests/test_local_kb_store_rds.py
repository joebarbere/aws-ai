"""The RDS (pgvector) store, against Phase 0b: local-kb/fixtures/rds/.

Offline, a fake connection records every statement, and the tests compare them with
postgresql-statements.json, the SQL Bedrock itself ran on Aurora. With the local Postgres up
(``make local-up``), the same Phase 0b sequence also runs against a real table.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_local_kb_ingest import DOCS, FakeS3

from aws_ai.local_kb import cli
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.ingest import Services, run_job
from aws_ai.local_kb.store import store_for
from aws_ai.local_kb.store_rds import RdsStore

REPO = Path(__file__).resolve().parents[2]
RDS = REPO / "local-kb" / "fixtures" / "rds"
DIMS = 4


def fixture(name: str) -> Any:
    return json.loads((RDS / name).read_text())


def bedrock_statements() -> list[tuple[str, str]]:
    """(time, SQL) for what bedrock_user ran, in order, with the log prefix stripped."""
    out = []
    for event in fixture("postgresql-statements.json"):
        message = event["message"]
        if "bedrock_user@" in message:
            sql = re.sub(r".*?LOG:  (execute [^:]*: |statement: )", "", message, count=1)
            out.append((message[11:19], sql))
    return out


def shape(sql: str) -> str:
    """Comparable SQL: placeholders as %s, uuid literals and vectors elided."""
    sql = re.sub(r"\$\d+", "%s", sql)
    sql = re.sub(r"'[0-9a-f]{8}-[0-9a-f-]{27}'", "'<id>'", sql)
    return re.sub(r"\[[^\]]*\]", "[…]", sql).strip()


# ------------------------------------------------------------- the fake connection


class FakePostgres:
    """Just enough of a psycopg connection to run the store's writes, recording each
    statement (BEGIN and COMMIT included) the way log_statement=all does."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[Any, ...]] = {}
        self.log: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append("BEGIN")
        yield
        self.log.append("COMMIT")

    def execute(self, sql: str, params: Any = None) -> FakePostgres:
        self.log.append(sql)
        self.result: list[tuple[Any, ...]] = []
        if sql.startswith("INSERT"):
            row_id, vector, text, metadata, custom = params
            self.rows[row_id] = (row_id, vector, text, metadata.obj, custom.obj)
        elif sql.startswith("DELETE") and "@>" in sql:
            wanted = params[0].obj.items()
            self.rows = {k: r for k, r in self.rows.items() if not wanted <= r[4].items()}
        elif sql.startswith("DELETE"):
            for row_id in params:
                self.rows.pop(row_id, None)
        elif sql.startswith("SELECT id, embedding"):
            ids = re.findall(r"'([0-9a-f-]{36})'", sql)
            self.result = [self.rows[i] for i in ids if i in self.rows]
        elif "pg_attribute" in sql:
            types = ["uuid", f"vector({DIMS})", "text", "json", "jsonb"]
            self.result = list(
                zip(
                    ["id", "embedding", "chunks", "metadata", "custom_metadata"], types, strict=True
                )
            )
        elif "pg_indexes" in sql:
            self.result = [("an_index", "…")]
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.result


def bedrock_tokens() -> Any:
    """Qwen's tokenizer, which reproduces Bedrock's 17 chunks; the sequence tests compare chunk
    texts and per-document transactions, so they need it."""
    try:
        from aws_ai.local_kb.embed import qwen_token_counter

        return qwen_token_counter()
    except Exception as exc:  # no network and no cached tokenizer
        pytest.skip(f"Qwen tokenizer unavailable: {exc}")


def phase0b(
    connect: Any, table: str | None = None, count_tokens: Any = None
) -> tuple[ControlPlane, Services, FakeS3, str, str, list[int]]:
    storage = cli.default_rds_storage_configuration()
    if table:
        storage["rdsConfiguration"]["tableName"] = table
    cp = ControlPlane(sqlite3.connect(":memory:"))
    kb = cp.create_knowledge_base(
        name="phase0b",
        knowledge_base_configuration=cli.default_knowledge_base_configuration(DIMS),
        storage_configuration=storage,
    )
    ds = cp.create_data_source(
        knowledge_base_id=kb["knowledgeBaseId"],
        name="docs",
        data_source_configuration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::kb-data", "inclusionPrefixes": ["kb/"]},
        },
    )
    s3, calls = FakeS3(), [0]

    def embed(texts: list[str]) -> list[list[float]]:
        calls[0] += len(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    services = Services(
        s3,
        None,  # type: ignore[arg-type]
        lambda _arn, _dims: embed,
        count_tokens or (lambda t: len(t) // 4),
        postgres=connect,
    )
    return cp, services, s3, kb["knowledgeBaseId"], ds["dataSourceId"], calls


def mutate(tmp_path: Path) -> Path:
    """The Phase 0 mutation: delete one file, edit one, change one sidecar."""
    mutated = tmp_path / "kb"
    shutil.copytree(DOCS, mutated)
    (mutated / "winter-closure-2025.txt").unlink()
    with (mutated / "night-log-template.txt").open("a") as f:
        f.write("\nFrom November 2026, also record the moon phase.\n")
    sidecar = mutated / "dew-heater-guide.md.metadata.json"
    meta = json.loads(sidecar.read_text())
    meta["metadataAttributes"]["revision"]["value"]["numberValue"] = 4
    sidecar.write_text(json.dumps(meta))
    return mutated


def writes(log: list[str]) -> list[str]:
    """The statements that change rows, as one letter each: the sequence the capture shows."""
    letters = {"INSERT": "I", "BEGIN": "B", "COMMIT": "C", "DELETE": "D", "SELECT id,": "S"}
    return [c for sql in log for k, c in letters.items() if sql.startswith(k)]


def transactions(letters: list[str]) -> list[int]:
    """Rows per write: a lone INSERT is 1, a BEGIN … COMMIT block its number of INSERTs."""
    sizes, inside = [], None
    for c in letters:
        if c == "B":
            inside = 0
        elif c == "C":
            sizes.append(inside)
            inside = None
        elif c == "I":
            if inside is None:
                sizes.append(1)
            else:
                inside += 1
    return sorted(sizes)


# ------------------------------------------------------------------- offline


def test_phase0b_sequence_issues_bedrocks_statements(tmp_path: Path) -> None:
    db = FakePostgres()
    cp, services, s3, kb_id, ds_id, calls = phase0b(lambda _database: db, None, bedrock_tokens())
    captured = bedrock_statements()
    job1 = [sql for t, sql in captured if t < "18:44"]
    job2 = [sql for t, sql in captured if t >= "18:44"]

    s3.load(DOCS)
    first = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert first["statistics"] == fixture("ingestion-job-1.json")["statistics"]
    # Validation, as Bedrock runs it at the start of a sync.
    assert {shape(s) for s in db.log if "pg_" in s or "LIMIT 1" in s} <= {shape(s) for s in job1}
    # Every write statement is Bedrock's, and documents go in the same groupings: one INSERT
    # for a one-chunk document, BEGIN … COMMIT around a document with several.
    ours = [s for s in db.log if s.startswith(("INSERT", "DELETE", "SELECT id,"))]
    assert {shape(s) for s in ours} <= {shape(s) for s in job1}
    assert transactions(writes(db.log)) == transactions(writes(job1))
    assert sorted(r[2] for r in db.rows.values()) == sorted(
        r["chunks"] for r in fixture("index-documents.json")["rows"]
    )

    s3.load(mutate(tmp_path))
    db.log.clear()
    calls[0] = 0
    second = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert second["statistics"] == fixture("ingestion-job-2.json")["statistics"]
    assert calls[0] == 1  # only the edited file is re-embedded
    # Removal, sidecar read-and-upsert, then the edit's DELETE before its INSERT.
    assert writes(db.log) == writes(job2) == ["D", "S", "I", "D", "I"]
    ours = [s for s in db.log if s.startswith(("INSERT", "DELETE", "SELECT id,"))]
    assert [shape(s) for s in ours] == [
        shape(s) for s in job2 if s.startswith(("INSERT", "DELETE", "SELECT id,"))
    ]
    assert sorted(r[2] for r in db.rows.values()) == sorted(
        r["chunks"] for r in fixture("index-documents-after-mutation.json")["rows"]
    )


def test_rows_have_bedrocks_columns_and_types() -> None:
    db = FakePostgres()
    cp, services, s3, kb_id, ds_id, _ = phase0b(lambda _database: db)
    s3.load(DOCS)
    run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    real = fixture("index-documents.json")["rows"]

    def find(rows: list[Any], suffix: str, uri: Any) -> Any:
        return next(r for r in rows if uri(r).endswith(suffix))

    for name in ("dew-heater-guide.md", "tidewater-handbook.pdf"):
        _, _, _, metadata, custom = find(list(db.rows.values()), name, lambda r: r[3]["sourceUrl"])
        aws = find(real, name, lambda r: r["metadata"]["sourceUrl"])
        assert set(metadata) == set(aws["metadata"]) == {"sourceUrl"}
        assert set(custom) == set(aws["custom_metadata"])
        assert {k: type(v) for k, v in custom.items()} == {
            k: type(v) for k, v in aws["custom_metadata"].items()
        }
    for row_id in db.rows:
        assert uuid.UUID(row_id).version == 4


def test_sidecar_rewrite_keeps_the_id_vector_and_bedrock_fields(tmp_path: Path) -> None:
    db = FakePostgres()
    cp, services, s3, kb_id, ds_id, _ = phase0b(lambda _database: db)
    s3.load(DOCS)
    run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    before = {k: r for k, r in db.rows.items() if r[3]["sourceUrl"].endswith("dew-heater-guide.md")}
    s3.load(mutate(tmp_path))
    run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    for row_id, (_, vector, text, metadata, custom) in before.items():
        after = db.rows[row_id]
        assert after[1:4] == (vector, text, metadata)
        assert after[4] == {**custom, "revision": 4.0}


def test_delete_data_source_drops_its_rows() -> None:
    db = FakePostgres()
    cp, services, s3, kb_id, ds_id, _ = phase0b(lambda _database: db)
    s3.load(DOCS)
    run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    store_for(cp.get_knowledge_base(kb_id), services).delete_data_source(ds_id)
    assert db.rows == {}


def test_a_table_without_bedrocks_indexes_fails_the_sync() -> None:
    class NoIndexes(FakePostgres):
        def execute(self, sql: str, params: Any = None) -> FakePostgres:
            super().execute(sql, params)
            if "pg_indexes" in sql and "hnsw" in sql:
                self.result = []
            return self

    cp, services, s3, kb_id, ds_id, _ = phase0b(lambda _database: NoIndexes())
    s3.load(DOCS)
    job = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert job["status"] == "FAILED"
    assert "hnsw" in job["failureReasons"][-1]


# ---------------------------------------------------------------------- live


def live_table() -> Iterator[tuple[Any, str]]:
    """A throwaway copy of AWS's table, vector(4), in the local Postgres; skip without one."""
    psycopg = pytest.importorskip("psycopg")
    from aws_ai.local_kb import config

    env = config.load_env()
    if "BEDROCK_PG_PASSWORD" not in env:
        pytest.skip("no local-kb/.env with BEDROCK_PG_PASSWORD")
    try:
        db = psycopg.connect(
            host=config.PG_HOST,
            port=config.PG_PORT,
            dbname=config.RDS_DATABASE,
            user=config.PG_USER,
            password=env["BEDROCK_PG_PASSWORD"],
            autocommit=True,
            connect_timeout=2,
        )
    except psycopg.OperationalError:
        pytest.skip("the local Postgres is not running (make local-up)")
    spec = importlib.util.spec_from_file_location("rds_kb", REPO / "python/scripts/rds_kb.py")
    assert spec and spec.loader
    rds_kb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rds_kb)
    name = f"test_{uuid.uuid4().hex[:12]}"
    for sql in rds_kb.TABLE_SQL:
        db.execute(
            sql.replace("bedrock_kb", name).replace(
                f"vector({rds_kb.DIMENSIONS})", f"vector({DIMS})"
            )
        )
    try:
        yield db, f"bedrock_integration.{name}"
    finally:
        db.execute(f"DROP TABLE bedrock_integration.{name}")
        db.close()


@pytest.fixture
def live() -> Iterator[tuple[Any, str]]:
    yield from live_table()


def test_phase0b_sequence_against_the_local_postgres(live: tuple[Any, str], tmp_path: Path) -> None:
    db, table = live
    cp, services, s3, kb_id, ds_id, _ = phase0b(lambda _database: db, table, bedrock_tokens())
    assert isinstance(store_for(cp.get_knowledge_base(kb_id), services), RdsStore)

    s3.load(DOCS)
    first = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert first["status"] == "COMPLETE", first.get("failureReasons")
    assert first["statistics"] == fixture("ingestion-job-1.json")["statistics"]
    rows = db.execute(f"SELECT chunks, metadata, custom_metadata FROM {table}").fetchall()
    assert sorted(r[0] for r in rows) == sorted(
        r["chunks"] for r in fixture("index-documents.json")["rows"]
    )
    aws_keys = {k for r in fixture("index-documents.json")["rows"] for k in r["custom_metadata"]}
    assert {k for r in rows for k in r[2]} == aws_keys
    assert {k for r in rows for k in r[1]} == {"sourceUrl"}

    s3.load(mutate(tmp_path))
    second = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert second["statistics"] == fixture("ingestion-job-2.json")["statistics"]
    rows = db.execute(f"SELECT chunks, custom_metadata FROM {table}").fetchall()
    assert sorted(r[0] for r in rows) == sorted(
        r["chunks"] for r in fixture("index-documents-after-mutation.json")["rows"]
    )
    dew = [
        r[1] for r in rows if r[1]["x-amz-bedrock-kb-source-uri"].endswith("dew-heater-guide.md")
    ]
    assert dew and all(c["revision"] == 4.0 for c in dew)
