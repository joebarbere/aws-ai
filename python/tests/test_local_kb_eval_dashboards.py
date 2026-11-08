"""Offline tests for the evaluation harness and the Dashboards saved objects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aws_ai.local_kb import evaluate as ev
from aws_ai.local_kb.ingest import Services

REPO = Path(__file__).resolve().parents[2]


def test_keywords_need_every_group_and_any_alternative() -> None:
    groups = [["red button"], ["north pier", "north-pier"]]
    assert ev.keywords_match("The RED BUTTON on the North Pier.", groups)
    assert not ev.keywords_match("The red button.", groups)


def test_dataset_rows_point_at_real_sample_documents() -> None:
    rows = ev.load(REPO / "local-kb" / "eval" / "qa.jsonl")
    sample = {p.name for p in (REPO / "local-kb" / "sample-docs" / "kb").iterdir()}
    assert len(rows) == 20
    for row in rows:
        assert row["source"] in sample
        assert row["keywords"] and all(isinstance(g, list) and g for g in row["keywords"])
        assert row["reference"]


def test_evaluate_scores_each_question(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_retrieve(*_: Any, **__: Any) -> dict[str, Any]:
        uri = "s3://kb-data/kb/tidewater-handbook.pdf"
        return {"retrievalResults": [{"location": {"s3Location": {"uri": uri}}}]}

    def fake_rag(*_: Any, **__: Any) -> dict[str, Any]:
        return {"output": {"text": "Students pay 30 dollars."}}

    monkeypatch.setattr(ev, "retrieve", fake_retrieve)
    monkeypatch.setattr(ev, "retrieve_and_generate", fake_rag)
    judged: list[str] = []

    def judge(system: str, _user: str, inference: dict[str, Any]) -> str:
        judged.append(system)
        assert inference["temperature"] == 0.0
        return "CORRECT\nSame fact."

    services = Services(None, None, None, len, judge)  # type: ignore[arg-type]
    rows = [
        {
            "question": "Student price?",
            "source": "tidewater-handbook.pdf",
            "keywords": [["30"]],
            "reference": "30 dollars.",
        },
        {
            "question": "Strap port?",
            "source": "dew-heater-guide.md",
            "keywords": [["dh-2"]],
            "reference": "DH-2.",
        },
    ]
    report = ev.summary(ev.evaluate(None, services, "KB", rows), "test")  # type: ignore[arg-type]
    assert [o["hit"] for o in report["outcomes"]] == [True, False]
    assert [o["keywords"] for o in report["outcomes"]] == [True, False]
    assert report["retrieval_hit_rate"] == 0.5 and report["judge_accuracy"] == 1.0
    assert "Reference answer: 30 dollars." in judged[0]
    assert "retrieved: tidewater-handbook.pdf" in ev.table(report)


def saved_objects() -> list[dict[str, Any]]:
    path = REPO / "local-kb" / "dashboards" / "bedrock-kb.ndjson"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_saved_objects_reference_the_index_pattern() -> None:
    objects = saved_objects()
    (pattern,) = [o for o in objects if o["type"] == "index-pattern"]
    assert pattern["attributes"]["title"] == "bedrock-knowledge-base-default-index"
    hidden = json.loads(pattern["attributes"]["sourceFilters"])
    assert hidden == [{"value": "bedrock-knowledge-base-default-vector"}]
    for obj in objects:
        for ref in obj["references"]:
            assert ref["id"] == pattern["id"]


def test_saved_search_columns_are_fields_bedrock_writes() -> None:
    """Every column is a field in a real Bedrock chunk (fixtures/index-documents.json)."""
    hit = json.loads((REPO / "local-kb" / "fixtures" / "index-documents.json").read_text())["hits"][
        0
    ]
    real = set(hit["_source"]) | {"_id", "x-amz-bedrock-kb-document-page-number"}
    (search,) = [o for o in saved_objects() if o["type"] == "search"]
    assert set(search["attributes"]["columns"]) <= real
