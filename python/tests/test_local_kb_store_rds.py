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
from aws_ai.local_kb.filters import to_sql
from aws_ai.local_kb.ingest import Services, run_job
from aws_ai.local_kb.retrieve import retrieve
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
        self.search: list[tuple[Any, ...]] = []  # what a SEMANTIC or HYBRID statement returns

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
        elif sql.startswith(("SELECT chunks", "WITH hybrid")):
            self.result = self.search
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


# ------------------------------------------------------------- query side


def search_shape(sql: str) -> str:
    """A search statement with its query vector and query text abstracted, whitespace
    collapsed: Bedrock inlines both as literals, the store binds them."""
    sql = re.sub(r"\$\d+", "%s", sql.replace("'[<1024 floats>]'::vector", "%s::vector"))
    sql = re.sub(r"plainto_tsquery\('simple', '[^']*'\)", "plainto_tsquery('simple', %s)", sql)
    return re.sub(r"\s+", " ", sql).strip()


def test_every_captured_query_runs_bedrocks_sql() -> None:
    """Each Retrieve fixture's request, sent to an RDS knowledge base, issues the statement
    Bedrock issued for it on Aurora — filters, HYBRID's fusion, LIMIT and all."""
    db = FakePostgres()
    cp, services, _, kb_id, _, _ = phase0b(lambda _database: db)
    captured = {
        search_shape(sql)
        for _, sql in bedrock_statements()
        if sql.startswith(("SELECT chunks", "WITH hybrid")) and "LIMIT 1;" not in sql
    }
    requests = [fixture(f.name)["request"] for f in sorted(RDS.glob("retrieve-*.json"))]
    assert len(requests) == 8
    for request in requests:
        db.log.clear()
        retrieve(
            cp,
            services,
            kb_id,
            {"text": request["query"]},
            {"vectorSearchConfiguration": request["vectorSearchConfiguration"]},
        )
        (ours,) = [s for s in db.log if s.startswith(("SELECT chunks", "WITH hybrid"))]
        assert search_shape(ours) in captured, request


@pytest.mark.parametrize(
    ("node", "sql", "params"),
    [
        (
            {"equals": {"key": "department", "value": "operations"}},
            "( custom_metadata @> %s or custom_metadata @> %s )",
            [{"department": "operations"}, {"department": ["operations"]}],
        ),
        (
            {"equals": {"key": "x-amz-bedrock-kb-document-page-number", "value": 2}},
            "custom_metadata @> %s",
            [{"x-amz-bedrock-kb-document-page-number": 2}],
        ),
        (
            {"startsWith": {"key": "k", "value": "s3://b/kb/50%_off"}},
            "(custom_metadata ->> %s) like %s",
            ["k", "s3://b/kb/50\\%\\_off%"],
        ),
        (
            {"greaterThanOrEquals": {"key": "effective_date", "value": 20261001}},
            "(custom_metadata ->> %s)::double precision >= 20261001.000000",
            ["effective_date"],
        ),
        (
            {"lessThan": {"key": "revision", "value": 2.5}},
            "(custom_metadata ->> %s)::double precision < 2.500000",
            ["revision"],
        ),
        (
            {"listContains": {"key": "tags", "value": "heater"}},
            "custom_metadata @> %s",
            [{"tags": ["heater"]}],
        ),
        (
            {
                "andAll": [
                    {"equals": {"key": "safety_critical", "value": True}},
                    {"listContains": {"key": "tags", "value": "heater"}},
                ]
            },
            "( custom_metadata @> %s and custom_metadata @> %s )",
            [{"safety_critical": True}, {"tags": ["heater"]}],
        ),
        (
            {"notIn": {"key": "d", "value": ["a", 1]}},
            "NOT ( ( custom_metadata @> %s or custom_metadata @> %s ) or custom_metadata @> %s )",
            [{"d": "a"}, {"d": ["a"]}, {"d": 1}],
        ),
    ],
    ids=[
        "equals-string",
        "equals-number",
        "startsWith-escaped",
        "gte",
        "lt",
        "listContains",
        "andAll",
        "notIn",
    ],
)
def test_filter_sql(node: dict[str, Any], sql: str, params: list[Any]) -> None:
    """The captured forms (equals, startsWith, ranges with %f, listContains, andAll) and how
    the GUESS forms compose. jsonb operands are bound as Jsonb, like Bedrock's ``$n``."""
    got_sql, got_params = to_sql(node, "custom_metadata")
    assert got_sql == sql
    assert [getattr(p, "obj", p) for p in got_params] == params


