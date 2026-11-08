# knowledge_base_rds

A Bedrock knowledge base on **Aurora PostgreSQL Serverless v2 + pgvector**: Bedrock's `RDS` vector
store, and the alternative to OpenSearch Serverless in [`../knowledge_base`](../knowledge_base).
It exists to record what Bedrock writes and runs against Postgres (local-kb/PGVECTOR_PLAN.md
§1), so the local pgvector store can copy it.

## Cost

About **$0.06/hour**: 0.5 ACU at about $0.12/ACU-hr, plus storage and one secret. That is about
$43/month if forgotten, an eighth of the OpenSearch Serverless floor. The minimum stays at 0.5
rather than 0, because a paused cluster takes about 15 s to resume, which is longer than Bedrock
waits.

The account seatbelt denies `rds:CreateDBCluster` and `rds:CreateDBInstance`. Lift that deny for
the session, and put it back afterwards.

## Three steps, and only the first is Terraform

```bash
# 1. cluster (Data API on, statement logging on), bedrock_user secret, role
terraform apply -var enable_knowledge_base_rds=true

# 2. AWS's setup SQL, then the knowledge base and data source
uv run python ../python/scripts/rds_kb.py create \
  --cluster-arn       "$(terraform output -raw kb_rds_cluster_arn)" \
  --master-secret-arn "$(terraform output -raw kb_rds_master_secret_arn)" \
  --user-secret-arn   "$(terraform output -raw kb_rds_bedrock_user_secret_arn)" \
  --role-arn          "$(terraform output -raw kb_rds_role_arn)" \
  --bucket            "$(terraform output -raw data_bucket)"

# 3. tear down, in this order
uv run python ../python/scripts/rds_kb.py delete
terraform apply -var enable_knowledge_base_rds=false
```

**Why the knowledge base is not Terraform.** The recommended layout keeps sidecar metadata in one
`custom_metadata` jsonb column, mapped with `fieldMapping.customMetadataField`. The AWS provider
added that argument in 6.22, and this repo pins `~> 5.70`. Without it, Bedrock expects one table
column per metadata attribute.

## What catches people

**The Data API is mandatory.** Bedrock reaches the table through the RDS Data API
(`enable_http_endpoint`), never over a Postgres connection. That is also why this cluster has no
ingress rule and no public address, and why `rds_kb.py` works from a laptop anyway.

**Two sets of credentials.** The master secret, which Aurora manages, runs the setup. Bedrock
gets `bedrock_user`, which owns only the `bedrock_integration` schema. `rds_kb.py` generates
that user's password and writes it straight into the secret, so it never appears on a command
line or in state.

**The table must exist first.** Bedrock checks the table and its columns when the knowledge base
is created, and the field mapping cannot be changed afterwards. The vector column's dimension
must match the embedding model: `vector(1024)` for Titan v2.

**Statement logging is on.** `log_statement = all` is exported to a CloudWatch log group with
one-day retention. This is how the capture sees Bedrock's own SQL. Set `log_statements = false`
for anything that stays up.

That log also records `rds_kb.py`'s `ALTER ROLE bedrock_user WITH PASSWORD '…'` in plain text,
and anyone who can read the log group can read the password. `capture_kb_fixtures.py` redacts it
before writing `postgresql-statements.json`. The log group itself expires after one day and is
destroyed with the stack.
