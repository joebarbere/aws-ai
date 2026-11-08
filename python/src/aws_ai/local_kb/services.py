"""Clients for the running local stack, built from the credentials in local-kb/.env."""

from __future__ import annotations

from typing import Any

import boto3

from aws_ai.local_kb import config, index
from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.embed import lm_studio_embedder, model_id, qwen_token_counter
from aws_ai.local_kb.ingest import Services, Target
from aws_ai.local_kb.opensearch import OpenSearch

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
    )


def purger(cp: ControlPlane, services: Services) -> Any:
    """The ``purge`` callback for deletes: drop a data source's chunks from its KB's index."""

    def purge(data_source: dict[str, Any]) -> None:
        target = Target.of(cp.get_knowledge_base(data_source["knowledgeBaseId"]))
        index.delete_chunks(services.opensearch, target.index, data_source["dataSourceId"], None)

    return purge
