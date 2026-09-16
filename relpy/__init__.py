"""
RelPy
=====

RelPy is a lightweight, strictly typed, in-memory relational data modeling
library for Python. It lets you define tables, columns, primary/foreign
keys, and views, then insert/update/delete rows and run a fluent query API
(where, select, order_by, limit/offset, distinct, joins, group_by/having)
over them -- all backed by plain Python dicts and lists, with no external
database required.

RelPy also supports secondary indexes for fast equality lookups,
column-level encryption with searchable blind indexes, JSON-based
persistence (save/load), and exports to Python lists, JSON, pandas, NumPy,
SQL DDL/INSERT statements, and a human-readable table preview.

Quick start:

    from relpy import RelPy, AutoNumber, col

    db = RelPy()
    db.create_table("users")
    db.add_column("users", "id", AutoNumber, is_primary_key=True)
    db.add_column("users", "name", str)

    db.insert("users", {"name": "Ada"})
    db.query("users").where(col("name") == "Ada").to_list()

Connectivity (the "server edition"):

    The same API can be backed by a real SQL database, or served over HTTP,
    with no changes to how you define tables, insert rows, or run queries:

        db = RelPy()                                   # in-memory (default)
        db = RelPy(backend="sqlite", path="data.db")   # local file
        db = RelPy(backend="postgres", url="postgresql+psycopg://...")
        db = RelPy.connect("mysql",  url="mysql+pymysql://user:pass@host/db")
        db = RelPy.connect("oracle", url="oracle+oracledb://user:pass@host/?service_name=FREEPDB1")

        db.serve(host="0.0.0.0", port=8000)            # run as a server
        remote = RelPy.connect("remote", url="http://localhost:8000")

    Supported SQL backends: SQLite, PostgreSQL, MySQL/MariaDB, Oracle, Azure
    SQL, and Amazon RDS/Aurora (all via SQLAlchemy connection URLs).

Package layout (for anyone exploring the source):

    tables.py       The RelPy class itself: __init__ and mixin composition.
    schema_types.py Schema dataclasses (ColumnDef, ForeignKeyDef, TableDef,
                     ViewDef), AutoNumber, and the DEFAULT_NOT_SET sentinel.
    schema.py       Schema definition/introspection, views, and shared
                     validation helpers (SchemaMixin).
    crud.py         insert / insert_many / update / delete (CrudMixin).
    ddl.py          SQL DDL export: to_ddl, table_to_ddl (DDLMixin).
    export.py       to_list / to_json / to_pandas / to_numpy / to_sql /
                     print_table (ExportMixin).
    indexes.py      Secondary indexes (IndexMixin).
    persistence.py  save() / load() (PersistenceMixin).
    encryption.py   Column-level encryption and blind indexes
                     (EncryptionMixin).
    queries.py      The fluent Query API: where/select/order_by/joins/etc.
    grouping.py     GroupedQuery: group_by/aggregate/having.
    joins.py        Join implementation used by Query.join().
    hashing.py      Shared helpers for hashable row keys (DISTINCT, GROUP
                     BY, JOIN keys, indexes) and JSON export fallbacks.
    exceptions.py   The RelPy exception hierarchy.

    sync.py         BackendSyncMixin: transparently mirrors in-memory changes
                     to whatever backend is attached (write-through).
    typemap.py      Shared Python<->name, value<->JSON, and Python<->SQLAlchemy
                     type mapping used by the backends and the server.
    backends/       Storage backends: base + in-memory, the SQLAlchemy-based
                     SQL backend, the HTTP backend, and the connection factory.
    server.py       RelPyServer / serve() / make_wsgi_app(): expose a database
                     over HTTP (a Django-style "runserver" for RelPy).
    remote.py       RemoteRelPy: a client that uses the normal RelPy API
                     against a remote RelPy server.
"""

from .tables import RelPy, AutoNumber, ViewDef
from .queries import Query, Condition, ColumnRef, col, AND, OR, NOT
from .exceptions import (
    RelPyError,
    RelPyValueError,
    RelPyTypeError,
    RelPyLookupError,
    RelPyKeyError,
    SchemaError,
    TableNotFoundError,
    ColumnNotFoundError,
    ConstraintError,
    QueryError,
    QueryTypeError,
    ViewError,
    RowNotFoundError,
    NoRowsFoundError,
    MultipleRowsFoundError,
)
from .grouping import (
    GroupedQuery,
    AggregationSpec,
    count,
    sum_,
    avg,
    min_,
    max_,
)
from .indexes import IndexDef
from .exceptions import EncryptionError
from .exceptions import (
    BackendError,
    BackendNotAvailableError,
    BackendConfigurationError,
    BackendConnectionError,
    BackendOperationError,
    ServerError,
    RemoteError,
)
from .backends import StorageBackend, MemoryBackend, create_backend
from .server import RelPyServer, serve, make_wsgi_app
from .remote import RemoteRelPy

# The C engine is required. Importing these fails loudly (with build
# instructions) if the native extensions were not compiled — RelPyDB always
# runs on C.
from . import _core  # noqa: F401  (raises a clear ImportError if not built)
from .core_engine import CoreDB

__version__ = "2.0.0"

__all__ = [
    "RelPy",
    "AutoNumber",
    "ViewDef",
    "Query",
    "Condition",
    "ColumnRef",
    "col",
    "AND",
    "OR",
    "NOT",

    "RelPyError",
    "RelPyValueError",
    "RelPyTypeError",
    "RelPyLookupError",
    "RelPyKeyError",
    "SchemaError",
    "TableNotFoundError",
    "ColumnNotFoundError",
    "ConstraintError",
    "QueryError",
    "QueryTypeError",
    "ViewError",
    "RowNotFoundError",
    "NoRowsFoundError",
    "MultipleRowsFoundError",

    "GroupedQuery",
    "AggregationSpec",
    "count",
    "sum_",
    "avg",
    "min_",
    "max_",
    "IndexDef",
    "EncryptionError",

    # Connectivity / server edition
    "BackendError",
    "BackendNotAvailableError",
    "BackendConfigurationError",
    "BackendConnectionError",
    "BackendOperationError",
    "ServerError",
    "RemoteError",
    "StorageBackend",
    "MemoryBackend",
    "create_backend",
    "RelPyServer",
    "serve",
    "make_wsgi_app",
    "RemoteRelPy",
    "CoreDB",
    "__version__",
]