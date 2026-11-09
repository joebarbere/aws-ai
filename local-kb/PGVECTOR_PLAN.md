# local-kb on PostgreSQL + pgvector: plan and comparison with OpenSearch

**Goal.** Add a second vector store to local-kb, PostgreSQL with pgvector. Then measure it
against the OpenSearch store already built:

- retrieval results;
- answer quality;
- latency;
- memory, CPU and disk use.

Finally, write down the pros and cons of running each one locally.

**Why this is not a side quest.** Bedrock itself supports pgvector. The Aurora PostgreSQL
Serverless store is `storageConfiguration.type = "RDS"`, and the console offers "Quick create"
for it. So this is not a made-up backend: it is the second store Bedrock ships. The same rules
apply as for the OpenSearch work:

- Follow AWS's documented schema exactly.
- Capture what the docs leave out from a real knowledge base before guessing.
- Mark every guess as a guess.

---

## 0. What AWS documents for the RDS store

Sources:

- the Aurora user guide, *Using Aurora PostgreSQL as a knowledge base for Amazon Bedrock*;
- the Bedrock user guide, *knowledge-base-setup*, Aurora tab;
- the API reference for `RdsConfiguration` and `RdsFieldMapping`.

**Versions.**

- Aurora PostgreSQL 12.16+, 13.12+, 14.9+, 15.4+ or 16.1+.
- pgvector **0.5.0 or later**, the first release with HNSW.
- `ef_construction=256` is recommended from 0.6.0, which builds indexes in parallel.
- Iterative index scans need 0.8.0.

**The SQL AWS tells you to run**, verbatim except that `{{n}}` becomes 1024:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA bedrock_integration;
CREATE ROLE bedrock_user WITH PASSWORD '...' LOGIN;
GRANT ALL ON SCHEMA bedrock_integration to bedrock_user;
-- as bedrock_user:
CREATE TABLE bedrock_integration.bedrock_kb (
  id uuid PRIMARY KEY, embedding vector(1024), chunks text, metadata json, custom_metadata jsonb);
CREATE INDEX ON bedrock_integration.bedrock_kb USING hnsw (embedding vector_cosine_ops)
  WITH (ef_construction=256);
CREATE INDEX ON bedrock_integration.bedrock_kb USING gin (to_tsvector('simple', chunks));
CREATE INDEX ON bedrock_integration.bedrock_kb USING gin (custom_metadata);
-- optional, pgvector >= 0.8.0, for filtered queries:
ALTER DATABASE postgres SET hnsw.iterative_scan = 'relaxed_order';
ALTER DATABASE postgres SET hnsw.max_scan_tuples = 20000;
```

**The API.**

- `rdsConfiguration` has five required fields: `resourceArn` (a cluster ARN),
  `credentialsSecretArn`, `databaseName`, `tableName` and `fieldMapping`.
- `fieldMapping` has four required fields: `primaryKeyField`, `vectorField`, `textField` and
  `metadataField`. `customMetadataField` is optional.
- The field mapping cannot be changed after the knowledge base is created.

**Metadata filtering.** There are two documented ways to store sidecar attributes:

- **Recommended:** one `custom_metadata jsonb` column that Bedrock fills from the
  `.metadata.json` files.
- **Alternative:** one typed column per attribute (text, number or boolean), with no
  `custom_metadata` column.

**Access.** The RDS Data API must be enabled, and the credentials live in Secrets Manager.

**Differences from the OpenSearch store that matter for faithfulness:**

| | OpenSearch (current) | RDS / pgvector |
|---|---|---|
| Primary key | `_id` = `1%3A0%3A` + 20 characters | `id uuid` (uuid4); results carry **no** chunk ID |
| Distance and score | faiss l2; score `1/(1+d²)`, best first | `<=>` cosine; score is the **raw distance `1 − cos`, returned worst-first** |
| Sidecar attributes | top-level fields, dynamic mapping | `custom_metadata` jsonb, or typed columns |
| `x-amz-bedrock-kb-*` fields | top-level fields in each document | inside `custom_metadata`, next to the sidecar attributes |
| Metadata column | `AMAZON_BEDROCK_METADATA` = `{"source":…}` | `metadata` = `{"sourceUrl": …}` |
| HYBRID | `match` (BM25) fused with k-NN (fusion not published) | RRF (k = 60, × 61/2) of the top 10 by `<=>` and the top 10 by `ts_rank` over `plainto_tsquery('simple', …)` |
| Consistency | AOSS is eventually consistent (Phase 0 saw a stale window) | settled when the job reports COMPLETE; an edit is delete-then-insert, not atomic |

## 1. Phase 0b: capture a real Aurora knowledge base ✅ done

Captured on 2026-11-08. The setup:

- Aurora PostgreSQL 17.11 with pgvector 0.8.2, Serverless v2 at 0.5–2 ACU, with the Data API on.
- `log_statement = all`, exported to CloudWatch.
- The same corpus, the same two syncs and the same queries as Phase 0.

The cluster ran for about an hour, cost about $0.10, and is destroyed. The tooling is
`terraform/modules/knowledge_base_rds` and `python/scripts/rds_kb.py`, and the capture command
is `capture_kb_fixtures.py --store rds`. The fixtures are in `local-kb/fixtures/rds/`; its
README has the details and Bedrock's SQL verbatim.

**What it settled:**

- **Ingestion matches OpenSearch exactly:**
  - the same 8 counters for both jobs;
  - the same 17 chunk texts;
  - sidecar-only changes rewrite in place under the same `id`, with no embedding call.
- **Row layout:**
  - `id` is a uuid4;
  - `metadata` is `{"sourceUrl": "s3://…"}`;
  - `custom_metadata` holds the sidecar attributes **and** `x-amz-bedrock-kb-source-uri`,
    `-data-source-id`, `-source-file-modality` and `-document-page-number`;
  - numbers are stored as json floats.
- **Writes:** `INSERT … ON CONFLICT(id) DO UPDATE … RETURNING NULL`.
  - A multi-chunk document goes in inside one transaction.
  - An edit is `DELETE … WHERE id IN (…)` followed by inserts, as separate statements.
  - A sidecar-only change reads the row (`SELECT id, embedding, …`) and upserts it.
- **Validation** runs at knowledge base creation and at the start of every sync:
  - a `<=>` probe;
  - the column types, from `pg_attribute`;
  - three `pg_indexes … LIKE` checks (text GIN with `simple` or `english`, HNSW
    `vector_cosine_ops`, and the `custom_metadata` GIN index).
- **SEMANTIC** is `SELECT chunks, metadata, embedding <=> $q as score, custom_metadata … ORDER
  BY score LIMIT k`. Bedrock then **returns the k rows worst-first, with `score` = cosine
  distance**. This looks like a Bedrock bug: distances sorted as similarities. The set is right;
  the order and the meaning of the score are inverted.
- **HYBRID** is reciprocal rank fusion in one SQL statement, using `rank()` over `<=>` and over
  `ts_rank(to_tsvector('simple', chunks), plainto_tsquery('simple', text))`.
  - Each side contributes its top 10 (for k = 5).
  - The fused score is `Σ 1/(60 + rank) × 61/2`, so a rank-1 hit on both sides scores 1.0.
  - Results come back best-first.
- **Filters** become jsonb conditions on `custom_metadata`:
  - `equals` on a string is `@> {"k":"v"} or @> {"k":["v"]}`;
  - `equals` on a number or boolean is a plain `@>`;
  - `startsWith` is `->> 'k' like 'v%'`;
  - numeric ranges are `(->> 'k')::double precision >= 20261001.000000`, with the value
    formatted into the SQL;
  - `listContains` is `@> {"k":["v"]}`;
  - all four filter fixtures return the same chunks as on OpenSearch.
- **Retrieve results** have `metadata` equal to `custom_metadata`, with no
  `x-amz-bedrock-kb-chunk-id`.

**Still open:** whether the hybrid sides' `LIMIT 10` means `2k` or a fixed 10 (only k = 5 was
captured). The capture also never exercised `in`, `notEquals`, `notIn`, `stringContains`,
`orAll`, or other ranges, so their SQL follows the same pattern and is marked `# GUESS`.

