"""The default parser: text only, normalized the way Bedrock's default parser normalizes it.

Each rule below reproduces ``AMAZON_BEDROCK_TEXT_CHUNK`` in local-kb/fixtures/index-documents.json
for the matching sample document:

- ``.txt``/``.csv``: every newline becomes a space, so a blank line becomes two spaces.
- ``.md``: all whitespace runs collapse to one space; markdown syntax (``#``, ``1.``) stays.
- ``.html``: the ``<title>``, then the body converted to markdown, whitespace collapsed.
- ``.pdf``: within a page, wrapped lines join with one space and paragraphs (a blank line in
  layout extraction) with three; pages are concatenated with no separator at all.

Sidecar ``.metadata.json`` files are parsed here too, since they are part of reading a document.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_SIDECAR_BYTES = 10 * 1024
SIDECAR_SUFFIX = ".metadata.json"
SUPPORTED = {".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".pdf"}
_RESERVED_PREFIX = "x-amz-bedrock"


class ParseError(Exception):
    """A document that cannot be ingested; becomes a ``failureReasons`` entry, not a job failure."""


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    # Offsets in ``text`` where each page begins; None for formats without pages.
    page_starts: tuple[int, ...] | None = None

    def page_at(self, offset: int) -> int | None:
        """The 1-based page a chunk starting at ``offset`` begins on."""
        if self.page_starts is None:
            return None
        return sum(1 for start in self.page_starts if start <= offset) or 1


@dataclass(frozen=True)
class Attribute:
    value: Any  # str, float, bool or list[str], as Bedrock stores them
    include_for_embedding: bool = False


@dataclass(frozen=True)
class Sidecar:
    attributes: dict[str, Attribute] = field(default_factory=dict)

    def embedding_prefix(self) -> str:
        """What Bedrock prepends to a chunk before embedding it (never to the stored text).

        Phase 0 captured exactly one case: ``"department: operations\\t\\n\\n"``. How several
        attributes, or list values, are joined is not observed; this extends the one example.
        """
        parts = [
            f"{key}: {', '.join(a.value) if isinstance(a.value, list) else a.value}\t"
            for key, a in self.attributes.items()
            if a.include_for_embedding
        ]
        return "".join(parts) + "\n\n" if parts else ""


def is_sidecar(key: str) -> bool:
    return key.endswith(SIDECAR_SUFFIX)


def sidecar_key(key: str) -> str:
    return key + SIDECAR_SUFFIX


def parse_document(key: str, data: bytes) -> ParsedDocument:
    suffix = PurePosixPath(key).suffix.lower()
    if suffix not in SUPPORTED:
        raise ParseError(f"{key}: unsupported file type {suffix or '(none)'}")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ParseError(f"{key}: larger than 50 MB")
    if suffix == ".pdf":
        return _pdf(data)
    text = data.decode("utf-8", errors="replace")
    if suffix in (".md", ".markdown"):
        return ParsedDocument(_collapse(text))
    if suffix in (".html", ".htm"):
        return ParsedDocument(_html(text))
    return ParsedDocument(text.strip().replace("\r\n", "\n").replace("\n", " "))


def parse_sidecar(key: str, data: bytes) -> Sidecar:
    """Both sidecar formats: typed values with ``includeForEmbedding``, and plain strings
    (equivalent to ``includeForEmbedding: false``)."""
    if len(data) > MAX_SIDECAR_BYTES:
        raise ParseError(f"{key}: metadata file larger than 10 KB")
    try:
        raw = json.loads(data)["metadataAttributes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ParseError(f"{key}: not a metadataAttributes document") from exc
    attributes = {}
    for name, entry in raw.items():
        if name.startswith(_RESERVED_PREFIX):
            raise ParseError(f"{key}: attribute {name} uses the reserved x-amz-bedrock prefix")
        if isinstance(entry, dict):
            attributes[name] = Attribute(
                _typed_value(key, name, entry.get("value", {})),
                bool(entry.get("includeForEmbedding", False)),
            )
        else:
            attributes[name] = Attribute(entry)
    return Sidecar(attributes)


def _typed_value(key: str, name: str, value: dict[str, Any]) -> Any:
    kind = value.get("type")
    if kind == "STRING":
        return str(value["stringValue"])
    if kind == "NUMBER":
        # Stored as a float, as Bedrock stores it (dynamic mapping then makes the field `float`).
        return float(value["numberValue"])
    if kind == "BOOLEAN":
        return bool(value["booleanValue"])
    if kind == "STRING_LIST":
        return [str(v) for v in value["stringListValue"]]
    raise ParseError(f"{key}: attribute {name} has unknown type {kind!r}")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _html(text: str) -> str:
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text() if soup.title else ""
    body = soup.body or soup
    markdown = markdownify(str(body), heading_style="ATX")
    return _collapse(f"{title} {markdown}")


def _pdf(data: bytes) -> ParsedDocument:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(io.BytesIO(data)).pages:
        layout = page.extract_text(extraction_mode="layout")
        paragraphs = [
            " ".join(
                re.sub(r"\s+", " ", line).strip() for line in block.splitlines() if line.strip()
            )
            for block in re.split(r"\n\s*\n", layout)
        ]
        pages.append("   ".join(p for p in paragraphs if p))
    starts, offset = [], 0
    for text in pages:
        starts.append(offset)
        offset += len(text)
    return ParsedDocument("".join(pages), tuple(starts))