def aurora_row(text: str, score: float) -> tuple[Any, ...]:
    custom = {"x-amz-bedrock-kb-source-uri": "s3://kb-data/kb/a.txt", "revision": 3.0}
    return (text, {"sourceUrl": "s3://kb-data/kb/a.txt"}, score, custom)


@pytest.mark.parametrize("fix", [False, True])
def test_semantic_comes_back_worst_first_unless_fixed(
    fix: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 0b: Bedrock reverses an RDS store's SEMANTIC results and keeps the distance as
    the score. HYBRID is best-first. LOCAL_KB_RDS_FIX_ORDER=1 undoes the reversal."""
    if fix:
        monkeypatch.setenv("LOCAL_KB_RDS_FIX_ORDER", "1")
    db = FakePostgres()
    cp, services, _, kb_id, _, _ = phase0b(lambda _database: db)
    db.search = [aurora_row("best", 0.1), aurora_row("next", 0.2), aurora_row("worst", 0.3)]
    semantic = retrieve(cp, services, kb_id, {"text": "q"})["retrievalResults"]
    order = ["best", "next", "worst"] if fix else ["worst", "next", "best"]
    assert [r["content"]["text"] for r in semantic] == order
    assert [r["score"] for r in semantic] == [0.1, 0.2, 0.3] if fix else [0.3, 0.2, 0.1]
    hybrid = retrieve(
        cp,
        services,
        kb_id,
        {"text": "q"},
        {"vectorSearchConfiguration": {"overrideSearchType": "HYBRID"}},
    )["retrievalResults"]
    assert [r["content"]["text"] for r in hybrid] == ["best", "next", "worst"]


def test_results_have_bedrocks_shape() -> None:
    db = FakePostgres()
    cp, services, _, kb_id, _, _ = phase0b(lambda _database: db)
    custom = fixture("index-documents.json")["rows"][0]["custom_metadata"]
    pdf = next(
        r["custom_metadata"]
        for r in fixture("index-documents.json")["rows"]
        if "x-amz-bedrock-kb-document-page-number" in r["custom_metadata"]
    )
    db.search = [("a", {"sourceUrl": "u"}, 0.25, custom), ("b", {"sourceUrl": "u"}, 0.5, pdf)]
    results = retrieve(cp, services, kb_id, {"text": "q"})["retrievalResults"]
    aws = fixture("retrieve-hybrid-keyword.json")["retrievalResults"]
    assert all(set(r) == set(aws[0]) for r in results)
    assert "x-amz-bedrock-kb-chunk-id" not in results[0]["metadata"]
    assert results[0]["metadata"]["x-amz-bedrock-kb-document-page-number"] == 1.0
    assert isinstance(results[0]["metadata"]["x-amz-bedrock-kb-document-page-number"], float)
    assert results[0]["location"] == {
        "s3Location": {"uri": pdf["x-amz-bedrock-kb-source-uri"]},
        "type": "S3",
    }


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
    for sql in rds_kb.table_sql(f"bedrock_integration.{name}", DIMS):
        db.execute(sql)
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


def matches(node: dict[str, Any], meta: dict[str, Any]) -> bool:
    """What each RetrievalFilter operator means, as the OpenSearch store implements it: the
    reference for the SQL forms, captured and GUESS alike."""
    ((op, arg),) = node.items()
    if op == "andAll":
        return all(matches(n, meta) for n in arg)
    if op == "orAll":
        return any(matches(n, meta) for n in arg)
    key, value = arg["key"], arg["value"]
    have = meta.get(key)
    members = have if isinstance(have, list) else [have]

    def equals(v: Any) -> bool:
        return key in meta and (have == v or (isinstance(have, list) and v in have))

    if op == "equals":
        return equals(value)
    if op == "notEquals":
        return not equals(value)
    if op == "in":
        return any(equals(v) for v in value)
    if op == "notIn":
        return not any(equals(v) for v in value)
    if op == "listContains":
        return isinstance(have, list) and value in have
    if op == "startsWith":
        return isinstance(have, str) and have.startswith(value)
    if op == "stringContains":
        return any(isinstance(m, str) and value in m for m in members)
    if not isinstance(have, int | float) or isinstance(have, bool):
        return False
    return {
        "greaterThan": have > value,
        "greaterThanOrEquals": have >= value,
        "lessThan": have < value,
        "lessThanOrEquals": have <= value,
    }[op]


FILTERS = [
    {"equals": {"key": "department", "value": "operations"}},
    {"equals": {"key": "revision", "value": 3}},
    {"equals": {"key": "safety_critical", "value": True}},
    {"equals": {"key": "tags", "value": "heater"}},  # a string equals also matches list members
    {"notEquals": {"key": "department", "value": "operations"}},
    {"in": {"key": "department", "value": ["operations", "membership"]}},
    {"notIn": {"key": "department", "value": ["operations", "membership"]}},
    {
        "startsWith": {
            "key": "x-amz-bedrock-kb-source-uri",
            "value": "s3://aws-ai-study-data-000000000000/kb/t",
        }
    },
    {"startsWith": {"key": "department", "value": "op_"}},  # LIKE wildcards are literal
    {"stringContains": {"key": "department", "value": "era"}},
    {"stringContains": {"key": "tags", "value": "eat"}},
    {"listContains": {"key": "tags", "value": "heater"}},
    {"greaterThan": {"key": "revision", "value": 2}},
    {"greaterThanOrEquals": {"key": "effective_date", "value": 20261001}},
    {"lessThan": {"key": "x-amz-bedrock-kb-document-page-number", "value": 2}},
    {"lessThanOrEquals": {"key": "revision", "value": 1.5}},
    {
        "andAll": [
            {"equals": {"key": "safety_critical", "value": True}},
            {"greaterThanOrEquals": {"key": "effective_date", "value": 20261001}},
            {"listContains": {"key": "tags", "value": "heater"}},
        ]
    },
    {
        "orAll": [
            {"equals": {"key": "department", "value": "membership"}},
            {"listContains": {"key": "tags", "value": "dew"}},
        ]
    },
]


def load_aurora_rows(db: Any, table: str) -> list[dict[str, Any]]:
    """Phase 0b's 17 rows, with their real custom_metadata, and small made-up vectors."""
    from aws_ai.local_kb.store_rds import _jsonb

    rows = fixture("index-documents.json")["rows"]
    for n, row in enumerate(rows):
        vector = [1.0, n / 4, (n % 3) / 2, 0.5]
        db.execute(
            f"INSERT INTO {table} (id, embedding, chunks, metadata, custom_metadata) "
            "VALUES (%s, %s::vector, %s, %s, %s)",
            (
                row["id"],
                str(vector),
                row["chunks"],
                json.dumps(row["metadata"]),
                _jsonb(row["custom_metadata"]),
            ),
        )
        row["vector"] = vector
    return rows


@pytest.mark.parametrize("node", FILTERS, ids=lambda n: json.dumps(n)[:60])
def test_filters_select_what_they_mean(live: tuple[Any, str], node: dict[str, Any]) -> None:
    db, table = live
    rows = load_aurora_rows(db, table)
    sql, params = to_sql(node, "custom_metadata")
    got = {r[0] for r in db.execute(f"SELECT chunks FROM {table} WHERE ({sql})", params).fetchall()}
    want = {r["chunks"] for r in rows if matches(node, r["custom_metadata"])}
    assert got == want


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = (sum(x * x for x in a) * sum(y * y for y in b)) ** 0.5
    return 1 - dot / norm


def test_semantic_and_hybrid_against_the_local_postgres(live: tuple[Any, str]) -> None:
    db, table = live
    rows = load_aurora_rows(db, table)
    _, services, _, _, _, _ = phase0b(lambda _database: db)
    kb = {
        "storageConfiguration": cli.default_rds_storage_configuration(),
        "knowledgeBaseConfiguration": cli.default_knowledge_base_configuration(DIMS),
    }
    kb["storageConfiguration"]["rdsConfiguration"]["tableName"] = table
    store = store_for(kb, services)
    q = [0.2, 1.0, 0.0, 0.3]
    by_distance = sorted(rows, key=lambda r: cosine_distance(r["vector"], q))

    hits = store.semantic(q, 5, None)  # best first; Retrieve reverses
    assert [h.result["content"]["text"] for h in hits] == [r["chunks"] for r in by_distance[:5]]
    for hit, row in zip(hits, by_distance, strict=False):
        assert hit.result["score"] == pytest.approx(cosine_distance(row["vector"], q), abs=1e-6)

    # One chunk has the code; it ranks first lexically, so its fused score is
    # 30.5/61 + 30.5/(60 + its vector rank), and every other row scores 30.5/(60 + rank).
    hits = store.hybrid("ZX-4471", q, 5, None)
    rank = {r["chunks"]: n for n, r in enumerate(by_distance, 1)}
    coded = next(r["chunks"] for r in rows if "ZX-4471" in r["chunks"])
    assert hits[0].result["content"]["text"] == coded
    for hit in hits:
        text = hit.result["content"]["text"]
        vector_part = 30.5 / (60 + rank[text]) if rank[text] <= 10 else 0.0
        expected = vector_part + (30.5 / 61 if text == coded else 0.0)
        assert hit.result["score"] == pytest.approx(expected, abs=1e-9)
