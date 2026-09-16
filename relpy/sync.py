"""
Transparent backend mirroring.

:class:`BackendSyncMixin` sits in front of the existing schema and CRUD mixins.
Each public mutation still runs the original in-memory implementation first
(via ``super()``), then quietly mirrors the change to whatever backend is
attached. When the backend is the default in-memory one -- or while RelPy is
loading data *from* a backend -- every mirror step is skipped, so the pure
in-memory behaviour and performance are exactly as before.

Because RelPy always holds a full copy of every table in memory, mirroring can
always be expressed as "rebuild this table from memory" (drop, create, bulk
insert). Inserts take a faster incremental path; structural changes and
updates/deletes rebuild the affected table(s). This keeps the mirror provably
consistent without any diffing logic.
"""

from __future__ import annotations

from typing import Any, Callable

from .indexes import INTERNAL_ROW_ID


class BackendSyncMixin:
    """Mirror in-memory mutations onto the attached storage backend."""

    # These attributes are initialised by RelPy.__init__ before any of the
    # overridden methods below can run.
    _backend: Any
    _materialized: set
    _suspend_sync: bool

    # ------------------------------------------------------------------ #
    # Sync helpers
    # ------------------------------------------------------------------ #

    @property
    def _sync_enabled(self) -> bool:
        """True when changes should be mirrored to a durable backend."""
        backend = getattr(self, "_backend", None)
        if backend is None or backend.is_in_memory:
            return False
        return not getattr(self, "_suspend_sync", False)

    def _clean_row_for_backend(self, row: dict[str, Any]) -> dict[str, Any]:
        """Strip RelPy's internal row id before sending a row to the backend."""
        if INTERNAL_ROW_ID in row:
            row = {k: v for k, v in row.items() if k != INTERNAL_ROW_ID}
        return row

    def _rebuild_backend_table(self, table_name: str) -> None:
        """Recreate one table in the backend from the current in-memory state."""
        self._backend.rebuild_table(self, table_name)
        self._materialized.add(table_name)

    def _ensure_backend_table(self, table_name: str) -> None:
        """Materialize a table in the backend if it isn't there yet."""
        if table_name not in self._materialized:
            self._rebuild_backend_table(table_name)

    def flush(self) -> "BackendSyncMixin":
        """
        Force every table (schema and rows) to be written to the backend.

        Useful to persist an empty or schema-only database, or to be certain
        everything is on disk. A no-op for the in-memory backend.
        """
        if self._sync_enabled:
            for table_name in list(self.schema.keys()):
                self._rebuild_backend_table(table_name)
        return self

    #: Alias -- some users prefer db.sync() to db.flush().
    def sync(self) -> "BackendSyncMixin":
        return self.flush()

    def close(self) -> None:
        """Flush pending schema-only tables and release backend resources."""
        backend = getattr(self, "_backend", None)
        if backend is None:
            return
        try:
            if self._sync_enabled:
                # Make sure schema-only (row-less) tables exist on disk too.
                for table_name in list(self.schema.keys()):
                    self._ensure_backend_table(table_name)
        finally:
            backend.close()

    def execute_sql(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        """
        Run a raw SQL statement against the backend (SQL backends only).

        This is an escape hatch for backend-specific features; it does not touch
        RelPy's in-memory copy, so follow it with a reload if it changes data.
        """
        backend = getattr(self, "_backend", None)
        if backend is None:
            from .exceptions import BackendOperationError

            raise BackendOperationError("No backend is attached.")
        return backend.execute_sql(sql, params)

    # ------------------------------------------------------------------ #
    # Context manager sugar
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "BackendSyncMixin":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # Schema mutations (mirrored)
    # ------------------------------------------------------------------ #

    def create_table(self, table_name: str):
        result = super().create_table(table_name)
        # Backend materialization is deferred until the table has columns
        # (and usually rows); an empty column-less SQL table is invalid.
        return result

    def add_column(self, table_name: str, *args, **kwargs):
        result = super().add_column(table_name, *args, **kwargs)
        if self._sync_enabled and table_name in self._materialized:
            # A materialized table's structure changed -- rebuild it.
            self._rebuild_backend_table(table_name)
        return result

    def set_primary_key(self, table_name: str, *args, **kwargs):
        result = super().set_primary_key(table_name, *args, **kwargs)
        if self._sync_enabled and table_name in self._materialized:
            self._rebuild_backend_table(table_name)
        return result

    def create_index(self, *args, **kwargs):
        result = super().create_index(*args, **kwargs)
        if self._sync_enabled:
            index_def = list(self.indexes.values())[-1] if self.indexes else None
            if index_def is not None and index_def.table_name in self._materialized:
                # Rebuild so key columns get index-appropriate SQL types.
                self._rebuild_backend_table(index_def.table_name)
        return result

    # ------------------------------------------------------------------ #
    # Data mutations (mirrored)
    # ------------------------------------------------------------------ #

    def insert(self, table_name: str, row: dict[str, Any]) -> dict[str, Any]:
        stored = super().insert(table_name, row)
        if self._sync_enabled:
            if table_name not in self._materialized:
                self._rebuild_backend_table(table_name)
            else:
                self._backend.insert_rows(
                    self, table_name, [self._clean_row_for_backend(stored)]
                )
        return stored

    def insert_many(self, table_name: str, rows) -> list[dict[str, Any]]:
        stored_rows = super().insert_many(table_name, rows)
        if self._sync_enabled and stored_rows:
            if table_name not in self._materialized:
                self._rebuild_backend_table(table_name)
            else:
                self._backend.insert_rows(
                    self,
                    table_name,
                    [self._clean_row_for_backend(r) for r in stored_rows],
                )
        return stored_rows

    def update(
        self,
        table_name: str,
        values: dict[str, Any],
        where: Callable[[dict[str, Any]], bool] | None = None,
        allow_all: bool = False,
    ) -> int:
        count = super().update(table_name, values, where=where, allow_all=allow_all)
        if self._sync_enabled and count:
            self._rebuild_backend_table(table_name)
        return count

    def delete(
        self,
        table_name: str,
        where: Callable[[dict[str, Any]], bool] | None = None,
        allow_all: bool = False,
    ) -> int:
        count = super().delete(table_name, where=where, allow_all=allow_all)
        if self._sync_enabled and count:
            # A delete can cascade to other tables, so resync everything that
            # already lives in the backend.
            for name in list(self.schema.keys()):
                if name in self._materialized:
                    self._rebuild_backend_table(name)
        return count
