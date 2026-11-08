"""A minimal OpenSearch client for the local cluster: JSON in, JSON out, basic auth.

Standard library only, like the scripts. The cluster's demo TLS certificate is self-signed, so
verification is off; that is acceptable only because the port is bound to 127.0.0.1.

Each thread keeps one connection open (HTTP keep-alive), as any real OpenSearch client does: a
TLS handshake per request costs more than a k-NN query on a small index, and would make the
store look slower than it is next to Postgres's persistent connection.
"""

from __future__ import annotations

import base64
import http.client
import json
import ssl
import threading
import urllib.parse
from typing import Any


class OpenSearchError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail[:500]}")
        self.status = status
        self.detail = detail


class OpenSearch:
    def __init__(self, endpoint: str, user: str, password: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        parts = urllib.parse.urlsplit(self.endpoint)
        self._https = parts.scheme == "https"
        self._host, self._port, self._base = parts.hostname or "", parts.port, parts.path
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE
        self._local = threading.local()

    def _send(
        self, method: str, path: str, data: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        """One round trip on this thread's connection; a connection the server has closed
        since its last use is replaced once."""
        for attempt in (1, 2):
            connection = getattr(self._local, "connection", None)
            if connection is None:
                connection = (
                    http.client.HTTPSConnection(
                        self._host, self._port, context=self.context, timeout=120
                    )
                    if self._https
                    else http.client.HTTPConnection(self._host, self._port, timeout=120)
                )
                self._local.connection = connection
            try:
                connection.request(method, self._base + path, body=data, headers=headers)
                response = connection.getresponse()
                return response.status, response.read()
            except (http.client.HTTPException, OSError):
                connection.close()
                self._local.connection = None
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        ndjson: list[dict[str, Any]] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> Any:
        """One request. ``ndjson`` sends a newline-delimited body (``_bulk``). A status in
        ``ok_statuses`` returns None instead of raising."""
        data: bytes | None
        if ndjson is not None:
            data = ("\n".join(json.dumps(line) for line in ndjson) + "\n").encode()
            content_type = "application/x-ndjson"
        else:
            data = json.dumps(body).encode() if body is not None else None
            content_type = "application/json"
        status, raw = self._send(method, path, data, {**self.headers, "Content-Type": content_type})
        if status >= 400:
            if status in ok_statuses:
                return None
            raise OpenSearchError(status, raw.decode(errors="replace"))
        return json.loads(raw or b"null")

    def exists(self, path: str) -> bool:
        """HEAD ``path``: True on 200, False on 404. (A 200 HEAD has no body, so ``request``'s
        None can't tell the two apart.)"""
        status, raw = self._send("HEAD", path, None, self.headers)
        if status == 404:
            return False
        if status >= 400:
            raise OpenSearchError(status, raw.decode(errors="replace"))
        return True

    def bulk(self, actions: list[dict[str, Any]]) -> None:
        """Send ``_bulk`` and raise if any item failed; OpenSearch reports item failures with
        HTTP 200 and ``errors: true``."""
        if not actions:
            return
        result = self.request("POST", "/_bulk", ndjson=actions)
        if result.get("errors"):
            failed = [i for i in result["items"] if next(iter(i.values())).get("error")]
            raise OpenSearchError(400, json.dumps(failed[:3]))

    @staticmethod
    def quote(index: str) -> str:
        return urllib.parse.quote(index, safe="")
