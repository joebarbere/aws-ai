"""The knowledge base as MCP tools, so an agent can use it alongside its other tools.

    local-kb mcp                          stdio: the agent launches it as a subprocess
    local-kb mcp --http                   Streamable HTTP at http://127.0.0.1:8097/mcp

Bedrock's RetrieveAndGenerate answers only from the chunks it retrieved. An agent (Hermes Agent,
LM Studio's chat, Claude Code, ...) can instead call these tools when it decides the knowledge
base is relevant, and combine what they return with web search, other tools and what the model
already knows.

The tools call Bedrock's API with stock boto3. The same server therefore works against
``local-kb serve`` (the default, http://127.0.0.1:8099) or against real Bedrock
(``--endpoint aws``, with the usual AWS credentials).

- ``list_knowledge_bases``: ids, names, descriptions and which store each uses.
- ``search_knowledge_base``: Retrieve. Ranked chunks with their source and attributes. Results
  are always best-first: the RDS store's SEMANTIC results, which Bedrock returns worst-first
  scored by distance, are put back in order.
- ``ask_knowledge_base``: RetrieveAndGenerate. A grounded answer with its sources, written by
  the knowledge base's own model rather than the agent's.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from aws_ai.local_kb import config, index

LOCAL_ENDPOINT = "http://127.0.0.1:8099"
LOCAL_MODEL_ARN = f"arn:aws:bedrock:{config.REGION}::foundation-model/kb-llm"
SearchType = Literal["HYBRID", "SEMANTIC"]

INSTRUCTIONS = """\
Tools for a Bedrock knowledge base: a store of documents searched by meaning and keywords.
Call list_knowledge_bases first to see what each knowledge base holds. Use
search_knowledge_base for questions its documents may answer, and cite each fact by its source.
If the results do not answer the question, say so before answering from other tools or your
own knowledge."""


@dataclass
class Bedrock:
    """Boto3's bedrock-agent and bedrock-agent-runtime clients, and the defaults to use."""

    agent: Any
    runtime: Any
    knowledge_base_id: str | None = None
    model_arn: str = LOCAL_MODEL_ARN
    _stores: dict[str, str] = field(default_factory=dict)

    @classmethod
    def connect(cls, endpoint: str, **defaults: Any) -> Bedrock:
        import boto3

        if endpoint == "aws":
            kw: dict[str, Any] = {}
        else:
            # local-kb accepts any signature; never send real credentials to it.
            kw = {
                "endpoint_url": endpoint,
                "region_name": config.REGION,
                "aws_access_key_id": "local-kb",
                "aws_secret_access_key": "local-kb",
            }
        return cls(
            boto3.client("bedrock-agent", **kw),
            boto3.client("bedrock-agent-runtime", **kw),
            **defaults,
        )

    def kb_id(self, given: str | None) -> str:
        """The knowledge base to use: the one given, the server's default, or the only one."""
        if chosen := given or self.knowledge_base_id:
            return chosen
        ids = [str(kb["knowledgeBaseId"]) for kb in self._summaries()]
        if len(ids) == 1:
            return ids[0]
        raise ValueError(
            "Name a knowledge_base_id; there are "
            + (", ".join(ids) if ids else "no knowledge bases")
            + ". list_knowledge_bases describes them."
        )

    def store(self, kb_id: str) -> str:
        """``OPENSEARCH_SERVERLESS``, ``RDS``, ...: the knowledge base's storage type."""
        if kb_id not in self._stores:
            kb = self.agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
            self._stores[kb_id] = kb["storageConfiguration"]["type"]
        return self._stores[kb_id]

    def _summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for page in self.agent.get_paginator("list_knowledge_bases").paginate():
            summaries.extend(page["knowledgeBaseSummaries"])
        return summaries

    # ------------------------------------------------------------------- tools

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        return [
            {
                "knowledge_base_id": kb["knowledgeBaseId"],
                "name": kb["name"],
                "description": kb.get("description", ""),
                "status": kb["status"],
                "store": self.store(kb["knowledgeBaseId"]),
            }
            for kb in self._summaries()
        ]

    def search(
        self,
        query: str,
        knowledge_base_id: str | None = None,
        number_of_results: int = 5,
        search_type: SearchType = "HYBRID",
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kb_id = self.kb_id(knowledge_base_id)
        vector: dict[str, Any] = {
            "numberOfResults": number_of_results,
            "overrideSearchType": search_type,
        }
        if filter:
            vector["filter"] = filter
        response = self.runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vector},
        )
        hits = response["retrievalResults"]
        # On RDS, Bedrock scores SEMANTIC results by cosine distance and lists them worst-first.
        distance = search_type == "SEMANTIC" and self.store(kb_id) == "RDS"
        hits = sorted(hits, key=lambda h: h.get("score", 0.0), reverse=not distance)
        return {
            "knowledge_base_id": kb_id,
            "search_type": search_type,
            "score": "cosine distance, lower is closer" if distance else "higher is better",
            "results": [_result(rank, hit) for rank, hit in enumerate(hits, 1)],
        }

    def ask(
        self,
        question: str,
        knowledge_base_id: str | None = None,
        number_of_results: int = 5,
        search_type: SearchType = "HYBRID",
    ) -> dict[str, Any]:
        kb_id = self.kb_id(knowledge_base_id)
        response = self.runtime.retrieve_and_generate(
            input={"text": question},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kb_id,
                    "modelArn": self.model_arn,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": number_of_results,
                            "overrideSearchType": search_type,
                        }
                    },
                },
            },
        )
        sources: list[str] = []
        for citation in response.get("citations", []):
            for ref in citation.get("retrievedReferences", []):
                uri = _uri(ref)
                if uri and uri not in sources:
                    sources.append(uri)
        return {
            "knowledge_base_id": kb_id,
            "answer": response["output"]["text"],
            "sources": sources,
        }


