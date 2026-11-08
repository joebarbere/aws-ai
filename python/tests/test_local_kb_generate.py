"""Offline tests for RetrieveAndGenerate, against what Bedrock did in local-kb/fixtures/."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aws_ai.local_kb import cli
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ResourceNotFoundException, ValidationException
from aws_ai.local_kb.generate import (
    UNABLE,
    family,
    parse_generic,
    parse_nova,
    prompt,
    retrieve_and_generate,
)
from aws_ai.local_kb.ingest import Services

FIXTURES = Path(__file__).resolve().parents[2] / "local-kb" / "fixtures"
GENERIC = "arn:aws:bedrock:us-east-1::foundation-model/kb-llm"
NOVA = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"


def captured() -> list[dict[str, Any]]:
    logs = json.loads((FIXTURES / "invocation-logs.json").read_text())
    logs += json.loads((FIXTURES / "invocation-logs-llama-nova-pro.json").read_text())
    return [r["input"]["inputBodyJson"] for r in logs if "embed" not in r["modelId"]]


def system(body: dict[str, Any]) -> str:
    text: str = body["system"][0]["text"]
    return text


def user(body: dict[str, Any]) -> str:
    text: str = body["messages"][0]["content"][0]["text"]
    return text


# ------------------------------------------------------------------ templates


def test_templates_rerender_bedrocks_prompts_exactly() -> None:
    bodies = captured()
    nova_gen, rewrite, decomposition, llama = bodies[0], bodies[1], bodies[4], bodies[6]

    results = system(nova_gen).split("Resource: Search Results: ")[1].removesuffix("\n Bot:")
    assert prompt("nova_generation").replace(
        "$output_format_instructions$", prompt("nova_output_format")
    ).replace("$query$", user(nova_gen)).replace("$search_results$", results) == system(nova_gen)

    llama_system = system(llama)
    xml = llama_system[
        llama_system.index("<search_results>") : llama_system.index("</search_results>") + 17
    ]
    assert (
        prompt("generic_generation")
        .replace("$search_results$", xml)
        .replace("$output_format_instructions$", prompt("generic_output_format"))
        == llama_system
    )

    history = (
        f"User: {user(nova_gen)} Bot: The model cannot find sufficient information to answer "
        "the question."
    )
    assert prompt("rewrite").replace("$conversation_history$", history).replace(
        "$query$", user(rewrite)
    ) == system(rewrite)
    assert prompt("decomposition").replace("$conversation_history$", "").replace(
        "$query$", user(decomposition)
    ) == system(decomposition)


def test_family_comes_from_the_model_id() -> None:
    assert family(NOVA) == "nova"
    assert family("arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0") == "nova"
    assert family(GENERIC) == "generic"
    assert (
        family("arn:aws:bedrock:us-east-1::foundation-model/meta.llama3-8b-instruct-v1:0")
        == "generic"
    )


# -------------------------------------------------------------------- parsing


def result(n: int) -> dict[str, Any]:
    return {
        "content": {"text": f"chunk {n}", "type": "TEXT"},
        "location": {"s3Location": {"uri": f"s3://b/{n}.txt"}, "type": "S3"},
        "metadata": {"x-amz-bedrock-kb-chunk-id": f"c{n}"},
        "score": 0.5,
    }


RESULTS = [result(n) for n in range(1, 4)]


def span_of(citation: dict[str, Any]) -> tuple[int, int, str]:
    part = citation["generatedResponsePart"]["textResponsePart"]
    return part["span"]["start"], part["span"]["end"], part["text"]


def test_generic_answer_parts_become_citations() -> None:
    reply = (
        "<answer>\n<answer_part>\n<text>\nStudents pay 30 dollars.\n</text>\n<sources>\n"
        "<source>1</source>\n</sources>\n</answer_part>\n<answer_part>\n<text>\nUse port DH-2.\n"
        "</text>\n<sources>\n<source>2</source>\n<source>9</source>\n</sources>\n</answer_part>\n"
        "</answer>"
    )
    text, citations = parse_generic(reply, RESULTS)
    assert text == "Students pay 30 dollars. Use port DH-2."
    for citation in citations:
        start, end, part = span_of(citation)
        assert text[start : end + 1] == part  # inclusive end, as in every fixture
    assert [
        [r["location"]["s3Location"]["uri"] for r in c["retrievedReferences"]] for c in citations
    ] == [
        ["s3://b/1.txt"],
        ["s3://b/2.txt"],  # source 9 doesn't exist: dropped
    ]
    assert "score" not in citations[0]["retrievedReferences"][0]


def test_generic_answer_without_xml_is_bedrocks_refusal() -> None:
    """fixtures/rag-llama3-8b.json: Llama answered in prose; Bedrock returned this instead."""
    text, citations = parse_generic("I could not find an exact answer.", RESULTS)
    real = json.loads((FIXTURES / "rag-llama3-8b.json").read_text())
    assert text == UNABLE == real["output"]["text"]
    assert citations == real["citations"]


def test_nova_markers_are_removed_with_the_space_before() -> None:
    """The rule in fixtures/rag-nova-pro.json and rag-decomposition.json."""
    text, citations = parse_nova(
        "Students pay 30 dollars %[1]%. Use port DH-2 %[2]%%[3]%.", RESULTS
    )
    assert text == "Students pay 30 dollars. Use port DH-2."
    assert [span_of(c)[2] for c in citations] == ["Students pay 30 dollars", ". Use port DH-2."]
    assert [len(c["retrievedReferences"]) for c in citations] == [1, 2]
    for citation in citations:
        start, end, part = span_of(citation)
        assert text[start : end + 1] == part


def test_nova_answer_without_markers_gets_one_empty_citation() -> None:
    """fixtures/rag-default.json: Nova's 'cannot find' answer."""
    real = json.loads((FIXTURES / "rag-default.json").read_text())
    text, citations = parse_nova(real["output"]["text"], RESULTS)
    assert text == real["output"]["text"]
    assert citations == real["citations"]


