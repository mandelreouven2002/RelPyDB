"""
Remote RelPy client.

``RelPy.connect("remote", url=...)`` returns a :class:`RemoteRelPy`. It behaves
like an ordinary RelPy database -- same ``create_table``/``insert``/``query``/
``update``/``delete`` API, same query engine -- but its durable "storage" is a
remote RelPy server reached over HTTP. The full table data is mirrored locally,
so every query runs with identical semantics (joins, group_by, lambdas, and
all), while every change is pushed to the server.

Because each client keeps a local mirror, call :meth:`RemoteRelPy.reload` to
pull the latest server state if other clients may have changed it.
"""

from __future__ import annotations

from typing import Any

from .backends.http_backend import HTTPBackend
from .tables import RelPy


class RemoteRelPy(RelPy):
    """A RelPy database backed by a remote RelPy server."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        encryption_key: bytes | str | None = None,
    ) -> None:
        backend = HTTPBackend(url, token=token, timeout=timeout)
        super().__init__(backend=backend, encryption_key=encryption_key)

    def reload(self) -> "RemoteRelPy":
        """
        Discard the local mirror and pull fresh schema + data from the server.

        Use this when other clients may have modified the server since this
        client connected or last reloaded.
        """
        self.schema.clear()
        self.data.clear()
        self.views.clear()
        self.indexes.clear()
        self._next_row_id.clear()
        self._row_positions.clear()
        self._primary_key_lookup.clear()
        self._materialized = set()

        self._suspend_sync = True
        try:
            self._backend.reflect_into(self)
            self._materialized = set(self.schema.keys())
        finally:
            self._suspend_sync = False
        return self
