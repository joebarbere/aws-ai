"""Kendra queries and data-source syncs.

Kendra returns three result types, and the distinction is exam-relevant: an ANSWER is a span
Kendra believes answers the question outright, a QUESTION_ANSWER is a match against an FAQ, and
a DOCUMENT is ordinary ranked retrieval. Collapsing them loses the thing Kendra is sold on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryResult:
    """One result from a Kendra query."""

    result_type: str
    document_title: str
    excerpt: str
    document_uri: str
    score: str

    @property
    def is_answer(self) -> bool:
        """True when Kendra claims this span answers the question, rather than merely matching."""
        return self.result_type == "ANSWER"


def _text_of(block: dict[str, Any] | None) -> str:
    return (block or {}).get("Text", "") if block else ""


def query_index(
    client: Any,
    index_id: str,
    query_text: str,
    *,
    page_size: int = 10,
    attribute_filter: dict[str, Any] | None = None,
) -> list[QueryResult]:
    """Run one natural-language query against a Kendra index.

    Args:
        client: A ``kendra`` client.
        index_id: Index id, from the ``kendra_index_id`` Terraform output.
        query_text: The natural-language question.
        page_size: Results per page.
        attribute_filter: Optional Kendra attribute filter, for narrowing by metadata.

    Returns:
        Results in Kendra's own ranked order, answers included in place rather than hoisted —
        the caller decides whether a confident answer outranks a document.
    """
    request: dict[str, Any] = {
        "IndexId": index_id,
        "QueryText": query_text,
        "PageSize": page_size,
    }

    if attribute_filter is not None:
        request["AttributeFilter"] = attribute_filter

    response = client.query(**request)

    return [
        QueryResult(
            result_type=item.get("Type", "DOCUMENT"),
            document_title=_text_of(item.get("DocumentTitle")),
            excerpt=_text_of(item.get("DocumentExcerpt")),
            document_uri=item.get("DocumentURI", ""),
            score=item.get("ScoreAttributes", {}).get("ScoreConfidence", "NOT_AVAILABLE"),
        )
        for item in response.get("ResultItems", [])
    ]


def start_sync(client: Any, index_id: str, data_source_id: str) -> str:
    """Kick off a data-source crawl and return the execution id.

    The call returns as soon as the job is queued; it does not wait for the crawl to finish.
    """
    response = client.start_data_source_sync_job(Id=data_source_id, IndexId=index_id)
    return str(response["ExecutionId"])
