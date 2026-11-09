# aws_ai

Thin boto3 wrappers for the AWS AI services on the AIF-C01 exam.

Every function takes its client as an argument instead of constructing one. That keeps the tests
offline — a fake client stands in for `bedrock-runtime` and `comprehend` — and lets a caller pass a
client configured for a different region or profile without any change here.

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
```

Usage:

```python
import boto3
from aws_ai import detect_sentiment, invoke_text_model

reply = invoke_text_model(
    boto3.client("bedrock-runtime", region_name="us-east-1"),
    "amazon.nova-lite-v1:0",
    "Explain inference latency in one sentence.",
    guardrail_id="<terraform output bedrock_guardrail_id>",
    guardrail_version="1",
)
print(reply.text, reply.blocked_by_guardrail)

sentiment = detect_sentiment(boto3.client("comprehend"), "This exam guide is excellent.")
print(sentiment.label, sentiment.confidence)
```

Calling Bedrock needs model access enabled in the console for that account and region; Terraform
cannot grant it.

## `aws_ai.local_kb`

A local Amazon Bedrock knowledge base. See [`local-kb/README.md`](../local-kb/README.md) for the
architecture and every component. It needs the `local` extra (pypdf, BeautifulSoup, markdownify,
tokenizers, FastAPI, uvicorn) and installs the `local-kb` command:

```bash
uv sync --extra dev --extra local
uv run local-kb --help
uv run local-kb serve --watch      # bedrock-agent + bedrock-agent-runtime for boto3 on :8099
```

| Module | What it does |
|---|---|
| `cli` | The `local-kb` command: `aws bedrock-agent` / `bedrock-agent-runtime` verbs, plus `sync`, `run-pending`, `watch`, `serve`, `evaluate`, `bench` and `mcp` |
| `server` | Bedrock's REST routes (FastAPI), plus a background ingestion worker |
| `mcp_server` | `local-kb mcp`: the knowledge base as MCP tools (search, ask, list) for agents, over stock boto3 |
| `control` | Knowledge bases, data sources, ingestion jobs, the document ledger and sessions, in SQLite |
| `ingest` | The ingestion job: list → diff → parse → chunk → embed → write, with Bedrock's counters |
| `parse` | The default parser, reproducing Bedrock's text normalization per file type; `.metadata.json` sidecars |
| `chunking` | FIXED_SIZE (the default), HIERARCHICAL, SEMANTIC and NONE, sized in Titan-equivalent tokens |
| `embed` | LM Studio embeddings (Titan-shaped) and chat (Converse-shaped) |
| `store` | The vector-store seam: store-neutral chunks and hits, and `store_for()` by storage type |
| `store_opensearch` | The OpenSearch store: Bedrock's document layout, k-NN and hybrid search, result shape |
| `store_rds` | The RDS (Aurora PostgreSQL + pgvector) store: Bedrock's table layout and the SQL it runs |
| `bench` | `local-kb bench`: both stores on the same chunks and vectors; recall, latency, load and build times |
| `bench_resources` | The bench's resource sampler: container RSS and CPU per phase, image and volume sizes |
| `index`, `opensearch` | The vector index Bedrock expects, and a minimal OpenSearch client |
| `trigger` | Bucket events and an ETag poll, debounced into ingestion jobs |
| `filters`, `retrieve` | `RetrievalFilter` → OpenSearch, and Retrieve (SEMANTIC and HYBRID) |
| `generate`, `prompts/` | RetrieveAndGenerate with Bedrock's own prompt templates and citation parsing |
| `evaluate` | The evaluation harness: retrieval hit@k, keywords, LLM-as-judge |
| `config`, `ids`, `errors`, `services` | Endpoints and names, Bedrock's id formats, its modeled errors, and clients for the running stack |