def _uri(hit: dict[str, Any]) -> str | None:
    location = hit.get("location", {})
    for value in location.values():
        if isinstance(value, dict) and value.get("uri"):
            return str(value["uri"])
    uri = hit.get("metadata", {}).get(index.SOURCE_URI)
    return str(uri) if uri else None


def _result(rank: int, hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata", {})
    out: dict[str, Any] = {
        "rank": rank,
        "source": _uri(hit),
        "text": hit["content"].get("text", ""),
        "score": hit.get("score"),
    }
    if index.PAGE_NUMBER in metadata:
        out["page"] = int(metadata[index.PAGE_NUMBER])
    attributes = {k: v for k, v in metadata.items() if not k.startswith("x-amz-bedrock-kb-")}
    if attributes:
        out["attributes"] = attributes
    return out


@contextmanager
def _explained(error: Callable[[str], Exception]) -> Iterator[None]:
    """Expected failures reach the agent with their reason as ``error(message)``. The SDK
    hides any other exception's text, so the agent would see only that the tool failed."""
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

    try:
        yield
    except EndpointConnectionError as exc:
        raise error(f"{exc}. Is `local-kb serve` running (make local-serve)?") from exc
    except (ValueError, ClientError, BotoCoreError) as exc:
        raise error(str(exc)) from exc


def create_server(bedrock: Bedrock) -> Any:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import ToolAnnotations

    server = MCPServer("local-kb", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def list_knowledge_bases() -> list[dict[str, Any]]:
        """The knowledge bases available: id, name, description, status, and storage type."""
        with _explained(ToolError):
            return bedrock.list_knowledge_bases()

    @server.tool(annotations=read_only)
    def search_knowledge_base(
        query: str,
        knowledge_base_id: str | None = None,
        number_of_results: int = 5,
        search_type: SearchType = "HYBRID",
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Search a knowledge base's documents. Returns the best-matching passages, best first,
        each with its source document (and page, for PDFs) to cite.

        Args:
            query: What to look for, in natural language or exact terms such as codes or names.
            knowledge_base_id: Which knowledge base; optional when the server has a default or
                there is only one.
            number_of_results: How many passages, 1-100.
            search_type: HYBRID (meaning and keywords; best for most questions) or SEMANTIC
                (meaning only).
            filter: Optional Bedrock metadata filter on document attributes, e.g.
                {"equals": {"key": "year", "value": 2024}}.
        """
        with _explained(ToolError):
            return bedrock.search(query, knowledge_base_id, number_of_results, search_type, filter)

    @server.tool(annotations=read_only)
    def ask_knowledge_base(
        question: str,
        knowledge_base_id: str | None = None,
        number_of_results: int = 5,
        search_type: SearchType = "HYBRID",
    ) -> dict[str, Any]:
        """Ask a knowledge base a question and get Bedrock's grounded answer with its sources.
        It answers only from the documents, and says it cannot help when they don't cover the
        question. Prefer search_knowledge_base when you want the passages themselves.

        Args:
            question: The question.
            knowledge_base_id: Which knowledge base; optional as for search_knowledge_base.
            number_of_results: How many passages the answer may draw on.
            search_type: HYBRID or SEMANTIC.
        """
        with _explained(ToolError):
            return bedrock.ask(question, knowledge_base_id, number_of_results, search_type)

    return server


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--endpoint",
        default=LOCAL_ENDPOINT,
        help=f"local-kb serve's URL (default {LOCAL_ENDPOINT}), or 'aws' for real Bedrock",
    )
    p.add_argument("--knowledge-base-id", help="The default knowledge base for the tools")
    p.add_argument(
        "--model-arn",
        default=LOCAL_MODEL_ARN,
        help="ask_knowledge_base's model (default: local-kb's kb-llm; on AWS, a Bedrock model ARN)",
    )
    p.add_argument("--http", action="store_true", help="Serve Streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8097)


def run(args: argparse.Namespace) -> None:
    bedrock = Bedrock.connect(
        args.endpoint, knowledge_base_id=args.knowledge_base_id, model_arn=args.model_arn
    )
    server = create_server(bedrock)
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")
