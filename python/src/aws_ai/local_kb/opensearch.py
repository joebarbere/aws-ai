"""A minimal OpenSearch client for the local cluster: JSON in, JSON out, basic auth.

Standard library only, like the scripts. The cluster's demo TLS certificate is self-signed, so
verification is off; that is acceptable only because the port is bound to 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class OpenSearchError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail[:500]}")
        self.status = status
        self.detail = detail


class OpenSearch:
    def __init__(self, endpoint: str, user: str, password: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.context = ssl.create_default_context()
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

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
        request = urllib.request.Request(  # noqa: S310 - endpoint comes from config
            self.endpoint + path,
            data=data,
            method=method,
            headers={**self.headers, "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=120) as response:  # noqa: S310
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            if exc.code in ok_statuses:
                return None
            raise OpenSearchError(exc.code, exc.read().decode(errors="replace")) from exc

    def exists(self, path: str) -> bool:
        """HEAD ``path``: True on 200, False on 404. (A 200 HEAD has no body, so ``request``'s
        None can't tell the two apart.)"""
        request = urllib.request.Request(  # noqa: S310 - endpoint comes from config
            self.endpoint + path, method="HEAD", headers=self.headers
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=30):  # noqa: S310
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise OpenSearchError(exc.code, exc.read().decode(errors="replace")) from exc

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
