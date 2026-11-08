"""RetrieveAndGenerate: retrieve, prompt, answer, cite — as Bedrock does it.

The prompts in ``prompts/`` are Bedrock's own, cut from the model invocations logged in Phase 0
(local-kb/fixtures/invocation-logs*.json); each re-renders to the captured prompt byte for byte.
Bedrock picks the prompt by **model family**, and so does this module, from the ``modelArn``:

- ``nova`` (any model id containing "nova"): a "curious User / Bot" system prompt, results as
  ``Passage %[n]% : …``, and inline ``%[n]%`` citation markers in the answer.
- ``generic`` (everything else — what Llama 3 got): results as ``<search_result>`` XML and an
  ``<answer><answer_part><text>…<sources>`` output format.

Whatever the ARN, the local model (``kb-llm``) answers; the ARN only chooses the prompt.

Follow-ups in a session are first rewritten into a standalone query, and
``QUERY_DECOMPOSITION`` splits a question into sub-queries; both use Bedrock's orchestration
prompts. Answer parsing follows the fixtures: markers are removed with the space before them,
spans are inclusive, an answer without citations still gets one citation with no references,
and a generic-family answer that ignores the XML format becomes Bedrock's
"Sorry, I am unable to assist you with this request."
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any

from aws_ai.local_kb import index
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ValidationException
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.retrieve import DEFAULT_RESULTS, retrieve

UNABLE = "Sorry, I am unable to assist you with this request."
# Bedrock's inference settings per family, from the logged requests.
INFERENCE: dict[str, dict[str, Any]] = {
    "generic": {
        "maxTokens": 2048,
        "temperature": 0.0,
        "topP": 1.0,
        "stopSequences": ["\nObservation"],
    },
    "nova": {"temperature": 0.0, "topP": 1.0},
}
ORCHESTRATION_INFERENCE: dict[str, Any] = {"temperature": 0.0, "topP": 1.0}


@cache
def prompt(name: str) -> str:
    return (resources.files("aws_ai.local_kb") / "prompts" / f"{name}.txt").read_text()


def family(model_arn: str) -> str:
    return "nova" if "nova" in model_arn.rsplit("/", 1)[-1].lower() else "generic"


def retrieve_and_generate(
    cp: ControlPlane,
    services: Services,
    input: dict[str, Any],  # noqa: A002 - the API's own name
    retrieve_and_generate_configuration: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    if services.chat is None:
        raise ValidationException("No generation model is configured.")
    config = retrieve_and_generate_configuration
    if config.get("type") != "KNOWLEDGE_BASE":
        raise ValidationException("local-kb supports type KNOWLEDGE_BASE only.")
    kb_config = config.get("knowledgeBaseConfiguration", {})
    question = input.get("text")
    if not isinstance(question, str) or not question:
        raise ValidationException("input.text is required.")
    kb_id, model_arn = kb_config.get("knowledgeBaseId"), kb_config.get("modelArn")
    if not kb_id or not model_arn:
        raise ValidationException("knowledgeBaseId and modelArn are required.")

    history = cp.session_turns(session_id) if session_id else []
    session_id = session_id or cp.create_session()
    kind = family(model_arn)

    queries = _orchestrate(services, question, history, kb_config.get("orchestrationConfiguration"))
    retrieval = kb_config.get("retrievalConfiguration")
    results = _merge(
        [
            retrieve(cp, services, kb_id, {"text": q}, retrieval)["retrievalResults"]
            for q in queries
        ],
        retrieval,
    )

    generation = kb_config.get("generationConfiguration", {})
    template = generation.get("promptTemplate", {}).get("textPromptTemplate") or prompt(
        f"{kind}_generation"
    )
    system = (
        template.replace("$search_results$", _render_results(results, kind))
        .replace("$output_format_instructions$", prompt(f"{kind}_output_format"))
        .replace("$query$", queries[0])
        .replace("$current_time$", datetime.now(UTC).isoformat())
    )
    inference = {
        **INFERENCE[kind],
        **generation.get("inferenceConfig", {}).get("textInferenceConfig", {}),
    }
    reply = services.chat(system, queries[0], inference)

    if "$output_format_instructions$" not in template:
        text, citations = reply.strip(), [_citation(reply.strip(), 0, [])]
    elif kind == "nova":
        text, citations = parse_nova(reply, results)
    else:
        text, citations = parse_generic(reply, results)
    cp.append_turn(session_id, question, text)
    return {"citations": citations, "output": {"text": text}, "sessionId": session_id}


# --------------------------------------------------------------- orchestration


def _orchestrate(
    services: Services,
    question: str,
    history: list[tuple[str, str]],
    orchestration: dict[str, Any] | None,
) -> list[str]:
    """The queries to retrieve with; the first is also the question the answer addresses."""
    assert services.chat is not None
    decompose = (orchestration or {}).get("queryTransformationConfiguration", {}).get(
        "type"
    ) == "QUERY_DECOMPOSITION"
    if not decompose and not history:
        return [question]
    conversation = "\n\n".join(f"User: {user} Bot: {bot}" for user, bot in history)
    template = prompt("decomposition" if decompose else "rewrite")
    system = template.replace("$conversation_history$", conversation).replace("$query$", question)
    reply = services.chat(system, question, ORCHESTRATION_INFERENCE)
    if decompose:
        standalone = re.search(r'standalone_question="((?:[^"\\]|\\.)*)"', reply)
        extra = re.search(r"additional_queries=\[(.*?)\]", reply, re.S)
        queries = [standalone.group(1)] if standalone else [question]
        if extra:
            queries += re.findall(r'"((?:[^"\\]|\\.)*)"', extra.group(1))
        return queries
    rewritten = re.search(r'query=["“]((?:[^"”\\]|\\.)*)["”]', reply)
    return [rewritten.group(1)] if rewritten else [question]


def _merge(
    lists: list[list[dict[str, Any]]], retrieval: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Round-robin across the queries' results, de-duplicated by chunk, cut to
    numberOfResults. (Decomposition's merge order isn't observable; Phase 0 showed only that
    one prompt carried results from every sub-query.)"""
    size = (
        (retrieval or {})
        .get("vectorSearchConfiguration", {})
        .get("numberOfResults", DEFAULT_RESULTS)
    )
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank in range(max((len(r) for r in lists), default=0)):
        for results in lists:
            if rank < len(results):
                result = results[rank]
                # An RDS store's results carry no chunk id; the source and text identify one.
                chunk = result["metadata"].get(index.CHUNK_ID) or (
                    f"{result['location']['s3Location']['uri']}\n{result['content']['text']}"
                )
                if chunk not in seen:
                    seen.add(chunk)
                    merged.append(result)
    return merged[:size]


