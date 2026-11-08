# Local Bedrock Knowledge Base — implementation plan

A local-only, wire-faithful re-implementation of an Amazon Bedrock Knowledge Base, so the RAG
pipeline in `terraform/modules/knowledge_base` can be studied without the ~$350/month
OpenSearch Serverless floor.

| AWS | Local stand-in |
|---|---|
| S3 data source | **RustFS 1.0.4** (`kb-data` bucket, `kb/` prefix), any S3-compatible store works |
| Titan Text Embeddings V2 (1024-d, normalized) | **Qwen3-Embedding-0.6B** in LM Studio (1024-d, normalized) |
| OpenSearch Serverless `VECTORSEARCH` collection | **OpenSearch 3.9.0** single node + **Dashboards 3.9.0** |
| `bedrock-agent` control plane (KB, data source, ingestion job) | `aws_ai.local_kb` control plane, SQLite-backed |
| `bedrock-agent-runtime` `Retrieve` / `RetrieveAndGenerate` | `aws_ai.local_kb` runtime, same JSON shapes and REST routes |
| Claude / Nova as the generation model | **Qwen3.5-9B** (Q6_K) in LM Studio |

**The faithfulness bar.** A boto3 `bedrock-agent-runtime` client pointed at
`endpoint_url="http://localhost:8099"` should get back responses with the same shape as the real
service. An OpenSearch Dashboards user should see the same internal fields a real KB index
contains: `bedrock-knowledge-base-default-vector`, `AMAZON_BEDROCK_TEXT_CHUNK`,
`AMAZON_BEDROCK_METADATA`, and the `x-amz-bedrock-kb-*` fields.

**Phase 0 has run (2026-11-08).** A real KB was synced with `sample-docs/`, queried, and
destroyed. §0 below records what it showed, and `fixtures/` holds the raw evidence. Several
documented-looking assumptions turned out to be wrong; they are called out where they change the
build.

---

## 0. Findings that shape the plan

### Your machine (checked 2026-11-07)

- **GPU: AMD RX 7600 XT, 16 GB VRAM**, with 62 GB RAM and 12 cores. LM Studio uses the ROCm
  llama.cpp runtime 2.71.0. The embedding model and a 9B LLM fit on the GPU together.
- **Already in LM Studio:**
  - `text-embedding-nomic-embed-text-v1.5`, which is 768-d. It is a fallback, not the target,
    because it needs `search_document:` / `search_query:` prefixes and is not Titan-shaped.
  - `huihui-qwen3.5-9b-abliterated` (Q4_K_S). The "abliterated" build has its refusal behaviour
    removed. A faithful Bedrock stand-in should use the stock model, because Bedrock's models
    keep that behaviour and Bedrock Guardrails sits on top of it.
- **Containers:** podman 6.1.3, aliased as `docker`. There is **no compose provider**;
  `uv tool install podman-compose` adds one without sudo.
- **MinIO is end-of-life upstream, so the object store is RustFS.**
  - `minio/minio` was archived on 2026-04-25 and is now source-only, with no security fixes. Both
    `docker.io/minio/minio` and `quay.io/minio/minio` refuse pulls (verified with `skopeo`).
  - **RustFS** (`docker.io/rustfs/rustfs:1.0.4`, Apache-2.0, GA release 2026-10-25) replaces it.
    It has S3 webhook notifications configured through the **standard**
    `PutBucketNotificationConfiguration` API (prefix filters included), so setup is plain boto3
    with no vendor CLI.
  - Rejected alternatives: Garage (no bucket notifications), SeaweedFS (notifications only at the
    filer level), Ceph RGW (too heavy for a laptop), and the `pgsty/minio` fork (provenance
    concerns).
  - The pipeline uses only the S3 API, and the polling trigger (Phase 4) doesn't need
    notifications at all, so any S3 store works. Your cached `quay.io/minio/minio:latest` stays a
    fallback; back it up with `podman save` if you want to keep it.

### Bedrock KB internals

These come from the docs, AWS samples, and the Phase 0 run against a real KB. The **Evidence**
column names the fixture file or doc each row rests on.

**What a stored chunk looks like**

| What | Finding | Evidence |
|---|---|---|
| Index settings Bedrock relies on | faiss / hnsw / l2 `knn_vector`; `AMAZON_BEDROCK_METADATA` as text with `index:false`; the text field indexed. nmslib cannot be filtered | [setup], [sync], `index-mapping.json` |
| **`_id`** | **Bedrock sets it**, and it *is* the chunk-id: the literal string `1%3A0%3A` + 20 random URL-safe characters (URL-encoded `1:0:<id>`) | `index-documents.json` |
| Top-level `_source` fields Bedrock writes | `x-amz-bedrock-kb-source-uri`, `x-amz-bedrock-kb-data-source-id`, `x-amz-bedrock-kb-source-file-modality` (`"TEXT"`, a field no doc mentions), `x-amz-bedrock-kb-document-page-number` (PDF only), and **`id`** (a random UUID, unrelated to `_id`). Plus every sidecar attribute. **No** chunk-id field is stored | `index-documents.json` |
| How dynamic mapping types them | String → `text` + `.keyword` (`ignore_above: 256`); `STRING_LIST` → `text` + `.keyword` holding an array; **`NUMBER` → `float`** (stored as `20261015.0`); `BOOLEAN` → `boolean`; page number → `long` | `index-mapping.json` |
| `AMAZON_BEDROCK_METADATA` | Exactly `{"source":"s3://bucket/key"}`. Sidecar attributes are **not** in it | `index-documents.json` |

**Ingestion**

| What | Finding | Evidence |
|---|---|---|
| Default chunking | About 1,030 characters / 210 words (≈300 Titan tokens). Overlap is 1–2 sentences, about 20%. A chunk **starts** on a sentence boundary but may **end** mid-sentence. Documents under ~300 tokens are a single chunk | `index-documents.json` |
| Page number | It is the page the chunk **starts** on. The 3-page, 231-word PDF became **one** chunk, `page = 1`, so a filter on `page = 2` returns nothing | `retrieve-filter-page.json` |
| Default parser text normalization | `.txt`: newline → space, blank line → two spaces. `.md`: everything collapsed to single spaces, `#` kept. `.html`: `<title>` + converted markdown (`Title # H1 body…`). PDF: pages joined with no separator (`…procedure.2. The main…`), line breaks → three spaces | `index-documents.json` |
| Embedding request | `{"inputText": …, "dimensions": 1024, "embeddingTypes": ["float"]}`. Vectors come back unit-norm. Queries are embedded raw, with no prefix | `invocation-logs.json` |
| `includeForEmbedding` | The embedded text is `"department: operations\t\n\n" + chunk` (note the **tab**). Only typed attributes with the flag set are prepended; simplified-format sidecars never are | `invocation-logs.json` |
| Incremental sync | An edited file is re-embedded (1 embedding call, for that file only). A sidecar-only change is updated **in place**: same `_id`, no embedding call, and, oddly, `id` is overwritten with the `_id` value. A deleted file's chunks are removed | `ingestion-job-2.json`, `invocation-logs.json` |
| Job statistics | **`numberOfDocumentsSkipped` is not "unchanged"**: three unchanged files counted nowhere. A sidecar-only change counts only in `numberOfMetadataDocumentsModified`. Job 2 was `scanned 5, metadataScanned 2, modified 1, metadataModified 1, deleted 1`. Each job took 6–11 s | `ingestion-job-*.json` |
| Eventual consistency | 34 s after `COMPLETE`, OpenSearch Serverless still had the deleted file, and the edited file's new chunk was not visible yet | `index-documents-after-mutation.json` |

