"""One contract, both stores: what ingestion and filtering must do whatever the vector store.

Each store's own layout and SQL or query DSL is tested beside it (test_local_kb_ingest,
test_local_kb_retrieve, test_local_kb_store_rds); this file runs the same Phase 0 sequence
through an OpenSearch knowledge base and an RDS one, and asks the same questions of both.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_local_kb_ingest import DOCS, phase0_setup
from test_local_kb_store_rds import FakePostgres, phase0b

from aws_ai.local_kb import index
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.ingest import Services, has_changes, run_job
from aws_ai.local_kb.store import store_for

FIXTURES = Path(__file__).resolve().parents[2] / "local-kb" / "fixtures"
INDEX = "bedrock-knowledge-base-default-index"


@dataclass
class Setup:
    cp: ControlPlane
    services: Services
    s3: Any
    kb_id: str
    ds_id: str
    calls: list[int]
    # chunk id -> (text, source uri, everything filterable), whatever the store's layout
    chunks: Callable[[], dict[str, tuple[str, str, dict[str, Any]]]]


def opensearch() -> Setup:
    cp, services, s3, fake, kb_id, ds_id, calls = phase0_setup()

    def chunks() -> dict[str, tuple[str, str, dict[str, Any]]]:
        return {
            i: (d["AMAZON_BEDROCK_TEXT_CHUNK"], d[index.SOURCE_URI], d)
            for i, d in fake.indexes.get(INDEX, {}).items()
        }

    return Setup(cp, services, s3, kb_id, ds_id, calls, chunks)


def rds() -> Setup:
    db = FakePostgres()
    cp, services, s3, kb_id, ds_id, calls = phase0b(lambda _database: db)

    def chunks() -> dict[str, tuple[str, str, dict[str, Any]]]:
        return {i: (r[2], r[4][index.SOURCE_URI], r[4]) for i, r in db.rows.items()}

    return Setup(cp, services, s3, kb_id, ds_id, calls, chunks)


@pytest.fixture(params=[opensearch, rds], ids=["opensearch", "rds"])
def store(request: pytest.FixtureRequest) -> Setup:
    setup: Setup = request.param()
    return setup


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_both_phase0_syncs_count_what_bedrock_counted(store: Setup, tmp_path: Path) -> None:
    """Phase 0 (OpenSearch Serverless) and Phase 0b (Aurora) counted identically; so must
    both local stores: a first sync, then a delete, an edit and a sidecar-only change."""
    assert (
        fixture("ingestion-job-1.json")["statistics"]
        == fixture("rds/ingestion-job-1.json")["statistics"]
    )
    store.s3.load(DOCS)
    first = run_job(
        store.cp, store.cp.start_ingestion_job(store.kb_id, store.ds_id), store.services
    )
    assert first["statistics"] == fixture("ingestion-job-1.json")["statistics"]
    before = store.chunks()
    dew = {i for i, (_, uri, _) in before.items() if uri.endswith("dew-heater-guide.md")}
    night = {i for i, (_, uri, _) in before.items() if uri.endswith("night-log-template.txt")}

    mutated = tmp_path / "kb"
    shutil.copytree(DOCS, mutated)
    (mutated / "winter-closure-2025.txt").unlink()
    with (mutated / "night-log-template.txt").open("a") as f:
        f.write("\nFrom November 2026, also record the moon phase.\n")
    sidecar = mutated / "dew-heater-guide.md.metadata.json"
    meta = json.loads(sidecar.read_text())
    meta["metadataAttributes"]["revision"]["value"]["numberValue"] = 4
    sidecar.write_text(json.dumps(meta))
    store.s3.load(mutated)
    store.calls[0] = 0

    second = run_job(
        store.cp, store.cp.start_ingestion_job(store.kb_id, store.ds_id), store.services
    )
    assert second["statistics"] == fixture("ingestion-job-2.json")["statistics"]
    assert store.calls[0] == 1  # only the edited document is embedded again
    after = store.chunks()
    # Sidecar-only: same chunk ids and text, new attribute value, as a float.
    for i in dew:
        assert after[i][0] == before[i][0]
        assert after[i][2]["revision"] == 4.0
    # Edited: new chunks under new ids, the old ones gone.
    assert not night & set(after)
    assert any(uri.endswith("night-log-template.txt") for _, uri, _ in after.values())
    # Deleted: nothing left from the removed document.
    assert not any(uri.endswith("winter-closure-2025.txt") for _, uri, _ in after.values())
    assert not has_changes(store.cp, store.s3, store.cp.get_data_source(store.kb_id, store.ds_id))


def test_deleting_the_data_source_empties_the_store(store: Setup) -> None:
    store.s3.load(DOCS)
    run_job(store.cp, store.cp.start_ingestion_job(store.kb_id, store.ds_id), store.services)
    assert store.chunks()
    store_for(store.cp.get_knowledge_base(store.kb_id), store.services).delete_data_source(
        store.ds_id
    )
    assert store.chunks() == {}


GOOD_FILTERS = [
    {"equals": {"key": "department", "value": "operations"}},
    {"notIn": {"key": "department", "value": ["a", "b"]}},
    {"stringContains": {"key": "tags", "value": "eat"}},
    {"lessThanOrEquals": {"key": "revision", "value": 3}},
    {
        "orAll": [
            {"startsWith": {"key": "x-amz-bedrock-kb-source-uri", "value": "s3://kb-data/"}},
            {"listContains": {"key": "tags", "value": "dew"}},
        ]
    },
]
BAD_FILTERS = [
    {},
    {"equals": {"key": "a"}},
    {"greaterThan": {"key": "a", "value": "high"}},
    {"in": {"key": "a", "value": []}},
    {"startsWith": {"key": "a", "value": 3}},
    {"andAll": [{"equals": {"key": "a", "value": 1}}]},
    {"equals": {"key": "a", "value": 1}, "notEquals": {"key": "a", "value": 2}},
    {"nearly": {"key": "a", "value": 1}},
]


@pytest.mark.parametrize("node", GOOD_FILTERS, ids=lambda n: next(iter(n)))
def test_both_stores_accept_the_same_filters(store: Setup, node: dict[str, Any]) -> None:
    store_for(store.cp.get_knowledge_base(store.kb_id), store.services).compile_filter(node)


@pytest.mark.parametrize("node", BAD_FILTERS, ids=lambda n: json.dumps(n)[:40])
def test_both_stores_refuse_the_same_filters(store: Setup, node: dict[str, Any]) -> None:
    with pytest.raises(ValidationException):
        store_for(store.cp.get_knowledge_base(store.kb_id), store.services).compile_filter(node)
