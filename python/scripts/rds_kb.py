#!/usr/bin/env python3
"""Set up, or tear down, a Bedrock knowledge base on Aurora PostgreSQL + pgvector.

Run after `terraform apply -var enable_knowledge_base_rds=true`
(see terraform/modules/knowledge_base_rds). ``create`` does what Terraform can't:

1. As the master user: the SQL from AWS's setup guide — the extension, the
   ``bedrock_integration`` schema, and ``bedrock_user`` with a freshly generated password, which
   goes straight into the bedrock_user secret (never onto a command line or into state).
2. As bedrock_user: the table and its three indexes, verbatim from the guide.
3. The knowledge base, with ``customMetadataField`` (the reason this isn't Terraform: the pinned
   provider predates it), and its S3 data source.

``delete`` removes the knowledge base and data source, which must happen before
`terraform destroy` takes the cluster and role away from under them.

All SQL goes through the RDS Data API, the same way Bedrock reaches the table, so no network
path to the cluster is needed.

    uv run python scripts/rds_kb.py create --cluster-arn ... --master-secret-arn ... \\
        --user-secret-arn ... --role-arn ... --bucket aws-ai-study-data-123
    uv run python scripts/rds_kb.py delete
"""

from __future__ import annotations

import argparse
import json
import secrets
import time
from typing import Any

import boto3

NAME = "aws-ai-study-kb-rds"
DATABASE = "bedrock"
TABLE = "bedrock_integration.bedrock_kb"
DIMENSIONS = 1024

# As the master user. The password goes in as a literal because CREATE ROLE takes no
# parameters; it is generated here and is all [A-Za-z0-9_-], so there is nothing to escape.
SETUP = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE SCHEMA IF NOT EXISTS bedrock_integration",
    "DO $$ BEGIN CREATE ROLE bedrock_user LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "ALTER ROLE bedrock_user WITH PASSWORD '{password}'",
    "GRANT ALL ON SCHEMA bedrock_integration to bedrock_user",
]

# As bedrock_user — verbatim from the guide, with IF NOT EXISTS so a rerun is harmless.
TABLE_SQL = [
    f"CREATE TABLE IF NOT EXISTS {TABLE} (id uuid PRIMARY KEY, embedding vector({DIMENSIONS}), "
    "chunks text, metadata json, custom_metadata jsonb)",
    f"CREATE INDEX IF NOT EXISTS bedrock_kb_embedding_idx ON {TABLE} "
    "USING hnsw (embedding vector_cosine_ops) WITH (ef_construction=256)",
    f"CREATE INDEX IF NOT EXISTS bedrock_kb_chunks_idx ON {TABLE} "
    "USING gin (to_tsvector('simple', chunks))",
    f"CREATE INDEX IF NOT EXISTS bedrock_kb_custom_metadata_idx ON {TABLE} "
    "USING gin (custom_metadata)",
]

# pgvector 0.8.0+: keep scanning the HNSW graph until a filtered query has its k rows.
ITERATIVE_SCAN = [
    f"ALTER DATABASE {DATABASE} SET hnsw.iterative_scan = 'relaxed_order'",
    f"ALTER DATABASE {DATABASE} SET hnsw.max_scan_tuples = 20000",
]


class DataApi:
    def __init__(self, session: boto3.Session, cluster_arn: str, secret_arn: str) -> None:
        self.client = session.client("rds-data")
        self.cluster_arn = cluster_arn
        self.secret_arn = secret_arn

    def run(self, sql: str) -> list[dict[str, Any]]:
        # A cluster that has just scaled up can refuse the first call or two.
        for attempt in range(10):
            try:
                response = self.client.execute_statement(
                    resourceArn=self.cluster_arn,
                    secretArn=self.secret_arn,
                    database=DATABASE,
                    sql=sql,
                    formatRecordsAs="JSON",
                )
                break
            except self.client.exceptions.DatabaseResumingException:
                time.sleep(5 * (attempt + 1))
        else:
            raise SystemExit("The cluster did not resume.")
        records: list[dict[str, Any]] = json.loads(response.get("formattedRecords") or "[]")
        return records


