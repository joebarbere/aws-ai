"""Offline tests for Retrieve: filter translation and the response shape in local-kb/fixtures/."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aws_ai.local_kb import cli
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.filters import to_query
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.retrieve import HYBRID_PIPELINE, retrieve

FIXTURES = Path(__file__).resolve().parents[2] / "local-kb" / "fixtures"
INDEX = "bedrock-knowledge-base-default-index"


# -------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("bedrock", "opensearch"),
    [
        (
            {"equals": {"key": "department", "value": "ops"}},
            {"term": {"department.keyword": "ops"}},
        ),
        (
            {"equals": {"key": "safety_critical", "value": True}},
            {"term": {"safety_critical": True}},
        ),
        (
            {"equals": {"key": "x-amz-bedrock-kb-document-page-number", "value": 2}},
            {"term": {"x-amz-bedrock-kb-document-page-number": 2}},
        ),
        (
            {"notEquals": {"key": "department", "value": "ops"}},
            {"bool": {"must_not": [{"term": {"department.keyword": "ops"}}]}},
        ),
        ({"greaterThan": {"key": "revision", "value": 2}}, {"range": {"revision": {"gt": 2}}}),
        (
            {"greaterThanOrEquals": {"key": "effective_date", "value": 20261001}},
            {"range": {"effective_date": {"gte": 20261001}}},
        ),
        ({"lessThan": {"key": "revision", "value": 4}}, {"range": {"revision": {"lt": 4}}}),
        (
            {"lessThanOrEquals": {"key": "revision", "value": 4}},
            {"range": {"revision": {"lte": 4}}},
        ),
        (
            {"in": {"key": "department", "value": ["ops", "membership"]}},
            {"terms": {"department.keyword": ["ops", "membership"]}},
        ),
        (
            {"notIn": {"key": "department", "value": ["ops"]}},
            {"bool": {"must_not": [{"terms": {"department.keyword": ["ops"]}}]}},
        ),
        (
            {"startsWith": {"key": "x-amz-bedrock-kb-source-uri", "value": "s3://kb-data/kb/t"}},
            {"prefix": {"x-amz-bedrock-kb-source-uri.keyword": "s3://kb-data/kb/t"}},
        ),
        (
            {"stringContains": {"key": "tags", "value": "eat*"}},
            {"wildcard": {"tags.keyword": {"value": "*eat\\**"}}},
        ),
        (
            {"listContains": {"key": "tags", "value": "heater"}},
            {"term": {"tags.keyword": "heater"}},
        ),
    ],
)
def test_each_operator(bedrock: dict[str, Any], opensearch: dict[str, Any]) -> None:
    assert to_query(bedrock) == opensearch


def test_and_or_nest() -> None:
    eq = {"equals": {"key": "a", "value": "x"}}
    gt = {"greaterThan": {"key": "n", "value": 1}}
    assert to_query({"andAll": [eq, {"orAll": [eq, gt]}]}) == {
        "bool": {
            "filter": [
                {"term": {"a.keyword": "x"}},
                {
                    "bool": {
                        "should": [{"term": {"a.keyword": "x"}}, {"range": {"n": {"gt": 1}}}],
                        "minimum_should_match": 1,
                    }
                },
            ]
        }
    }


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"equals": {"key": "a", "value": 1}, "in": {"key": "b", "value": [1]}},
        {"andAll": [{"equals": {"key": "a", "value": 1}}]},
        {"fuzzy": {"key": "a", "value": 1}},
        {"equals": {"key": "a"}},
        {"greaterThan": {"key": "n", "value": "7"}},
        {"greaterThan": {"key": "n", "value": True}},
        {"startsWith": {"key": "a", "value": 1}},
        {"in": {"key": "a", "value": []}},
    ],
)
def test_malformed_filters_are_validation_errors(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationException):
        to_query(bad)


# ------------------------------------------------------------------- retrieve


class FakeOpenSearch:
    """Answers every search with the stored chunks from fixtures/index-documents.json."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.embedded: list[str] = []  # what the query embedder was asked to embed
        self.hits = [
            {**h, "_score": 0.5 - i / 100}
            for i, h in enumerate(
                json.loads((FIXTURES / "index-documents.json").read_text())["hits"]
            )
        ]

    def request(self, method: str, path: str, body: Any = None, **_: Any) -> Any:
        self.calls.append((method, path, body))
        if method == "PUT":
            return {"acknowledged": True}
        return {"hits": {"hits": self.hits[: body["size"]]}}

    @staticmethod
    def quote(index: str) -> str:
        return index


