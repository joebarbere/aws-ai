"""Offline tests for local knowledge-base ingestion, checked against local-kb/fixtures/.

The parser and chunker must reproduce the text Bedrock stored; the job must reproduce Bedrock's
counters for the Phase 0 sequence (sync, then edit one file, delete one, change one sidecar).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pypdf")  # the `local` extra

from aws_ai.local_kb import cli  # noqa: E402
from aws_ai.local_kb.chunking import Chunk, chunk, pack, sentence_spans  # noqa: E402
from aws_ai.local_kb.control import ControlPlane  # noqa: E402
from aws_ai.local_kb.embed import truncate  # noqa: E402
from aws_ai.local_kb.ingest import Services, has_changes, run_job  # noqa: E402
from aws_ai.local_kb.parse import ParseError, parse_document, parse_sidecar  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "local-kb" / "sample-docs" / "kb"
FIXTURES = REPO / "local-kb" / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def bedrock_chunks() -> dict[str, list[tuple[str, Any]]]:
    out: dict[str, list[tuple[str, Any]]] = {}
    for hit in fixture("index-documents.json")["hits"]:
        source = hit["_source"]
        name = source["x-amz-bedrock-kb-source-uri"].rsplit("/", 1)[-1]
        page = source.get("x-amz-bedrock-kb-document-page-number")
        out.setdefault(name, []).append((source["AMAZON_BEDROCK_TEXT_CHUNK"], page))
    return out


def words(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------- parse


@pytest.mark.parametrize(
    "name",
    [
        "dew-heater-guide.md",
        "meteor-night.html",
        "night-log-template.txt",
        "tidewater-handbook.pdf",
        "winter-closure-2025.txt",
    ],
)
def test_parser_reproduces_bedrocks_text(name: str) -> None:
    """Each of these became a single chunk on AWS, so the chunk is the whole parsed text."""
    ((expected, _page),) = bedrock_chunks()[name]
    assert parse_document(name, (DOCS / name).read_bytes()).text == expected


def test_pdf_pages_are_tracked_by_offset() -> None:
    parsed = parse_document("h.pdf", (DOCS / "tidewater-handbook.pdf").read_bytes())
    assert parsed.page_starts is not None and len(parsed.page_starts) == 3
    assert parsed.page_at(0) == 1
    assert parsed.page_at(parsed.text.index("2. The main instrument")) == 2
    assert parsed.page_at(len(parsed.text) - 1) == 3


def test_unsupported_and_oversized_files_fail_cleanly() -> None:
    with pytest.raises(ParseError, match="unsupported"):
        parse_document("kb/photo.tiff", b"...")
    with pytest.raises(ParseError, match="50 MB"):
        parse_document("kb/huge.txt", b"x" * (50 * 1024 * 1024 + 1))


def test_sidecar_types_and_embedding_prefix() -> None:
    typed = parse_sidecar(
        "d.md.metadata.json", (DOCS / "dew-heater-guide.md.metadata.json").read_bytes()
    )
    values = {k: a.value for k, a in typed.attributes.items()}
    assert values == {
        "department": "operations",
        "revision": 3.0,
        "effective_date": 20261015.0,
        "safety_critical": True,
        "tags": ["dew", "heater", "maintenance"],
    }
    # The exact prefix Bedrock embedded (fixtures/invocation-logs.json), tab included.
    assert typed.embedding_prefix() == "department: operations\t\n\n"

    simple = parse_sidecar(
        "h.pdf.metadata.json", (DOCS / "tidewater-handbook.pdf.metadata.json").read_bytes()
    )
    assert simple.embedding_prefix() == ""


def test_reserved_attribute_names_are_refused() -> None:
    with pytest.raises(ParseError, match="reserved"):
        parse_sidecar(
            "x.metadata.json", b'{"metadataAttributes": {"x-amz-bedrock-kb-source-uri": "s3://x"}}'
        )


# ------------------------------------------------------------------- chunking


def test_default_chunking_reproduces_all_17_bedrock_chunks() -> None:
    """Sentence packing at 300 Titan tokens / 20% overlap, counted with Qwen's tokenizer."""
    try:
        from aws_ai.local_kb.embed import qwen_token_counter

        count = qwen_token_counter()
    except Exception as exc:  # no network and no cached tokenizer
        pytest.skip(f"Qwen tokenizer unavailable: {exc}")
    for name, expected in bedrock_chunks().items():
        parsed = parse_document(name, (DOCS / name).read_bytes())
        pages = name.endswith(".pdf")
        mine = [
            (c.text, parsed.page_at(c.start) if pages else None)
            for c in chunk(parsed.text, None, count)
        ]
        assert sorted(mine, key=str) == sorted(expected, key=str), name


