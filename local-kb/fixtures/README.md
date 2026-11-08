# Phase 0 fixtures

Captured from a real Bedrock knowledge base on 2026-11-08: the cloud `knowledge_base` module, run
for about 18 minutes and then destroyed. The run used `python/scripts/capture_kb_fixtures.py`
against `local-kb/sample-docs/kb/`. The account id is replaced with `000000000000` throughout.

**Setup:**

- Embeddings: Titan Text Embeddings V2, 1024 dimensions.
- Vector index: created by `create_vector_index.py`, not by console quick-create. Field names are
  therefore the ones this repo chose (`AMAZON_BEDROCK_TEXT_CHUNK`); everything else in the
  mapping was added by Bedrock.
- Data source: default chunking and the default parser.
- Generation: Nova Lite. `rag-llama3-8b.json` and `rag-nova-pro.json` come from a follow-up call
  made a few minutes later with the same code, and their prompts are in
  `invocation-logs-llama-nova-pro.json`.

| File | What it settles |
|---|---|
| `knowledge-base.json`, `data-source.json` | The defaults Bedrock fills in, e.g. `dataDeletionPolicy: DELETE`, and no `vectorIngestionConfiguration` when chunking is left at its default |
| `index-mapping.json` | The fields Bedrock added through dynamic mapping |
| `index-documents.json` | Every stored chunk's `_id` and `_source`, with the vector dropped (a length/norm probe is kept) |
| `ingestion-job-1.json`, `ingestion-job-2.json` | Statistics for the first sync, and for the second after one edit, one delete and one sidecar-only change |
| `index-documents-after-mutation.json` | The index 34 s after the second job reported `COMPLETE`. **This snapshot is not settled:** the edited file's new chunk is missing and the deleted file is still present, which is evidence of OpenSearch Serverless's eventual consistency |
| `retrieve-*.json` | Raw `Retrieve` responses: semantic, hybrid, and each filter operator |
| `rag-*.json` | Raw `RetrieveAndGenerate` responses |
| `invocation-logs*.json` | The exact model requests Bedrock made: embedding inputs, including the `includeForEmbedding` prefix, and the rendered generation and orchestration prompts |

What these files show is summarized in `../PLAN.md` §0.

`rds/` holds the same capture against Bedrock's other vector store, Aurora PostgreSQL with pgvector
(Phase 0b of `../PGVECTOR_PLAN.md`). It includes every SQL statement Bedrock ran; see
`rds/README.md`.
