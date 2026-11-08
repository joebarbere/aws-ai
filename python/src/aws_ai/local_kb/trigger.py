"""The sync trigger: bucket changes in, ingestion jobs out.

On AWS this is wiring you build yourself — S3 event → SQS (or EventBridge) → a Lambda that calls
StartIngestionJob — because a knowledge base never syncs on its own. Here the queue is the
kb-events container, and two inputs mark a data source dirty:

- **events**, long-polled from kb-events (fast, needs bucket notifications);
- **an ETag poll** of the bucket against the ledger (slow, works on any S3 store).

A dirty data source is synced once no new event has arrived for ``debounce`` seconds, so a
burst of uploads becomes one job. If a job is already running for that knowledge base, it stays
dirty and goes next.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from aws_ai.local_kb.control import ControlPlane
from aws_ai.local_kb.errors import ConflictException
from aws_ai.local_kb.ingest import Services, has_changes, run_pending

QUEUE = "http://127.0.0.1:8098/messages"


def data_sources(cp: ControlPlane) -> list[dict[str, Any]]:
    return [
        cp.get_data_source(kb["knowledgeBaseId"], summary["dataSourceId"])
        for kb in cp.list_knowledge_bases()
        for summary in cp.list_data_sources(kb["knowledgeBaseId"])
    ]


def matches(data_source: dict[str, Any], bucket: str, key: str) -> bool:
    s3 = data_source["dataSourceConfiguration"]["s3Configuration"]
    prefix = (s3.get("inclusionPrefixes") or [""])[0]
    return bool(s3["bucketArn"] == f"arn:aws:s3:::{bucket}" and key.startswith(prefix))


def poll_queue(after: int, wait: float, url: str = QUEUE) -> list[dict[str, Any]]:
    """Records newer than ``after``; empty if the queue is unreachable (the ETag poll covers
    for it)."""
    try:
        with urllib.request.urlopen(f"{url}?after={after}&wait={wait}", timeout=wait + 10) as r:  # noqa: S310
            records: list[dict[str, Any]] = json.loads(r.read())["records"]
            return records
    except (urllib.error.URLError, TimeoutError):
        time.sleep(wait)
        return []


def watch(
    cp: ControlPlane,
    services: Services,
    *,
    poll_seconds: float = 60,
    debounce: float = 10,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
    keep_going: Callable[[], bool] = lambda: True,
    queue: Callable[[int, float], list[dict[str, Any]]] = poll_queue,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    after = max((int(r["seq"]) for r in queue(0, 0)), default=0)  # history is the poll's job
    dirty: dict[tuple[str, str], float] = {}
    next_poll = clock()
    while keep_going():
        sources = data_sources(cp)
        for record in queue(after, 2):
            after = max(after, int(record["seq"]))
            for ds in sources:
                if matches(ds, str(record["bucket"]), str(record["key"])):
                    dirty[(ds["knowledgeBaseId"], ds["dataSourceId"])] = clock()
                    log(f"{record['eventName']} {record['key']} -> {ds['dataSourceId']} dirty")

        if poll_seconds and clock() >= next_poll:
            next_poll = clock() + poll_seconds
            for ds in sources:
                ids = (ds["knowledgeBaseId"], ds["dataSourceId"])
                if ids not in dirty and has_changes(cp, services.s3, ds):
                    dirty[ids] = clock() - debounce  # nothing to wait for: the burst is over
                    log(f"ETag poll: {ds['dataSourceId']} differs from its last sync")

        by_id = {ds["dataSourceId"]: ds for ds in sources}
        for (kb_id, ds_id), last_event in list(dirty.items()):
            if clock() - last_event < debounce:
                continue
            if ds_id not in by_id or not has_changes(cp, services.s3, by_id[ds_id]):
                # Delivery is at-least-once: a late or duplicate event for changes the last
                # job already took in would otherwise start a job that finds nothing to do.
                del dirty[(kb_id, ds_id)]
                continue
            try:
                job = cp.start_ingestion_job(kb_id, ds_id, description="local-kb watch")
            except ConflictException:
                continue  # a job is running for this knowledge base; stay dirty
            del dirty[(kb_id, ds_id)]
            log(f"started ingestion job {job['ingestionJobId']} for {ds_id}")

        for done in run_pending(cp, services):
            log(
                f"ingestion job {done['ingestionJobId']} {done['status']}: "
                + ", ".join(
                    f"{k.removeprefix('numberOf')}={v}" for k, v in done["statistics"].items() if v
                )
            )
