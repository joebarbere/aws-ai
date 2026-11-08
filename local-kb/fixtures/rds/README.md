# Phase 0b fixtures: the Aurora PostgreSQL (`RDS`) store

These were captured from a real Bedrock knowledge base on Aurora PostgreSQL 17.11 with pgvector
0.8.2, on 2026-11-08. It ran for about an hour and was then destroyed. The setup was
`terraform/modules/knowledge_base_rds` plus `python/scripts/rds_kb.py`, and the capture was
`capture_kb_fixtures.py --store rds`. Everything else matches `../`: the same corpus, the same
two syncs, the same queries, Titan v2 at 1024 dimensions, default chunking, and Nova Lite.

The account ID is masked as `000000000000`. In `postgresql-statements.json`, vector literals are
shortened to `[<1024 floats>]`, and bedrock_user's password is redacted. `log_statement = all`
recorded it, because `rds_kb.py` sets it with `ALTER ROLE`.

| File | What it settles |
|---|---|
| `table-schema.json` | the columns and indexes as AWS's guide creates them; pgvector version; `hnsw.*` settings |
| `index-documents*.json` | every row, with `metadata` and `custom_metadata` parsed and a vector probe (dimensions, norm, first 8 values) |
| `postgresql-statements.json` | **every SQL statement Bedrock ran**: validation, inserts, deletes, searches, filters |
| everything else | the same files as `../`, for the RDS store |

## What it shows

**Ingestion.**

- The statistics for both jobs are identical to the OpenSearch Serverless run.
- The rows are the same 17 chunks, with identical text.

**Row layout.**

- `id` is a fresh uuid4 for each chunk.
- `metadata` is the json `{"sourceUrl": "s3://…"}`. The key is `sourceUrl`, where OpenSearch's
  `AMAZON_BEDROCK_METADATA` uses `source`.
- `custom_metadata` holds **both** the sidecar attributes and Bedrock's own fields:
  `x-amz-bedrock-kb-source-uri`, `-data-source-id`, `-source-file-modality`, and
  `-document-page-number` for PDFs.
- Numbers are stored as json floats (`3.0`, `20261015.0`).
- There is no top-level `id` field and no chunk-id field.

**Writes.**

```sql
INSERT INTO bedrock_integration.bedrock_kb (id, embedding, chunks, metadata, custom_metadata)
VALUES ($1, $2::vector, $3, $4, $5) ON CONFLICT(id) DO UPDATE SET embedding=EXCLUDED.embedding,
chunks=EXCLUDED.chunks, metadata=EXCLUDED.metadata, custom_metadata=EXCLUDED.custom_metadata
RETURNING NULL
DELETE FROM bedrock_integration.bedrock_kb WHERE id IN ($1) RETURNING NULL
```

- A multi-chunk document is inserted inside one `BEGIN`/`COMMIT`. A one-chunk document is a
  single autocommitted `INSERT`.
- **Edited document:** the old rows are deleted, *then* the new rows are inserted. These are
  separate statements, so the change is not atomic.
- **Sidecar-only change:** Bedrock reads the row back
  (`SELECT id, embedding, chunks, metadata, custom_metadata … WHERE id IN ('…')`) and upserts it
  under the same `id` with the new `custom_metadata`. This is the same read-modify-write as on
  OpenSearch, and it makes no embedding call.
- The snapshot taken 120 s after the second job is fully settled: 16 rows. It has none of the
  stale state that OpenSearch Serverless showed.

**Knowledge base creation and each sync start by validating the table:**

- a probe: `SELECT embedding <=> '[…]'::vector as score FROM … LIMIT 1`;
- the column types, read from `pg_attribute`;
- three `pg_indexes` lookups using `LIKE`:
  - `%USING gin (to_tsvector(''simple''::regconfig, chunks))%` **or** the `''english''`
    version;
  - `%USING hnsw (embedding vector_cosine_ops)%`;
  - `%USING gin (custom_metadata)%`.