# ------------------------------------------------------------------ the flow


class FakeOpenSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def request(self, method: str, path: str, body: Any = None, **_: Any) -> Any:
        if method == "PUT":
            return {"acknowledged": True}
        return {
            "hits": {
                "hits": [
                    {
                        "_id": f"1%3A0%3A{self.queries[-1][:6]}{n}",
                        "_score": 0.5,
                        "_source": {
                            "AMAZON_BEDROCK_TEXT_CHUNK": f"{self.queries[-1]} / result {n}",
                            "x-amz-bedrock-kb-source-uri": f"s3://kb-data/kb/{n}.txt",
                        },
                    }
                    for n in range(1, 4)
                ]
            }
        }

    @staticmethod
    def quote(index: str) -> str:
        return index


class Model:
    """Scripted replies; records each (system, user, inference) it is sent."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, system: str, user: str, inference: dict[str, Any]) -> str:
        self.calls.append((system, user, inference))
        return self.replies.pop(0)


def setup(model: Model) -> tuple[ControlPlane, Services, str]:
    cp = ControlPlane(sqlite3.connect(":memory:"))
    kb = cp.create_knowledge_base(
        name="kb",
        knowledge_base_configuration=cli.default_knowledge_base_configuration(4),
        storage_configuration=cli.default_storage_configuration("i"),
    )["knowledgeBaseId"]
    opensearch = FakeOpenSearch()

    def embed(texts: list[str]) -> list[list[float]]:
        opensearch.queries.extend(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    return cp, Services(None, opensearch, lambda _a, _d: embed, len, model), kb  # type: ignore[arg-type]


def ask(
    cp: ControlPlane,
    services: Services,
    kb: str,
    text: str,
    model_arn: str = GENERIC,
    session: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return retrieve_and_generate(
        cp,
        services,
        {"text": text},
        {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {"knowledgeBaseId": kb, "modelArn": model_arn, **extra},
        },
        session,
    )


ANSWER = (
    "<answer><answer_part><text>30 dollars.</text>"
    "<sources><source>1</source></sources></answer_part></answer>"
)


def test_first_question_has_no_orchestration_and_bedrocks_inference() -> None:
    model = Model(ANSWER)
    cp, services, kb = setup(model)
    out = ask(cp, services, kb, "How much is a student membership?")
    assert list(out) == ["citations", "output", "sessionId"]
    assert out["output"]["text"] == "30 dollars."
    ((system_prompt, user_message, inference),) = model.calls
    assert user_message == "How much is a student membership?"
    assert "<source>\n1\n</source>" in system_prompt and "$" not in system_prompt
    assert inference == {
        "maxTokens": 2048,
        "temperature": 0.0,
        "topP": 1.0,
        "stopSequences": ["\nObservation"],
    }


def test_follow_up_is_rewritten_with_the_history() -> None:
    model = Model(ANSWER, 'Action: FUNCTION(query="How long is the orientation course?")', ANSWER)
    cp, services, kb = setup(model)
    first = ask(cp, services, kb, "How much is a student membership?")
    second = ask(cp, services, kb, "and the orientation?", session=first["sessionId"])
    assert second["sessionId"] == first["sessionId"]
    rewrite_system, _, _ = model.calls[1]
    assert "User: How much is a student membership? Bot: 30 dollars." in rewrite_system
    assert 'User: ORIGINAL(text="and the orientation?") Bot:' in rewrite_system
    assert services.opensearch.queries[-1] == "How long is the orientation course?"  # type: ignore[attr-defined]
    assert model.calls[2][1] == "How long is the orientation course?"


def test_decomposition_retrieves_for_every_sub_query() -> None:
    model = Model(
        'Action: FUNCTION(standalone_question="Cost and port?", '
        'additional_queries=["What does membership cost?", "Which port?"])',
        ANSWER,
    )
    cp, services, kb = setup(model)
    ask(
        cp,
        services,
        kb,
        "cost and port?",
        orchestrationConfiguration={
            "queryTransformationConfiguration": {"type": "QUERY_DECOMPOSITION"}
        },
    )
    assert services.opensearch.queries == [
        "Cost and port?",
        "What does membership cost?",
        "Which port?",
    ]  # type: ignore[attr-defined]
    generation_system, generation_user, _ = model.calls[1]
    assert generation_user == "Cost and port?"  # the standalone question, as in Phase 0
    # Round-robin merge, cut to the default 5: two from the first query, two, then one.
    assert generation_system.count("<search_result>") == 5


def test_nova_family_uses_novas_prompt_and_inference() -> None:
    model = Model("A 2-amp fuse %[1]%.")
    cp, services, kb = setup(model)
    out = ask(cp, services, kb, "Which fuse?", model_arn=NOVA)
    system_prompt, _, inference = model.calls[0]
    assert system_prompt.startswith("A chat between a curious User")
    assert "Resource: Search Results: Passage %[1]% : Which fuse? / result 1" in system_prompt
    assert inference == {"temperature": 0.0, "topP": 1.0}
    assert out["output"]["text"] == "A 2-amp fuse."


def test_custom_template_without_format_instructions_has_no_references() -> None:
    model = Model("A 2-amp fuse.")
    cp, services, kb = setup(model)
    out = ask(
        cp,
        services,
        kb,
        "Which fuse?",
        generationConfiguration={
            "promptTemplate": {"textPromptTemplate": "Results:\n$search_results$\nQ: $query$"}
        },
    )
    assert out["output"]["text"] == "A 2-amp fuse."
    assert [c["retrievedReferences"] for c in out["citations"]] == [[]]


def test_unknown_session_and_bad_requests_are_refused() -> None:
    cp, services, kb = setup(Model())
    with pytest.raises(ResourceNotFoundException):
        ask(cp, services, kb, "q", session="not-a-session")
    with pytest.raises(ValidationException):
        retrieve_and_generate(cp, services, {"text": "q"}, {"type": "EXTERNAL_SOURCES"})
    with pytest.raises(ValidationException):
        ask(cp, services, kb, "")
