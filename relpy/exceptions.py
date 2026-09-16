class RelPyError(Exception):
    """
    Base exception for all RelPy errors.
    """
    pass


class RelPyValueError(RelPyError, ValueError):
    """
    Base class for RelPy errors that behave like ValueError.
    """
    pass


class RelPyTypeError(RelPyError, TypeError):
    """
    Base class for RelPy errors that behave like TypeError.
    """
    pass


class RelPyLookupError(RelPyError, LookupError):
    """
    Base class for RelPy errors that behave like LookupError.
    """
    pass


class RelPyKeyError(RelPyError, KeyError):
    """
    Base class for RelPy errors that behave like KeyError.
    """
    pass


class SchemaError(RelPyValueError):
    """
    Raised when the schema is invalid.
    """
    pass


class TableNotFoundError(RelPyKeyError):
    """
    Raised when a table does not exist.
    """
    pass


class ColumnNotFoundError(RelPyKeyError):
    """
    Raised when a column does not exist.
    """
    pass


class ConstraintError(RelPyValueError):
    """
    Raised when a relational constraint is violated.
    """
    pass


class QueryError(RelPyValueError):
    """
    Raised when a query is invalid.
    """
    pass


class QueryTypeError(RelPyTypeError):
    """
    Raised when a query argument has an invalid type.
    """
    pass


class ViewError(RelPyValueError):
    """
    Raised when a view is invalid.
    """
    pass


class RowNotFoundError(RelPyLookupError):
    """
    Raised when a requested row does not exist.
    """
    pass


class NoRowsFoundError(RelPyLookupError):
    """
    Raised when Query.one() expected one row but found zero.
    """
    pass


class MultipleRowsFoundError(QueryError):
    """
    Raised when Query.one() expected one row but found multiple rows.
    """
    pass


class EncryptionError(RelPyValueError):
    """Raised when encryption/decryption cannot be performed."""
    pass

# =============================================================================
# Connectivity / server exceptions (RelPy server edition)
# =============================================================================
#
# These were added when RelPy grew from a purely in-memory library into one
# that can mirror its data to a real SQL database (SQLite, PostgreSQL, MySQL,
# Oracle, Azure SQL, Amazon RDS/Aurora) and expose itself over the network as
# a server. They all inherit from RelPyError so existing `except RelPyError`
# handlers keep working, while still being catchable individually.


class BackendError(RelPyError):
    """
    Base class for every error raised by a storage backend.

    A "backend" is whatever actually stores the rows: the default in-memory
    store, a SQL database reached through SQLAlchemy, or a remote RelPy server.
    """
    pass


class BackendNotAvailableError(BackendError):
    """
    Raised when a backend cannot be used because an optional dependency or
    driver is missing.

    Example:
        Selecting backend="postgres" without SQLAlchemy or psycopg installed.

    The message always explains exactly what to `pip install`.
    """
    pass


class BackendConfigurationError(BackendError, RelPyValueError):
    """
    Raised when the connection configuration is invalid, for example an
    unknown backend name, a missing url/path, or a malformed connection URL.
    """
    pass


class BackendConnectionError(BackendError):
    """
    Raised when RelPy cannot connect to (or loses its connection to) the
    underlying database or remote server.
    """
    pass


class BackendOperationError(BackendError):
    """
    Raised when an operation against the backend fails, for example a failed
    CREATE TABLE, INSERT, or reflection step. The original driver exception is
    always chained with `raise ... from original`.
    """
    pass


class ServerError(RelPyError):
    """
    Base class for errors raised by the RelPy HTTP server.
    """
    pass


class RemoteError(BackendError):
    """
    Raised on the client side when a remote RelPy server returns an error, or
    when the client cannot reach the server. The server's error type and
    message are preserved in the exception text when available.
    """
    pass
