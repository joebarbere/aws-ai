"""Where the local stand-ins live, and the names a knowledge base is built from.

Endpoints match local-kb/compose.yaml and the LM Studio identifiers ``make local-models`` loads.
"""

from __future__ import annotations

import os
from pathlib import Path

REGION = "us-east-1"
# The same mask the Phase 0 fixtures use, so local ARNs and fixture ARNs line up.
ACCOUNT_ID = "000000000000"

S3_ENDPOINT = "http://127.0.0.1:9000"
OPENSEARCH_ENDPOINT = "https://127.0.0.1:9200"
LM_STUDIO_ENDPOINT = "http://127.0.0.1:1234/v1"

DEFAULT_BUCKET = "kb-data"
DEFAULT_PREFIX = "kb/"

# Field names a Bedrock knowledge base expects by default (and the Phase 0 index used).
INDEX_NAME = "bedrock-knowledge-base-default-index"
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"

# The embedding model is named by an AWS-shaped ARN whose model id is the LM Studio identifier.
EMBEDDING_MODEL_ARN = f"arn:aws:bedrock:{REGION}::foundation-model/kb-embed"
EMBEDDING_DIMENSIONS = 1024


def env_file() -> Path:
    """local-kb/.env, written by ``make local-up``: the stack's generated credentials."""
    return default_state_dir().parent / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    """``KEY=value`` lines, as compose reads them. Process environment variables win."""
    target = path or env_file()
    values: dict[str, str] = {}
    if target.exists():
        for line in target.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return {**values, **{k: v for k, v in os.environ.items() if k in values}}


def default_state_dir() -> Path:
    """``$LOCAL_KB_STATE_DIR``, else ``local-kb/.state`` in the enclosing checkout, else
    ``~/.local/state/local-kb``."""
    if override := os.environ.get("LOCAL_KB_STATE_DIR"):
        return Path(override)
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "local-kb" / "compose.yaml").exists():
            return parent / "local-kb" / ".state"
    return Path.home() / ".local" / "state" / "local-kb"
