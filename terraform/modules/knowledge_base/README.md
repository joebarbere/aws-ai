# knowledge_base

Managed RAG: documents in S3, embedded by Bedrock, stored in an OpenSearch Serverless vector
collection, queried through `RetrieveAndGenerate`.

## Cost

**~$350/month, and Bedrock is not the expensive part.** An OpenSearch Serverless vector collection
has a floor of 2 OCUs (1 indexing, 1 search) at roughly $0.24/OCU-hr, billed from the moment the
collection exists whether or not anything queries it. Embedding and generation cost cents by
comparison.

This is the second-most expensive thing in the repo, after Kendra. Destroy it the same day.

## It applies in three steps, and the middle one is not Terraform

A knowledge base needs a vector index that **already exists** inside the collection, and
OpenSearch Serverless index creation is an HTTP call to the collection endpoint, not a
control-plane API — so no Terraform resource exists for it.

```bash
# 1. collection, policies, role
terraform apply -var enable_knowledge_base=true

# 2. the index itself (SigV4-signed HTTP, no extra dependencies)
python ../python/scripts/create_vector_index.py \
  --endpoint "$(terraform output -raw kb_collection_endpoint)" \
  --region   "$(terraform output -raw region)"

# 3. the knowledge base and its data source
terraform apply -var enable_knowledge_base=true -var kb_vector_index_ready=true
```

Skipping step 2 makes step 3 fail with a validation error that does not mention the missing index.

## Two things that catch people

**Dimensions must match.** Titan Text Embeddings V2 emits 1024 dimensions. The index created in
step 2 uses 1024. Change `embedding_model_id` and you must change the index to match, or ingestion
fails per-document.

**IAM is not enough.** OpenSearch Serverless has its own data-access policy layer. A role with
`aoss:APIAccessAll` in IAM still reads nothing unless a data-access policy names that principal —
which is why `additional_data_access_principals` exists for your own user.

## Ingesting

Upload under the prefix, then start an ingestion job:

```bash
aws s3 cp notes.pdf "s3://$(terraform output -raw data_bucket)/kb/"
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id "$(terraform output -raw kb_knowledge_base_id)" \
  --data-source-id    "$(terraform output -raw kb_data_source_id)"
```
