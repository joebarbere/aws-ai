#!/usr/bin/env python3
"""Enable the pgvector extension on the Aurora PostgreSQL cluster, and create a demo table.

`CREATE EXTENSION vector;` is SQL against a running database, so Terraform cannot do it — the
same gap as the OpenSearch Serverless index in modules/knowledge_base. This script closes it.

The master password is never passed on the command line: it is read from the Secrets Manager
secret Aurora manages, whose ARN is a Terraform output.

    python enable_pgvector.py --endpoint <writer-endpoint> \\
                              --secret-arn <aurora_secret_arn> --region us-east-1

Needs psycopg:  uv sync --extra db
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

# 1024 matches Titan Text Embeddings V2, the same width modules/knowledge_base uses, so the
# two RAG stores stay comparable.
DEFAULT_DIMENSIONS = 1024


def master_credentials(secret_arn: str, region: str) -> tuple[str, str]:
    """Read the managed master username and password out of Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=region)
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    return secret["username"], secret["password"]


def statements(dimensions: int, table: str) -> list[str]:
    """SQL to run, in order.

    The index choice matters: ivfflat is cheap to build and needs rows present before it is
    useful, while hnsw costs more to build and performs better on small result sets. Neither
    is exact — both trade recall for speed, which is the thing to understand about vector
    indexes generally.
    """
    return [
        "CREATE EXTENSION IF NOT EXISTS vector;",
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id          bigserial PRIMARY KEY,
            chunk       text NOT NULL,
            metadata    jsonb,
            embedding   vector({dimensions})
        );
        """,
        f"""
        CREATE INDEX IF NOT EXISTS {table}_embedding_idx
            ON {table} USING hnsw (embedding vector_cosine_ops);
        """,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Aurora writer endpoint")
    parser.add_argument("--secret-arn", required=True, help="Managed master password secret ARN")
    parser.add_argument("--region", required=True)
    parser.add_argument("--database", default="vectors")
    parser.add_argument("--table", default="document_chunks")
    parser.add_argument("--dimensions", type=int, default=DEFAULT_DIMENSIONS)
    args = parser.parse_args()

    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run: uv sync --extra db", file=sys.stderr)
        return 1

    username, password = master_credentials(args.secret_arn, args.region)

    conninfo = (
        f"host={args.endpoint} port=5432 dbname={args.database} "
        f"user={username} password={password} sslmode=require"
    )

    with psycopg.connect(conninfo, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            for sql in statements(args.dimensions, args.table):
                cur.execute(sql)  # type: ignore[arg-type]
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
            row = cur.fetchone()

    print(f"pgvector {row[0] if row else 'unknown'} ready")
    print(f"table {args.table} with vector({args.dimensions}) and an hnsw cosine index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
