"""Clients for the running local stack, built from the credentials in local-kb/.env."""

from __future__ import annotations

from typing import Any

import boto3

from aws_ai.local_kb import config
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.embed import (
    lm_studio_chat,
    lm_studio_embedder,
    model_id,
    qwen_token_counter,
)
from aws_ai.local_kb.ingest import Services
from aws_ai.local_kb.opensearch import OpenSearch
from aws_ai.local_kb.store import store_for

OPENSEARCH_USER = "bedrock-kb"  # the least-privilege user python/scripts/local_init.py creates


def local_services(env: dict[str, str] | None = None) -> Services:
    values = env if env is not None else config.load_env()
    missing = {"RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY", "BEDROCK_KB_PASSWORD"} - set(values)
    if missing:
        raise SystemExit(
            f"{config.env_file()} lacks {', '.join(sorted(missing))}. Run `make local-up` first."
        )
    s3 = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        region_name=config.REGION,
        aws_access_key_id=values["RUSTFS_ACCESS_KEY"],
        aws_secret_access_key=values["RUSTFS_SECRET_KEY"],
    )
    return Services(
        s3=s3,
        opensearch=OpenSearch(
            config.OPENSEARCH_ENDPOINT, OPENSEARCH_USER, values["BEDROCK_KB_PASSWORD"]
        ),
        embedder=lambda arn, dims: lm_studio_embedder(model_id(arn), dims),
        # Loaded on first use: only chunking needs the tokenizer, and it downloads once.
        count_tokens=lambda text: qwen_token_counter()(text),
        chat=lm_studio_chat(),
        postgres=lambda database: _postgres(database, values),
    )


def _postgres(database: str, values: dict[str, str]) -> Any:
    """A connection as bedrock_user: the role AWS's guide creates, and Bedrock's credentials
    secret names. Autocommit, as each Data API call is; the store opens transactions itself."""
    if "BEDROCK_PG_PASSWORD" not in values:
        raise SystemExit(f"{config.env_file()} lacks BEDROCK_PG_PASSWORD. Run `make local-up`.")
    import psycopg

    return psycopg.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=database,
        user=config.PG_USER,
        password=values["BEDROCK_PG_PASSWORD"],
        autocommit=True,
    )


def purger(cp: ControlPlane, services: Services) -> Any:
    """The ``purge`` callback for deletes: drop a data source's chunks from its KB's store."""

    def purge(data_source: dict[str, Any]) -> None:
        knowledge_base = cp.get_knowledge_base(data_source["knowledgeBaseId"])
        store_for(knowledge_base, services).delete_data_source(data_source["dataSourceId"])

    return purge