def _render_results(results: list[dict[str, Any]], kind: str) -> str:
    texts = [r["content"]["text"] for r in results]
    if kind == "nova":
        return "\n".join(f"Passage %[{n}]% : {t}" for n, t in enumerate(texts, 1))
    body = "".join(
        f"<search_result>\n<content>\n{t}\n</content>\n<source>\n{n}\n</source>\n</search_result>\n"
        for n, t in enumerate(texts, 1)
    )
    return f"<search_results>\n{body}\n</search_results>"


# ------------------------------------------------------------------- parsing


def _reference(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in ("content", "location", "metadata")}


def _citation(text: str, start: int, refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generatedResponsePart": {
            "textResponsePart": {
                "span": {"end": start + len(text) - 1, "start": start},
                "text": text,
            }
        },
        "retrievedReferences": refs,
    }


def _refs(ids: list[str], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for raw in ids:
        if raw.strip().isdigit() and 1 <= int(raw) <= len(results) and int(raw) not in seen:
            seen.add(int(raw))
            out.append(_reference(results[int(raw) - 1]))
    return out


def parse_generic(reply: str, results: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    parts = re.findall(
        r"<answer_part>\s*<text>(.*?)</text>\s*<sources>(.*?)</sources>\s*</answer_part>",
        reply,
        re.S,
    )
    if not parts:
        return UNABLE, [_citation(UNABLE, 0, [])]
    text, citations = "", []
    for body, sources in parts:
        piece = body.strip()
        if text:
            text += " "
        citations.append(
            _citation(
                piece, len(text), _refs(re.findall(r"<source>(.*?)</source>", sources), results)
            )
        )
        text += piece
    return text, citations


_MARKERS = re.compile(r"\s?((?:%\[\d+\]%)+)")


def parse_nova(reply: str, results: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Each run of ``%[n]%`` markers closes a cited part; markers go, with the space before."""
    reply = reply.strip()
    pieces: list[tuple[str, list[str]]] = []
    position = 0
    for match in _MARKERS.finditer(reply):
        pieces.append((reply[position : match.start()], re.findall(r"%\[(\d+)\]%", match.group(1))))
        position = match.end()
    tail = reply[position:]
    if not pieces:
        return reply, [_citation(reply, 0, [])]
    pieces[-1] = (pieces[-1][0] + tail, pieces[-1][1])
    text, citations = "", []
    for piece, ids in pieces:
        citations.append(_citation(piece, len(text), _refs(ids, results)))
        text += piece
    return text, citations