def test_packing_starts_and_ends_on_sentences_with_overlap() -> None:
    text = " ".join(f"Sentence {i} has five words." for i in range(10))
    chunks = pack(text, sentence_spans(text), words, max_tokens=15, overlap_tokens=5)
    assert all(c.text.startswith("Sentence") and c.text.endswith(".") for c in chunks)
    assert [c.text.count("Sentence") for c in chunks] == [3, 3, 3, 3, 2]
    # Each chunk repeats the previous chunk's last sentence (5 words fit the 5-token overlap).
    assert chunks[1].text.startswith(chunks[0].text.rsplit(". ", 1)[-1].rstrip("."))


def test_a_sentence_longer_than_the_limit_is_split_at_words() -> None:
    text = "one two three four five six seven eight nine ten."
    assert [c.text for c in pack(text, sentence_spans(text), words, 4, 0)] == [
        "one two three four",
        "five six seven eight",
        "nine ten.",
    ]


def test_none_and_hierarchical_strategies() -> None:
    text = " ".join(f"Sentence {i} has five words." for i in range(12))
    assert chunk(text, {"chunkingStrategy": "NONE"}, words) == [Chunk(text, 0)]
    children = chunk(
        text,
        {
            "chunkingStrategy": "HIERARCHICAL",
            "hierarchicalChunkingConfiguration": {
                "levelConfigurations": [{"maxTokens": 26}, {"maxTokens": 9}],
                "overlapTokens": 1,
            },
        },
        words,
    )
    assert all(c.parent_text and c.text in c.parent_text for c in children)
    assert len({c.parent_text for c in children}) > 1


def test_semantic_chunking_splits_where_meaning_shifts() -> None:
    text = "Cats purr. Cats nap. Cats climb. Tax forms are due. Tax forms need receipts."
    vectors = {"Cats": [1.0, 0.0], "Tax": [0.0, 1.0]}
    pieces = chunk(
        text,
        {
            "chunkingStrategy": "SEMANTIC",
            "semanticChunkingConfiguration": {
                "maxTokens": 100,
                "bufferSize": 0,
                "breakpointPercentileThreshold": 75,
            },
        },
        words,
        embed=lambda texts: [vectors[t.split()[0]] for t in texts],
    )
    assert [p.text for p in pieces] == [
        "Cats purr. Cats nap. Cats climb.",
        "Tax forms are due. Tax forms need receipts.",
    ]


def test_truncation_renormalizes() -> None:
    vector = truncate([3.0, 4.0, 12.0], 2)
    assert vector == pytest.approx([0.6, 0.8])


# ------------------------------------------------------------- the whole job


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def load(self, folder: Path) -> None:
        self.objects = {f"kb/{p.name}": p.read_bytes() for p in sorted(folder.iterdir())}

    def get_paginator(self, _name: str) -> FakeS3:
        return self

    def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
        return [
            {
                "Contents": [
                    {"Key": k, "ETag": f'"{hashlib.md5(v).hexdigest()}"'}  # noqa: S324
                    for k, v in self.objects.items()
                    if k.startswith(Prefix)
                ]
            }
        ]

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeOpenSearch:
    def __init__(self) -> None:
        self.indexes: dict[str, dict[str, dict[str, Any]]] = {}

    def exists(self, path: str) -> bool:
        return path.lstrip("/") in self.indexes

    def request(self, method: str, path: str, body: Any = None, **_: Any) -> Any:
        name = path.lstrip("/").split("/")[0]
        if method == "PUT":
            self.indexes[name] = {}
            return {"acknowledged": True}
        if path.endswith("/_mget"):
            docs = self.indexes[name]
            return {
                "docs": [
                    {"_id": i, "found": i in docs, "_source": docs.get(i)} for i in body["ids"]
                ]
            }
        raise AssertionError(f"unexpected {method} {path}")

    def bulk(self, actions: list[dict[str, Any]]) -> None:
        it = iter(actions)
        for action in it:
            ((op, meta),) = action.items()
            docs = self.indexes[meta["_index"]]
            if op == "index":
                docs[meta["_id"]] = next(it)
            else:
                docs.pop(meta["_id"], None)

    @staticmethod
    def quote(index: str) -> str:
        return index