def setup(
    chunking: dict[str, Any] | None = None,
) -> tuple[ControlPlane, Services, FakeOpenSearch, str]:
    cp = ControlPlane(sqlite3.connect(":memory:"))
    kb = cp.create_knowledge_base(
        name="kb",
        knowledge_base_configuration=cli.default_knowledge_base_configuration(4),
        storage_configuration=cli.default_storage_configuration(INDEX),
    )["knowledgeBaseId"]
    cp.create_data_source(
        knowledge_base_id=kb,
        name="docs",
        data_source_configuration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::b"},
        },
        vector_ingestion_configuration=chunking,
    )
    opensearch = FakeOpenSearch()

    def embed(texts: list[str]) -> list[list[float]]:
        opensearch.embedded.extend(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    services = Services(None, opensearch, lambda _a, _d: embed, len)  # type: ignore[arg-type]
    return cp, services, opensearch, kb


def test_results_have_the_fixture_shape() -> None:
    cp, services, opensearch, kb = setup()
    out = retrieve(cp, services, kb, {"text": "Which fuse does the dew controller take?"})
    real = json.loads((FIXTURES / "retrieve-semantic.json").read_text())
    assert list(out) == ["retrievalResults"]  # no nextToken, as observed
    assert len(out["retrievalResults"]) == 5  # the default numberOfResults
    for mine in out["retrievalResults"]:
        assert list(mine) == list(real["retrievalResults"][0])  # content, location, metadata, score
        assert mine["content"]["type"] == "TEXT"
        assert mine["location"]["type"] == "S3"
        assert "id" not in mine["metadata"] and "AMAZON_BEDROCK_METADATA" not in mine["metadata"]
    by_uri = {r["location"]["s3Location"]["uri"]: r for r in out["retrievalResults"]}
    dew = by_uri["s3://aws-ai-study-data-000000000000/kb/dew-heater-guide.md"]
    fixture_dew = real["retrievalResults"][0]
    assert dew["metadata"] == {
        **fixture_dew["metadata"],
        "x-amz-bedrock-kb-chunk-id": dew["metadata"]["x-amz-bedrock-kb-chunk-id"],
    }
    # The raw question, no instruction prefix: what Bedrock sent Titan in Phase 0.
    assert opensearch.embedded == ["Which fuse does the dew controller take?"]


def test_page_numbers_come_back_as_floats() -> None:
    cp, services, _, kb = setup()
    out = retrieve(
        cp, services, kb, {"text": "q"}, {"vectorSearchConfiguration": {"numberOfResults": 17}}
    )
    pages = [
        r["metadata"]["x-amz-bedrock-kb-document-page-number"]
        for r in out["retrievalResults"]
        if "x-amz-bedrock-kb-document-page-number" in r["metadata"]
    ]
    assert pages == [1.0] and isinstance(pages[0], float)


def test_semantic_puts_the_filter_inside_knn() -> None:
    cp, services, opensearch, kb = setup()
    flt = {"equals": {"key": "department", "value": "operations"}}
    retrieve(
        cp,
        services,
        kb,
        {"text": "q"},
        {"vectorSearchConfiguration": {"numberOfResults": 3, "filter": flt}},
    )
    ((_, path, body),) = opensearch.calls
    assert path == f"/{INDEX}/_search"
    knn = body["query"]["knn"]["bedrock-knowledge-base-default-vector"]
    assert knn["k"] == 3 and knn["filter"] == {"term": {"department.keyword": "operations"}}


def test_hybrid_uses_the_pipeline_and_a_wider_pool() -> None:
    cp, services, opensearch, kb = setup()
    out = retrieve(
        cp,
        services,
        kb,
        {"text": "ZX-4471"},
        {"vectorSearchConfiguration": {"overrideSearchType": "HYBRID"}},
    )
    (put, _, _), (_, path, body) = opensearch.calls
    assert put == "PUT" and path.endswith(f"?search_pipeline={HYBRID_PIPELINE}")
    lexical, vector = body["query"]["hybrid"]["queries"]
    assert lexical == {"match": {"AMAZON_BEDROCK_TEXT_CHUNK": "ZX-4471"}}
    assert body["size"] == 50 and vector["knn"]["bedrock-knowledge-base-default-vector"]["k"] == 50
    assert len(out["retrievalResults"]) == 5


@pytest.mark.parametrize(
    "configuration",
    [
        {"numberOfResults": 0},
        {"numberOfResults": 101},
        {"overrideSearchType": "KEYWORD"},
        {"rerankingConfiguration": {}},
        {"implicitFilterConfiguration": {}},
    ],
)
def test_bad_configurations_are_refused(configuration: dict[str, Any]) -> None:
    cp, services, _, kb = setup()
    with pytest.raises(ValidationException):
        retrieve(cp, services, kb, {"text": "q"}, {"vectorSearchConfiguration": configuration})


def test_hierarchical_answers_with_parents_once_each() -> None:
    cp, services, opensearch, kb = setup(
        {
            "chunkingConfiguration": {
                "chunkingStrategy": "HIERARCHICAL",
                "hierarchicalChunkingConfiguration": {
                    "levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}],
                    "overlapTokens": 60,
                },
            }
        }
    )
    for i, hit in enumerate(opensearch.hits):
        parent = "PARENT-A" if i < 3 else f"PARENT-{i}"
        hit["_source"] = {
            **hit["_source"],
            "AMAZON_BEDROCK_METADATA": json.dumps({"source": "s3://b/x", "parentText": parent}),
        }
    out = retrieve(
        cp, services, kb, {"text": "q"}, {"vectorSearchConfiguration": {"numberOfResults": 3}}
    )
    assert [r["content"]["text"] for r in out["retrievalResults"]] == [
        "PARENT-A",
        "PARENT-3",
        "PARENT-4",
    ]
