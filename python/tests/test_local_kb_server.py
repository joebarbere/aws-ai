"""Offline tests for the local knowledge base's HTTP server: Bedrock's routes, codes and errors."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

import botocore
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aws_ai.local_kb import cli  # noqa: E402
from aws_ai.local_kb.ingest import Services  # noqa: E402
from aws_ai.local_kb.server import create_app  # noqa: E402

IMPLEMENTED = {
    "bedrock-agent": [
        "CreateKnowledgeBase",
        "GetKnowledgeBase",
        "ListKnowledgeBases",
        "DeleteKnowledgeBase",
        "CreateDataSource",
        "GetDataSource",
        "ListDataSources",
        "DeleteDataSource",
        "StartIngestionJob",
        "GetIngestionJob",
        "ListIngestionJobs",
        "StopIngestionJob",
    ],
    "bedrock-agent-runtime": ["Retrieve", "RetrieveAndGenerate"],
}


def operations(service: str) -> dict[str, Any]:
    root = Path(botocore.__file__).parent / "data" / service
    model = sorted(root.glob("*/service-2.json*"))[-1]
    raw = gzip.open(model).read() if model.suffix == ".gz" else model.read_bytes()
    ops: dict[str, Any] = json.loads(raw)["operations"]
    return ops


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    services = Services(None, None, lambda _a, _d: lambda t: [], len)  # type: ignore[arg-type]
    return TestClient(create_app(services, tmp_path))


@pytest.mark.parametrize(
    ("service", "operation"),
    [(s, o) for s, ops in IMPLEMENTED.items() for o in ops],
)
def test_every_operation_is_routed_as_botocore_expects(service: str, operation: str) -> None:
    """Method and path from botocore's own model: a missing or misplaced route would 404/405."""
    http = operations(service)[operation]["http"]
    path = re.sub(r"\{\w+\}", "ABCDEFGHIJ", http["requestUri"])
    app = create_app(Services(None, None, None, len), None)  # type: ignore[arg-type]
    routed = {
        (method, re.sub(r"\{\w+\}", "ABCDEFGHIJ", route.path))  # type: ignore[attr-defined]
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert (http["method"], path) in routed


def test_create_answers_with_botocores_status_and_shape(client: TestClient) -> None:
    expected = operations("bedrock-agent")["CreateKnowledgeBase"]["http"]["responseCode"]
    response = client.put(
        "/knowledgebases/",
        json={
            "name": "kb",
            "roleArn": "arn:aws:iam::000000000000:role/local-kb",
            "knowledgeBaseConfiguration": cli.default_knowledge_base_configuration(1024),
            "storageConfiguration": cli.default_storage_configuration("i"),
        },
    )
    assert response.status_code == expected == 202
    kb = response.json()["knowledgeBase"]
    assert kb["status"] == "ACTIVE" and kb["createdAt"].endswith("+00:00")

    listed = client.post("/knowledgebases/").json()["knowledgeBaseSummaries"]
    assert [k["knowledgeBaseId"] for k in listed] == [kb["knowledgeBaseId"]]

    created = client.put(
        f"/knowledgebases/{kb['knowledgeBaseId']}/datasources/",
        json={
            "name": "docs",
            "dataSourceConfiguration": {
                "type": "S3",
                "s3Configuration": {"bucketArn": "arn:aws:s3:::kb-data"},
            },
        },
    )
    assert created.status_code == 200
    ds = created.json()["dataSource"]["dataSourceId"]
    job = client.put(f"/knowledgebases/{kb['knowledgeBaseId']}/datasources/{ds}/ingestionjobs/")
    assert job.status_code == 202 and job.json()["ingestionJob"]["status"] == "STARTING"


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        ("GET", "/knowledgebases/NOPE000000", None, 404, "ResourceNotFoundException"),
        ("PUT", "/knowledgebases/", {"name": "kb"}, 400, "ValidationException"),
        ("POST", "/knowledgebases/NOPE000000/retrieve", {}, 400, "ValidationException"),
        ("POST", "/retrieveAndGenerate", {"input": {"text": "q"}}, 400, "ValidationException"),
    ],
)
def test_errors_use_the_header_botocore_reads(
    client: TestClient, method: str, path: str, body: Any, status: int, code: str
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == status
    assert response.headers["x-amzn-ErrorType"] == code
    assert response.json()["message"]


def test_bad_json_is_a_validation_error_not_a_422(client: TestClient) -> None:
    response = client.put(
        "/knowledgebases/", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.headers["x-amzn-ErrorType"] == "ValidationException"
