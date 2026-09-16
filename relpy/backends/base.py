"""
Storage backend interface.

A *backend* is the thing that actually persists RelPy's rows. RelPy always
keeps a full working copy of every table in memory (so the entire query engine
-- where/join/group_by/order_by/etc. -- is unchanged and runs locally), and a
backend is a durable mirror of that data.

Two rules keep this design simple and correct:

* RelPy's in-memory state is the source of truth *during a session*: every
  mutation is applied in memory first, then mirrored to the backend.
* Because the whole table lives in memory, any table can be rebuilt in the
  backend from scratch at any time. Hot paths (insert) mirror incrementally;
  structural changes and updates/deletes rebuild the affected table(s). This
  guarantees the mirror is always exactly consistent without complex diffing.

The default backend is :class:`MemoryBackend`, which does nothing -- it exists
so that ``RelPy()`` (pure in-memory) and ``RelPy(backend="postgres", ...)``
travel the exact same code path.
"""

from __future__ import annotations

from typing import Any


class StorageBackend:
    """
    Abstract base class for RelPy storage backends.

    Subclasses persist table schemas and rows somewhere durable. All methods
    have safe defaults here so a backend only needs to override what it
    actually supports.
    """

    #: Human-readable backend name, e.g. "sqlite" or "postgresql".
    name: str = "abstract"

    #: True if this backend keeps data only in memory (no durability).
    is_in_memory: bool = False

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Release any resources (connections, sockets, files). Idempotent."""

    # -- reflection (loading an existing database) -------------------------

    def reflect_into(self, db: Any) -> None:
        """
        Load an existing database's schema and rows into ``db``.

        Called once when connecting. The default implementation does nothing,
        which is correct for empty/in-memory backends.
        """

    # -- schema mirroring --------------------------------------------------

    def rebuild_table(self, db: Any, table_name: str) -> None:
        """
        Recreate ``table_name`` in the backend from RelPy's current schema and
        rows (drop-if-exists, create, bulk insert). Always safe to call.
        """

    def drop_table(self, table_name: str) -> None:
        """Drop ``table_name`` from the backend if it exists."""

    # -- data mirroring ----------------------------------------------------

    def insert_rows(self, db: Any, table_name: str, rows: list[dict[str, Any]]) -> None:
        """Append already-validated public rows to the backend table."""

    # -- raw access --------------------------------------------------------

    def execute_sql(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        """
        Run a raw SQL statement against the backend and return rows as dicts.

        Backends that are not SQL databases raise ``BackendOperationError``.
        """
        from ..exceptions import BackendOperationError

        raise BackendOperationError(
            f"The {self.name!r} backend does not support raw SQL execution."
        )


class MemoryBackend(StorageBackend):
    """
    The default backend: everything lives in RelPy's in-memory structures and
    nothing is mirrored anywhere. Every method is a no-op.

    This is what you get from ``RelPy()`` or ``RelPy(backend="memory")``.
    """

    name = "memory"
    is_in_memory = True
