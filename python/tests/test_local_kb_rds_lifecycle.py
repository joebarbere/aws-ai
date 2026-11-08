"""Stock boto3, end to end, against an RDS knowledge base served by ``local-kb serve``.

The PGVECTOR_PLAN.md P5 gate. It needs the whole local stack — RustFS with the sample documents
in ``s3://kb-data/kb/`` (``make local-sample-docs``), the local Postgres, and LM Studio's
``kb-embed`` and ``kb-llm`` — and skips when any of it is missing. The server runs in-process on
a free port, with a throwaway state directory and a throwaway table, so nothing it does touches
an existing knowledge base.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aws_ai.local_kb import cli, config
from aws_ai.local_kb.store_rds import table_sql


def stack() -> tuple[Any, Any]:
    """The live services and a bedrock_user connection, or a skip naming what is missing."""
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("uvicorn")
    from aws_ai.local_kb.services import local_services

    env = config.load_env()
    if "BEDROCK_PG_PASSWORD" not in env:
        pytest.skip("no local-kb/.env with BEDROCK_PG_PASSWORD (make local-up)")
    services = local_services(env)
    try:
        db = services.postgres(config.RDS_DATABASE)  # type: ignore[misc]
    except psycopg.OperationalError:
        pytest.skip("the local Postgres is not running (make local-up)")
    try:
        keys = services.s3.list_objects_v2(Bucket="kb-data", Prefix="kb/").get("KeyCount", 0)
        with urllib.request.urlopen(f"{config.LM_STUDIO_ENDPOINT}/models", timeout=2) as r:  # noqa: S310
            models = {m["id"] for m in json.loads(r.read())["data"]}
    except Exception as exc:  # noqa: BLE001 - any failure here means "stack not up"
        pytest.skip(f"RustFS or LM Studio is not reachable: {exc}")
    if keys == 0 or not {"kb-embed", "kb-llm"} <= models:
        pytest.skip(
            "needs the sample docs in kb-data (make local-sample-docs) and make local-models"
        )
    return services, db


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Any, Any, Any, str]]:
    import uvicorn

    from aws_ai.local_kb.server import create_app, start_worker

    services, db = stack()
    table = f"bedrock_integration.lifecycle_{uuid.uuid4().hex[:12]}"
    for sql in table_sql(table, 1024):
        db.execute(sql)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(services, tmp_path), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    start_worker(services, tmp_path, False, lambda _line: None)
    while not server.started:
        time.sleep(0.05)

    import boto3

    kw = {
        "endpoint_url": f"http://127.0.0.1:{port}",
        "region_name": config.REGION,
        "aws_access_key_id": "x",
        "aws_secret_access_key": "x",
    }
    try:
        yield (
            boto3.client("bedrock-agent", **kw),
            boto3.client("bedrock-agent-runtime", **kw),
            db,
            table,
        )
    finally:
        server.should_exit = True
        db.execute(f"DROP TABLE IF EXISTS {table}")
        db.close()


def test_stock_boto3_lifecycle_on_an_rds_knowledge_base(served: tuple[Any, Any, Any, str]) -> None:
    agent, runtime, db, table = served
    storage = cli.default_rds_storage_configuration()
    storage["rdsConfiguration"]["tableName"] = table

    kb = agent.create_knowledge_base(
        name="lifecycle",
        roleArn=f"arn:aws:iam::{config.ACCOUNT_ID}:role/local-kb",
        knowledgeBaseConfiguration=cli.default_knowledge_base_configuration(1024),
        storageConfiguration=storage,
    )["knowledgeBase"]
    kb_id = kb["knowledgeBaseId"]
    assert (
        agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["storageConfiguration"]
        == storage
    )
    ds_id = agent.create_data_source(
        knowledgeBaseId=kb_id,
        name="docs",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::kb-data", "inclusionPrefixes": ["kb/"]},
        },
    )["dataSource"]["dataSourceId"]

    job_id = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"][
        "ingestionJobId"
    ]
    deadline = time.monotonic() + 180
    while True:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        if job["status"] in ("COMPLETE", "FAILED") or time.monotonic() > deadline:
            break
        time.sleep(1)
    assert job["status"] == "COMPLETE", job.get("failureReasons")
    stats = job["statistics"]
    assert stats["numberOfNewDocumentsIndexed"] == stats["numberOfDocumentsScanned"] > 0
    rows = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert rows > 0

    semantic = runtime.retrieve(
        knowledgeBaseId=kb_id, retrievalQuery={"text": "Which fuse does the dew controller take?"}
    )["retrievalResults"]
    distances = [r["score"] for r in semantic]
    assert len(semantic) == 5 and distances == sorted(distances, reverse=True)  # worst first
    assert all("x-amz-bedrock-kb-chunk-id" not in r["metadata"] for r in semantic)

    hybrid = runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": "ZX-4471"},
        retrievalConfiguration={"vectorSearchConfiguration": {"overrideSearchType": "HYBRID"}},
    )["retrievalResults"]
    assert hybrid[0]["location"]["s3Location"]["uri"].endswith("dew-heater-guide.md")

    filtered = runtime.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": "What should I do at the start of a session?"},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "filter": {"equals": {"key": "department", "value": "operations"}}
            }
        },
    )["retrievalResults"]
    assert filtered and all(r["metadata"]["department"] == "operations" for r in filtered)

    with pytest.raises(runtime.exceptions.ValidationException):  # the server's check, not boto3's
        runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": "q"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "filter": {"greaterThan": {"key": "revision", "value": "high"}}
                }
            },
        )

    answer = runtime.retrieve_and_generate(
        input={"text": "What part number is the replacement heater strap?"},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": f"arn:aws:bedrock:{config.REGION}::foundation-model/kb-llm",
            },
        },
    )
    assert "ZX-4471" in answer["output"]["text"]

    deleted = agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    assert deleted["status"] == "DELETING"
    assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # policy DELETE
    agent.delete_knowledge_base(knowledgeBaseId=kb_id)
    with pytest.raises(agent.exceptions.ResourceNotFoundException):
        agent.get_knowledge_base(knowledgeBaseId=kb_id)