def create(args: argparse.Namespace, session: boto3.Session) -> None:
    master = DataApi(session, args.cluster_arn, args.master_secret_arn)
    password = secrets.token_urlsafe(24)
    print("schema and bedrock_user (as master)")
    for sql in SETUP:
        master.run(sql.format(password=password))
    (row,) = master.run("SELECT extversion FROM pg_extension WHERE extname='vector'")
    version = row["extversion"]
    print(f"  pgvector {version}")
    if tuple(int(p) for p in version.split(".")[:2]) >= (0, 8):
        for sql in ITERATIVE_SCAN:
            master.run(sql)
        print("  iterative index scans on")

    session.client("secretsmanager").put_secret_value(
        SecretId=args.user_secret_arn,
        SecretString=json.dumps({"username": "bedrock_user", "password": password}),
    )
    print("  password stored in the bedrock_user secret")

    print("table and indexes (as bedrock_user)")
    user = DataApi(session, args.cluster_arn, args.user_secret_arn)
    for attempt in range(6):
        try:
            user.run("SELECT 1")
            break
        except user.client.exceptions.BadRequestException:
            # A new secret version takes a moment to be visible to the Data API.
            time.sleep(5 * (attempt + 1))
    for sql in TABLE_SQL:
        user.run(sql)
    for row in master.run(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='bedrock_integration'"
    ):
        print(f"  {row['indexdef']}")

    agent = session.client("bedrock-agent")
    region = session.region_name
    print("knowledge base")
    kb = agent.create_knowledge_base(
        name=NAME,
        roleArn=args.role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{region}::foundation-model/"
                "amazon.titan-embed-text-v2:0"
            },
        },
        storageConfiguration={
            "type": "RDS",
            "rdsConfiguration": {
                "resourceArn": args.cluster_arn,
                "credentialsSecretArn": args.user_secret_arn,
                "databaseName": DATABASE,
                "tableName": TABLE,
                "fieldMapping": {
                    "primaryKeyField": "id",
                    "vectorField": "embedding",
                    "textField": "chunks",
                    "metadataField": "metadata",
                    "customMetadataField": "custom_metadata",
                },
            },
        },
    )["knowledgeBase"]
    kb_id = kb["knowledgeBaseId"]
    while kb["status"] == "CREATING":
        time.sleep(5)
        kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    if kb["status"] != "ACTIVE":
        raise SystemExit(f"knowledge base {kb['status']}: {kb.get('failureReasons')}")
    ds = agent.create_data_source(
        knowledgeBaseId=kb_id,
        name=f"{NAME}-s3",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{args.bucket}",
                "inclusionPrefixes": [args.prefix],
            },
        },
    )["dataSource"]
    print(f"  knowledge base {kb_id}, data source {ds['dataSourceId']}")


def delete(_: argparse.Namespace, session: boto3.Session) -> None:
    agent = session.client("bedrock-agent")
    for summary in agent.list_knowledge_bases()["knowledgeBaseSummaries"]:
        if summary["name"] != NAME:
            continue
        kb_id = summary["knowledgeBaseId"]
        for ds in agent.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]:
            agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"])
            print(f"  deleting data source {ds['dataSourceId']}")
        agent.delete_knowledge_base(knowledgeBaseId=kb_id)
        print(f"  deleting knowledge base {kb_id}")
        while True:
            try:
                agent.get_knowledge_base(knowledgeBaseId=kb_id)
            except agent.exceptions.ResourceNotFoundException:
                break
            time.sleep(5)
        print("  deleted")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--region", default="us-east-1")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("create", help="schema, table, knowledge base and data source")
    up.add_argument("--cluster-arn", required=True, help="terraform output kb_rds_cluster_arn")
    up.add_argument("--master-secret-arn", required=True, help="kb_rds_master_secret_arn")
    up.add_argument("--user-secret-arn", required=True, help="kb_rds_bedrock_user_secret_arn")
    up.add_argument("--role-arn", required=True, help="kb_rds_role_arn")
    up.add_argument("--bucket", required=True, help="terraform output data_bucket")
    up.add_argument("--prefix", default="kb/", help="Data source inclusion prefix")
    commands.add_parser("delete", help="knowledge base and data source")
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    {"create": create, "delete": delete}[args.command](args, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