## 2. Design: a vector-store seam, then a second store

Today OpenSearch is wired into three places:

- `ingest.Target` and the `_Sync` writes (`bulk` index, delete and rewrite);
- `retrieve._semantic` and `_hybrid`;
- `filters.to_query`.

`index.py` and `opensearch.py` hold the rest.

**Phase P1: extract a `VectorStore` protocol** ✅ done 2026-11-08. There is no behaviour change,
and all 141 tests pass unchanged.

- `store.py` holds the protocol, `StoredChunk`, `Hit` and `store_for()`.
- `store_opensearch.py` holds the code that moved out of `ingest.py` and `retrieve.py`.
- Live check against the running stack:
  - a sidecar-only round trip counted `MetadataDocumentsModified` 1 each way and kept the
    `_id`;
  - the evaluation scored 90/85/90 for SEMANTIC and 100/95/100 for HYBRID, as before, with
    SEMANTIC returning identical lists.
- HYBRID returned different lists for 5 of the 20 questions. The cause is the index, not the
  code: the rewritten chunk's deleted copies shift BM25 statistics until segments merge. Old and
  new code return identical results on the same index.

The interface as built:

```python
class VectorStore(Protocol):
    dimensions: int
    def ensure(self) -> None: ...
    def new_id(self) -> str: ...                       # "1%3A0%3A…" on OpenSearch, uuid4 on RDS
    def write(self, chunks: list[StoredChunk]) -> None: ...
    def rewrite_attributes(self, chunk_ids: list[str], attributes: dict) -> None: ...  # sidecar-only
    def delete(self, chunk_ids: list[str]) -> None: ...
    def delete_data_source(self, data_source_id: str) -> None: ...
    def compile_filter(self, retrieval_filter: dict) -> Any: ...   # validates; store's own form
    def semantic(self, vector, k, compiled_filter) -> list[Hit]: ...
    def hybrid(self, text, vector, k, compiled_filter) -> list[Hit]: ...
```

- `StoredChunk` is a store-neutral record holding:
  - the id, vector and text;
  - the source URI, data source and page;
  - the sidecar attributes;
  - the parent text.
- Each store serializes it its own way.
- `Hit` is a Retrieve result already shaped the way Bedrock returns it from that store, plus the
  parent text for HIERARCHICAL collapsing.
- `store_for(kb, services)` dispatches on `storageConfiguration.type`.
- `compile_filter` validates, then translates. OpenSearch uses `filters.to_query`; RDS gets
  `to_sql`.

**Phase P2: the Postgres service** ✅ done 2026-11-08.

- `postgres` in `compose.yaml`:
  - image `docker.io/pgvector/pgvector:0.8.2-pg17`: the pgvector version the Aurora capture
    reported (0.8.2), on Postgres 17.10 against Aurora's 17.11;
  - port `127.0.0.1:5432`, the `postgres-data` volume, database `bedrock`;
  - `shared_buffers=256MB` and `maintenance_work_mem=512MB` for HNSW builds, and
    `default_text_search_config=simple`, as on Aurora (the image defaults to english).
- It starts with the rest of the stack, so both stores can be benchmarked side by side. There
  is no compose profile: to run one store alone, start only its services, for example
  `podman-compose up -d rustfs kb-events postgres`.