**Retrieve**

| What | Finding | Evidence |
|---|---|---|
| Response shape | `content {text, type:"TEXT"}`, `location {type:"S3", s3Location.uri}`, `metadata`, `score`. **No `documentId`, no `nextToken`.** `metadata` = the source-uri, data-source-id and source-file-modality fields + `x-amz-bedrock-kb-chunk-id` (= `_id`) + page number (returned as **`1.0`**) + sidecar attributes (numbers as floats) | `retrieve-*.json` |
| Score | The top semantic score was 0.503. That fits OpenSearch's l2 `1/(1+d²)` on unit vectors (cos ≈ 0.51) | `retrieve-semantic.json` |
| **HYBRID** | For `"ZX-4471"`, HYBRID returned **the same ranking and the same scores** as SEMANTIC, so no keyword boost was visible on this index. The cause is unknown | `retrieve-*-keyword.json` |
| Filters | `equals` on a custom string, `startsWith` on `x-amz-bedrock-kb-source-uri`, and `andAll` of a boolean, `greaterThanOrEquals` on a float and `listContains` all behaved as documented | `retrieve-filter-*.json` |
| Retrieval failure worth keeping | "What temperature must the camera reach…" never retrieved the handbook chunk, even in the top 10. The one 3-topic PDF chunk is outranked by inventory chunks that say "camera" repeatedly. A faithful local copy should fail the same way | `rag-*.json` |

**Generation and identities**

| What | Finding | Evidence |
|---|---|---|
| **Default prompt depends on the model family** | **Nova:** system prompt `"A chat between a curious User and an artificial intelligence Bot…"`, results rendered as `Passage %[n]% : text`, inline `%[n]%` citation markers. **Others (Llama 3):** the XML template from the AWS samples, `<search_results><search_result><content>\n…\n</content><source>\nN\n</source></search_result>`, then `<answer><answer_part><text>…</text><sources><source>N</source></sources></answer_part></answer>` instructions | `invocation-logs*.json` |
| Request shape | Generation goes through the **Converse** API: the template goes in `system`, and the user's query is the user message. Nova gets `temperature 0, topP 1`. Generic models get `maxTokens 2048, temperature 0, topP 1, stopSequences ["\nObservation"]` | `invocation-logs*.json` |
| No answer / parse failure | If the answer isn't found, `citations` has **one** entry spanning the whole text with `retrievedReferences: []`, not an empty list. When Llama answered without the `<answer>` XML, Bedrock replaced its text with `"Sorry, I am unable to assist you with this request."` | `rag-default.json`, `rag-llama3-8b.json` |
| Custom template without `$output_format_instructions$` | Still one citation with empty references. `$search_results$` is rendered in the model family's own format | `rag-no-format-instructions.json` |
| Orchestration (follow-up with a `sessionId`) | A rewrite prompt makes the model emit `Action: FUNCTION(query="…")`. Query decomposition emits `Action: FUNCTION(standalone_question="…", additional_queries=["…"])`. Nova Lite then leaked that trace into the answer | `invocation-logs.json`, `rag-decomposition.json` |
| `sessionId` | A UUID | `rag-default.json` |
| Identities | Ingestion embeds as the KB role, session `EmbeddingTask-<jobId>`. Query-time embedding is `BKB-Retrieve-<kbId>-<uuid>`. Generation runs as the **caller** | `invocation-logs.json` |

**Still unknown:**

