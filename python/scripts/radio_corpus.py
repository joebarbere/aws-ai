#!/usr/bin/env python3
"""The radio astronomy corpora for `local-kb bench` (local-kb/PGVECTOR_PLAN.md, §3).

    python radio_corpus.py select      pick the papers; writes local-kb/bench/radio-papers.tsv
    python radio_corpus.py fetch       download their PDFs into local-kb/.state/corpus/radio/
    python radio_corpus.py abstracts   radio-related astro-ph abstracts, for the large corpus

Only the list of arXiv ids is committed. PDFs and abstracts stay in the gitignored state
directory: they are read locally, never redistributed. Requests follow arXiv's API terms —
export.arxiv.org, one request every 3 seconds, a single connection.

- **medium** is full papers from astro-ph.IM (instrumentation and methods) whose abstracts are
  about radio telescopes: interferometry, calibration, RFI, receivers, beamforming. About 40-50
  chunks each, through the same PDF parser and chunker as any upload.
- **large** is one chunk per abstract: every radio-related paper across astro-ph, submitted in
  the date window below.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PAPERS = REPO / "local-kb" / "bench" / "radio-papers.tsv"
CORPUS = REPO / "local-kb" / ".state" / "corpus"
PDFS = CORPUS / "radio"
ABSTRACTS = CORPUS / "radio-abstracts.jsonl"

API = "https://export.arxiv.org/api/query"
PDF = "https://export.arxiv.org/pdf/{id}"
USER_AGENT = "aws-ai-local-kb-bench/0.1 (vector store benchmark; one request per 3 s)"
PAUSE = 3.0
MAX_PDF_BYTES = 20 * 2**20  # figure-heavy papers run to 30 MB+; their text is not bigger

# A fixed window, so a rerun selects the same papers.
WINDOW = "submittedDate:[201501010000 TO 202610312359]"
RADIO = (
    'abs:"radio telescope" OR abs:"radio astronomy" OR abs:"radio interferometer" OR '
    'abs:"radio interferometry" OR abs:VLBI OR abs:"radio frequency interference" OR '
    'abs:"radio array" OR abs:"21 cm"'
)
MEDIUM_QUERY = f"cat:astro-ph.IM AND ({RADIO}) AND {WINDOW}"
LARGE_QUERY = (
    "(cat:astro-ph.IM OR cat:astro-ph.GA OR cat:astro-ph.HE OR cat:astro-ph.CO OR "
    f"cat:astro-ph.SR) AND ({RADIO} OR abs:radio OR abs:pulsar) AND "
)
LARGE_YEARS = range(2015, 2027)  # one query per year keeps each under the API's 30k cap

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data: bytes = response.read()
                return data
        except OSError as exc:
            if attempt == 4:
                raise
            print(f"  retrying after {exc}", file=sys.stderr)
            time.sleep(PAUSE * (attempt + 2))
    raise AssertionError("unreachable")


def search(query: str, limit: int, page: int = 1000) -> Iterator[dict[str, Any]]:
    """Entries for ``query``, oldest first, ``page`` per request, paced at PAUSE."""
    start = 0
    while start < limit:
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": start,
                "max_results": min(page, limit - start),
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
        )
        feed = ET.fromstring(get(f"{API}?{params}"))  # noqa: S314 - arXiv's own feed
        entries = feed.findall("a:entry", NS)
        total = int(feed.findtext("os:totalResults", "0", NS))
        for entry in entries:
            versioned = entry.findtext("a:id", "", NS).rsplit("/abs/", 1)[1]
            yield {
                "id": versioned.rsplit("v", 1)[0],
                "version": versioned,
                "published": entry.findtext("a:published", "", NS)[:10],
                "category": entry.find("arxiv:primary_category", NS).get("term"),  # type: ignore[union-attr]
                "title": " ".join(entry.findtext("a:title", "", NS).split()),
                "abstract": " ".join(entry.findtext("a:summary", "", NS).split()),
            }
        start += len(entries)
        if not entries or start >= total:
            return
        time.sleep(PAUSE)


def select(count: int) -> None:
    """Every ``len(all) / count``-th matching paper, so the selection spans the window."""
    # cat: also matches cross-lists; keep papers whose home is instrumentation.
    matches = [p for p in search(MEDIUM_QUERY, 5000) if p["category"] == "astro-ph.IM"]
    step = max(1, len(matches) // count)
    chosen = matches[::step][:count]
    PAPERS.parent.mkdir(parents=True, exist_ok=True)
    with PAPERS.open("w") as out:
        out.write("# arxiv_id\tversion\tpublished\tcategory\ttitle\n")
        out.write(
            f"# query: {MEDIUM_QUERY}\n# {len(chosen)} of {len(matches)} matches, every {step}th\n"
        )
        for p in chosen:
            out.write(
                f"{p['id']}\t{p['version']}\t{p['published']}\t{p['category']}\t{p['title']}\n"
            )
    print(f"wrote {PAPERS.relative_to(REPO)}: {len(chosen)} of {len(matches)} matching papers")


def papers() -> list[dict[str, str]]:
    keys = ["id", "version", "published", "category", "title"]
    return [
        dict(zip(keys, line.rstrip("\n").split("\t"), strict=True))
        for line in PAPERS.read_text().splitlines()
        if line and not line.startswith("#")
    ]


def fetch() -> None:
    PDFS.mkdir(parents=True, exist_ok=True)
    skipped = CORPUS / "radio-skipped.txt"
    too_big = set(skipped.read_text().split()) if skipped.exists() else set()
    todo = [
        p for p in papers() if not (PDFS / f"{p['id']}.pdf").exists() and p["id"] not in too_big
    ]
    print(f"{len(todo)} to download, {PAUSE:g} s apart")
    for n, paper in enumerate(todo, 1):
        data = get(PDF.format(id=paper["version"]))
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
            with skipped.open("a") as out:
                out.write(f"{paper['id']}\n")
            print(f"[{n}/{len(todo)}] skip {paper['id']}: {len(data) / 2**20:.1f} MB")
        else:
            (PDFS / f"{paper['id']}.pdf").write_bytes(data)
            print(f"[{n}/{len(todo)}] {paper['id']} {len(data) / 2**20:.1f} MB", flush=True)
        time.sleep(PAUSE)


def abstracts() -> None:
    seen: set[str] = set()
    ABSTRACTS.parent.mkdir(parents=True, exist_ok=True)
    with ABSTRACTS.open("w") as out:
        for year in LARGE_YEARS:
            window = f"submittedDate:[{year}01010000 TO {year}12312359]"
            if year == 2026:
                window = f"submittedDate:[{year}01010000 TO {year}10312359]"
            before = len(seen)
            for entry in search(LARGE_QUERY + window, 30000, page=2000):
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"{year}: {len(seen) - before} abstracts", flush=True)
            time.sleep(PAUSE)
    print(f"wrote {ABSTRACTS.relative_to(REPO)}: {len(seen)} abstracts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["select", "fetch", "abstracts"])
    parser.add_argument("--papers", type=int, default=240, help="select: how many papers")
    args = parser.parse_args()
    if args.command == "select":
        select(args.papers)
    elif args.command == "fetch":
        fetch()
    else:
        abstracts()
    return 0


if __name__ == "__main__":
    sys.exit(main())