- `local_init.py pg` runs `rds_kb.py`'s SQL, so AWS's setup lives in one place. The extension,
  schema and `bedrock_user` are created as the superuser (Aurora's master user), then the table
  and its three indexes as `bedrock_user`, so it owns them as on AWS. `init` runs it too.
  `local_init.py env` now adds `POSTGRES_PASSWORD` and `BEDROCK_PG_PASSWORD` to an existing
  `.env`, without touching the keys already there.
- Against the capture's own schema queries, the columns, the indexes and the pgvector version
  are identical to Aurora's, and all three of Bedrock's `pg_indexes LIKE` checks match. The
  `<=>` probe runs.
- **`hnsw.iterative_scan`:** `table-schema.json` shows `off`, but that dump came from a pooled
  Data API connection (pid 922) opened before `rds_kb.py`'s `ALTER DATABASE`. Bedrock's own
  session (pid 923) connected a second after the `ALTER`, so it most likely ran with
  `relaxed_order`. Local sessions get `relaxed_order` too.
- The control plane accepts `type: "RDS"`. It checks `rdsConfiguration` against the botocore
  model's patterns and requires `customMetadataField`, the only layout local-kb copies. The
  table checks Bedrock also runs at create time need a database connection, so the store runs
  them at the start of each sync instead, which Bedrock also does.
- `local-kb create-knowledge-base --store rds` describes the local Postgres as Aurora:
  - `arn:aws:rds:us-east-1:000000000000:cluster:local-kb`;
  - a placeholder secret ARN, with the real password kept in `.env`;
  - the database, table and field mapping from `fixtures/rds/knowledge-base.json`.
- `psycopg[binary]` 3 is in the `local` extra.
- A sync of an RDS knowledge base fails until P3 adds the store.

**Phase P3: the pgvector store, ingest side** ✅ done 2026-11-08. `store_rds.py`.

Every statement is one Bedrock ran in Phase 0b, with `$n` written as `%s`:

- **Insert:** `INSERT … ON CONFLICT(id) DO UPDATE … RETURNING NULL`, one statement per chunk. A
  document with several chunks goes in inside one `BEGIN`/`COMMIT`; a one-chunk document is a
  lone, autocommitted `INSERT`. Each row has:
  - `id`: a uuid4;
  - `embedding`: the vector, as a `'[...]'::vector` literal;
  - `chunks`;
  - `metadata`: compact json `{"sourceUrl": uri}`, plus `parentText` for hierarchical chunking
    (`# GUESS`: not captured);
  - `custom_metadata`: the sidecar attributes with numbers as floats, plus the
    `x-amz-bedrock-kb-*` fields, with the page number left an integer, as on Aurora.
- **Sidecar-only change:** Bedrock's `SELECT id, embedding, chunks, metadata, custom_metadata …
  WHERE id IN ('…')` with the ids inlined, then an upsert under the same `id`.
- **Edited document:** parse and embed first, then `DELETE … WHERE id IN (…) RETURNING NULL`,
  then the inserts, as separate statements. Not atomic, as on AWS.
- **Removed documents are handled first,** before any changed document, as Bedrock's second sync
  did. The reorder applies to both stores; OpenSearch's counters and results are unchanged.
- **`ensure()` validates and never creates,** with Bedrock's own probe, `pg_attribute` and three
  `pg_indexes LIKE` queries. A missing index or a wrong column type fails the sync. The error
  wording is local-kb's, because the capture only saw checks that passed.
- **Data source delete** (`# GUESS`, not captured): `DELETE … WHERE custom_metadata @> {ds id}`.
- `Services.postgres` connects by database name as `bedrock_user`, in autocommit mode, as each
  Data API call is.

Verification:

- **Offline**, a recording fake connection runs the Phase 0b sequence with Qwen's tokenizer:
  - the 8 counters for both jobs match;
  - every write statement matches the capture's text;
  - the transaction groupings match job 1's;
  - job 2's writes run `DELETE, SELECT, INSERT, DELETE, INSERT`, Bedrock's exact order;
  - the 17 and 16 chunk texts match.
- **Against the local Postgres,** the same sequence on a throwaway `vector(4)` copy of AWS's
  table gives the same counters, texts and key sets. The test skips if Postgres is not up.
- **Live, with LM Studio:** `pg-kb` synced the sample bucket into 17 rows of 1024-dim unit
  vectors. Its rows have the same `metadata` and `custom_metadata` as Aurora's, apart from the
  bucket name and data source id. A sidecar edit and revert each counted
  `numberOfMetadataDocumentsModified = 1`, and the row kept its id.
- Unlike OpenSearch, superseded row versions do not skew HYBRID: `ts_rank` scores a row on its
  own, while BM25 uses statistics across the whole index.

**Phase P4: the pgvector store, query side** ✅ done 2026-11-08.

- **SEMANTIC,** Bedrock's statement:

  ```sql
  SELECT chunks, metadata, embedding <=> %s::vector as score, custom_metadata
  FROM t [WHERE (<filter>)] ORDER BY score LIMIT k;
  ```

  - The store returns the k rows best-first. Retrieve reverses them after trimming, because
    the store sets `semantic_worst_first`; the score stays the distance.
    `LOCAL_KB_RDS_FIX_ORDER=1` turns the reversal off.
  - **RetrieveAndGenerate gets the reversed list too.** Phase 0b's invocation log shows the
    same five passages as on OpenSearch Serverless, in exactly reverse order.
  - Unfiltered, the planner uses the HNSW index, with `iterative_scan = relaxed_order`. With a
    selective filter, it picks the GIN index on `custom_metadata` and sorts exactly. Benchmark
    both, together with `iterative_scan = off`.
- **Filters → SQL** (`filters.to_sql`). Validation is shared with OpenSearch's `to_query`, so
  both stores refuse the same filters.
  - The captured forms are used verbatim.
  - The `# GUESS` forms are built from the same pieces:
    - `notEquals` is `NOT (equals)`, which also matches rows without the key;
    - `in` and `notIn` are or-ed equals, plain or negated;
    - `stringContains` is `like` on the string or on any member of a string list;
    - `orAll` is `( a or b )`.
  - LIKE wildcards in a value are escaped (also a GUESS).
- **HYBRID:** Bedrock's RRF statement, with the vector and query text bound rather than
  inlined.
  - Each side takes `LIMIT 2k` (`# GUESS` for k ≠ 5).
  - A filter goes on both sides (`# GUESS`).
  - Results come back best-first.
- **Results:** `metadata` is the `custom_metadata` column with every number as a float (the page
  number too) and no chunk id. RetrieveAndGenerate's de-duplication falls back to source plus
  text when a result has no chunk id.

Verification:

- **Offline:**
  - each of the 8 Retrieve fixture requests issues a statement identical to Bedrock's;
  - the order is reversed by default, and kept with the switch;
  - results have the fixture's shape.
- **Against the local Postgres,** loaded with Phase 0b's 17 rows and their real
  `custom_metadata`:
  - 18 filters, covering every operator with the GUESS ones included, select exactly what a
    Python reference of OpenSearch's semantics selects;
  - SEMANTIC equals brute-force cosine order and distance;
  - HYBRID scores equal `30.5/(60 + rank)` summed over both sides.
- **Live `pg-kb` against the local OpenSearch `study-kb`,** on the 8 Aurora fixture queries:
  - every SEMANTIC result set is identical to OpenSearch's, in exactly reverse order, which is
    how Aurora relates to OpenSearch Serverless;
  - the four filters return Aurora's chunk sets;
  - `ZX-4471` ranks the dew heater guide first under HYBRID. It scores 0.9841, because Qwen
    puts it third on the vector side where Titan put it first.
- **The 20-question evaluation:**
  - SEMANTIC scores 90/85/90, the same as OpenSearch.
  - HYBRID scores 90/85/90, against OpenSearch's 100/95/100. With `plainto_tsquery`, neither
    miss ("emergency stop", "raw frames kept") matches lexically, so hybrid falls back to the
    vector ranking, as predicted.
- RetrieveAndGenerate answers the fuse and strap questions correctly, citing the dew heater
  guide.

**Phase P5: API parity** ✅ done 2026-11-08.

- **Stock boto3, end to end** (`tests/test_local_kb_rds_lifecycle.py`, the gate). `local-kb
  serve` runs in-process on a free port, with a throwaway state directory and table, against
  the live RustFS, Postgres and LM Studio. The test:
  - creates an RDS knowledge base and an S3 data source;
  - starts an ingestion job and polls it to COMPLETE;
  - runs SEMANTIC (worst-first distances, no chunk id), HYBRID (`ZX-4471` first) and a filter;
  - gets a server-side ValidationException;
  - runs RetrieveAndGenerate, whose answer contains `ZX-4471`;
  - deletes the data source, which empties the table under policy DELETE;
  - deletes the knowledge base and gets ResourceNotFound.

  It takes about 7 s, and skips when any part of the stack is down.
- **One table per RDS knowledge base,** as on AWS: two knowledge bases on one table would see
  each other's rows. `local_init.py pg --table bedrock_integration.<name> [--dimensions N]`
  creates another. `rds_kb.table_sql()` names the indexes after the table, and the default's
  names are unchanged.
- **The smoke test** gains 8 Postgres checks:
  - pgvector 0.8+;
  - `relaxed_order`;
  - the `simple` text search default;
  - the default table passes Bedrock's validation;
  - a write found at once at distance 0;
  - HYBRID scores an exact code 1.0;
  - a sidecar rewrite keeps the id;
  - a data source delete empties the rows.
- **Tests:**
  - `test_local_kb_stores.py` runs one contract over both stores: the Phase 0 counters for
    both syncs, re-embedding only the edited file, the sidecar rewrite in place, edits and
    deletes, the data source purge, and the same filters accepted and refused;
  - unit tests for the filter SQL;
  - the live Postgres tests from P3 and P4 skip when Postgres is down.
- **`pg/inspect.sql`** (`make local-pg-inspect [TABLE=…]`) is the Dashboards view's counterpart.
  It shows:
  - chunks per file;
  - Bedrock's `x-amz-bedrock-kb-*` fields, one row per chunk and field;
  - sidecar attributes with their JSON types;
  - the `metadata` keys;
  - vector dimensions and norms;
  - indexes with sizes;
  - live and dead rows (the sidecar round trip left 2 dead);
  - the search settings.

  `make local-psql` opens psql as `bedrock_user`, with the password passed through the
  environment. No browser UI: psql covers it, and adminer would add a container for little
  gain.

## 3. The comparison (Phases P6–P7)

**Phase P6: the tooling** ✅ done 2026-11-08. This is what was built; the design notes below
are kept as written.

- **Corpora: radio astronomy, from arXiv,** in place of a generic text set
  (`python/scripts/radio_corpus.py`, `make local-corpus`). Only the arXiv ids are committed
  (`local-kb/bench/radio-papers.tsv`); the PDFs and abstracts stay in gitignored `.state/`.
  Downloads follow arXiv's API terms: `export.arxiv.org`, one request every 3 s.
  - `medium`: 240 astro-ph.IM papers on radio instrumentation, every 7th of 1,666 matches from
    2015 to October 2026. 232 downloaded (8 were over 20 MB) and gave **13,602 chunks** through
    the normal PDF parser and default chunking. The queries are 200 paper titles.
  - `large`: **49,950 abstracts**, one chunk each ("title. abstract"), of radio-related astro-ph
    papers from 1993 to October 2026, with 200 titles as queries. That is about 3.7× medium
    (68 M characters against 14.6 M), not the 10× planned: arXiv has no more radio abstracts.
    It was 26,278 from 2015 until P7 widened the window.
  - `sample`: the 17 Phase 0 chunks, with the 20 evaluation questions as queries.
- **`local-kb bench --corpus … [--stores …]`** (`make local-bench CORPUS=…`, `bench.py`):
  - Vectors are cached in SQLite by model and sha256 of the text (`.state/bench/vectors.sqlite`),
    so both stores load identical vectors and LM Studio is out of every timing.
  - Each store gets its own index or table, `…-bench-<corpus>`, recreated each run. Every chunk
    carries `bench_n`, so results are scored by chunk number even though RDS results have no
    chunk id. It also carries `bench_shard = n % 50`, so the filter keeps 2% of rows.
  - Phases: load through each store's own write path; build (OpenSearch force merge, Postgres
    ANALYZE, and a separate Postgres COPY then CREATE INDEX); recall@5 and recall@20 against
    numpy brute force; filtered recall@10; latency p50/p95, single-threaded and with 8 workers;
    a sidecar rewrite of 100 chunks; disk.
  - Postgres runs at `ef_search` 40 and 512, and with `iterative_scan` relaxed and off, and the
    report records which plan Postgres chose for the filtered query.
  - `bench_resources.py` samples `podman stats` every second, tagged by phase, and reads
    CPU-seconds per phase from each container's cgroup `cpu.stat`. It also records image sizes
    and data-volume sizes (`podman unshare du`).
  - The report goes to `local-kb/bench/report-<corpus>.json`, with the hardware, image tags and
    parameters.