- Whether console quick-create names the text field `AMAZON_BEDROCK_TEXT` (Phase 0 used this
  repo's own index script, not quick-create).
- How hybrid would fuse scores when it does change the ranking.
- How change detection works internally. ETag behaviour matched, but nothing distinguishes it from
  a LastModified check.

**About the "prefix filter" from the course video.** There is no AWS doc for it, but Phase 0
confirms the mechanism:

1. Create an index pattern on `bedrock-knowledge-base-default-index`.
2. In Discover, type `x-amz-bedrock` in the *Search field names* box. You see exactly
   `x-amz-bedrock-kb-source-uri`, `-data-source-id`, `-source-file-modality` and
   `-document-page-number`, each string one with its `.keyword` twin.
3. Type `AMAZON_BEDROCK` to see the text and metadata fields.

Those fields exist only because Bedrock writes them as top-level `_source` keys, and dynamic
mapping types them. Phase 4 reproduces this, including the dynamic mapping.

---

## 1. Architecture

```
                 (control plane — SQLite: KBs, data sources, jobs, document ledger)
                                         │
 ┌──────────┐ webhook  ┌───────────┐  ┌─┴──────────────┐ StartIngestionJob ┌──────────────────────┐
 │  RustFS  │ ───────▶ │ kb-events │◀─│ sync trigger   │ ────(debounced)─▶ │ ingestion worker     │
 │ kb/*.pdf │  (SQS)   │  :8098    │  │ polls queue    │                   │ list → diff → parse  │
 │ *.metadata.json                     └────────────────┘                   │ → chunk → embed      │
 └────┬─────┘ ◀────────────────── GetObject / ListObjectsV2 ─────────────────│ → bulk index/delete  │
      │                                                                     └───┬───────────┬──────┘
      │                                       LM Studio :1234                   │ /v1/embeddings
      │                                ┌──────────────────────────┐ ◀───────────┘           │ _bulk
      │                                │ Qwen3-Embedding-0.6B     │                         ▼
      │                                │ Qwen3.5-9B (generation)  │ ◀──┐        ┌──────────────────────┐
      │                                └──────────────────────────┘    │        │ OpenSearch :9200     │
      │                                                                │        │ bedrock-knowledge-   │
      │   boto3 bedrock-agent-runtime(endpoint_url=:8099)              │        │ base-default-index   │
      │        │                                                       │        └─────────┬────────────┘
      │        ▼                                                       │                  │
      │  ┌────────────────────────────────────────┐   /v1/chat/completions                │
      └─▶│ runtime API :8099                      │───────────────────┘   Dashboards :5601 ◀─┘
         │ /knowledgebases/{id}/retrieve          │── knn / hybrid query ─▶ OpenSearch
         │ /retrieveAndGenerate(/Stream)          │
         └────────────────────────────────────────┘
```

Two design rules, both copied from the real service:

1. **A file event triggers a sync job, not an embedding.** Bedrock never auto-syncs. The
   documented pattern is S3 event → Lambda → `StartIngestionJob`. The trigger only debounces
   events and starts a job, and the job does a full list-and-diff. That keeps the ingestion-job
   statistics honest. The trigger has two inputs:
   - the `kb-events` queue, which RustFS's webhook fills (S3 → SQS → poller), and which is fast;
   - an ETag poll of the bucket, which works on any S3 store.

   Neither is required: `make local-sync` is the console's Sync button.
2. **The control plane and the data plane are separate.**
   - Control plane: knowledge-base, data-source and job records, plus the per-document ledger.
     These live in SQLite, in the same role Bedrock's hidden service state plays.
   - Data plane: chunks, which live only in OpenSearch.
   - So `dataDeletionPolicy=RETAIN` versus `DELETE` behaves as it does on AWS.

Infra runs in containers. The worker and runtime run on the host under `uv`, which avoids the
container-to-LM-Studio networking problem: LM Studio binds `127.0.0.1` by default.

---

## 2. Repository layout

```
local-kb/
  PLAN.md                     # this file
  README.md                   # quickstart, mirrors terraform/modules/knowledge_base/README.md
  compose.yaml                # rustfs, kb-events, opensearch, opensearch-dashboards
  events/queue.py             # kb-events: the bucket-event queue (stdlib, python:3.13-alpine)
  .env.example                # OPENSEARCH_INITIAL_ADMIN_PASSWORD, RUSTFS_ACCESS_KEY/SECRET_KEY
  dashboards/
    bedrock-kb.ndjson         # saved objects: index pattern, "Bedrock internals" saved search, viz
  fixtures/                   # Phase 0 captures from a real KB (mapping, _search hit, Retrieve JSON)
  sample-docs/                # a few PDFs/MD + .metadata.json sidecars for the demo
python/src/aws_ai/local_kb/
  __init__.py
  config.py                   # endpoints, model ids, dims — one place
  ids.py                      # 10-char [0-9A-Z] ids for KB/DS/job, chunk-id encoding
  control.py                  # SQLite: knowledge_bases, data_sources, ingestion_jobs, documents
  index.py                    # index body, create/ensure, field-name constants
  parse.py                    # default parser: txt, md, html, csv, pdf (per page), docx
  chunking.py                 # FIXED_SIZE, HIERARCHICAL, SEMANTIC, NONE
  embed.py                    # LM Studio /v1/embeddings, MRL truncate + L2 normalize
  ingest.py                   # list → diff → parse → chunk → embed → bulk; statistics
  filters.py                  # RetrievalFilter → OpenSearch bool query
  retrieve.py                 # Retrieve: semantic / hybrid, score, response shaping
  generate.py                 # RetrieveAndGenerate: prompt templates, citation parsing, sessions
  server.py                   # FastAPI: Bedrock REST routes + S3 event webhook receiver
  cli.py                      # `local-kb create-knowledge-base …` mirroring `aws bedrock-agent`
python/tests/local_kb/        # offline tests with fakes, like the existing 25
```

`pyproject.toml` gets a `local` extra (`opensearch-py`, `pypdf`, `python-docx`, `beautifulsoup4`,
`fastapi`, `uvicorn`, `httpx`, `tokenizers`). That keeps the default install boto3-only, the same
way the existing `db` extra does. New Makefile targets: `local-up`, `local-down`, `local-init`,
`local-sync`, `local-serve`.

Every function takes its clients (S3, OpenSearch, an embedder, an LLM) as arguments, matching
`aws_ai/*.py`. Tests can then run offline with fakes.

---

## 3. Phases

### Phase 0 — Capture ground truth from one real KB ✅ done 2026-11-08

The collection was billed from 05:10 to 05:28 UTC, roughly $0.15–$0.50 of OpenSearch Serverless
time. The sequence:

1. Lifted `aoss:CreateCollection` from the seatbelt in `aws-cloud` and applied it.
2. Ran the three-step KB apply here.
3. Ran `python/scripts/capture_kb_fixtures.py`.
4. Destroyed the KB module and confirmed with `list-collections` and `list-knowledge-bases`.
5. Restored the seatbelt; `make drift` in `aws-cloud` reports no drift.

Results are in §0 and `fixtures/`. These fixtures are the expected values for the Phase 4–6
tests.

Three repo problems surfaced and are fixed:

- **`create_vector_index.py` always got HTTP 403.** OpenSearch Serverless rejects any signed
  request that has a body unless it carries `x-amz-content-sha256`. GETs worked and PUTs didn't,
  which looks exactly like a data-access-policy problem. The script now sends the header.
- **The data-access principal was the SSO session ARN** (`sts:…:assumed-role/…`). The
  `knowledge_base` module is now passed `aws_iam_session_context.issuer_arn`, the underlying role
  ARN. The 403 above masked whether the old ARN also failed, so this is a correctness fix that
  wasn't isolated by testing.
- **`us.*` inference profiles are blocked by the seatbelt's region lock**, because they route to
  other regions. The capture script defaults to `amazon.nova-lite-v1:0` on-demand in us-east-1.
  Current Claude models are reachable only through those profiles, so the Claude default prompt
  could not be captured.

To repeat Phase 0 later, for example to try quick-create field names, follow the same sequence;
the script reproduces every fixture.

### Phase 1 — Infrastructure (`local-kb/compose.yaml`) ✅ done 2026-11-08

`make local-wipe && make local-up && make local-smoke` passes all 8 checks from a clean start.
What it took, beyond the original sketch:

- **The webhook can't reach the host.** RustFS's SSRF guard blocks link-local addresses even
  when they are allow-listed, and rootless podman's `host.containers.internal` is `169.254.1.2`.
  Containers also can't reach host ports bound to 127.0.0.1. So events go to **`kb-events`**, a
  stdlib queue container on the compose network, which a service name makes allowlistable. The
  host polls it on `127.0.0.1:8098`. This is S3 → SQS → poller, a real AWS pattern, and nothing
  on the host listens beyond loopback.
- RustFS persists undelivered events and delivered them once `kb-events` came up. Each record
  carries the object's new ETag.
- The worker's role can't call `GET /`, which needs `cluster:monitor/main`. `init` verifies the
  login with `_plugins/_security/authinfo` instead of widening the role.
- `podman-compose` was installed with `uv tool install podman-compose`.

The sketch as first planned:

| Service | Image | Notes |
|---|---|---|
| `rustfs` | `docker.io/rustfs/rustfs:1.0.4` | `:9000` API, `:9001` console. Credentials from `.env` (`RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY`; never the `rustfsadmin` default). Webhook target `kb` → `http://kb-events:8098/s3-events`, with that origin in `RUSTFS_OUTBOUND_ALLOW_ORIGINS`. Undelivered events are queued on the `/data` volume |
| `kb-events` | `docker.io/library/python:3.13-alpine` running `events/queue.py` | `127.0.0.1:8098`. Accepts webhook POSTs and answers the `HEAD /` health check; `GET /messages?after=N&wait=S` long-polls. In memory: a restart drops undelivered records, which the next full list-and-diff covers |
| `opensearch` | `opensearchproject/opensearch:3.9.0` | `discovery.type=single-node`, `OPENSEARCH_INITIAL_ADMIN_PASSWORD` from `.env`, security plugin **on** (HTTPS + basic auth) to stand in for the AOSS data-access policy. Needs `vm.max_map_count=262144` on the host. Heap 2 GB |
| `dashboards` | `opensearchproject/opensearch-dashboards:3.9.0` | `:5601` |

The RustFS and OpenSearch volumes are named, so `local-down` keeps data and `local-down -v` wipes
it. That is the local version of `terraform destroy`.

`make local-init` (`python/scripts/local_init.py`, idempotent) then runs these steps:

1. Create bucket `kb-data`.
2. Register the event with boto3 `put_bucket_notification_configuration`, the same call you'd make
   against AWS S3: a `QueueConfigurations` entry for `s3:ObjectCreated:*` and
   `s3:ObjectRemoved:*`, prefix `kb/`, target ARN `arn:rustfs:sqs::kb:webhook`. The docs show
   two ARN forms; the empty-region one is accepted.
3. Create a least-privilege OpenSearch role and internal user `bedrock-kb` with index permissions
   `crud`, `search`, `create_index` and `indices:admin/mapping/put` on
   `bedrock-knowledge-base-*`, plus `cluster_composite_ops` (`_bulk`) and search-pipeline admin
   (for hybrid). This is the local version of the Terraform data-access policy, and the worker and
   runtime use this user, never `admin`.
4. *(Phase 8)* Import `dashboards/bedrock-kb.ndjson`.

**Smoke test (`make local-smoke`)** checks the storage assumptions the sync relies on:

- A single-part PUT gets an MD5 ETag, like S3.
- Overwriting changes the ETag; rewriting identical bytes does not.
- A deleted key disappears from `ListObjectsV2`.
- The webhook delivers both a put and a delete under `kb/`, but not outside it.

**Done when:** the smoke test passes, `bedrock-kb` authenticates (`/_plugins/_security/authinfo`),
and Dashboards loads. All three are met.

### Phase 2 — Models in LM Studio ✅ done 2026-11-08

```bash
lms get https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF   # Q8_0, 639 MB (official GGUF)
lms get "qwen/qwen3.5-9b@q6_k"                                  # 7.4 GB + 0.9 GB vision projector
make local-models   # server on :1234, loads kb-embed and kb-llm with the flags below
```

`make local-models` runs:

```bash
lms server start --port 1234
lms load text-embedding-qwen3-embedding-0.6b --identifier kb-embed --gpu max
lms load qwen/qwen3.5-9b --identifier kb-llm --context-length 16384 --parallel 1 --gpu max
```

`qwen/qwen3-embedding-0.6b` is not on the LM Studio hub, so the embedder comes straight from
Qwen's Hugging Face repo.

**What the checks found:**

- **Embedder.** It returns 1024 floats with **norm 1.0**, matching Titan V2's `normalize=true`
  default, so nothing needs normalizing at full size.
  - **LM Studio ignores `dimensions`** (asking for 256 still returns 1024). So `embed.py` truncates
    to 512 or 256 itself and re-normalizes, as planned.
  - `usage` reports 0 tokens, so chunk sizes need a local tokenizer.
- **Thinking.** Qwen3.5 thinks by default, and **only `reasoning_effort: "none"` turns it off**
  in LM Studio. `chat_template_kwargs.enable_thinking`, `reasoning.effort` and a top-level
  `enable_thinking` are all ignored. Left on, a citation prompt spent its entire 2,048-token
  budget reasoning and returned an empty answer. `generate.py` must always send
  `reasoning_effort: "none"`.
- **Bedrock's own prompt works.** Replaying the exact XML prompt captured from the Llama 3 run
  (`fixtures/invocation-logs-llama-nova-pro.json`), with Bedrock's generic inference settings
  (`temperature 0, topP 1, maxTokens 2048, stop ["\nObservation"]`):
  - Every answer was well-formed `<answer><answer_part>…<sources>` XML.
  - The answer cited the right source when the relevant chunk was result 2 of 3.
  - A two-part question cited both sources.
  - Where AWS's own retrieval missed the answer, the reply was a clean "not found".
- **Speed.** About 21–27 tokens/s generating; a 3.7K-token prompt answers in under 4 s.
- **VRAM: 16.6 of 17.2 GB, with 0.5 GB spilled to system RAM (GTT).** The embedder takes 2.3 GB
  (its 8K context buffers), the 9B model about 13.8 GB including the vision projector. Dropping
  from 4 parallel slots to 1 saved only 0.3 GB. If headroom is needed later, load the embedder
  with `--context-length 2048` or `--gpu off`.

**Model choices (unchanged):**

- **Qwen3-Embedding-0.6B** is the only local option matching Titan V2 at every dimension: 1024,
  and MRL-truncatable to 512 and 256.
  - Queries are embedded raw, as Bedrock does (§0). Qwen's recommended `Instruct:` query prefix
    is a Phase 9 experiment.
  - With unit vectors, faiss l2 behaves like cosine, so `1/(1+d²)` scores are comparable to the
    fixtures.
- **Qwen3.5-9B Q6_K**, the stock build rather than the abliterated one. **Alternative:**
  `gpt-oss-20b`, but it doesn't fit next to the embedder in 16 GB.
- **Fallback embedder:** the installed `nomic-embed-text-v1.5`, with `LOCAL_KB_EMBED_DIMS=768`.
  The index dimension follows config: the local version of the "dimensions must match" lesson.

### Phase 3 — Control plane (`control.py`, `ids.py`, `cli.py`) ✅ done 2026-11-08

`python/src/aws_ai/local_kb/`:

- `errors.py`: `ValidationException`, `ResourceNotFoundException` and `ConflictException`, with
  AWS's codes and HTTP statuses.
- `ids.py`: 10-character IDs, and chunk IDs in the `1%3A0%3A…` format.
- `config.py`: endpoints and field names.
- `control.py`: the SQLite store.
- `cli.py`: the `local-kb` command, mirroring `aws bedrock-agent` verbs, JSON output and the
  `An error occurred (…)` error line.

28 new offline tests, 53 in total. They check output key sets against the Phase 0 fixtures, every
chunking range from the botocore model, the one-running-job-per-knowledge-base rule, and
deletion policies.

How this differs from the sketch below:

- **Storage is described as `OPENSEARCH_MANAGED_CLUSTER`** (a `domainEndpoint` of
  `https://127.0.0.1:9200`), which fits a self-hosted cluster better than the Serverless type.
- **The embedding model is the AWS-shaped ARN
  `arn:aws:bedrock:us-east-1::foundation-model/kb-embed`.** Its model id is the LM Studio
  identifier, so botocore accepts it and Phase 4 needs no lookup table.
- **Deletion takes a `purge` callback** that the control plane calls under
  `dataDeletionPolicy: DELETE`, so the control plane stays free of OpenSearch.
- **`inclusionPrefixes` is capped at one prefix**, as the API model requires.
- **There is no update operation,** because chunking is fixed at creation.


SQLite at `local-kb/.state/control.db` (gitignored):

- **`knowledge_bases`**:
  - `knowledgeBaseId` (10 chars `[0-9A-Z]`), name, status
  - `embeddingModelArn`: a local pseudo-ARN, `arn:local:bedrock:::foundation-model/kb-embed`
  - storage configuration: index name and the three `fieldMapping` names
- **`data_sources`**:
  - `dataSourceId`, `bucketArn` (`arn:aws:s3:::kb-data`), `inclusionPrefixes`
  - `chunkingConfiguration`: **immutable after creation**, as on AWS
  - `parsingStrategy` (`BEDROCK_DEFAULT_PARSER` only)
  - `dataDeletionPolicy` (`DELETE` | `RETAIN`)
- **`ingestion_jobs`**:
  - `ingestionJobId` (10 alphanumeric)
  - status (`STARTING | IN_PROGRESS | COMPLETE | FAILED | STOPPING | STOPPED`)
  - the full `IngestionJobStatistics` field set, `failureReasons[]`, `startedAt`, `updatedAt`
  - limit of **one concurrent job per data source**: a second `StartIngestionJob` gets
    `ConflictException`
- **`documents`** (the ledger):
  - `(dataSourceId, s3Uri)` → `etag`, `metadataEtag`, `lastModified`, `chunkIds[]`, `status`
  - This is the stand-in for Bedrock's undocumented change tracking.
  - Change detection is ETag-based. That reproduces every counter in `ingestion-job-2.json`.

The CLI mirrors the AWS CLI verbs and output JSON: `create-knowledge-base`, `create-data-source`,
`start-ingestion-job`, `get-ingestion-job`, `list-ingestion-jobs`, `delete-data-source`.

### Phase 4 — Ingestion (`index.py`, `parse.py`, `chunking.py`, `embed.py`, `ingest.py`) ✅ done 2026-11-08

**Replaying Phase 0 locally matches Bedrock on everything the fixtures record:**

- **Ingestion counters:** both jobs, all 8 counters. Job 1 is 6 scanned / 2 metadata / 6 new;
  job 2 is 5 / 2 / 1 modified / 1 metadata-modified / 1 deleted.
- **The index mapping:** every field and type, including those dynamic mapping added.
- **Chunk text:** all 17 chunks, character for character, with page numbers.
- **Chunk fields and types:** identical to Bedrock's.
- **The sidecar-only change:** rewritten in place under the same `_id`, with no embedding call
  and `id` overwritten by the `_id`.

A job over the sample documents takes about 1 s.

What it took:

- **Default chunking is sentence packing.** Chunks start *and* end on sentences. Whole sentences
  are packed up to 300 tokens, and the next chunk repeats the trailing sentences that fit in 20%.
- **Calibrating to Titan's tokenizer.** Tokens are counted with Qwen3's tokenizer and scaled.
  Every scale from 1.11 to 1.16 Qwen tokens per Titan token reproduces all 12 inventory
  boundaries; `chunking.QWEN_TOKENS_PER_TITAN_TOKEN = 1.13`.
- **PDF parsing.** pypdf's layout extraction plus Bedrock's joining rules:
  - wrapped lines: one space;
  - paragraphs: three spaces;
  - pages: nothing.
- **Disk watermarks.** OpenSearch's defaults (85/90/95%) blocked index creation on a 94%-full
  disk with 56 GB free. compose.yaml now sets the watermarks as free space (10/5/2 GB), and
  `local_init.py` clears the persistent `cluster.blocks.create_index` such a block leaves behind.
- **The worker role needed `indices:admin/get`** to check whether the index exists. Reading the
  mapping stays admin-only.
- **`local-kb watch` (`make local-watch`) is the trigger.** It long-polls kb-events, runs an ETag
  poll every 60 s, waits 10 s after the last event, then syncs.
  - Before starting a job it re-checks the bucket against the ledger. Delivery is at-least-once,
    and a late duplicate event otherwise started a job with nothing to do.
  - `local-kb sync` is the console's Sync button.

18 new offline tests (71 in total):

- parser output against Bedrock's stored text;
- every chunking strategy;
- the full Phase 0 sequence against fakes, asserting both fixture statistics and that only one
  embedding call happens on the second sync.

The sketch as first planned:


**Index (`index.py`).** Create the index once, with only the fields Bedrock's own setup declares:

```json
{ "settings": { "index": { "knn": true, "knn.algo_param.ef_search": 512 } },
  "mappings": { "properties": {
    "bedrock-knowledge-base-default-vector": { "type": "knn_vector", "dimension": 1024,
      "method": { "name": "hnsw", "engine": "faiss", "space_type": "l2",
                  "parameters": { "m": 16, "ef_construction": 512 } } },
    "AMAZON_BEDROCK_METADATA":   { "type": "text", "index": false },
    "AMAZON_BEDROCK_TEXT_CHUNK": { "type": "text", "index": true } } } }
```

- **Do not map the `x-amz-bedrock-kb-*` fields, `id`, or sidecar attributes.** Leave them to
  dynamic mapping. It produces exactly `index-mapping.json`: strings → `text` + `.keyword`,
  numbers (written as JSON floats) → `float`, booleans → `boolean`, page number (written as an
  int) → `long`. That is what makes them show up in the Dashboards field list, and what makes
  `startsWith` need `.keyword`.
- The text field name comes from the KB's `fieldMapping` record, so if quick-create turns out to
  use `AMAZON_BEDROCK_TEXT`, changing it is configuration, not code.
- Refactor `python/scripts/create_vector_index.py` to import the body from `index.py`, so the
  cloud and local indexes cannot drift.

**The job (`ingest.py`).** For each data source:

1. **List.** `ListObjectsV2` under each `inclusionPrefix`. Pair `X` with `X.metadata.json`,
   reject sidecars over 10 KB, and count `numberOfDocumentsScanned` and
   `numberOfMetadataDocumentsScanned`.
2. **Diff against the ledger.**

   | Case | Action | Counter (as observed) |
   |---|---|---|
   | New file | ingest | `numberOfNewDocumentsIndexed` |
   | Content ETag changed | delete old chunks and re-ingest | `numberOfModifiedDocumentsIndexed` |
   | Only the sidecar changed (non-CSV) | `_update_by_query` the attribute fields **in place**, same `_id`, no embedding call, and set `id` to the `_id` value (the observed quirk) | `numberOfMetadataDocumentsModified` **only** |
   | Unchanged | nothing | **none**: `numberOfDocumentsSkipped` stayed 0 |
   | In the ledger but gone from the bucket | `_delete_by_query` on `x-amz-bedrock-kb-source-uri.keyword` + `x-amz-bedrock-kb-data-source-id.keyword` | `numberOfDocumentsDeleted` |

   - The only metadata change that *does* need re-embedding is a flip of `includeForEmbedding`.
   - `numberOfDocumentsSkipped` is reserved for files skipped for a reason, such as an
     unsupported type. That reading is an inference, since Phase 0 had no such file.
3. **Parse (`parse.py`).** This is the default parser: text only and free. Reproduce the
   normalization rules observed in §0, with a test against each fixture's
   `AMAZON_BEDROCK_TEXT_CHUNK`:
   - `.txt`: newline → space, so a blank line becomes two spaces.
   - `.md`: collapse whitespace to single spaces, keeping `#`.
   - `.html`: `<title>` + markdown conversion.
   - PDF: lines joined with three spaces, pages concatenated with no separator.
   - Also `.csv` and `.doc(x)`.
   - PDFs are read with pypdf **page by page**, keeping each page's start offset, so a chunk gets
     the page it **starts** on.
   - Files over 50 MB, and unsupported types, increment `numberOfDocumentsFailed` with a
     `failureReasons` entry, and the job continues.
   - Bedrock's FM and Data Automation parsers are out of scope. They could later be a
     vision-model parse with the Qwen3.5 model, which has vision.
4. **Chunk (`chunking.py`).** Count tokens with the embedding model's own tokenizer
   (`tokenizers`). Bedrock's tokenizer is not documented, so chunk boundaries are an
   approximation.
   - **Default:** 300 tokens with about 20% overlap. Start each chunk on a sentence boundary and
     let it end at the token limit, even mid-sentence (observed). Target: the inventory file
     splits into 12 chunks of about 1,030 characters, each overlapping the previous one by 1–2
     lines.
   - **`FIXED_SIZE`:** `maxTokens` 1–8192 and `overlapPercentage` 1–99. Console default is
     300 / 20% [unverified].
   - **`HIERARCHICAL`:** `levelConfigurations` of exactly two levels (parent, child) plus
     `overlapTokens`. Only children are embedded and indexed. The parent text goes into
     `AMAZON_BEDROCK_METADATA.parentText`.
   - **`SEMANTIC`:** `maxTokens`, `bufferSize` 0–1, and `breakpointPercentileThreshold` 50–99.
     Embed sentence windows, split where adjacent cosine distance exceeds the percentile, and
     re-split anything over `maxTokens`. This costs extra embedding calls, as on AWS.
   - **`NONE`:** one chunk per file, with no page number.
5. **Embed (`embed.py`).** Batches of 32 to LM Studio.
   - Prepend each typed sidecar attribute with `includeForEmbedding: true` as `key: value\t\n\n`
     (the tab is in the fixture) to the **embedded** text only. The stored
     `AMAZON_BEDROCK_TEXT_CHUNK` stays clean.
   - Embed queries raw, as Bedrock does. Qwen3-Embedding *recommends* an `Instruct:` query
     prefix; whether to add it is a measured choice for Phase 9, off by default to stay faithful.
6. **Write (`_bulk`).** One document per chunk, with **`_id` set explicitly**, not auto-generated:

   ```json
   PUT bedrock-knowledge-base-default-index/_doc/1%3A0%3ApeaYuaEBYQaVZPWvI07K
   { "bedrock-knowledge-base-default-vector": [ …1024 floats, unit norm… ],
     "AMAZON_BEDROCK_TEXT_CHUNK": "# Dew heater guide Dew forms on …",
     "AMAZON_BEDROCK_METADATA": "{\"source\":\"s3://kb-data/kb/dew-heater-guide.md\"}",
     "x-amz-bedrock-kb-source-uri": "s3://kb-data/kb/dew-heater-guide.md",
     "x-amz-bedrock-kb-data-source-id": "9WZXXZH6ZH",
     "x-amz-bedrock-kb-source-file-modality": "TEXT",
     "id": "302f58c2-e7c0-4240-8d25-a480666448d5",
     "department": "operations", "revision": 3.0, "effective_date": 20261015.0,
     "safety_critical": true, "tags": ["dew", "heater", "maintenance"] }
   ```

   - `_id` = `quote("1:0:" + 20 random URL-safe characters, safe="")`. The same value is
     returned as `x-amz-bedrock-kb-chunk-id`; there is no stored chunk-id field.
   - `id` is a fresh `uuid4()` per chunk.
   - `x-amz-bedrock-kb-document-page-number` is an int, PDF only (omitted otherwise, and under
     `NONE` chunking).
   - Write sidecar `NUMBER`s as JSON floats, so dynamic mapping picks `float` as on AWS. That
     copies the **precision gotcha** too: a float32 holds integers exactly only up to 2²⁴, so
     `20261015` is indexed as `20261016` (the `_source` and Retrieve metadata keep `20261015.0`), so a
     range filter can be off by one. Worth a test, and a callout in the README.
   - Reject custom keys starting with `x-amz-bedrock` (reserved).
7. **Finish.** Update the ledger and statistics and set status `COMPLETE`. The job is `FAILED`
   only on infrastructure errors; per-document failures are reported, not fatal.

**Sync trigger (`server.py`).** There are two inputs, and both feed the same debounce:

- **Queue (`kb-events`).** Long-poll `GET /messages?after=<last seq>&wait=20` on
  `127.0.0.1:8098`. Records carry `eventName`, the decoded `key` and the new `eTag`. Delivery is
  at-least-once, so an event only ever marks the matching data source dirty.
- **Poll (`LOCAL_KB_POLL_SECONDS`, default 60, 0 = off).** `ListObjectsV2` under each
  `inclusionPrefix`, compared with the ledger's ETags. This works on any S3 store, MinIO and
  Garage included.

After 10 s with no new events, the trigger calls `StartIngestionJob` for the matching data
source. If a job is already running, it sets a "dirty" flag and starts one more job when that one
finishes. There is also a `make local-sync` manual path, which is the console's **Sync** button.

**Direct ingestion.** `IngestKnowledgeBaseDocuments` (inline TEXT plus inline attributes,
1–25 docs) writes straight to the index without touching the bucket. A later sync overwrites those
changes, as AWS documents. This is a stretch goal.

**Done when:** `sample-docs/` syncs to 17 chunks and the statistics equal
`ingestion-job-1.json` and `ingestion-job-2.json` exactly. Every chunk's text must also match its
fixture counterpart, except for chunk boundaries, which come from a different tokenizer (see the
risks).

### Phase 5 — Retrieve (`filters.py`, `retrieve.py`)

**Request and response.** Match `bedrock-agent-runtime` exactly:

- **In:** `retrievalQuery.text` and `retrievalConfiguration.vectorSearchConfiguration`.
  - `numberOfResults`: 1–100, default 5.
  - `overrideSearchType`: `HYBRID | SEMANTIC`. If omitted, use SEMANTIC; hybrid is available
    because the text field is indexed.
  - `filter`.
- **Out:** `{"retrievalResults": [...]}` only, each with `content.{text,type:"TEXT"}`,
  `location.{type:"S3", s3Location.uri}`, `metadata`, and `score`. Phase 0 showed **no**
  `documentId` and no `nextToken`, despite the API reference.
- **`metadata`** = the top-level `x-amz-bedrock-kb-*` fields + `x-amz-bedrock-kb-chunk-id`
  (= `_id`) + the sidecar attributes. Numbers are returned as floats, **page number included**
  (`1.0`). Leave out `id`, the vector, and the `AMAZON_BEDROCK_*` fields.

**Semantic search.** A faiss `knn` query on the vector field, with the filter placed **inside**
`knn.filter` (efficient filtering), so a selective filter still returns k results.
`score = 1/(1+d²)`, which is OpenSearch's own l2 score passed through.

**Hybrid search.**

- An OpenSearch `hybrid` query: [knn, `match` on `AMAZON_BEDROCK_TEXT_CHUNK`].
- It goes through a search pipeline, `bedrock-kb-hybrid`, made of a `normalization-processor`
  with `min_max` and `arithmetic_mean`, weights `[0.3, 0.7]` (text, vector).
- AWS doesn't document its fusion, so this is an honest stand-in. An `rrf` variant
  (`score-ranker-processor`) behind a flag lets you compare the two.
- **Caveat from Phase 0:** real Bedrock returned identical results for HYBRID and SEMANTIC on
  `"ZX-4471"`. A real hybrid locally will *beat* AWS on that query. Keep that difference visible
  (a test that asserts the local hybrid does rank the dew guide's exact-match chunk first) rather
  than faking AWS's result.

**Filters (`filters.py`).** Translate `RetrievalFilter` into an OpenSearch `bool` query:

| Bedrock filter | OpenSearch query |
|---|---|
| `equals` | `term` (`.keyword` for strings) |
| `notEquals` | `must_not term`. Note: like Bedrock, it also matches documents missing the key |
| `greaterThan(OrEquals)` / `lessThan(OrEquals)` | `range` |
| `in` / `notIn` | `terms` / `must_not terms` |
| `startsWith` | `prefix` on `.keyword` |
| `stringContains` | `wildcard *v*` on `.keyword` |
| `listContains` | `term` |
| `andAll` / `orAll` | `bool.filter` / `bool.should` with `minimum_should_match: 1`; at least 2 children |

- Reject unknown operators and more than one operator per node with a `ValidationException`, the
  same error class boto3 raises.
- `implicitFilterConfiguration`: the LLM is given the attribute schema and returns a filter JSON,
  which then goes through the same translator. This is a stretch goal.

**Hierarchical chunking.** Replace each child hit with its `parentText` and merge children that
share a parent, so fewer than `numberOfResults` can come back, as on AWS.

**Done when:** each filter operator has a test, and hybrid versus semantic visibly reorders an
exact-keyword query, for example a product code.

### Phase 6 — RetrieveAndGenerate (`generate.py`)

1. **Session.** No `sessionId` means generate a `uuid4()` and return it (that is the observed
   format); a supplied one means load its history.
2. **Orchestration (only when there is history, or when `QUERY_DECOMPOSITION` is set).** Use the
   captured rewrite and decomposition prompts verbatim from `invocation-logs.json`. Parse
   `Action: FUNCTION(query="…")` for a rewrite, and
   `Action: FUNCTION(standalone_question="…", additional_queries=[…])` for decomposition. Then
   retrieve for each query and de-duplicate.
3. **Retrieve,** using Phase 5.
4. **Generate, through two prompt families, as Bedrock does.** Each prompt is copied verbatim
   from the invocation logs into `prompts/`:
   - **`generic`**: the XML template captured from the Llama 3 run. This is the default for the
     local model, since Bedrock uses it for every non-Nova model seen. Results render as
     `<search_result>\n<content>\n…\n</content>\n<source>\nN\n</source>\n</search_result>`.
     The output-format block, including its "no inline citations" paragraph, is
     `$output_format_instructions$`. Request: `maxTokens 2048, temperature 0, topP 1,
     stopSequences ["\nObservation"]`.
   - **`nova`**: the captured "A chat between a curious User…" system prompt, with
     `Passage %[n]% : …` results and inline `%[n]%` markers. Request: `temperature 0, topP 1`.
     Selectable by config, so the local model can be run under either prompt and compared.
   - Both are sent like Converse: the rendered template is the **system** message and the query
     is the **user** message. That maps directly to LM Studio's `/v1/chat/completions`.
5. **Parse citations, matching the observed shapes.**
   - **`generic`:** each `<answer_part>` becomes one citation with
     `textResponsePart.{text, span:{start,end}}` and the `retrievedReferences` its `<source>`s
     name. **If the XML is missing, return `"Sorry, I am unable to assist you with this
     request."`**, which is what Bedrock did with Llama.
   - **`nova`:** split on the `%[n]%` markers, strip them from `output.text`, and attach the
     results they name.
   - **No answer, or no `$output_format_instructions$`:** return **one** citation spanning the
     whole text with `retrievedReferences: []`, not an empty list.
6. **Stream (`/retrieveAndGenerateStream`).** Emit `output`, `citation` and `guardrail` events,
   with citations sent right after the text part they cover. The session ID goes in the
   `x-amzn-bedrock-knowledge-base-session-id` header.
7. **Inference config.** `inferenceConfig.textInferenceConfig.{maxTokens, temperature, topP,
   stopSequences}` passes through to LM Studio. `modelArn` is accepted and mapped to `kb-llm`.

### Phase 7 — Runtime server (`server.py`)

A FastAPI app on `:8099`. Routes are copied from the installed botocore service models (verified):

| Operation | Route |
|---|---|
| CreateKnowledgeBase | `PUT /knowledgebases/` |
| CreateDataSource | `PUT /knowledgebases/{kbId}/datasources/` |
| StartIngestionJob | `PUT /knowledgebases/{kbId}/datasources/{dsId}/ingestionjobs/` |
| GetIngestionJob | `GET /knowledgebases/{kbId}/datasources/{dsId}/ingestionjobs/{jobId}` |
| ListIngestionJobs | `POST /knowledgebases/{kbId}/datasources/{dsId}/ingestionjobs/` |
| IngestKnowledgeBaseDocuments | `PUT /knowledgebases/{kbId}/datasources/{dsId}/documents` |
| Retrieve | `POST /knowledgebases/{kbId}/retrieve` |
| RetrieveAndGenerate | `POST /retrieveAndGenerate` |
| RetrieveAndGenerateStream | `POST /retrieveAndGenerateStream` (needs AWS event-stream framing; stretch) |

- The SigV4 header is accepted and ignored, so stock boto3 works with dummy credentials.
- Errors use the AWS JSON error shape and the `x-amzn-ErrorType` header, so botocore raises the
  modeled exceptions (`ValidationException`, `ResourceNotFoundException`, `ConflictException`).

**The litmus test.** The existing `python/src/aws_ai/bedrock.py` style of code works unchanged:

```python
rt = boto3.client("bedrock-agent-runtime", endpoint_url="http://localhost:8099",
                  region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
rt.retrieve_and_generate(input={"text": "..."}, retrieveAndGenerateConfiguration={...})
```

### Phase 8 — OpenSearch Dashboards: show the AWS internal fields

`dashboards/bedrock-kb.ndjson`, imported by `local-init`, contains:

1. **Index pattern** `bedrock-knowledge-base-default-index`, with no time field (KB chunks have
   no timestamp). The vector field is excluded from Discover through the pattern's
   `sourceFilters`.
2. **Saved search "Bedrock KB — internals",** with columns `x-amz-bedrock-kb-source-uri`,
   `x-amz-bedrock-kb-data-source-id`, `x-amz-bedrock-kb-source-file-modality`,
   `x-amz-bedrock-kb-document-page-number`, `id`, `AMAZON_BEDROCK_TEXT_CHUNK`, and
   `AMAZON_BEDROCK_METADATA`. Also show `_id`, since it is the chunk-id.
3. **Visualization "Chunks per source document":** a terms aggregation on
   `x-amz-bedrock-kb-source-uri.keyword`.
4. **README walkthrough,** reproducing the course demo:
   - In Discover, type `x-amz-bedrock` in the field-name search box, which leaves only Bedrock's
     internal fields.
   - Then type `AMAZON_BEDROCK`.
   - Then try `x-amz-bedrock-kb-source-uri.keyword : s3\://kb-data/kb/tidewater*` as a prefix
     filter.
5. **Dev Tools snippets:**
   - `GET bedrock-knowledge-base-default-index/_mapping` shows which fields dynamic mapping added.
   - A `prefix` query on `x-amz-bedrock-kb-source-uri.keyword`.
   - A raw `knn` query, for comparison with what `Retrieve` returns.

### Phase 9 — Tests and an evaluation harness

- **Unit tests, offline with fakes:** chunkers (boundaries, overlap, hierarchical parent linkage),
  filter translation (one case per operator), ledger diff (all five cases), citation parsing
  (well-formed, malformed, missing `$output_format_instructions$`), chunk-id encoding.
- **Fixture tests** (offline): each `fixtures/*.json` is a golden file. Compare key sets, types
  (`1.0` floats, `_id` format, no `documentId`), the ingestion counters, the parser's text
  normalization, and the rendered prompts character for character. Values themselves are
  ignored: the scores and IDs will differ.
- **Integration test** (`pytest -m local`, needs the stack up): the full sync of `sample-docs/`,
  then `retrieve` and `retrieve_and_generate` through **stock boto3** against `:8099`.
- **Evaluation:** 15–20 hand-written Q&A pairs over `sample-docs/`, scored on retrieval recall@5
  and answer correctness by an LLM judge (the local LLM).
  - Seed it with the Phase 0 failure: the camera-temperature question that AWS never retrieved.
    That makes it a fidelity check (local should miss it too under default chunking) and a
    lesson: switch to `FIXED_SIZE` 100 tokens, or semantic chunking, and watch it get fixed.
  - This is the AIF-C01 evaluation topic (RAG evaluation, LLM-as-judge) made concrete.

---

## 4. Deliberately out of scope

| Feature | Why |
|---|---|
| Foundation-model and Data Automation parsing, multimodal (image) chunks | Needs vision parsing plus `x-amz-bedrock-kb-byte-content-source`. Possible later with Qwen3.5's vision |
| Reranking (`rerankingConfiguration`) | Could be added with a local cross-encoder (e.g. `bge-reranker-v2-m3`), but LM Studio has no rerank endpoint |
| Guardrails on RetrieveAndGenerate | `guardrailAction` is always `NONE`. The cloud `bedrock` module's guardrail is the place to study that |
| Non-S3 connectors (Web crawler, Confluence, SharePoint), GraphRAG, structured (SQL) KBs | Not part of "local S3 → vectors" |
| Agents for Bedrock | Separate service |

## 5. Risks and open questions

1. **RustFS maturity.** It reached 1.0 only recently. The smoke test pins the behaviours the sync
   depends on, so a regression shows up as a failed check rather than wrong counters. If it
   misbehaves, any S3 store plus the polling trigger is the fallback.
2. **ROCm on gfx1102.** LM Studio already runs your 9B on the ROCm runtime, so this looks fine.
   If embeddings and the LLM fight over VRAM, move the embedder to the CPU runtime: it is 0.6B and
   CPU is fast enough.
3. **What Phase 0 could not settle:**
   - Quick-create field names (the run used this repo's own index).
   - Hybrid fusion (AWS showed no hybrid effect at all).
   - The Claude-family default prompt (blocked by the region lock).
   - Phase 0 sampled one run of each behaviour, not every case.

   Each of these sits behind a constant, config value or prompt file, so a new fixture corrects
   it in one place.
4. **Token counting differs from Bedrock's.** Titan's tokenizer isn't public, so chunk boundaries
   will be close to AWS's (target: the same 12 inventory chunks) but not byte-identical. To
   calibrate, tune the per-model token ratio until the inventory file splits at the same `INV-`
   lines as the fixture.

## 6. Suggested build order

Phases 1 → 2 → 3 → 4 (with the Dashboards import from Phase 8 as soon as the first chunks land) →
5 → 6 → 7 → 9. Phase 0 is done; its fixtures are the tests' expected values from Phase 4 on. Each
phase is one PR, small enough for `make test lint typecheck` to stay green.

---

[setup]: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html
[sync]: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html

## Sources

- Bedrock user guide — set up a vector index (OpenSearch Serverless and managed clusters) — <https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html>
- Bedrock user guide — sync / incremental ingestion — <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html>
- Bedrock user guide — query configuration: filters, placeholders, reserved keys — <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html>
- Bedrock user guide — chunking strategies — <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html>
- Bedrock user guide — S3 data source and .metadata.json sidecars — <https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html>
- API reference — ChunkingConfiguration — <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_ChunkingConfiguration.html>
- API reference — IngestionJobStatistics — <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestionJobStatistics.html>
- API reference — IngestionJob — <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestionJob.html>
- What's New — hybrid search for knowledge bases (2024-03) — <https://aws.amazon.com/about-aws/whats-new/2024/03/knowledge-bases-amazon-bedrock-hybrid-search/>
- aws-samples — OpenSearch Serverless index creation (oss_utils.py) — <https://github.com/aws-samples/amazon-bedrock-samples/blob/main/rag/knowledge-bases/features-examples/04-infrastructure/e2e-rag-using-bedrock-kb-cfn/lambdas/oss_utils.py>
- aws-samples — custom prompting and number of results — <https://github.com/aws-samples/amazon-bedrock-samples/blob/main/docs/rag/knowledge-bases/features-examples/01-rag-concepts/02_managed_rag_custom_prompting_and_no_of_results.md>
- aws/bedrock-agentcore-starter-toolkit — default KB prompt fixtures — <https://github.com/aws/bedrock-agentcore-starter-toolkit/blob/main/src/bedrock_agentcore_starter_toolkit/services/import_agent/assets/template_fixtures_merged.json>
- aws-samples — direct ingestion example showing Retrieve metadata — <https://github.com/aws-samples/amazon-bedrock-samples/blob/main/docs/rag/knowledge-bases/use-case-examples/direct-ingestion-of-documents-using-custom-datasource/google-drive-document-ingestion.md>
- OpenSearch docs — k-NN spaces and score formulas — <https://github.com/opensearch-project/documentation-website/blob/main/_mappings/supported-field-types/knn-spaces.md>
- Classmethod — parentText in AMAZON_BEDROCK_METADATA (hierarchical chunking) — <https://dev.classmethod.jp/en/articles/bedrock-kb-metadata-parenttext-created-on/>
- Classmethod — KB metadata as OpenSearch fields — <https://dev.classmethod.jp/en/articles/amazon-bedrock-knowledge-bases-sync-metadata-amazon-opensearch-service/>

Further reading: AWS Prescriptive Guidance, *Retrieval Augmented Generation options and
architectures on AWS* — <https://docs.aws.amazon.com/prescriptive-guidance/latest/retrieval-augmented-generation-options/what-is-rag.html>.
It describes the four stages this plan follows (ingest → query → orchestrate and retrieve →
generate) and the managed-versus-custom trade-off that the cloud and local KBs make concrete.
