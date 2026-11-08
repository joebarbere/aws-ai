"""kb-events: the SQS queue between the bucket and the sync trigger.

RustFS delivers bucket events here as webhooks (POST /s3-events). The host-side sync trigger
polls GET /messages, as a consumer would poll SQS with ReceiveMessage, and starts an ingestion
job. Events flow in through the container network and out through a port published on
127.0.0.1, so no host process has to listen beyond loopback. RustFS's SSRF guard refuses
host.containers.internal (link-local under rootless podman) even when allow-listed.

    GET  /messages?after=<seq>&wait=<seconds>   records with seq > after, long-polled
    POST /s3-events                             RustFS webhook delivery
    HEAD /                                      RustFS's target health check

The queue is in memory: a restart loses undelivered records. That is acceptable because every
ingestion job does a full list-and-diff, and the trigger's ETag poll catches anything missed.
Standard library only, so it runs on a stock python image.
"""

import json
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_RECORDS = 10_000
MAX_WAIT_SECONDS = 20

records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
arrived = threading.Condition()
next_seq = 1


def enqueue(payload: dict[str, Any]) -> int:
    global next_seq
    added = 0
    with arrived:
        for record in payload.get("Records", []):
            s3 = record.get("s3", {})
            records.append(
                {
                    "seq": next_seq,
                    "eventName": record.get("eventName"),
                    "eventTime": record.get("eventTime"),
                    "bucket": s3.get("bucket", {}).get("name"),
                    # Keys arrive URL-encoded (kb%2Fnotes.pdf), as they do from S3.
                    "key": urllib.parse.unquote_plus(s3.get("object", {}).get("key", "")),
                    "eTag": s3.get("object", {}).get("eTag"),
                }
            )
            next_seq += 1
            added += 1
        arrived.notify_all()
    return added


def since(after: int, wait: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + wait
    with arrived:
        while True:
            found = [r for r in records if r["seq"] > after]
            remaining = deadline - time.monotonic()
            if found or remaining <= 0:
                return found
            arrived.wait(remaining)


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/s3-events":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            added = enqueue(json.loads(body))
        except (json.JSONDecodeError, AttributeError):
            self.send_error(400, "expected an S3 event notification")
            return
        self._json({"queued": added})

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path != "/messages":
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(url.query)
        after = int(query.get("after", ["0"])[0])
        wait = min(float(query.get("wait", ["0"])[0]), MAX_WAIT_SECONDS)
        self._json({"records": since(after, wait)})

    def _json(self, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    print("kb-events listening on :8098", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8098), Handler).serve_forever()  # noqa: S104 - in-container