def phase0_setup() -> tuple[ControlPlane, Services, FakeS3, FakeOpenSearch, str, str, list[int]]:
    cp = ControlPlane(sqlite3.connect(":memory:"))
    kb = cp.create_knowledge_base(
        name="phase0",
        knowledge_base_configuration=cli.default_knowledge_base_configuration(4),
        storage_configuration=cli.default_storage_configuration(
            "bedrock-knowledge-base-default-index"
        ),
    )
    ds = cp.create_data_source(
        knowledge_base_id=kb["knowledgeBaseId"],
        name="docs",
        data_source_configuration={
            "type": "S3",
            "s3Configuration": {"bucketArn": "arn:aws:s3:::kb-data", "inclusionPrefixes": ["kb/"]},
        },
    )
    s3, opensearch, calls = FakeS3(), FakeOpenSearch(), [0]

    def embed(texts: list[str]) -> list[list[float]]:
        calls[0] += len(texts)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    services = Services(s3, opensearch, lambda _arn, _dims: embed, lambda t: len(t) // 4)  # type: ignore[arg-type]
    return cp, services, s3, opensearch, kb["knowledgeBaseId"], ds["dataSourceId"], calls


def test_phase0_sequence_reproduces_bedrocks_counters(tmp_path: Path) -> None:
    cp, services, s3, opensearch, kb_id, ds_id, calls = phase0_setup()

    s3.load(DOCS)
    first = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert first["status"] == "COMPLETE"
    assert first["statistics"] == fixture("ingestion-job-1.json")["statistics"]

    mutated = tmp_path / "kb"
    shutil.copytree(DOCS, mutated)
    (mutated / "winter-closure-2025.txt").unlink()
    with (mutated / "night-log-template.txt").open("a") as f:
        f.write("\nFrom November 2026, also record the moon phase.\n")
    sidecar = mutated / "dew-heater-guide.md.metadata.json"
    meta = json.loads(sidecar.read_text())
    meta["metadataAttributes"]["revision"]["value"]["numberValue"] = 4
    sidecar.write_text(json.dumps(meta))
    s3.load(mutated)
    dew_before = {
        i: d
        for i, d in opensearch.indexes["bedrock-knowledge-base-default-index"].items()
        if d["x-amz-bedrock-kb-source-uri"].endswith("dew-heater-guide.md")
    }
    calls[0] = 0

    second = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert second["statistics"] == fixture("ingestion-job-2.json")["statistics"]
    assert calls[0] == 1  # only the edited file is re-embedded, as on AWS
    docs = opensearch.indexes["bedrock-knowledge-base-default-index"]
    for _id in dew_before:  # the sidecar-only change rewrote in place: same _id, id = _id
        assert docs[_id]["revision"] == 4.0
        assert docs[_id]["id"] == _id
    assert not any(
        d["x-amz-bedrock-kb-source-uri"].endswith("winter-closure-2025.txt") for d in docs.values()
    )
    assert not has_changes(cp, s3, cp.get_data_source(kb_id, ds_id))


def test_stored_chunk_has_bedrocks_fields() -> None:
    cp, services, s3, opensearch, kb_id, ds_id, _ = phase0_setup()
    s3.load(DOCS)
    run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    docs = opensearch.indexes["bedrock-knowledge-base-default-index"]
    mine = next(
        d for d in docs.values() if d["x-amz-bedrock-kb-source-uri"].endswith("dew-heater-guide.md")
    )
    real = next(
        h["_source"]
        for h in fixture("index-documents.json")["hits"]
        if h["_source"]["x-amz-bedrock-kb-source-uri"].endswith("dew-heater-guide.md")
    )
    assert set(mine) - {"bedrock-knowledge-base-default-vector"} == set(real)
    assert {k: type(v) for k, v in mine.items() if k in real} == {
        k: type(v) for k, v in real.items()
    }
    assert mine["AMAZON_BEDROCK_METADATA"] == '{"source":"s3://kb-data/kb/dew-heater-guide.md"}'
    pdf = next(d for d in docs.values() if d["x-amz-bedrock-kb-source-uri"].endswith(".pdf"))
    assert pdf["x-amz-bedrock-kb-document-page-number"] == 1


def test_unreadable_file_is_counted_not_fatal() -> None:
    cp, services, s3, _, kb_id, ds_id, _ = phase0_setup()
    s3.objects = {"kb/notes.txt": b"Fine.", "kb/scan.tiff": b"\x00"}
    job = run_job(cp, cp.start_ingestion_job(kb_id, ds_id), services)
    assert job["status"] == "COMPLETE"
    assert job["statistics"]["numberOfNewDocumentsIndexed"] == 1
    assert job["statistics"]["numberOfDocumentsFailed"] == 1
    assert "unsupported" in job["failureReasons"][0]
