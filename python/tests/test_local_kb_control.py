"""Offline tests for the local knowledge base's control plane and CLI.

Output shapes are checked against local-kb/fixtures/, what a real Bedrock knowledge base
returned in Phase 0.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aws_ai.local_kb import cli, ids
from aws_ai.local_kb.control import ControlPlane, DocumentRecord
from aws_ai.local_kb.errors import (
    ConflictException,
    ResourceNotFoundException,
    ValidationException,
)

FIXTURES = Path(__file__).resolve().parents[2] / "local-kb" / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


class Clock:
    """Ticks a second per call, so ordering by start time is deterministic."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def cp() -> ControlPlane:
    return ControlPlane(sqlite3.connect(":memory:"), clock=Clock())


def make_kb(cp: ControlPlane) -> dict[str, Any]:
    return cp.create_knowledge_base(
        name="study-kb",
        knowledge_base_configuration=cli.default_knowledge_base_configuration(1024),
        storage_configuration=cli.default_storage_configuration(
            "bedrock-knowledge-base-default-index"
        ),
    )


def make_ds(cp: ControlPlane, kb_id: str, **kwargs: Any) -> dict[str, Any]:
    return cp.create_data_source(
        knowledge_base_id=kb_id,
        name="aws-ai-study-s3",
        data_source_configuration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::kb-data", "inclusionPrefixes": ["kb/"]},
        },
        **kwargs,
    )


# --------------------------------------------------------------------- ids


def test_resource_ids_look_like_bedrocks() -> None:
    for real in ("JQQRYKM3SZ", "9WZXXZH6ZH", "SMH9OVRN1I"):
        assert re.fullmatch(r"[0-9A-Z]{10}", real)
    assert all(re.fullmatch(r"[0-9A-Z]{10}", ids.resource_id()) for _ in range(50))


def test_chunk_ids_match_the_fixture_format() -> None:
    real = [h["_id"] for h in fixture("index-documents.json")["hits"]]
    pattern = re.compile(r"1%3A0%3A[A-Za-z0-9_-]{20}")
    assert all(pattern.fullmatch(r) for r in real)
    generated = ids.chunk_id()
    assert pattern.fullmatch(generated)
    assert urllib.parse.unquote(generated).startswith("1:0:")


# ------------------------------------------------------------ knowledge bases


def test_knowledge_base_has_the_fixture_shape(cp: ControlPlane) -> None:
    kb = make_kb(cp)
    real = fixture("knowledge-base.json")
    assert set(kb) == set(real)
    assert kb["status"] == "ACTIVE"
    assert kb["knowledgeBaseArn"].endswith(f":knowledge-base/{kb['knowledgeBaseId']}")
    assert isinstance(kb["createdAt"], datetime)
    assert cp.get_knowledge_base(kb["knowledgeBaseId"]) == kb


def test_storage_other_than_a_managed_cluster_is_refused(cp: ControlPlane) -> None:
    with pytest.raises(ValidationException):
        cp.create_knowledge_base(
            name="kb",
            knowledge_base_configuration=cli.default_knowledge_base_configuration(1024),
            storage_configuration=fixture("knowledge-base.json")["storageConfiguration"],
        )


@pytest.mark.parametrize("name", ["", "has space", "double--dash", "-leading", "x" * 101])
def test_bad_names_are_refused(cp: ControlPlane, name: str) -> None:
    with pytest.raises(ValidationException):
        cp.create_knowledge_base(
            name=name,
            knowledge_base_configuration=cli.default_knowledge_base_configuration(1024),
            storage_configuration=cli.default_storage_configuration("i"),
        )


def test_missing_knowledge_base_is_not_found(cp: ControlPlane) -> None:
    with pytest.raises(ResourceNotFoundException):
        cp.get_knowledge_base("NOPE000000")


# --------------------------------------------------------------- data sources


def test_data_source_has_the_fixture_shape(cp: ControlPlane) -> None:
    kb = make_kb(cp)
    ds = make_ds(cp, kb["knowledgeBaseId"])
    real = fixture("data-source.json")
    # Default chunking: Bedrock leaves vectorIngestionConfiguration out entirely.
    assert set(ds) == set(real)
    assert ds["dataDeletionPolicy"] == real["dataDeletionPolicy"] == "DELETE"
    assert ds["status"] == "AVAILABLE"


