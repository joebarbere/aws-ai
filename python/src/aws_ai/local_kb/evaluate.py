"""Evaluate a knowledge base: does retrieval find the right document, and is the answer right?

Each row of the dataset (local-kb/eval/qa.jsonl) names the question, the source file that holds
the answer, keyword groups the answer must contain (every group, any alternative within one),
and a reference answer. Three scores per question:

- **retrieval hit@k** — the expected source is among the results Retrieve returns;
- **keywords** — a cheap, deterministic check on the generated answer;
- **judge** — an LLM-as-judge verdict (the local model) comparing the answer with the reference.

Keywords and judge disagree usefully: a keyword miss with a CORRECT verdict is usually a
paraphrase ("3 hours" for "three hours"); a keyword hit with INCORRECT is worth reading.

This is the AIF-C01 "evaluate a RAG system" topic in miniature: the same retrieval and
generation-quality questions Bedrock's managed RAG evaluation asks, with a judge you can read.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.generate import retrieve_and_generate
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.retrieve import retrieve

JUDGE = """You are grading an answer produced by a question-answering system.

Question: {question}
Reference answer: {reference}
System answer: {answer}

Is the system answer correct? It is correct if it states the same facts as the reference answer;
wording, extra detail, and politeness do not matter. Saying the information was not found is
incorrect. Reply with exactly one word, CORRECT or INCORRECT, then a short reason on the
next line."""


@dataclass(frozen=True)
class Outcome:
    question: str
    expected_source: str
    retrieved: list[str]
    hit: bool
    answer: str
    keywords: bool
    judge: bool
    judge_reason: str
    seconds: float
    note: str = ""


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def keywords_match(answer: str, groups: list[list[str]]) -> bool:
    text = answer.lower()
    return all(any(alt.lower() in text for alt in group) for group in groups)


def evaluate(
    cp: ControlPlane,
    services: Services,
    knowledge_base_id: str,
    dataset: list[dict[str, Any]],
    *,
    search_type: str = "SEMANTIC",
    number_of_results: int = 5,
    model_arn: str = "arn:aws:bedrock:us-east-1::foundation-model/kb-llm",
) -> list[Outcome]:
    assert services.chat is not None
    retrieval = {
        "vectorSearchConfiguration": {
            "numberOfResults": number_of_results,
            "overrideSearchType": search_type,
        }
    }
    outcomes = []
    for row in dataset:
        started = time.monotonic()
        results = retrieve(cp, services, knowledge_base_id, {"text": row["question"]}, retrieval)
        sources = [
            r["location"]["s3Location"]["uri"].rsplit("/", 1)[-1]
            for r in results["retrievalResults"]
        ]
        answer = retrieve_and_generate(
            cp,
            services,
            {"text": row["question"]},
            {
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": knowledge_base_id,
                    "modelArn": model_arn,
                    "retrievalConfiguration": retrieval,
                },
            },
        )["output"]["text"]
        verdict = services.chat(
            JUDGE.format(question=row["question"], reference=row["reference"], answer=answer),
            "Grade the answer.",
            {"temperature": 0.0, "topP": 1.0, "maxTokens": 100},
        ).strip()
        outcomes.append(
            Outcome(
                question=row["question"],
                expected_source=row["source"],
                retrieved=sources,
                hit=row["source"] in sources,
                answer=answer,
                keywords=keywords_match(answer, row["keywords"]),
                judge=verdict.upper().startswith("CORRECT"),
                judge_reason=verdict.split("\n", 1)[-1].strip() if "\n" in verdict else verdict,
                seconds=round(time.monotonic() - started, 1),
                note=row.get("note", ""),
            )
        )
    return outcomes


def summary(outcomes: list[Outcome], label: str) -> dict[str, Any]:
    n = len(outcomes) or 1
    return {
        "run": label,
        "questions": len(outcomes),
        "retrieval_hit_rate": round(sum(o.hit for o in outcomes) / n, 3),
        "keyword_accuracy": round(sum(o.keywords for o in outcomes) / n, 3),
        "judge_accuracy": round(sum(o.judge for o in outcomes) / n, 3),
        "mean_seconds": round(sum(o.seconds for o in outcomes) / n, 1),
        "outcomes": [asdict(o) for o in outcomes],
    }


def table(report: dict[str, Any]) -> str:
    lines = [
        f"{report['run']}: hit@k {report['retrieval_hit_rate']:.0%}, "
        f"keywords {report['keyword_accuracy']:.0%}, judge {report['judge_accuracy']:.0%}, "
        f"{report['mean_seconds']}s/question",
        "",
        f"{'hit':4} {'kw':3} {'judge':6} question",
    ]
    for o in report["outcomes"]:
        marks = ["✓" if o[key] else "✗" for key in ("hit", "keywords", "judge")]
        lines.append(f"{marks[0]:4} {marks[1]:3} {marks[2]:6} {o['question']}")
        if not (o["hit"] and o["keywords"] and o["judge"]):
            lines.append(f"{'':15}answer: {o['answer'][:110]}")
            if not o["hit"]:
                lines.append(f"{'':15}retrieved: {', '.join(o['retrieved'])}")
    return "\n".join(lines)
