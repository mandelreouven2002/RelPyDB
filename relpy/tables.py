from __future__ import annotations

import re
from typing import Any

from .backends import StorageBackend, create_backend
from .crud import CrudMixin
from .ddl import DDLMixin
from .encryption import EncryptionMixin
from .exceptions import BackendConfigurationError
from .export import ExportMixin
from .indexes import IndexDef, IndexMixin
from .persistence import PersistenceMixin
from .schema import SchemaMixin
from .sync import BackendSyncMixin
from ._coremirror import CoreMirrorMixin
from .schema_types import (
    AutoNumber,
    ColumnDef,
    DEFAULT_NOT_SET,
    ForeignKeyDef,
    TableDef,
    ViewDef,
)

# =============================================================================
# RelPy
# =============================================================================
#
# RelPy is an open-source Python library for defining and working with
# relational information models in memory.
#
# RelPy is not a SQL database and is not intended to replace production
# database engines such as PostgreSQL, SQLite, or DuckDB. Instead, it
# provides a Python-native way to define tables, columns, relationships, and
# constraints directly in Python code, then query, transform, and export that
# data using a fluent, chainable API.
#
# The RelPy class itself is intentionally small: it owns the in-memory state
# (schema, data, indexes, views, encryption settings) and is otherwise
# assembled from focused mixins, each implemented in its own module:
#
#   schema.py      SchemaMixin       create_table, add_column, views, schema
#                                     introspection, and shared validation
#   crud.py         CrudMixin        insert, insert_many, update, delete
#   ddl.py          DDLMixin         to_ddl, table_to_ddl (SQL DDL export)
#   export.py       ExportMixin      to_list, to_json, to_pandas, to_numpy,
#                                     to_sql, print_table
#   indexes.py      IndexMixin       create_index and index-backed lookups
#   persistence.py  PersistenceMixin save / load
#   encryption.py   EncryptionMixin  column-level encryption and blind indexes
#
# Queries themselves (where, select, order_by, joins, group_by, ...) are
# implemented separately in queries.py, grouping.py, and joins.py, and are
# returned by RelPy.query()/view() rather than being mixins of RelPy.
#
# The dataclasses describing schema metadata (ColumnDef, ForeignKeyDef,
# TableDef, ViewDef), the AutoNumber marker type, and the DEFAULT_NOT_SET
# sentinel live in schema_types.py and are re-exported here for backward
# compatibility, since other modules historically imported them from
# `relpy.tables`.