def test_explicit_chunking_is_kept(cp: ControlPlane) -> None:
    kb = make_kb(cp)
    chunking = {"chunkingConfiguration": {"chunkingStrategy": "NONE"}}
    ds = make_ds(cp, kb["knowledgeBaseId"], vector_ingestion_configuration=chunking)
    assert ds["vectorIngestionConfiguration"] == chunking


@pytest.mark.parametrize(
    "chunking",
    [
        {"chunkingStrategy": "FIXED_SIZE", "fixedSizeChunkingConfiguration": {"maxTokens": 300}},
        {
            "chunkingStrategy": "FIXED_SIZE",
            "fixedSizeChunkingConfiguration": {"maxTokens": 9000, "overlapPercentage": 20},
        },
        {
            "chunkingStrategy": "HIERARCHICAL",
            "hierarchicalChunkingConfiguration": {
                "levelConfigurations": [{"maxTokens": 1500}],
                "overlapTokens": 60,
            },
        },
        {
            "chunkingStrategy": "SEMANTIC",
            "semanticChunkingConfiguration": {
                "maxTokens": 300,
                "bufferSize": 0,
                "breakpointPercentileThreshold": 40,
            },
        },
        {"chunkingStrategy": "PARAGRAPH"},
    ],
)
def test_chunking_outside_the_documented_ranges_is_refused(
    cp: ControlPlane, chunking: dict[str, Any]
) -> None:
    kb = make_kb(cp)
    with pytest.raises(ValidationException):
        make_ds(
            cp,
            kb["knowledgeBaseId"],
            vector_ingestion_configuration={"chunkingConfiguration": chunking},
        )


def test_at_most_one_inclusion_prefix(cp: ControlPlane) -> None:
    kb = make_kb(cp)
    with pytest.raises(ValidationException):
        cp.create_data_source(
            knowledge_base_id=kb["knowledgeBaseId"],
            name="two",
            data_source_configuration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": "arn:aws:s3:::b",
                    "inclusionPrefixes": ["a/", "b/"],
                },
            },
        )


@pytest.mark.parametrize(("policy", "purged"), [("DELETE", True), ("RETAIN", False)])
def test_deletion_policy_decides_whether_vectors_go(
    cp: ControlPlane, policy: str, purged: bool
) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    ds_id = make_ds(cp, kb_id, data_deletion_policy=policy)["dataSourceId"]
    cp.put_document(ds_id, DocumentRecord("s3://kb-data/kb/a.txt", "e1", None, ("c1",)))
    calls: list[str] = []

    result = cp.delete_data_source(kb_id, ds_id, purge=lambda ds: calls.append(ds["dataSourceId"]))

    assert result == {"knowledgeBaseId": kb_id, "dataSourceId": ds_id, "status": "DELETING"}
    assert calls == ([ds_id] if purged else [])
    assert cp.documents(ds_id) == {}
    with pytest.raises(ResourceNotFoundException):
        cp.get_data_source(kb_id, ds_id)


