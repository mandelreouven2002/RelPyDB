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