- **Fixed on the way.** The first two would have made OpenSearch look slower than it is; the
  last three are real local-kb bugs the radio papers exposed:
  - The OpenSearch client opened a new HTTPS connection for every request. It now keeps one per
    thread: SEMANTIC p50 went from 4.0 to 1.2 ms.
  - The hybrid store re-sent its pipeline before every query. It now sends it once per client:
    HYBRID p50 went from 13.3 to 1.7 ms on `sample`.
  - A PDF page pypdf cannot lay out (`LimitReachedError` on a huge figure stream) failed the
    whole ingestion job. It now falls back to plain extraction, then to an empty page, and an
    unreadable PDF counts as one failed document.
  - Lone UTF-16 surrogates from math fonts (arXiv 2506.22617, 133 of them) crashed the
    tokenizer.
  - NUL bytes in extracted text were accepted by OpenSearch but refused by Postgres.

    The parser now replaces NUL and surrogates with U+FFFD, one character for one, in every
    format.
- **Added in P7:**
  - `--isolate` (`make local-bench ISOLATE=1`) stops Dashboards and both stores, then starts
    one store at a time, so each is measured alone. It restarts whatever was running when it
    finishes. Cold start is timed from `podman start` to ready: Postgres accepting a
    `bedrock_user` connection, or an authenticated cluster-health call returning yellow or
    green, which also means the security plugin has initialised.
  - **Relevance** against each query's own paper: hit@5, hit@10 and MRR@10 for SEMANTIC and
    HYBRID on each store. RDS SEMANTIC is also scored in the order Bedrock returns it,
    worst-first.
  - **A fusion matrix:** the exact vector ranking fused with each lexical engine (OpenSearch BM25,
    Bedrock's `plainto_tsquery`, and the same terms OR'd), under RRF and under min-max, and each
    engine alone. This separates the lexical engine from the fusion method.