def test_deleting_a_knowledge_base_takes_its_data_sources(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    make_ds(cp, kb_id)
    purged: list[str] = []
    cp.delete_knowledge_base(kb_id, purge=lambda ds: purged.append(ds["dataSourceId"]))
    assert len(purged) == 1
    assert cp.list_knowledge_bases() == []


# ------------------------------------------------------------- ingestion jobs


def test_ingestion_job_has_the_fixture_shape(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    ds_id = make_ds(cp, kb_id)["dataSourceId"]
    job = cp.start_ingestion_job(kb_id, ds_id)
    real = fixture("ingestion-job-1.json")
    assert set(job) == set(real)
    assert set(job["statistics"]) == set(real["statistics"])
    assert job["status"] == "STARTING"
    assert all(v == 0 for v in job["statistics"].values())


def test_one_running_job_per_knowledge_base(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    first = make_ds(cp, kb_id)["dataSourceId"]
    second = cp.create_data_source(
        knowledge_base_id=kb_id,
        name="second",
        data_source_configuration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::b"},
        },
    )["dataSourceId"]
    job = cp.start_ingestion_job(kb_id, first)
    with pytest.raises(ConflictException):
        cp.start_ingestion_job(kb_id, second)
    with pytest.raises(ConflictException):
        cp.delete_data_source(kb_id, first)

    cp.update_ingestion_job(job["ingestionJobId"], status="COMPLETE")
    assert cp.start_ingestion_job(kb_id, second)["status"] == "STARTING"


def test_worker_updates_merge_statistics(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    ds_id = make_ds(cp, kb_id)["dataSourceId"]
    job_id = cp.start_ingestion_job(kb_id, ds_id)["ingestionJobId"]

    cp.update_ingestion_job(
        job_id, status="IN_PROGRESS", statistics={"numberOfDocumentsScanned": 6}
    )
    done = cp.update_ingestion_job(
        job_id,
        status="COMPLETE",
        statistics={"numberOfNewDocumentsIndexed": 6},
        failure_reasons=["kb/huge.pdf: larger than 50 MB"],
    )

    assert done["statistics"]["numberOfDocumentsScanned"] == 6
    assert done["statistics"]["numberOfNewDocumentsIndexed"] == 6
    assert done["failureReasons"] == ["kb/huge.pdf: larger than 50 MB"]
    assert done["updatedAt"] > done["startedAt"]


def test_jobs_list_newest_first_and_filter_by_status(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    ds_id = make_ds(cp, kb_id)["dataSourceId"]
    first = cp.start_ingestion_job(kb_id, ds_id)["ingestionJobId"]
    cp.update_ingestion_job(first, status="COMPLETE")
    second = cp.start_ingestion_job(kb_id, ds_id)["ingestionJobId"]

    listed = cp.list_ingestion_jobs(kb_id, ds_id)
    assert [j["ingestionJobId"] for j in listed] == [second, first]
    complete = cp.list_ingestion_jobs(kb_id, ds_id, statuses=["COMPLETE"])
    assert [j["ingestionJobId"] for j in complete] == [first]


def test_only_running_jobs_can_be_stopped(cp: ControlPlane) -> None:
    kb_id = make_kb(cp)["knowledgeBaseId"]
    ds_id = make_ds(cp, kb_id)["dataSourceId"]
    job_id = cp.start_ingestion_job(kb_id, ds_id)["ingestionJobId"]
    assert cp.stop_ingestion_job(kb_id, ds_id, job_id)["status"] == "STOPPING"
    cp.update_ingestion_job(job_id, status="STOPPED")
    with pytest.raises(ConflictException):
        cp.stop_ingestion_job(kb_id, ds_id, job_id)


# --------------------------------------------------------------- the ledger


def test_document_ledger_round_trips(cp: ControlPlane) -> None:
    record = DocumentRecord("s3://kb-data/kb/a.md", "etag-1", "meta-1", ("c1", "c2"))
    cp.put_document("DS00000001", record)
    cp.put_document("DS00000001", DocumentRecord("s3://kb-data/kb/a.md", "etag-2", None, ("c3",)))
    assert cp.documents("DS00000001") == {
        "s3://kb-data/kb/a.md": DocumentRecord("s3://kb-data/kb/a.md", "etag-2", None, ("c3",))
    }
    cp.remove_document("DS00000001", "s3://kb-data/kb/a.md")
    assert cp.documents("DS00000001") == {}


# ---------------------------------------------------------------------- CLI


def run_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    code = cli.main(["--state-dir", str(tmp_path), *argv])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out else None), err


def test_cli_walks_the_aws_cli_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run_cli(tmp_path, capsys, "create-knowledge-base", "--name", "study-kb")
    kb_id = out["knowledgeBase"]["knowledgeBaseId"]
    _, out, _ = run_cli(
        tmp_path, capsys, "create-data-source", "--knowledge-base-id", kb_id, "--name", "docs"
    )
    ds_id = out["dataSource"]["dataSourceId"]
    assert out["dataSource"]["dataSourceConfiguration"]["s3Configuration"] == {
        "bucketArn": "arn:aws:s3:::kb-data",
        "inclusionPrefixes": ["kb/"],
    }
    ids_ = ("--knowledge-base-id", kb_id, "--data-source-id", ds_id)
    _, out, _ = run_cli(tmp_path, capsys, "start-ingestion-job", *ids_)
    assert out["ingestionJob"]["status"] == "STARTING"
    # Timestamps print as ISO 8601, as the AWS CLI prints them.
    datetime.fromisoformat(out["ingestionJob"]["startedAt"])

    code, out, err = run_cli(tmp_path, capsys, "start-ingestion-job", *ids_)
    assert code == 254
    assert "(ConflictException) when calling the StartIngestionJob operation" in err
