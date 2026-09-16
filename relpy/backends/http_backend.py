"""
HTTP backend: makes a remote RelPy server look like a storage backend.

This reuses the exact same mirroring machinery as the SQL backend. A remote
RelPy client is therefore just ``RelPy`` with an :class:`HTTPBackend`: the whole
query engine runs locally on a mirror of the server's data, and every mutation
is pushed to the server, which applies it to its own (possibly SQL-backed)
database. See :class:`relpy.remote.RemoteRelPy`.
"""

from __future__ import annotations

from typing import Any

from ..indexes import INTERNAL_ROW_ID
from ..typemap import encode_row
from .base import StorageBackend
from .http_client import RelPyHTTPClient


class HTTPBackend(StorageBackend):
    """A durable mirror whose "storage" is a remote RelPy server."""

    is_in_memory = False
    name = "remote"

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0):
        self.client = RelPyHTTPClient(base_url, token=token, timeout=timeout)
        self.base_url = base_url
        # Fail fast if the server is unreachable.
        self.client.call("ping")

    def close(self) -> None:
        # HTTP is connectionless; nothing to release.
        pass

    # -- schema payload helpers -------------------------------------------

    def _table_schema_payload(self, db: Any, table_name: str) -> dict[str, Any]:
        """Serialize a single table's schema in the server's expected format."""
        full = db._serialize_schema()
        return {table_name: full[table_name]}

    def _public_rows(self, db: Any, table_name: str) -> list[dict[str, Any]]:
        rows = []
        for stored_row in db.data.get(table_name, []):
            public = db._stored_row_to_python_dict(
                stored_row, table_name=table_name, decrypt=True
            )
            public.pop(INTERNAL_ROW_ID, None)
            rows.append(encode_row(public))
        return rows

    # -- mirroring ---------------------------------------------------------

    def rebuild_table(self, db: Any, table_name: str) -> None:
        self.client.call(
            "replace_table",
            table=table_name,
            schema=self._table_schema_payload(db, table_name),
            rows=self._public_rows(db, table_name),
        )

    def drop_table(self, table_name: str) -> None:
        self.client.call("drop_table", table=table_name)

    def insert_rows(self, db: Any, table_name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self.client.call(
            "append_rows",
            table=table_name,
            rows=[encode_row(row) for row in rows],
        )

    def execute_sql(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.client.call("execute_sql", sql=sql, params=params)

    # -- reflection --------------------------------------------------------

    def reflect_into(self, db: Any) -> None:
        """Pull the server's schema and rows into the local mirror."""
        from ..server import apply_schema_payload, load_rows_into  # local import

        schema_payload = self.client.call("get_schema")
        apply_schema_payload(db, schema_payload)

        snapshot = self.client.call("snapshot")
        for table_name, encoded_rows in snapshot.items():
            if table_name in db.schema:
                load_rows_into(db, table_name, encoded_rows)

        db._rebuild_all_indexes()
        db._refresh_all_primary_key_lookups()