**First medium run, 2026-11-08.** This validates the tooling; P7 repeats it under clean
conditions. It ran on a Ryzen 5 5600X (6 cores, 12 threads) with 64 GB of RAM, with the whole
stack up, Dashboards included. Corpus: 13,602 chunks (14.6 M characters); the filter kept 272
rows.

| | OpenSearch 3.9 (faiss) | Postgres 17 + pgvector 0.8.2 |
|---|---|---|
| Load through the store (Bedrock's write path) | 10.7 s (1,266 chunks/s) | 44.4 s (307 chunks/s), one INSERT per chunk |
| Build | force merge 10.4 s | ANALYZE 1.4 s; or COPY 5.4 s + CREATE INDEX 3.6 s |
| recall@5 / @20 | 1.000 / 0.9995 | 0.992 / 0.986 (ef_search 40); 1.000 / 0.9998 (512) |
| Filtered (2%) recall@10, rows returned | 1.000, 10 | 1.000, 10, via GIN and an exact sort (iterative scan never engaged) |
| SEMANTIC p50 / p95 | 1.49 / 1.74 ms | 1.13 / 1.31 ms |
| HYBRID p50 / p95 | 5.05 / 5.65 ms | 2.57 / 3.27 ms |
| Filtered SEMANTIC p50 | 1.16 ms | 2.32 ms |
| 8 workers, SEMANTIC | 1,502 qps | 1,705 qps |
| Sidecar rewrite, 100 chunks | 0.11 s | 0.35 s |
| On disk (store's own figure) | 131 MB, 1 segment | 229 MB: HNSW 111 MB, heap and TOAST, two GIN indexes 19 MB |
| Container RSS: idle → peak | 3.41 → 3.52 GB (2 GB heap) | 46 MB → 328 MB; 844 MB during CREATE INDEX |
| CPU-seconds to load and build | 21.8 | 39.6 (incremental); 12.1 (bulk) |

- Both stores agree on SEMANTIC: Jaccard 0.988 over the top 10, and the same first result for
  99.5% of queries. HYBRID agrees far less (Jaccard 0.564, the same first result 83%), because
  the fusions differ: min-max over BM25 against Bedrock's RRF over `plainto_tsquery`.
- The data volumes are 145 MB for OpenSearch and 682 MB for Postgres. The Postgres volume also
  holds the WAL, the `pg-kb` table and space the bulk-build table freed but has not returned;
  the store's own figure (229 MB) is the one to compare.

**Phase P7: the isolated runs** ✅ done 2026-11-08, with `local-kb bench --corpus … --isolate`
on the same machine. The reports are `local-kb/bench/report-{sample,medium,large}.json`. Each store
ran alone, Dashboards was stopped, and each store was measured straight after a cold start. A cold
JVM is why OpenSearch's medium SEMANTIC p50 here (2.03 ms) is slower than in the first run
(1.49 ms).

| | medium (13,602 chunks) | | large (49,950 chunks) | |
|---|---|---|---|---|
| | **OpenSearch** | **Postgres** | **OpenSearch** | **Postgres** |
| Cold start to ready | 13.5 s | 0.2 s | 13.6 s | 0.4 s |
| Load through the store | 12.1 s (1,126/s) | 44.0 s (309/s) | 43.2 s (1,157/s) | 232.5 s (215/s) |
| Build after load | merge 11.3 s | ANALYZE 1.4 s | merge 81.5 s | ANALYZE 3.9 s |
| Bulk path instead (COPY + CREATE INDEX) | — | 5.3 + 3.7 s | — | 19.6 + 28.6 s |
| CPU-s, load + build | 36 | 39; 12 bulk | 156 | 223; 90 bulk |
| recall@5 / @20 | 1.000 / 0.999 | 0.993 / 0.986 (ef 40); 1.000 / 1.000 (ef 512) | 0.997 / 0.986 | 0.991 / 0.964 (ef 40); 1.000 / 1.000 (ef 512) |
| Filtered (2%) recall@10, rows | 1.0, 10 | 1.0, 10 (GIN + exact sort) | 1.0, 10 | 1.0, 10 (GIN + exact sort) |
| SEMANTIC p50 / p95 | 2.03 / 2.35 ms | 1.11 / 1.35 ms | 2.15 / 2.56 ms | 1.71 / 2.27 ms |
| HYBRID p50 / p95 | 5.91 / 7.30 ms | 1.81 / 2.65 ms | 7.34 / 9.16 ms | 5.49 / 7.36 ms |
| Filtered SEMANTIC p50 | 1.44 ms | 1.76 ms | 1.55 ms | 4.23 ms |
| 8 workers: SEMANTIC / HYBRID / filtered, qps | 1,247 / 710 / 1,586 | 1,757 / 1,628 / 1,644 | 1,174 / 638 / 1,508 | 1,680 / 609 / 985 |
| Sidecar rewrite, 100 chunks | 0.12 s | 0.41 s | 0.13 s | 0.62 s |
| On disk (store's figure) | 131 MB | 229 MB | 496 MB | 826 MB (HNSW 409, heap 85, GIN 53, rest TOAST) |
| RSS: idle after start → loaded | 2.69 → 3.09 GB | 27 → 298 MB | 2.68 → 3.41 GB | 27 → 300 MB |
| RSS peak | 3.09 GB | 842 MB (CREATE INDEX) | 3.41 GB | 849 MB (CREATE INDEX) |

On `sample` (17 chunks), cold start was 13.3 s for OpenSearch and 0.4 s for Postgres. Idle RSS
was 2.66 GB and 27 MB, and both reached recall 1.0. The images are 1.70 GB for OpenSearch, plus
1.80 GB for Dashboards, against 0.45 GB for Postgres.

**Relevance** (MRR@10 for each query's own paper, or for the eval's file on `sample`). SEMANTIC
is identical on both stores, since they hold the same vectors. HYBRID and fusion differ:

| | sample (20 questions) | medium (200 titles) | large (200 titles) |
|---|---|---|---|
| SEMANTIC, either store | 0.814 | 0.919 | 0.925 |
| RDS SEMANTIC in Bedrock's worst-first order | **0.236** | **0.693** | **0.219** |
| OpenSearch HYBRID (BM25, min-max) | **0.975** | 0.967 | 0.968 |
| RDS HYBRID (Bedrock: `plainto_tsquery`, RRF) | 0.814 | 0.970 | **0.988** |
| `plainto_tsquery` alone | 0.05 | 0.729 | 0.990 |
| Terms OR'd, alone / + min-max | 1.0 / 0.975 | 0.928 / 0.966 | 0.964 / 0.978 |
| BM25 alone / + RRF | 1.0 / 0.875 | 0.930 / 0.965 | 0.955 / 0.968 |

- **The lexical engine matters; the fusion method barely does.** Bedrock's RDS hybrid either
  ties or wins when the query's words all appear in the target (titles), and it adds nothing
  for questions (`sample`: 1 of 20 matched).
  - `plainto_tsquery` ANDs every term, so a title finds its own abstract, and on `large` it beats
    BM25.
  - A question has words such as "the" and "how" that are not in the answer, and the `simple`
    configuration Aurora uses keeps every stop word.

  OR'ing the same terms recovers OpenSearch's quality everywhere: with min-max, 0.975, 0.966 and
  0.978.
- **But OR'd `ts_rank` is not affordable on Bedrock's schema.** On `large`, an OR of a title's
  words matches all 49,950 rows: stop words again. `ts_rank` then re-tokenises every row's
  `chunks`, because Bedrock's schema indexes `to_tsvector(chunks)` as an expression and stores
  no tsvector. The 200 OR'd queries took 1,257 s, about 6 s each, against 1 s in all for OpenSearch's
  200 BM25 queries. Bedrock's own `plainto_tsquery` is
  fast because the AND is selective.
- **Bedrock's worst-first order is the biggest quality effect measured.** The right top 5 come
  back, but read in the order returned, MRR@10 is 0.22–0.69 instead of 0.81–0.93.
  RetrieveAndGenerate is exposed too, since the prompt lists the results in that order.
- The stores' HYBRID top 10 overlap with Jaccard 0.56, and their SEMANTIC top 10 with 0.97–1.0.

### What to keep identical

- The same parsed chunks and the **same vectors**:
  - Embed once and cache to `.state/vectors.npy` (keyed by sha256 of the text), then load the
    cache into both stores.
  - This removes LM Studio from the ingest timings and guarantees both stores see identical
    data.
- The same queries and k. Fusion is reported both ways: as each store does it on AWS (RRF on Postgres; this repo's min-max on OpenSearch), and with each store using the other's fusion.
- The same machine, one store measured at a time for resources, with Dashboards stopped. It is
  a UI, not part of the store.

### Corpora

| Name | Size | Purpose |
|---|---|---|
| `sample` | the 17 chunks from `sample-docs/kb/` | correctness and parity; the eval harness's 20 questions |
| `medium` | ~10k chunks (a public-domain text set, e.g. a Wikipedia subset or Project Gutenberg) | where HNSW parameters and filtering start to matter |
| `large` | ~100k chunks, 1024-d (≈400 MB of raw float32) | memory, index build time and disk at a size that separates the two |

At 17 chunks, both stores are effectively doing exact search, so results will tie and resource
use is pure baseline overhead. The larger corpora are where the comparison means something.

To limit embedding time, embed `medium` with `kb-embed`. For `large`, also reuse `medium`'s
vectors with small random perturbations (resource measurements only; quality numbers come from
`sample` and `medium`).

### Metrics: `local-kb bench` (new) plus `local-kb evaluate` (existing)

**Result quality.**

- **recall@k against exact search.** Ground truth is a brute-force cosine top-k computed in
  numpy. This scores each store's ANN index on equal terms. Report k = 5 and 20, for 200
  queries.
- **Overlap@k between the stores.** Jaccard similarity and the rank correlation of the two
  stores' top-k, for SEMANTIC and HYBRID.
- **Filtered recall.** A selective filter (≈2% of rows) with pgvector iterative scan off and on,
  against OpenSearch efficient filtering. The question is whether each returns k results, and
  whether they are the right ones.
- **The existing eval harness** (`eval/qa.jsonl`): hit@5, keyword score and judge score for
  SEMANTIC and HYBRID on each store. The baseline is the OpenSearch store's current numbers,
  90/85/90 for SEMANTIC and 100/95/100 for HYBRID. Some keyword-only queries such as `ZX-4471`
  show the BM25 vs `ts_rank` difference directly.

**Performance.**

- Query latency p50 and p95 for SEMANTIC, HYBRID and filtered SEMANTIC: 500 queries after
  warm-up, run single-threaded and with 8 concurrent clients.
- Ingest throughput, in chunks per second, writing pre-computed vectors.
- Time to build the index:
  - Postgres: `CREATE INDEX` after a bulk `COPY`, and incremental inserts into an existing index;
  - OpenSearch: bulk load, then a forced merge.
- Sidecar-only update latency for 100 chunks: `UPDATE` against read-modify-write `bulk`.

**Resources.**

- Container RSS at idle, after load, and peak during the index build: sample
  `podman stats --no-stream` every second while the benchmark runs.
- CPU-seconds per phase, taken from the container's cgroup `cpu.stat`.
- Disk:
  - the image size;
  - the data volume on disk (`du` of the volume);
  - the store's own figure: `pg_total_relation_size` and `pg_relation_size` of each index,
    against `_cat/indices?bytes=b` and `_cat/segments`.
- Cold start, from `compose up` to ready: `pg_isready` against cluster health `yellow` plus the
  security plugin initialised.

**Report.**

- `local-kb/bench/report-<corpus>.json` holds the raw numbers.
- A results section is added to `local-kb/README.md`, with tables and a short conclusion on
  where each store wins.
- Each run records the hardware, the image tags and the HNSW parameters.

### Parameters to align

| | OpenSearch | pgvector |
|---|---|---|
| Graph degree | `m: 16` | `m = 16` (the default) |
| Build beam | `ef_construction: 512` | `ef_construction = 256` (AWS's recommendation); also run 512 for an equal-terms comparison |
| Search beam | `ef_search: 512` | `hnsw.ef_search = 40` by default; run 40 and 512 |
| Metric | l2 on unit-norm vectors (same ranking as cosine) | cosine |

Qwen3 embeddings are unit-norm, so l2 and cosine rank identically. Any difference in the
results comes from the index, not the metric.

## 4. Baseline measured now (OpenSearch stack, idle, `sample` corpus)

From `podman stats` and `podman image ls` on 2026-11-08, with the stack up for about 12 hours:

| Container | RSS | Image |
|---|---|---|
| opensearch 3.9.0 (2 GB heap) | **2.92 GB** | 1.7 GB |
| opensearch-dashboards 3.9.0 | 252 MB | 1.8 GB |
| rustfs 1.0.4 | 124 MB | 301 MB |
| kb-events | 13 MB | 48 MB |

The expectation for pgvector (a hypothesis to test, not a result): roughly 30–100 MB RSS idle
for the sample corpus, and an image of about 450 MB. With `shared_buffers=256MB`, the RSS grows
toward 256 MB plus per-connection memory as data is touched.

## 5. Pros and cons of running each locally (measured in P7)

The figures come from §3's P7 tables: a Ryzen 5 5600X, one store running at a time.

### OpenSearch

**Pros**
- It is the store Bedrock quick-creates by default. Its field layout and the Dashboards
  prefix-filter view are what the AIF-C01 material shows, and they are reproduced exactly.
- **Real BM25 and a hybrid pipeline.** HYBRID MRR is 0.97–0.98 on every corpus, questions
  included. The 200 BM25 queries took 1 s on 50k chunks.
- **Faster ingest through Bedrock's write path:** 1,126–1,157 chunks/s, against Postgres's
  215–309. Postgres gets this because Bedrock writes one INSERT per chunk.
- Filtered search stays flat: 1.55 ms p50 on `large`, against 4.23 ms for Postgres.
- Sidecar-only updates are 3–5× faster: 0.13 s per 100 chunks on `large`.
- Smaller on disk: 496 MB against 826 MB on `large`. Postgres TOASTs each 4 KB vector beside
  its HNSW copy.
- Dynamic mapping, Dashboards, Dev Tools; it scales out if a corpus ever outgrows one machine.

**Cons**
- **Memory:** 2.7 GB RSS idle with no data, 3.1–3.4 GB loaded, plus 0.24 GB for Dashboards.
  That is 100× Postgres at idle and about 11× once loaded, and it competes with LM Studio.
- **Startup:** 13.3–13.6 s from `podman start` to ready, every time.
- **Images:** 3.5 GB with Dashboards.
- Force merge is the slow step at scale: 81 s and 90 CPU-seconds for 50k chunks. Without it,
  queries hit many segments.
- Operational sharp edges seen in this repo: the disk watermarks, the persistent
  `create_index` block, and the security plugin's role permissions.
- `refresh_interval`: writes become visible after about 1 s.

### PostgreSQL + pgvector

**Pros**
- It is also a first-class Bedrock store (`RDS`), with a fixed, documented schema, reproduced
  exactly (§1).
- **Tiny when idle:** 27 MB RSS, and 300 MB with 50k chunks loaded. **Cold start 0.2–0.4 s.** A
  0.45 GB image.
- **Faster unfiltered queries, single-threaded:**
  - SEMANTIC p50 is 1.11 against 2.03 ms on `medium`, and 1.71 against 2.15 ms on `large`;
  - HYBRID is 1.8 against 5.9 ms on `medium`, and 5.5 against 7.3 ms on `large`.

  With 8 workers, SEMANTIC is about 40% higher (1,680–1,757 qps).
- **Bulk build is cheap:** COPY plus CREATE INDEX took 48 s and 90 CPU-seconds for 50k chunks,
  against OpenSearch's 125 s and 156 CPU-seconds to load and merge.
- **Recall is exact once tuned:** `ef_search` 512 gives 1.000 at k=5 and k=20 on both corpora.
- ACID delete-and-replace; plain SQL; `pg_dump`; exact search as a plain query, which is how the
  bench's own ground truth could be checked.

**Cons**
- **Bedrock's defaults cost recall:** `ef_search` 40 gives 0.964 at k=20 on `large`. Bedrock
  cannot set it, so on AWS it is whatever the cluster's parameter group says.
- **Bedrock's worst-first SEMANTIC order** drops MRR@10, read in the order returned, to 0.22–0.69. That comes
  from AWS, not pgvector, but anyone choosing RDS on Bedrock gets it.
- **Lexical search is all-or-nothing.** `plainto_tsquery` ANDs every word of the query:
  - on the 20 natural-language questions, HYBRID added nothing, with MRR 0.81, the same as
    SEMANTIC;
  - on title queries it is excellent, 0.97–0.99.

  OR'ing the terms fixes quality (0.966–0.978 with min-max). On Bedrock's schema, though, it costs
  6 s a query at 50k rows: the `simple` configuration keeps stop words, so every row matches, and
  `ts_rank` re-tokenises each row's text, because no tsvector is stored. BM25 would need an
  extension (ParadeDB `pg_search`, VectorChord-bm25).
- Filtered queries use the GIN index and then sort exactly, not HNSW: 4.2 ms on `large`, and the
  slowest under concurrency (985 qps). Iterative scan never engaged at 2% selectivity.
- **Bedrock's write path is slow:** 215 chunks/s on `large` (5.4× OpenSearch), at 223
  CPU-seconds. The fast path (COPY) is not what Bedrock runs.
- 1.7× OpenSearch's disk, and CREATE INDEX peaks at about 850 MB RSS.
- No Dashboards. `inspect.sql` and psql stand in.

### Verdict

How the §5 hypotheses held up:

| Hypothesis | Result |
|---|---|
| Quality ties on `sample` | **Partly.** SEMANTIC ties exactly. HYBRID does not: 0.975 for OpenSearch against 0.814 for Bedrock's RDS hybrid. |
| Postgres matches SEMANTIC recall once `ef_search` is aligned | **Yes.** It reaches 1.000 at ef 512. At Bedrock's likely default of 40 it is 0.964–0.986 at k=20. |
| Postgres loses some HYBRID lexical precision | **It depends on the query.** It loses everything on questions and wins on titles. |
| 10–30× less idle memory | **More:** about 100× at idle (27 MB against 2.7 GB) and about 11× loaded. |
| Much smaller disk | **No.** The image is 4× smaller, but the data is 1.7× larger. |
| Postgres ingests faster | **No.** Through Bedrock's write path it is 3.6–5.4× slower; only the bulk path (not Bedrock's) is faster. |
| OpenSearch is the better demonstration store | **Yes.** Dashboards, and a hybrid that works on questions. |
| pgvector is the better always-on dev store | **Yes.** It starts in 0.4 s, idles at 27 MB and serves SEMANTIC faster. |

**Use OpenSearch** to study Bedrock's default store, to use HYBRID on natural-language
questions, or for filtered search under load. **Use pgvector** when RAM is short, for example
next to a 9B model in LM Studio. It is also the choice when the stack starts and stops often,
or when you want to show that the vector store under Bedrock is replaceable, worst-first order
and all. At this repo's scale (up to 50k chunks) both answer in single-digit milliseconds.
Neither one's latency is the deciding factor.

## 6. Phases and order

| Phase | Deliverable | Gate |
|---|---|---|
| 0b ✅ | `fixtures/rds/` from a real Aurora knowledge base; plan updated with what it settles | done 2026-11-08 |
| P1 ✅ | `VectorStore` seam, OpenSearch behind it | 141 tests pass unchanged; live sync, sidecar rewrite and retrieve unchanged; eval 90/85/90 · 100/95/100 as before |
| P2 ✅ | `postgres` service, `local_init.py pg`, RDS storage config accepted | `create-knowledge-base --store rds` works; schema identical to Aurora's; 149 tests pass |
| P3 ✅ | pgvector ingest with Bedrock's own INSERT, DELETE and read-and-upsert | Phase 0b replay: same 8 counters, same 17 chunk texts, the same `metadata` and `custom_metadata` keys |
| P4 ✅ | SEMANTIC (worst-first, distance score), filters → SQL, RRF HYBRID | the RDS fixtures' result sets, orders and filter chunk sets; ZX-4471 first under HYBRID |
| P5 ✅ | serve/CLI/boto3 parity, smoke test, `inspect.sql`, tests parametrized over both stores | stock-boto3 lifecycle against an RDS knowledge base |
| P6 ✅ | `local-kb bench`, vector cache, corpora loaders, resource sampler | reproducible JSON reports |
| P7 ✅ | isolated runs on sample/medium/large; relevance and fusion matrix; README results and a measured verdict (§3, §5) | done 2026-11-08 |

Commit once per phase, as before.
