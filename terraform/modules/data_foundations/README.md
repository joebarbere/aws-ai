# data_foundations

The Glue Data Catalog that feeds training data, and an Aurora PostgreSQL cluster with pgvector as a
second RAG store to compare against OpenSearch Serverless.

## Cost

| Thing | Charge |
|---|---|
| Glue Data Catalog | First million objects free |
| Glue crawler | ~$0.44/DPU-hr **only while running**, 10-minute minimum |
| Glue job | Same rate, only while running; `timeout = 10` caps a runaway |
| Aurora Serverless v2 | ~$0.12/ACU-hr, **0.5 ACU floor ≈ $43/month** |

Aurora is the hourly one and is gated off. Note that Serverless v2 **does not scale to zero** — the
floor bills continuously from creation. It is still roughly an eighth the cost of the OpenSearch
Serverless collection, which is exactly why having both is instructive.

## pgvector needs a third step

`CREATE EXTENSION vector;` is SQL against a running database, so no Terraform resource exists —
the same shape as the vector index in `modules/knowledge_base`:

```bash
terraform apply -var enable_data_foundations=true -var enable_aurora=true \
                -var aurora_publicly_accessible=true \
                -var 'allowed_cidr_blocks=["203.0.113.4/32"]'   # your address

python ../python/scripts/enable_pgvector.py \
  --endpoint "$(terraform output -raw aurora_endpoint)" \
  --secret-arn "$(terraform output -raw aurora_secret_arn)" \
  --region "$(terraform output -raw region)"
```

The script needs `psycopg` (`uv sync --extra db`). Without a public IP you must run it from inside
the VPC.

## Two security choices worth reading

**The password is never in state.** `manage_master_user_password = true` hands generation and
rotation to Secrets Manager. The alternative — `master_password` in a variable — puts a live
credential in `terraform.tfstate`, which is the most common way a study repo leaks one.

**Ingress defaults to nothing.** No CIDR means no ingress rule at all, and
`allowed_cidr_blocks` refuses `0.0.0.0/0` via a validation block. Open it to your own address only.

## Comparing the two RAG stores

Same documents, same embedding model, two stores:

| | OpenSearch Serverless | Aurora + pgvector |
|---|---|---|
| Cost floor | ~$350/month | ~$43/month |
| Bedrock KB integration | Native | Also supported |
| You manage | Nothing | Schema, indexes, vacuum |
| Index type | HNSW/faiss | `ivfflat` or `hnsw` |

The exam cares that you know a knowledge base can sit on either. Running both for an afternoon
makes the tradeoff concrete in a way the docs do not.
