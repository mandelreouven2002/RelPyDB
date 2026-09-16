"""
Tiny JSON-over-HTTP client used to talk to a RelPy server.

Standard library only (``urllib``), so connecting to a remote RelPy needs no
extra dependencies. Every call posts ``{"op": ..., "args": ...}`` to ``/rpc``
and expects ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": ...}``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..exceptions import BackendConnectionError, RemoteError


class RelPyHTTPClient:
    """Thin RPC client for a RelPy server."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, op: str, **args: Any) -> Any:
        """Invoke a server operation and return its result (or raise)."""
        payload = json.dumps({"op": op, "args": args}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            f"{self.base_url}/rpc", data=payload, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            if exc.code == 401:
                raise RemoteError(
                    "RelPy server rejected the request (401 Unauthorized). "
                    "Check the access token."
                ) from exc
            raise RemoteError(
                f"RelPy server returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BackendConnectionError(
                f"Could not reach RelPy server at {self.base_url}: {exc.reason}"
            ) from exc

        if not body.get("ok", False):
            error = body.get("error", {}) or {}
            raise RemoteError(
                f"{error.get('type', 'ServerError')}: "
                f"{error.get('message', 'unknown server error')}"
            )
        return body.get("result")