**SEMANTIC.**

```sql
SELECT chunks, metadata, embedding <=> '[…]'::vector as score, custom_metadata
FROM bedrock_integration.bedrock_kb [WHERE …] ORDER BY score LIMIT k;
```

The SQL picks the right k rows. Then **Bedrock returns them worst-first, and `score` is the raw
cosine distance** (`1 − cos`, lower is better):

- In `retrieve-semantic.json`, the most relevant chunk (the dew heater guide) comes **last**,
  with score 0.4933.
- All three unfiltered semantic fixtures come back in exactly the reverse of the OpenSearch
  order.
- Every score equals `1 − cos`, where cos is recovered from the OpenSearch run's
  `1/(1+d²)` scores, to four decimal places.

This looks like a Bedrock bug: the store returns distances, and Bedrock sorts them as if they
were similarities. Retrieval *quality* is unaffected, because the set is right, but anything
that trusts the order or the score is misled. `RetrieveAndGenerate` gets the same set.

**HYBRID** is reciprocal rank fusion with k = 60, run in SQL:

```sql
WITH hybrid AS (SELECT searches.id, (sum(COALESCE(1.0 / (60 + searches.rank), 0.0)) * 61.0/2) AS score
  FROM ( ( SELECT id, rank() OVER (ORDER BY embedding <=> $q) AS rank FROM t
           ORDER BY embedding <=> $q LIMIT 10 )
         UNION ALL
         ( SELECT id, rank() OVER (ORDER BY ts_rank(to_tsvector('simple', chunks),
                                                    plainto_tsquery('simple', $text)) DESC) AS rank
           FROM t WHERE plainto_tsquery('simple', $text) @@ to_tsvector('simple', chunks)
           ORDER BY rank LIMIT 10 ) ) searches
  GROUP BY searches.id ORDER BY score DESC LIMIT 5)
SELECT m.chunks, m.metadata, hybrid.score, m.custom_metadata FROM t m JOIN hybrid ON m.id = hybrid.id
ORDER by hybrid.score DESC
```

- `× 61/2` scales the score so that rank 1 on both sides scores 1.0. Rank r on one side alone
  scores `30.5 / (60 + r)`.
- The fixture fits this exactly: 1.0, 0.4919, 0.4841, 0.4766, 0.4692.
- The order is correct here, best first.
- Each side takes `LIMIT 10` for `numberOfResults` 5. Whether that is `2k` or a fixed 10 is not
  settled by this capture.
- The query text goes in as a literal. `plainto_tsquery` ANDs every term, so a multi-word
  question rarely matches on the lexical side, and hybrid then reduces to the vector ranking.

**Filters** become jsonb conditions on `custom_metadata`, with parameters bound as json:

| Filter | SQL |
|---|---|
| `equals` (string) | `( custom_metadata @> {"k": "v"} or custom_metadata @> {"k": ["v"]} )`: a scalar *or* a list containing it |
| `equals` (number) | `custom_metadata @> {"k": 2.0}` |
| `equals` (boolean) | `custom_metadata @> {"k": true}` |
| `startsWith` | `(custom_metadata ->> 'k') like 'v%'` |
| `greaterThanOrEquals` | `(custom_metadata ->> 'k')::double precision >= 20261001.000000`: the value is formatted into the SQL with `%f`, not bound |
| `listContains` | `custom_metadata @> {"k": ["v"]}` |
| `andAll` | `( a and b and c )` |

All four filter fixtures return the same chunks as on OpenSearch.

**Retrieve results.**

- `metadata` is `custom_metadata` as stored: sidecar attributes plus the `x-amz-bedrock-kb-*`
  fields.
- There is **no `x-amz-bedrock-kb-chunk-id`**; on OpenSearch the response carries one.
- `location` and `content` are the same shape as on OpenSearch.
