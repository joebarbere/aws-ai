#!/usr/bin/env python3
"""Check the object-store behaviours the local ingestion sync depends on (local-kb/PLAN.md,
Phase 1). Run after `local_init.py init`.

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
    print(f"\n{len(seen)} event(s): {sorted({n for n, _, _ in seen})}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
