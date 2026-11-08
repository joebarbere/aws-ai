"""``local-kb bench``: the OpenSearch and RDS (pgvector) stores on the same chunks and vectors.

Methodology in local-kb/PGVECTOR_PLAN.md §3. In outline:

1. **Corpus.** ``sample`` is the 17 Phase 0 chunks; ``medium`` is ~240 radio astronomy papers
   from arXiv, parsed and chunked like any upload; ``large`` is one chunk per abstract of every
   radio-related astro-ph paper (python/scripts/radio_corpus.py fetches both). Chunks are cached
   in ``.state/bench/``.
2. **Vectors.** Embedded once with ``kb-embed`` and cached by sha256 of the text, so both stores
   load identical vectors and LM Studio stays out of every timing.
3. **Per store,** on a fresh index or table of its own:
   - load, timed, through the store's own write path (Bedrock's bulk or INSERT sequence);
   - build: OpenSearch's force merge, Postgres's ANALYZE, and separately a Postgres bulk load
     followed by CREATE INDEX;
   - quality: recall@k against brute-force cosine search (numpy), unfiltered and under a 2%
     filter, at several search-time settings;
   - latency: SEMANTIC, HYBRID and filtered SEMANTIC, single-threaded and concurrent;
   - a sidecar-only rewrite of 100 chunks;
   - disk: the store's own size figures, then the data volumes.
4. **Resources** throughout (bench_resources.py): RSS, CPU %, CPU-seconds per phase.

Quality is scored by chunk number (``bench_n``, a sidecar attribute every chunk carries), so it
works for both stores although only OpenSearch results carry a chunk id.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np

from aws_ai.local_kb import bench_resources, cli, config
from aws_ai.local_kb.chunking import chunk
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.opensearch import OpenSearch
from aws_ai.local_kb.parse import ParseError, parse_document
from aws_ai.local_kb.store import StoredChunk, store_for
from aws_ai.local_kb.store_rds import table_sql

CORPORA = ("sample", "medium", "large")
STORES = ("opensearch", "rds")
SHARDS = 50  # bench_shard = n % 50: an equals filter keeps 2% of the rows
FILTER = {"equals": {"key": "bench_shard", "value": 7}}
BATCH = 100


@dataclass(frozen=True)
class BenchChunk:
    text: str
    source_uri: str
    page: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Corpus:
    name: str
    chunks: list[BenchChunk]
    queries: list[str]


def repo_dir() -> Path:
    return config.default_state_dir().parent  # local-kb/


def bench_dir() -> Path:
    path = config.default_state_dir() / "bench"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------- corpora


def load_corpus(name: str, count_tokens: Callable[[str], int], queries: int = 200) -> Corpus:
    """The corpus's chunks and queries, built once and cached in .state/bench/."""
    cache = bench_dir() / f"{name}-corpus.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        chunks = [BenchChunk(**c) for c in data["chunks"]]
        return Corpus(name, chunks, data["queries"][:queries])
    build = {"sample": _sample, "medium": _medium, "large": _large}[name]
    chunks, all_queries = build(count_tokens)
    chunks = [
        BenchChunk(
            c.text, c.source_uri, c.page, {**c.attributes, "bench_n": n, "bench_shard": n % SHARDS}
        )
        for n, c in enumerate(chunks)
    ]
    cache.write_text(
        json.dumps(
            {"chunks": [asdict(c) for c in chunks], "queries": all_queries}, ensure_ascii=False
        )
    )
    return Corpus(name, chunks, all_queries[:queries])


def _parse_and_chunk(
    key: str, data: bytes, count_tokens: Callable[[str], int]
) -> list[tuple[str, int | None]]:
    parsed = parse_document(key, data)
    pdf = key.lower().endswith(".pdf")
    return [
        (c.text, parsed.page_at(c.start) if pdf else None)
        for c in chunk(parsed.text, None, count_tokens)
    ]


