#!/usr/bin/env python3
"""Create the vector index a Bedrock knowledge base expects, inside an OpenSearch Serverless
collection.

Terraform has no resource for this: index creation is a SigV4-signed HTTP PUT against the
collection endpoint, not a control-plane API call. This script is the missing middle step
between `terraform apply` and `terraform apply -var kb_vector_index_ready=true`.

It uses botocore's signer directly, so it needs no dependency beyond boto3.

    python create_vector_index.py --endpoint https://abc123.us-east-1.aoss.amazonaws.com \
                                  --region us-east-1
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

DEFAULT_INDEX = "bedrock-knowledge-base-default-index"

# Field names Bedrock expects by default. They must match the field_mapping block in
# modules/knowledge_base/main.tf.
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"


def index_body(dimensions: int) -> dict[str, object]:
    """The k-NN index definition.

    Args:
        dimensions: Embedding width. Titan Text Embeddings V2 is 1024; Titan V1 is 1536.
            A mismatch here fails at ingestion, not at creation.
    """
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                VECTOR_FIELD: {
                    "type": "knn_vector",
                    "dimension": dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                        "parameters": {"m": 16, "ef_construction": 512},
                    },
                },
                TEXT_FIELD: {"type": "text"},
                METADATA_FIELD: {"type": "text", "index": False},
            }
        },
    }


def create_index(endpoint: str, region: str, index_name: str, dimensions: int) -> int:
    """PUT the index and return the HTTP status code.

    Returns 200 on creation. A 400 with resource_already_exists_exception means the index is
    already there, which is fine — the script is safe to re-run.
    """
    url = f"{endpoint.rstrip('/')}/{index_name}"
    payload = json.dumps(index_body(dimensions)).encode()

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise SystemExit("No AWS credentials found. Configure a profile or SSO session first.")

    request = AWSRequest(
        method="PUT", url=url, data=payload, headers={"Content-Type": "application/json"}
    )
    SigV4Auth(credentials.get_frozen_credentials(), "aoss", region).add_auth(request)

    signed = urllib.request.Request(  # noqa: S310 - fixed https endpoint from terraform output
        url, data=payload, headers=dict(request.headers), method="PUT"
    )

    try:
        with urllib.request.urlopen(signed, timeout=60) as response:  # noqa: S310
            print(f"created {index_name}: HTTP {response.status}")
            return int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        if "resource_already_exists_exception" in detail:
            print(f"{index_name} already exists — nothing to do")
            return int(exc.code)
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Collection endpoint (https://...)")
    parser.add_argument("--region", required=True, help="Region the collection lives in")
    parser.add_argument("--index-name", default=DEFAULT_INDEX)
    parser.add_argument(
        "--dimensions",
        type=int,
        default=1024,
        help="Embedding dimensions: 1024 for titan-embed-text-v2, 1536 for v1",
    )
    args = parser.parse_args()

    create_index(args.endpoint, args.region, args.index_name, args.dimensions)

    print(
        "\nNow run:\n"
        "  terraform apply -var enable_knowledge_base=true -var kb_vector_index_ready=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
