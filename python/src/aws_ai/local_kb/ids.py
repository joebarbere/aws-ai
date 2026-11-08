"""Identifiers in the formats a real knowledge base uses (local-kb/fixtures/)."""

from __future__ import annotations

import secrets
import string
import urllib.parse

_RESOURCE_ALPHABET = string.ascii_uppercase + string.digits


def resource_id() -> str:
    """A knowledge-base, data-source or ingestion-job id: 10 characters of ``[0-9A-Z]``.

    The API pattern is ``[0-9a-zA-Z]{10}``, but every id Bedrock issued in Phase 0
    (JQQRYKM3SZ, 9WZXXZH6ZH, SMH9OVRN1I) was upper case and digits.
    """
    return "".join(secrets.choice(_RESOURCE_ALPHABET) for _ in range(10))


def chunk_id() -> str:
    """A chunk's OpenSearch ``_id``, which is also its ``x-amz-bedrock-kb-chunk-id``.

    Bedrock writes the literal URL-encoded string, e.g. ``1%3A0%3ApeaYuaEBYQaVZPWvI07K`` —
    ``1:0:`` plus 20 URL-safe characters — and stores no separate chunk-id field.
    """
    return urllib.parse.quote(f"1:0:{secrets.token_urlsafe(15)}", safe="")
