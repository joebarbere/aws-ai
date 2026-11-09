"""local-kb mcp: the knowledge base as MCP tools, over fake bedrock-agent clients."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

pytest.importorskip("mcp")

from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402
from mcp import Client  # noqa: E402

from aws_ai.local_kb import index  # noqa: E402
from aws_ai.local_kb.mcp_server import Bedrock, create_server  # noqa: E402

KBS = {"OSKB": "OPENSEARCH_SERVERLESS", "PGKB": "RDS"}


class FakeAgent:
    def __init__(self, kbs: dict[str, str]) -> None:
        self.kbs = kbs
        self.gets = 0

    def get_paginator(self, name: str) -> Any:
        assert name == "list_knowledge_bases"
        summaries = [
            {"knowledgeBaseId": i, "name": i.lower(), "description": "d", "status": "ACTIVE"}
            for i in self.kbs
        ]

        class Pages:
            def paginate(self) -> list[dict[str, Any]]:
                return [{"knowledgeBaseSummaries": summaries}]

        return Pages()

    def get_knowledge_base(self, knowledgeBaseId: str) -> dict[str, Any]:  # noqa: N803
        self.gets += 1
        return {"knowledgeBase": {"storageConfiguration": {"type": self.kbs[knowledgeBaseId]}}}


def hit(text: str, score: float, uri: str, **metadata: Any) -> dict[str, Any]:
    return {
        "content": {"text": text, "type": "TEXT"},
        "location": {"type": "S3", "s3Location": {"uri": uri}},
        "metadata": {index.SOURCE_URI: uri, index.CHUNK_ID: "c", **metadata},
        "score": score,
    }


class FakeRuntime:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"retrievalResults": self.hits}

    def retrieve_and_generate(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        refs = [{"location": h["location"], "metadata": h["metadata"]} for h in self.hits]
        return {
            "output": {"text": "The answer."},
            "citations": [{"retrievedReferences": refs}, {"retrievedReferences": refs[:1]}],
            "sessionId": "s",
        }


# As Bedrock returns them from RDS under SEMANTIC: worst-first, scored by cosine distance.
WORST_FIRST = [
    hit("far", 0.61, "s3://kb-data/kb/far.md"),
    hit("near", 0.12, "s3://kb-data/kb/near.pdf", **{index.PAGE_NUMBER: 3.0, "year": 2024.0}),
    hit("mid", 0.35, "s3://kb-data/kb/mid.md"),
]


def bedrock(hits: list[dict[str, Any]] = WORST_FIRST, **kw: Any) -> Bedrock:
    return Bedrock(FakeAgent(dict(KBS)), FakeRuntime(hits), **kw)


def test_rds_semantic_results_come_back_best_first() -> None:
    out = bedrock().search("q", "PGKB", search_type="SEMANTIC")
    assert [r["text"] for r in out["results"]] == ["near", "mid", "far"]
    assert [r["rank"] for r in out["results"]] == [1, 2, 3]
    assert out["score"].startswith("cosine distance")


def test_other_searches_are_ordered_by_score_descending() -> None:
    b = bedrock()
    for kb, search_type in [("PGKB", "HYBRID"), ("OSKB", "SEMANTIC"), ("OSKB", "HYBRID")]:
        out = b.search("q", kb, search_type=search_type)  # type: ignore[arg-type]
        assert [r["text"] for r in out["results"]] == ["far", "mid", "near"]
        assert out["score"] == "higher is better"


def test_results_carry_source_page_and_user_attributes_only() -> None:
    near = bedrock().search("q", "PGKB", search_type="SEMANTIC")["results"][0]
    assert near == {
        "rank": 1,
        "source": "s3://kb-data/kb/near.pdf",
        "text": "near",
        "score": 0.12,
        "page": 3,
        "attributes": {"year": 2024.0},
    }


def test_search_sends_bedrocks_retrieve_request() -> None:
    b = bedrock()
    b.search("dew heater", "OSKB", 7, "SEMANTIC", {"equals": {"key": "year", "value": 2024}})
    assert b.runtime.calls == [
        {
            "knowledgeBaseId": "OSKB",
            "retrievalQuery": {"text": "dew heater"},
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "numberOfResults": 7,
                    "overrideSearchType": "SEMANTIC",
                    "filter": {"equals": {"key": "year", "value": 2024}},
                }
            },
        }
    ]


def test_the_store_type_is_looked_up_once_and_only_for_semantic() -> None:
    b = bedrock()
    b.search("q", "PGKB", search_type="HYBRID")
    assert b.agent.gets == 0
    for _ in range(3):
        b.search("q", "PGKB", search_type="SEMANTIC")
    assert b.agent.gets == 1


def test_knowledge_base_defaults() -> None:
    assert bedrock(knowledge_base_id="OSKB").kb_id(None) == "OSKB"
    assert bedrock(knowledge_base_id="OSKB").kb_id("PGKB") == "PGKB"
    only = Bedrock(FakeAgent({"ONLY": "RDS"}), FakeRuntime([]))
    assert only.kb_id(None) == "ONLY"
    with pytest.raises(ValueError, match="OSKB, PGKB"):
        bedrock().kb_id(None)


def test_ask_returns_the_answer_and_each_source_once() -> None:
    b = bedrock(model_arn="arn:aws:bedrock:us-east-1::foundation-model/m")
    out = b.ask("why?", "OSKB", 4, "SEMANTIC")
    assert out == {
        "knowledge_base_id": "OSKB",
        "answer": "The answer.",
        "sources": ["s3://kb-data/kb/far.md", "s3://kb-data/kb/near.pdf", "s3://kb-data/kb/mid.md"],
    }
    config = b.runtime.calls[0]["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]
    assert config["modelArn"] == "arn:aws:bedrock:us-east-1::foundation-model/m"
    assert config["retrievalConfiguration"]["vectorSearchConfiguration"] == {
        "numberOfResults": 4,
        "overrideSearchType": "SEMANTIC",
    }


def call(b: Bedrock, tool: str, arguments: dict[str, Any]) -> Any:
    async def go() -> Any:
        async with Client(create_server(b)) as client:
            return await client.call_tool(tool, arguments)

    return anyio.run(go)


def test_the_server_offers_three_read_only_tools() -> None:
    async def go() -> Any:
        async with Client(create_server(bedrock())) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"list_knowledge_bases", "search_knowledge_base", "ask_knowledge_base"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert tools["search_knowledge_base"].input_schema["required"] == ["query"]


def test_tools_over_mcp() -> None:
    result = call(bedrock(), "list_knowledge_bases", {})
    assert not result.is_error
    assert [kb["store"] for kb in result.structured_content["result"]] == [
        "OPENSEARCH_SERVERLESS",
        "RDS",
    ]
    result = call(
        bedrock(),
        "search_knowledge_base",
        {"query": "q", "knowledge_base_id": "PGKB", "search_type": "SEMANTIC"},
    )
    assert result.structured_content["results"][0]["text"] == "near"


class Failing(FakeRuntime):
    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self.exc = exc

    def retrieve(self, **kw: Any) -> dict[str, Any]:
        raise self.exc


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        (FakeRuntime([]), "Name a knowledge_base_id; there are OSKB, PGKB"),
        (
            Failing(EndpointConnectionError(endpoint_url="http://127.0.0.1:8099/x")),
            "Is `local-kb serve` running",
        ),
        (
            Failing(
                ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "No KB NOPE"}},
                    "Retrieve",
                )
            ),
            "No KB NOPE",
        ),
    ],
)
def test_failures_reach_the_agent_with_their_reason(runtime: FakeRuntime, message: str) -> None:
    b = Bedrock(FakeAgent(dict(KBS)), runtime)
    arguments = (
        {"query": "q"}
        if message.startswith("Name")
        else {"query": "q", "knowledge_base_id": "OSKB"}
    )
    result = call(b, "search_knowledge_base", arguments)
    assert result.is_error
    assert message in result.content[0].text
