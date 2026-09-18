"""Offline tests for the Kendra wrappers."""

from __future__ import annotations

from typing import Any

from aws_ai.kendra import QueryResult, query_index, start_sync


class FakeKendraClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] | None = None

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return self.response

    def start_data_source_sync_job(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs
        return self.response


def test_parses_each_result_type() -> None:
    client = FakeKendraClient(
        {
            "ResultItems": [
                {
                    "Type": "ANSWER",
                    "DocumentTitle": {"Text": "Inference"},
                    "DocumentExcerpt": {"Text": "Inference is running a trained model."},
                    "DocumentURI": "s3://bucket/kendra/notes.pdf",
                    "ScoreAttributes": {"ScoreConfidence": "VERY_HIGH"},
                },
                {
                    "Type": "DOCUMENT",
                    "DocumentTitle": {"Text": "Training"},
                    "DocumentExcerpt": {"Text": "Training fits parameters."},
                    "DocumentURI": "s3://bucket/kendra/train.pdf",
                    "ScoreAttributes": {"ScoreConfidence": "MEDIUM"},
                },
            ]
        }
    )

    results = query_index(client, "idx-1", "what is inference?")

    assert [r.result_type for r in results] == ["ANSWER", "DOCUMENT"]
    assert results[0].is_answer
    assert not results[1].is_answer
    assert results[0].score == "VERY_HIGH"
    assert results[0].document_title == "Inference"


def test_missing_fields_do_not_raise() -> None:
    client = FakeKendraClient({"ResultItems": [{}]})

    (result,) = query_index(client, "idx-1", "anything")

    assert result == QueryResult(
        result_type="DOCUMENT",
        document_title="",
        excerpt="",
        document_uri="",
        score="NOT_AVAILABLE",
    )


def test_empty_response_gives_empty_list() -> None:
    assert query_index(FakeKendraClient({}), "idx-1", "nothing here") == []


def test_attribute_filter_is_sent_only_when_given() -> None:
    client = FakeKendraClient({"ResultItems": []})

    query_index(client, "idx-1", "q")
    assert client.last_kwargs is not None
    assert "AttributeFilter" not in client.last_kwargs

    filt = {"EqualsTo": {"Key": "_language_code", "Value": {"StringValue": "en"}}}
    query_index(client, "idx-1", "q", attribute_filter=filt, page_size=3)
    assert client.last_kwargs["AttributeFilter"] == filt
    assert client.last_kwargs["PageSize"] == 3


def test_start_sync_returns_execution_id() -> None:
    client = FakeKendraClient({"ExecutionId": "exec-42"})

    assert start_sync(client, "idx-1", "ds-1") == "exec-42"
    assert client.last_kwargs == {"Id": "ds-1", "IndexId": "idx-1"}
