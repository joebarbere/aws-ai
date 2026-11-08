#!/usr/bin/env python3
"""Check the object-store behaviours the local ingestion sync depends on (local-kb/PLAN.md,
Phase 1), then the Postgres the RDS store needs (PGVECTOR_PLAN.md). Run after
`local_init.py init`.

    python local_smoke.py

Incremental sync diffs ETags against a ledger and reacts to bucket events, so a store that
breaks any of these produces wrong ingestion counters rather than an error. Each check prints
PASS or FAIL; the exit code is the number of failures.

Events are read the way the sync trigger will read them: by polling the kb-events queue that
RustFS delivers to (S3 -> SQS -> poller).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from typing import Any

from local_init import BUCKET, PREFIX, load_env, s3_client

EVENTS = "http://127.0.0.1:8098/messages"
EVENT_WAIT_SECONDS = 20


def poll(after: int, wait: float = 0) -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"{EVENTS}?after={after}&wait={wait}", timeout=wait + 10) as r:  # noqa: S310
        records: list[dict[str, Any]] = json.loads(r.read())["records"]
        return records


def main() -> int:
    s3 = s3_client(load_env())
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else "")
        )

    inside = f"{PREFIX}smoke/sample.txt"
    outside = "smoke-outside-prefix/sample.txt"
    first, second = b"first version\n", b"second version\n"
    start = max((int(r["seq"]) for r in poll(0)), default=0)

    etag_1 = s3.put_object(Bucket=BUCKET, Key=inside, Body=first)["ETag"].strip('"')
    check(
        "single-part PUT returns the MD5 as ETag",
        etag_1 == hashlib.md5(first).hexdigest(),  # noqa: S324 - S3's ETag is MD5
        etag_1,
    )
    same = s3.put_object(Bucket=BUCKET, Key=inside, Body=first)["ETag"].strip('"')
    check("rewriting identical bytes keeps the ETag", same == etag_1, same)
    etag_2 = s3.put_object(Bucket=BUCKET, Key=inside, Body=second)["ETag"].strip('"')
    check("changed bytes change the ETag", etag_2 != etag_1, etag_2)
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX).get("Contents", [])
    listed_etag = next((o["ETag"].strip('"') for o in listed if o["Key"] == inside), None)
    check("ListObjectsV2 reports the current ETag", listed_etag == etag_2, str(listed_etag))

    s3.put_object(Bucket=BUCKET, Key=outside, Body=first)
    s3.delete_object(Bucket=BUCKET, Key=inside)
    s3.delete_object(Bucket=BUCKET, Key=outside)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    check("a deleted key disappears from ListObjectsV2", inside not in keys, str(keys))

    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + EVENT_WAIT_SECONDS
    while time.monotonic() < deadline and not any(
        str(e["eventName"]).startswith("s3:ObjectRemoved") for e in events
    ):
        events = poll(start, wait=2)
    time.sleep(2)  # let a wrongly delivered outside-prefix event land before judging
    events = poll(start)
    seen = [(str(e["eventName"]), e["key"], e.get("eTag")) for e in events]

    check(
        "event: ObjectCreated under the prefix, carrying the new ETag",
        any(n.startswith("s3:ObjectCreated") and k == inside and t == etag_2 for n, k, t in seen),
        str(seen),
    )
    check(
        "event: ObjectRemoved under the prefix",
        any(n.startswith("s3:ObjectRemoved") and k == inside for n, k, _ in seen),
        str(seen),
    )
    check(
        "no event for keys outside the prefix", not any(k == outside for _, k, _ in seen), str(seen)
    )
    print(f"\n{len(seen)} event(s): {sorted({n for n, _, _ in seen})}\n")
    return failures + postgres_checks()


def postgres_checks() -> int:
    """The RDS store's prerequisites (PGVECTOR_PLAN.md), then a write, search, sidecar rewrite
    and delete through the store itself, on a throwaway table."""
    import uuid

    from rds_kb import TABLE

    from aws_ai.local_kb import cli, config
    from aws_ai.local_kb.services import local_services
    from aws_ai.local_kb.store import StoredChunk, store_for
    from aws_ai.local_kb.store_rds import table_sql

    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else "")
        )

    services = local_services()
    db = services.postgres(config.RDS_DATABASE)  # type: ignore[misc]

    def show(setting: str) -> str:
        return str(db.execute(f"SHOW {setting}").fetchone()[0])

    version = db.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    check(
        "pgvector 0.8+, for iterative index scans",
        version is not None and tuple(map(int, version[0].split(".")[:2])) >= (0, 8),
        str(version),
    )
    check("hnsw.iterative_scan is relaxed_order", show("hnsw.iterative_scan") == "relaxed_order")
    check(
        "text search defaults to simple, as on Aurora",
        show("default_text_search_config") == "pg_catalog.simple",
        show("default_text_search_config"),
    )

    def kb(table: str, dimensions: int) -> dict[str, Any]:
        storage = cli.default_rds_storage_configuration()
        storage["rdsConfiguration"]["tableName"] = table
        return {
            "storageConfiguration": storage,
            "knowledgeBaseConfiguration": cli.default_knowledge_base_configuration(dimensions),
        }

    try:
        store_for(kb(TABLE, 1024), services).ensure()
        check(f"{TABLE} passes Bedrock's validation queries", True)
    except Exception as exc:  # noqa: BLE001
        check(f"{TABLE} passes Bedrock's validation queries", False, str(exc))

    table = f"bedrock_integration.smoke_{uuid.uuid4().hex[:8]}"
    for sql in table_sql(table, 4):
        db.execute(sql)
    try:
        store = store_for(kb(table, 4), services)
        store.ensure()
        chunk = StoredChunk(
            store.new_id(), [1.0, 0.0, 0.0, 0.0], "smoke ZX-0000", "s3://kb-data/kb/s.txt", "SMOKE"
        )
        store.write([chunk])
        hits = store.semantic([1.0, 0.0, 0.0, 0.0], 5, None)
        check(
            "a written chunk is found at once, distance 0",
            len(hits) == 1 and abs(hits[0].result["score"]) < 1e-6,
            str([h.result["score"] for h in hits]),
        )
        hits = store.hybrid("ZX-0000", [0.0, 1.0, 0.0, 0.0], 5, None)
        check("hybrid finds it by its code, score 1.0", [h.result["score"] for h in hits] == [1.0])
        store.rewrite_attributes([chunk.id], {"revision": 2})
        row = db.execute(
            f"SELECT custom_metadata FROM {table} WHERE id = %s", [chunk.id]
        ).fetchone()
        check("a sidecar rewrite keeps the id", row is not None and row[0].get("revision") == 2.0)
        store.delete_data_source("SMOKE")
        left = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        check("deleting the data source removes its rows", left == 0, str(left))
    finally:
        db.execute(f"DROP TABLE {table}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