class RelPy(
    CoreMirrorMixin,
    BackendSyncMixin,
    SchemaMixin,
    DDLMixin,
    CrudMixin,
    ExportMixin,
    IndexMixin,
    PersistenceMixin,
    EncryptionMixin,
):
    """
    A lightweight, strictly typed, in-memory relational data modeling library.

    A RelPy instance holds:
    - schema: table/column/foreign-key/view definitions (SchemaMixin)
    - data: actual row storage
    - indexes: secondary indexes used to speed up equality lookups
    - optional column-level encryption settings

    On top of that foundation, RelPy supports:
    - insert / insert_many / update / delete, with foreign key and primary
      key enforcement (CrudMixin)
    - a fluent query API: where, select, order_by, limit/offset, distinct,
      group_by/aggregate/having, and joins (see queries.py, grouping.py,
      joins.py)
    - logical views defined as named queries (SchemaMixin)
    - exports to Python lists/JSON/pandas/NumPy/SQL, plus a human-readable
      table preview (ExportMixin), and SQL DDL generation (DDLMixin)
    - save/load to JSON (PersistenceMixin) and column-level encryption with
      blind indexes for equality lookups on encrypted columns
      (EncryptionMixin)
    """

    VALID_ON_DELETE_RULES = {"CASCADE", "SET NULL", "RESTRICT"}

    # Table, column, index, and view names are intentionally restricted to a
    # single conservative pattern.
    #
    # Valid examples:
    #   users
    #   user_id
    #   orders_2026
    #
    # Invalid examples:
    #   ""
    #   "user id"
    #   "123users"
    #   "users.email"
    #
    # This keeps names safe to use directly in generated SQL identifiers
    # (DDL, to_sql) and in future exports such as Mermaid ERDs, JSON Schema,
    # or Pydantic models.
    NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(
        self,
        backend: str | StorageBackend | None = None,
        *,
        url: str | None = None,
        path: str | None = None,
        encryption_key: bytes | str | None = None,
        connect_args: dict[str, Any] | None = None,
        echo: bool = False,
        **backend_options: Any,
    ):
        """
        Initializes a RelPy model.

        With no arguments, RelPy behaves exactly as before: a fast, in-memory
        relational model with no external database. Pass a ``backend`` (and its
        connection details) to make the same model durable, mirroring every
        change to a real SQL database behind the scenes. The front-facing API
        (create_table, add_column, insert, query, ...) is identical either way.

        Args:
            backend:
                Backend name or a ready StorageBackend instance. Names:
                ``"memory"`` (default), ``"sqlite"``, ``"postgres"``,
                ``"mysql"``, ``"oracle"``, ``"azure"``, ``"aws"``. ``None`` is
                the in-memory backend.

            url:
                A SQLAlchemy connection URL for SQL backends, e.g.
                ``"postgresql+psycopg://user:pass@host/db"``.

            path:
                A file path for the sqlite backend, e.g. ``"data.db"``.

            encryption_key:
                Optional key (bytes or base64 string) enabling column-level
                encryption at rest for columns added with is_encrypted=True.
                Note: encryption applies to RelPy's own in-memory/JSON storage;
                values mirrored to an external SQL backend are stored as their
                logical (decrypted) values, leaving encryption-at-rest to the
                database itself.

            connect_args:
                Optional dict passed straight to the database driver.

            echo:
                If True, SQL backends log every statement they run.

            **backend_options:
                Extra keyword arguments forwarded to the backend/engine.

        Raises:
            BackendConfigurationError: for an unknown backend or bad config.
            BackendNotAvailableError:  if a required driver is missing.
            BackendConnectionError:    if the database cannot be reached.
        """

        self.schema: dict[str, TableDef] = {}
        self.data: dict[str, list[dict[str, Any]]] = {}
        self.views: dict[str, ViewDef] = {}

        self.indexes: dict[str, IndexDef] = {}
        self._next_row_id: dict[str, int] = {}
        self._row_positions: dict[str, dict[int, int]] = {}
        self._primary_key_lookup: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}

        self._encryption_key = None
        self._fernet = None
        self._blind_index_key = None

        # --- backend / mirroring state -----------------------------------
        # Set before any mirrored method can run. _suspend_sync guards the
        # brief window while we load an existing database into memory.
        self._materialized: set[str] = set()
        self._suspend_sync: bool = False
        self._init_core_mirror()

        if backend is not None and not isinstance(backend, str):
            # A ready-made backend instance (StorageBackend or compatible).
            self._backend: StorageBackend = backend
        else:
            self._backend = create_backend(
                backend,
                url=url,
                path=path,
                connect_args=connect_args,
                echo=echo,
                **backend_options,
            )

        if encryption_key is not None:
            self.set_encryption_key(encryption_key)

        # If we connected to a real database, load whatever already exists in
        # it so the full query API works over that data immediately. Mirroring
        # is suspended during load to avoid writing the data straight back.
        if not self._backend.is_in_memory:
            self._suspend_sync = True
            try:
                self._backend.reflect_into(self)
                self._materialized = set(self.schema.keys())
            finally:
                self._suspend_sync = False

    # -------------------------------------------------------------------------
    # Connection helpers
    # -------------------------------------------------------------------------

    @classmethod
    def connect(
        cls,
        backend: str,
        *,
        url: str | None = None,
        path: str | None = None,
        encryption_key: bytes | str | None = None,
        **kwargs: Any,
    ):
        """
        Connect to a backend and return a ready-to-use RelPy database.

        This is a readable alternative to the constructor and also the entry
        point for connecting to a remote RelPy server:

            db = RelPy.connect("sqlite", path="data.db")
            db = RelPy.connect("postgres", url="postgresql+psycopg://...")
            db = RelPy.connect("mysql",  url="mysql+pymysql://user:pass@host/db")
            db = RelPy.connect("oracle", url="oracle+oracledb://user:pass@host/?service_name=FREEPDB1")
            db = RelPy.connect("remote", url="http://localhost:8000", token="secret")

        For ``"remote"``, a lightweight client is returned that talks to a
        RelPy server over HTTP while exposing the same front-facing API.
        """
        if (backend or "").strip().lower() == "remote":
            if not url:
                raise BackendConfigurationError(
                    "RelPy.connect('remote', ...) requires url=..., e.g. "
                    "url='http://localhost:8000'."
                )
            from .remote import RemoteRelPy

            return RemoteRelPy(url, encryption_key=encryption_key, **kwargs)

        return cls(
            backend,
            url=url,
            path=path,
            encryption_key=encryption_key,
            **kwargs,
        )

    @property
    def backend(self) -> StorageBackend:
        """The storage backend currently mirroring this database."""
        return self._backend

    def serve(self, host: str = "127.0.0.1", port: int = 8000, **kwargs: Any):
        """
        Expose this database over HTTP as a RelPy server (blocking call).

        Any client can then connect with
        ``RelPy.connect("remote", url="http://host:port")`` and use the normal
        RelPy API against this live database. See :mod:`relpy.server`.
        """
        from .server import serve as _serve

        return _serve(self, host=host, port=port, **kwargs)