def _sample(count_tokens: Callable[[str], int]) -> tuple[list[BenchChunk], list[str]]:
    docs = repo_dir() / "sample-docs" / "kb"
    chunks = []
    for path in sorted(docs.iterdir()):
        if path.name.endswith(".metadata.json"):
            continue
        for text, page in _parse_and_chunk(path.name, path.read_bytes(), count_tokens):
            chunks.append(BenchChunk(text, f"s3://kb-data/kb/{path.name}", page))
    lines = (repo_dir() / "eval" / "qa.jsonl").read_text().splitlines()
    return chunks, [json.loads(line)["question"] for line in lines if line.strip()]


def _medium(count_tokens: Callable[[str], int]) -> tuple[list[BenchChunk], list[str]]:
    listing = repo_dir() / "bench" / "radio-papers.tsv"
    pdfs = config.default_state_dir() / "corpus" / "radio"
    papers = [
        line.split("\t")
        for line in listing.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    chunks, titles, failed = [], [], []
    for arxiv_id, _version, published, category, title in papers:
        path = pdfs / f"{arxiv_id}.pdf"
        if not path.exists():
            continue
        try:
            pieces = _parse_and_chunk(path.name, path.read_bytes(), count_tokens)
        except ParseError as exc:
            failed.append(f"{arxiv_id}: {exc}")
            continue
        attributes = {"arxiv_id": arxiv_id, "year": int(published[:4]), "category": category}
        uri = f"s3://kb-data/bench/radio/{arxiv_id}.pdf"
        chunks += [BenchChunk(text, uri, page, attributes) for text, page in pieces]
        titles.append(title)
    if failed:
        print(f"{len(failed)} papers failed to parse: {failed[:3]}")
    return chunks, _spread(titles)


def _large(_count_tokens: Callable[[str], int]) -> tuple[list[BenchChunk], list[str]]:
    path = config.default_state_dir() / "corpus" / "radio-abstracts.jsonl"
    chunks, titles = [], []
    for line in path.read_text().splitlines():
        paper = json.loads(line)
        attributes = {
            "arxiv_id": paper["id"],
            "year": int(paper["published"][:4]),
            "category": paper["category"],
        }
        text = f"{paper['title']}. {paper['abstract']}"
        chunks.append(
            BenchChunk(text, f"s3://kb-data/bench/abstracts/{paper['id']}", None, attributes)
        )
        titles.append(paper["title"])
    return chunks, _spread(titles)


def _spread(items: list[str], count: int = 1000) -> list[str]:
    """Up to ``count`` items evenly spaced through the list: queries from across the corpus."""
    step = max(1, len(items) // count)
    return items[::step][:count]


# ------------------------------------------------------------------- vectors


class VectorCache:
    """Embeddings by (model, sha256 of the text), in SQLite, as float32."""

    def __init__(self, path: Path, model: str, dimensions: int) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vectors "
            "(model TEXT, sha TEXT, vec BLOB, PRIMARY KEY (model, sha))"
        )
        self.model, self.dimensions = model, dimensions

    def get(
        self, texts: list[str], embed: Callable[[list[str]], list[list[float]]], batch: int = 64
    ) -> Any:
        """A (len(texts), dimensions) float32 array, embedding only what is missing."""
        shas = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        known = self._lookup(set(shas))
        missing = list(dict.fromkeys(s for s in shas if s not in known))
        by_sha = dict(zip(shas, texts, strict=True))
        started, done = time.monotonic(), 0
        for start in range(0, len(missing), batch):
            part = missing[start : start + batch]
            vectors = embed([by_sha[s] for s in part])
            with self.db:
                self.db.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                    [
                        (self.model, s, np.asarray(v, np.float32).tobytes())
                        for s, v in zip(part, vectors, strict=True)
                    ],
                )
            for s, v in zip(part, vectors, strict=True):
                known[s] = np.asarray(v, np.float32)
            done += len(part)
            if done % (batch * 20) < batch or done == len(missing):
                rate = done / max(time.monotonic() - started, 1e-9)
                print(f"  embedded {done}/{len(missing)} ({rate:.0f}/s)", flush=True)
        return (
            np.stack([known[s] for s in shas])
            if shas
            else np.zeros((0, self.dimensions), np.float32)
        )

    def _lookup(self, shas: set[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        rows = self.db.execute("SELECT sha, vec FROM vectors WHERE model = ?", (self.model,))
        for sha, blob in rows:
            if sha in shas:
                out[sha] = np.frombuffer(blob, np.float32)
        return out


# -------------------------------------------------------------------- stores


def knowledge_base(store: str, corpus: str, dimensions: int) -> dict[str, Any]:
    """A knowledge base description for the bench's own index or table, never a real KB's."""
    if store == "opensearch":
        storage = cli.default_storage_configuration(f"bedrock-knowledge-base-bench-{corpus}")
    else:
        storage = cli.default_rds_storage_configuration()
        storage["rdsConfiguration"]["tableName"] = f"bedrock_integration.bench_{corpus}"
    return {
        "storageConfiguration": storage,
        "knowledgeBaseConfiguration": cli.default_knowledge_base_configuration(dimensions),
    }


class Bench:
    def __init__(
        self,
        services: Services,
        corpus: Corpus,
        vectors: Any,
        queries: Any,
        log: Callable[[str], None],
    ) -> None:
        self.services, self.corpus, self.vectors, self.query_vectors = (
            services,
            corpus,
            vectors,
            queries,
        )
        self.log = log
        env = config.load_env()
        self.admin = OpenSearch(
            config.OPENSEARCH_ENDPOINT, "admin", env["OPENSEARCH_INITIAL_ADMIN_PASSWORD"]
        )
        self.dimensions = int(vectors.shape[1])
        unit = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        self.unit = unit.astype(np.float32)
        self.unit_queries = (queries / np.linalg.norm(queries, axis=1, keepdims=True)).astype(
            np.float32
        )
        shard = np.array([c.attributes["bench_shard"] for c in corpus.chunks])
        self.filtered_rows = np.flatnonzero(shard == FILTER["equals"]["value"])

    def store(self, kind: str) -> Any:
        """A fresh store, and with it a fresh connection. Typed Any: the bench also reaches
        the RDS store's connection (``.db``) for SET, ANALYZE, COPY and EXPLAIN."""
        return store_for(knowledge_base(kind, self.corpus.name, self.dimensions), self.services)

    # ------------------------------------------------------------ ground truth

    def exact(self, k: int, rows: Any = None) -> list[list[int]]:
        return exact_top_k(self.unit_queries, self.unit, k, rows)

    # ------------------------------------------------------------------ phases

    def reset(self, kind: str) -> None:
        kb = knowledge_base(kind, self.corpus.name, self.dimensions)
        if kind == "opensearch":
            name = kb["storageConfiguration"]["opensearchManagedClusterConfiguration"][
                "vectorIndexName"
            ]
            self.admin.request("DELETE", f"/{name}", ok_statuses=(404,))
        else:
            table = kb["storageConfiguration"]["rdsConfiguration"]["tableName"]
            db = self.store("rds").db
            db.execute(f"DROP TABLE IF EXISTS {table}")
            for sql in table_sql(table, self.dimensions):
                db.execute(sql)
            db.close()

    def load(self, kind: str) -> dict[str, Any]:
        store = self.store(kind)
        store.ensure()
        started = time.perf_counter()
        for start in range(0, len(self.corpus.chunks), BATCH):
            store.write(
                [
                    StoredChunk(
                        store.new_id(),
                        self.vectors[n].tolist(),
                        c.text,
                        c.source_uri,
                        "BENCHDS000",
                        c.page,
                        c.attributes,
                    )
                    for n, c in enumerate(self.corpus.chunks[start : start + BATCH], start)
                ]
            )
        seconds = time.perf_counter() - started
        return {
            "chunks": len(self.corpus.chunks),
            "seconds": round(seconds, 2),
            "chunks_per_second": round(len(self.corpus.chunks) / seconds, 1),
        }

    def build(self, kind: str) -> dict[str, Any]:
        """Make what was loaded searchable at its best: OpenSearch refreshes and merges to one
        segment (one faiss graph); Postgres's HNSW is built as rows arrive, so it ANALYZEs."""
        started = time.perf_counter()
        if kind == "opensearch":
            index = self._index()
            self.admin.request("POST", f"/{index}/_refresh")
            self.admin.request("POST", f"/{index}/_forcemerge?max_num_segments=1")
            self.admin.request("POST", f"/{index}/_refresh")
            return {"force_merge_seconds": round(time.perf_counter() - started, 2)}
        store = self.store("rds")
        store.db.execute(f"ANALYZE {self._table()}")
        return {"analyze_seconds": round(time.perf_counter() - started, 2)}

    def pg_bulk_build(self) -> dict[str, Any]:
        """The other way to fill pgvector: COPY into a table with no vector index, then CREATE
        INDEX — usually much faster than inserting into a live HNSW graph."""
        table = f"{self._table()}_bulk"
        store = self.store("rds")
        db = store.db
        db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute(
            f"CREATE TABLE {table} (id uuid PRIMARY KEY, embedding vector({self.dimensions}), "
            "chunks text, metadata json, custom_metadata jsonb)"
        )
        started = time.perf_counter()
        with db.cursor().copy(f"COPY {table} (id, embedding, chunks) FROM STDIN") as copy:
            for n, c in enumerate(self.corpus.chunks):
                copy.write_row(
                    (
                        store.new_id(),
                        "[" + ",".join(map(str, self.vectors[n].tolist())) + "]",
                        c.text,
                    )
                )
        copied = time.perf_counter()
        name = table.rpartition(".")[2]
        db.execute(
            f"CREATE INDEX {name}_embedding_idx ON {table} "
            "USING hnsw (embedding vector_cosine_ops) WITH (ef_construction=256)"
        )
        built = time.perf_counter()
        db.execute(f"DROP TABLE {table}")
        return {
            "copy_seconds": round(copied - started, 2),
            "create_index_seconds": round(built - copied, 2),
            "total_seconds": round(built - started, 2),
        }

    def quality(self, kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in (5, 20):
            truth = self.exact(k)
            for label, setup in self._search_settings(kind):
                store = self.store(kind)
                setup(store)
                got = [
                    self._numbers(store.semantic(q.tolist(), k, None)) for q in self.query_vectors
                ]
                out[f"recall@{k}{label}"] = _recall(got, truth, k)
        k = 10
        truth = self.exact(k, self.filtered_rows)
        for label, setup in self._filter_settings(kind):
            store = self.store(kind)
            setup(store)
            compiled = store.compile_filter(FILTER)
            got = [
                self._numbers(store.semantic(q.tolist(), k, compiled)) for q in self.query_vectors
            ]
            out[f"filtered_recall@{k}{label}"] = _recall(got, truth, k)
            out[f"filtered_returned@{k}{label}"] = round(statistics.mean(len(g) for g in got), 2)
            if kind == "rds":
                out[f"filtered_plan{label}"] = self._plan(store, compiled)
        return out

    def results(self, kind: str, k: int = 10) -> dict[str, list[list[int]]]:
        """Top-k chunk numbers per query, SEMANTIC and HYBRID, for the overlap between stores."""
        store = self.store(kind)
        return {
            "semantic": [
                self._numbers(store.semantic(q.tolist(), k, None)) for q in self.query_vectors
            ],
            "hybrid": [
                self._numbers(store.hybrid(text, q.tolist(), k, None))
                for text, q in zip(self.corpus.queries, self.query_vectors, strict=True)
            ],
        }

    def latency(self, kind: str, calls: int, concurrency: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        compiled = self.store(kind).compile_filter(FILTER)
        modes: dict[str, Callable[[Any, int], Any]] = {
            "semantic": lambda s, i: s.semantic(self._q(i), 5, None),
            "hybrid": lambda s, i: s.hybrid(
                self.corpus.queries[i % len(self.corpus.queries)], self._q(i), 5, None
            ),
            "filtered_semantic": lambda s, i: s.semantic(self._q(i), 5, compiled),
        }
        for mode, call in modes.items():
            store = self.store(kind)
            for i in range(20):  # warm caches and the connection
                call(store, i)
            single = []
            for i in range(calls):
                started = time.perf_counter()
                call(store, i)
                single.append(time.perf_counter() - started)
            out[mode] = {
                "single": _percentiles(single),
                "concurrent": self._concurrent(kind, call, calls, concurrency),
            }
        return out

    def sidecar_rewrite(self, kind: str, count: int = 100) -> dict[str, Any]:
        """A sidecar-only change touching ``count`` chunks: each store's read-and-rewrite.
        Runs last: it replaces those chunks' attributes, bench_n included."""
        store = self.store(kind)
        ids = self._chunk_ids(kind, count)
        started = time.perf_counter()
        store.rewrite_attributes(ids, {"revision": 2})
        return {"chunks": len(ids), "seconds": round(time.perf_counter() - started, 3)}

    def disk(self, kind: str) -> dict[str, Any]:
        if kind == "opensearch":
            index = self._index()
            (row,) = self.admin.request("GET", f"/_cat/indices/{index}?bytes=b&format=json")
            segments = self.admin.request("GET", f"/_cat/segments/{index}?format=json")
            return {
                "store_bytes": int(row["store.size"]),
                "docs": int(row["docs.count"]),
                "deleted_docs": int(row["docs.deleted"]),
                "segments": len(segments),
            }
        db = self.store("rds").db
        table = self._table()
        total, heap = db.execute(
            f"SELECT pg_total_relation_size('{table}'), pg_relation_size('{table}')"
        ).fetchone()
        indexes = db.execute(
            "SELECT indexrelid::regclass::text, pg_relation_size(indexrelid) FROM pg_index "
            f"WHERE indrelid = '{table}'::regclass"
        ).fetchall()
        return {"total_bytes": total, "heap_bytes": heap, "indexes": {n: b for n, b in indexes}}

    # ----------------------------------------------------------------- helpers

    def _q(self, i: int) -> list[float]:
        vector: list[float] = self.query_vectors[i % len(self.query_vectors)].tolist()
        return vector

    def _index(self) -> str:
        return f"bedrock-knowledge-base-bench-{self.corpus.name}"

    def _table(self) -> str:
        return f"bedrock_integration.bench_{self.corpus.name}"

    @staticmethod
    def _numbers(hits: list[Any]) -> list[int]:
        return [int(h.result["metadata"]["bench_n"]) for h in hits]

    def _search_settings(self, kind: str) -> list[tuple[str, Callable[[Any], None]]]:
        if kind == "opensearch":  # ef_search is an index setting: 512, from index.index_body
            return [("", _nothing)]
        return [(f"/ef_search={ef}", partial(_set, "hnsw.ef_search", ef)) for ef in (40, 512)]

    def _filter_settings(self, kind: str) -> list[tuple[str, Callable[[Any], None]]]:
        if kind == "opensearch":
            return [("", _nothing)]
        return [
            (f"/iterative_scan={mode}", partial(_set, "hnsw.iterative_scan", mode))
            for mode in ("relaxed_order", "off")
        ]

    def _plan(self, store: Any, compiled: tuple[str, list[Any]]) -> str:
        """How Postgres runs a filtered SEMANTIC query: through the HNSW index (where
        iterative scans matter) or the GIN index plus an exact sort."""
        sql, params = compiled
        q = "[" + ",".join(map(str, self._q(0))) + "]"
        rows = store.db.execute(
            f"EXPLAIN SELECT chunks FROM {self._table()} WHERE ({sql}) "
            "ORDER BY embedding <=> %s::vector LIMIT 10",
            [*params, q],
        ).fetchall()
        plan = " / ".join(r[0].strip() for r in rows)
        if "embedding_idx" in plan:
            return "hnsw"
        if "Bitmap" in plan:
            return "gin + exact sort"
        if "Seq Scan" in plan:
            return "seq scan + exact sort"
        return plan[:200]

    def _concurrent(
        self, kind: str, call: Callable[[Any, int], Any], calls: int, workers: int
    ) -> dict[str, Any]:
        stores = [self.store(kind) for _ in range(workers)]
        for s in stores:
            call(s, 0)
        timings: list[float] = []
        lock = threading.Lock()

        def work(w: int) -> None:
            mine = []
            for i in range(w, calls, workers):
                started = time.perf_counter()
                call(stores[w], i)
                mine.append(time.perf_counter() - started)
            with lock:
                timings.extend(mine)

        started = time.perf_counter()
        threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - started
        return {**_percentiles(timings), "workers": workers, "qps": round(calls / wall, 1)}

    def _chunk_ids(self, kind: str, count: int) -> list[str]:
        if kind == "opensearch":
            body = {"size": count, "_source": False, "query": {"match_all": {}}}
            hits = self.admin.request("POST", f"/{self._index()}/_search", body)["hits"]["hits"]
            return [h["_id"] for h in hits]
        rows = (
            self.store("rds")
            .db.execute(f"SELECT id::text FROM {self._table()} LIMIT {count}")
            .fetchall()
        )
        return [r[0] for r in rows]


def exact_top_k(queries: Any, matrix: Any, k: int, rows: Any = None) -> list[list[int]]:
    """Brute-force cosine top-k row numbers per query, best first, optionally only among
    ``rows``. Both arrays must already be unit length."""
    candidates = matrix if rows is None else matrix[rows]
    k = min(k, candidates.shape[0])
    scores = queries @ candidates.T
    top = np.argpartition(-scores, k - 1, axis=1)[:, :k]
    order = np.take_along_axis(scores, top, axis=1).argsort(axis=1)[:, ::-1]
    best = np.take_along_axis(top, order, axis=1)
    return [[int(rows[i]) if rows is not None else int(i) for i in row] for row in best]


def _nothing(_store: Any) -> None:
    return None


def _set(setting: str, value: object, store: Any) -> None:
    """A search setting for the RDS store's session (its connection lives as long as it)."""
    store.db.execute(f"SET {setting} = {value}")


def _recall(got: list[list[int]], truth: list[list[int]], k: int) -> float:
    scores = [len(set(g) & set(t)) / min(k, len(t)) for g, t in zip(got, truth, strict=True) if t]
    return round(statistics.mean(scores), 4)


def _percentiles(seconds: list[float]) -> dict[str, float]:
    ms = sorted(s * 1000 for s in seconds)
    return {
        "calls": len(ms),
        "p50_ms": round(ms[len(ms) // 2], 2),
        "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 2),
        "mean_ms": round(statistics.mean(ms), 2),
    }


def overlap(a: list[list[int]], b: list[list[int]]) -> dict[str, float]:
    """Jaccard of the two stores' top-k per query, and how often they agree on the first."""
    jaccard = [
        len(set(x) & set(y)) / len(set(x) | set(y)) for x, y in zip(a, b, strict=True) if x or y
    ]
    first = [x[:1] == y[:1] for x, y in zip(a, b, strict=True)]
    return {
        "jaccard": round(statistics.mean(jaccard), 4),
        "same_first": round(sum(first) / len(first), 4),
    }


# ------------------------------------------------------------------- the run


def environment() -> dict[str, Any]:
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    mem_kb = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal")
    )
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "cpu": cpu,
        "cores": os.cpu_count(),
        "ram_bytes": mem_kb * 1024,
        "kernel": platform.release(),
        "images": bench_resources.image_sizes(),
        "parameters": {
            "opensearch": {
                "engine": "faiss",
                "space": "l2",
                "m": 16,
                "ef_construction": 512,
                "ef_search": 512,
            },
            "rds": {
                "m": 16,
                "ef_construction": 256,
                "ef_search": [40, 512],
                "iterative_scan": ["relaxed_order", "off"],
            },
            "hybrid": {
                "opensearch": "min-max, arithmetic mean 0.5/0.5, pool 50",
                "rds": "Bedrock's RRF, k=60, 2k per side",
            },
            "filter": FILTER,
        },
    }


def run(
    services: Services,
    corpus_name: str,
    stores: list[str],
    queries: int = 200,
    calls: int = 500,
    concurrency: int = 8,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    from aws_ai.local_kb.embed import model_id, qwen_token_counter

    log(f"corpus {corpus_name}: building or loading chunks")
    corpus = load_corpus(corpus_name, qwen_token_counter(), queries)
    log(f"  {len(corpus.chunks)} chunks, {len(corpus.queries)} queries")
    cache = VectorCache(
        bench_dir() / "vectors.sqlite",
        model_id(config.EMBEDDING_MODEL_ARN),
        config.EMBEDDING_DIMENSIONS,
    )
    embed = services.embedder(config.EMBEDDING_MODEL_ARN, config.EMBEDDING_DIMENSIONS)
    vectors = cache.get([c.text for c in corpus.chunks], embed)
    query_vectors = cache.get(corpus.queries, embed)
    bench = Bench(services, corpus, vectors, query_vectors, log)

    report: dict[str, Any] = {
        "corpus": {
            "name": corpus_name,
            "chunks": len(corpus.chunks),
            "queries": len(corpus.queries),
            "characters": sum(len(c.text) for c in corpus.chunks),
            "filtered_rows": int(len(bench.filtered_rows)),
        },
        "environment": environment(),
        "stores": {},
    }
    top: dict[str, dict[str, list[list[int]]]] = {}
    with bench_resources.Sampler() as sampler:
        time.sleep(3)  # an idle baseline
        for kind in stores:
            result: dict[str, Any] = {}

            def phase(
                name: str,
                step: Callable[[], Any],
                kind: str = kind,
                result: dict[str, Any] = result,
            ) -> None:
                sampler.phase(f"{kind}:{name}")
                log(f"{kind}: {name}")
                result[name] = step()

            phase("reset", partial(bench.reset, kind))
            phase("load", partial(bench.load, kind))
            phase("build", partial(bench.build, kind))
            phase("quality", partial(bench.quality, kind))
            top[kind] = bench.results(kind)
            phase("latency", partial(bench.latency, kind, calls, concurrency))
            phase("sidecar_rewrite", partial(bench.sidecar_rewrite, kind))
            phase("disk", partial(bench.disk, kind))
            if kind == "rds":
                phase("bulk_build", bench.pg_bulk_build)
            del result["reset"]
            report["stores"][kind] = result
        sampler.phase("idle-after")
        time.sleep(3)
    report["resources"] = sampler.summary()
    report["volumes"] = bench_resources.volume_bytes()
    if set(STORES) <= set(top):
        report["overlap@10"] = {
            mode: overlap(top["opensearch"][mode], top["rds"][mode])
            for mode in ("semantic", "hybrid")
        }
    return report


def iter_lines(report: dict[str, Any]) -> Iterator[str]:
    """A short human summary of a report."""
    c = report["corpus"]
    yield f"{c['name']}: {c['chunks']} chunks, {c['queries']} queries"
    for kind, r in report["stores"].items():
        yield f"\n{kind}"
        yield f"  load      {r['load']['chunks_per_second']} chunks/s ({r['load']['seconds']} s)"
        yield f"  build     {r['build']}"
        if "bulk_build" in r:
            yield f"  bulk      {r['bulk_build']}"
        for key, value in r["quality"].items():
            yield f"  {key:<42} {value}"
        for mode, lat in r["latency"].items():
            s, cc = lat["single"], lat["concurrent"]
            yield (
                f"  {mode:<18} p50 {s['p50_ms']} ms  p95 {s['p95_ms']} ms   "
                f"x{cc['workers']}: p50 {cc['p50_ms']} ms  {cc['qps']} qps"
            )
        yield f"  sidecar   {r['sidecar_rewrite']}"
        yield f"  disk      {r['disk']}"
    for mode, o in report.get("overlap@10", {}).items():
        yield f"\noverlap@10 {mode}: {o}"
    yield "\nresources (peak RSS MB, CPU-s):"
    for label, phase in report["resources"].items():
        cells = []
        for name, v in phase["containers"].items():
            short = name.removeprefix("local-kb_").removesuffix("_1")
            peak = f"{v['rss_peak'] / 1e6:.0f}" if v["rss_peak"] else "-"
            cpu = f"{v['cpu_seconds']:.1f}" if v["cpu_seconds"] is not None else "-"
            cells.append(f"{short} {peak}/{cpu}")
        yield f"  {label:<26} {phase['seconds']:>7}s  " + "  ".join(cells)
