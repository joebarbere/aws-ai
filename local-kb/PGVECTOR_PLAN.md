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

**Phase P5: API parity.**

- `local-kb serve` and the CLI work unchanged against an RDS knowledge base.
- `CreateKnowledgeBase` with an RDS `storageConfiguration` works from stock boto3.
- Add an RDS run to `local_smoke.py`.
- Tests:
  - mirror `test_local_kb_ingest` and `test_local_kb_retrieve`, parametrized over both stores;
  - add a unit test for the filter → SQL translation;
  - add a Postgres integration test, skipped when port 5432 is closed (same pattern as the
    OpenSearch tests).
- **Viewing the internals.** Dashboards has no Postgres equivalent. Ship
  `local-kb/pg/inspect.sql`, a set of psql queries that does the same job as the Dashboards
  prefix-filter view:
  - chunks per source;
  - `metadata` and `custom_metadata` flattened with `jsonb_each`;
  - the index list with sizes.
  - Optional: a lightweight browser UI such as `adminer` under the `pg` profile. Leave out
    pgAdmin, which is about 500 MB.

## 3. The comparison (Phases P6–P7)

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

## 5. Pros and cons of running each locally (expected; P7 confirms or corrects)

### OpenSearch

**Pros**
- It is the store Bedrock quick-creates by default. Its field layout and the Dashboards
  prefix-filter view are what the AIF-C01 material shows, and they are already reproduced
  exactly.
- Real BM25 plus a built-in hybrid normalization pipeline. Exact codes and rare terms rank
  well.
- Dynamic mapping: a new sidecar attribute becomes a filterable field with no schema change.
- faiss efficient filtering returns k results under selective filters without tuning.
- Dashboards gives visual inspection, saved searches, charts and Dev Tools for free.
- It scales out (shards and replicas) if a corpus ever outgrows one machine.

**Cons**
- Memory: the JVM heap is reserved up front. It uses about 3 GB at idle for 17 chunks; with
  Dashboards it is about 3.2 GB, and it competes with LM Studio for RAM.
- Images total about 3.5 GB, and startup takes tens of seconds before the security plugin is
  ready.
- Operational sharp edges seen in this repo: the disk watermark locked index creation on a
  94%-full disk and needed absolute watermarks; the persistent `create_index` block had to be
  cleared; and the security plugin's roles needed `indices:admin/get` and mapping permissions.
- `refresh_interval` makes newly written chunks visible only after about 1 s. The test and smoke
  scripts have to `_refresh`.
- A sidecar-only update means reading the chunk back and re-indexing it, vector included.
- Backups and snapshots need a repository plugin or `elasticdump`-style tooling.

### PostgreSQL + pgvector

**Pros**
- It is also a first-class Bedrock store (`RDS`), so it is just as faithful a target, with a
  documented, fixed schema.
- Small footprint: one process of tens of MB at idle, a single ~450 MB image, and startup in
  about 1–2 s.
- ACID: delete-and-replace and sidecar updates are atomic, with no stale-read window.
  Phase 0 caught AOSS in exactly that kind of window.
- Plain SQL. The vectors can be joined with anything, even the local-kb control plane, which
  could move from SQLite into the same database. `pg_dump` is the backup, and the tools are
  familiar (psql, any SQL client).
- Exact search is just a query with no index. That gives free ground truth, and is fine below
  roughly 50k rows.
- The `halfvec` type (fp16) halves vector storage, which is easy to try.

**Cons**
- No BM25 out of the box. `ts_rank` has no IDF or length normalization, so the lexical half of
  HYBRID is weaker. Getting BM25 needs a third-party extension (ParadeDB `pg_search`,
  VectorChord-bm25).
- Hybrid fusion is hand-written SQL, not a pipeline setting.
- HNSW pitfalls:
  - post-filtering can return fewer than k rows unless `iterative_scan` (0.8+) is on;
  - `ef_search` defaults to 40;
  - index builds are slow and memory-hungry when the graph doesn't fit in
    `maintenance_work_mem`;
  - HNSW indexes are limited to 2,000 dimensions for `vector` (4,000 for `halfvec`).
- The schema is fixed. The dimension is in the column type, so changing the embedding size means
  a new table. A new filterable attribute either goes into jsonb, which needs casts for numeric
  comparisons and has weaker statistics, or needs an `ALTER TABLE`.
- No built-in UI matching Dashboards: the prefix-filter demonstration becomes SQL.
- Tuning (`shared_buffers`, `work_mem`, autovacuum after heavy update churn) is on you, but the
  defaults are fine at this scale.

### Likely outcome

These are hypotheses for P7 to confirm or overturn:

- Quality ties on `sample`.
- Postgres matches OpenSearch on SEMANTIC recall once `ef_search` is aligned, and loses some
  HYBRID lexical precision.
- Postgres uses about 10–30× less idle memory, has a much smaller disk footprint, and
  ingests faster at `sample` and `medium` scale.
- OpenSearch is the better *demonstration* store, because of Dashboards and BM25.
- pgvector is the better *always-on dev* store: it uses little memory and costs nothing to
  leave running next to a 9B LLM.

## 6. Phases and order

| Phase | Deliverable | Gate |
|---|---|---|
| 0b ✅ | `fixtures/rds/` from a real Aurora knowledge base; plan updated with what it settles | done 2026-11-08 |
| P1 ✅ | `VectorStore` seam, OpenSearch behind it | 141 tests pass unchanged; live sync, sidecar rewrite and retrieve unchanged; eval 90/85/90 · 100/95/100 as before |
| P2 ✅ | `postgres` service, `local_init.py pg`, RDS storage config accepted | `create-knowledge-base --store rds` works; schema identical to Aurora's; 149 tests pass |
| P3 ✅ | pgvector ingest with Bedrock's own INSERT, DELETE and read-and-upsert | Phase 0b replay: same 8 counters, same 17 chunk texts, the same `metadata` and `custom_metadata` keys |
| P4 ✅ | SEMANTIC (worst-first, distance score), filters → SQL, RRF HYBRID | the RDS fixtures' result sets, orders and filter chunk sets; ZX-4471 first under HYBRID |
| P5 | serve/CLI/boto3 parity, smoke test, `inspect.sql`, tests parametrized over both stores | stock-boto3 lifecycle against an RDS knowledge base |
| P6 | `local-kb bench`, vector cache, corpora loaders, resource sampler | reproducible JSON reports |
| P7 | runs on sample/medium/large; README results and a pros/cons verdict with measured numbers | — |

Commit once per phase, as before.
