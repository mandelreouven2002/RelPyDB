"""
Shared type- and value-coding helpers for RelPy's connectivity layer.

This module is the single place that knows how to translate between:

1. Python/RelPy logical types (int, float, str, bool, bytes, dict, list,
   datetime, date, time, and the AutoNumber marker) and stable string names,
   used by the network protocol and by SQL column creation.

2. Python values and a JSON-safe representation, used both by the RelPy HTTP
   protocol and by the portable JSON column type in the SQL backend. The
   encoding is intentionally identical to the one used by
   ``PersistenceMixin`` (``__relpy_encoded__`` envelopes) so a value looks the
   same whether it is saved to a ``.relpy.json`` file, sent over the wire, or
   stored inside a JSON/TEXT column.

3. Python/RelPy logical types and SQLAlchemy column types (only imported when
   SQLAlchemy is actually installed, so importing RelPy never requires it).

Keeping this logic in one module means the wire format, the on-disk format,
and the SQL storage format can never drift apart.
"""

from __future__ import annotations

import base64
import datetime as _dt
from typing import Any

from .schema_types import AutoNumber

# ---------------------------------------------------------------------------
# Stable type names
# ---------------------------------------------------------------------------

_NAME_BY_TYPE: dict[Any, str] = {
    AutoNumber: "AutoNumber",
    int: "int",
    float: "float",
    str: "str",
    bool: "bool",
    bytes: "bytes",
    dict: "dict",
    list: "list",
    _dt.datetime: "datetime",
    _dt.date: "date",
    _dt.time: "time",
}

_TYPE_BY_NAME: dict[str, Any] = {name: tp for tp, name in _NAME_BY_TYPE.items()}


def type_to_name(data_type: type) -> str:
    """Convert a Python/RelPy type into a stable transportable name."""
    try:
        return _NAME_BY_TYPE[data_type]
    except KeyError:
        raise ValueError(f"Unsupported RelPy column type: {data_type!r}") from None


def name_to_type(type_name: str) -> type:
    """Convert a stable transportable name back into a Python/RelPy type."""
    try:
        return _TYPE_BY_NAME[type_name]
    except KeyError:
        raise ValueError(f"Unknown RelPy type name: {type_name!r}") from None


# ---------------------------------------------------------------------------
# JSON-safe value encoding (matches PersistenceMixin's format)
# ---------------------------------------------------------------------------

def encode_value(value: Any) -> Any:
    """
    Encode a Python value into a JSON-serializable structure.

    Bytes and date/time values are wrapped in a small ``__relpy_encoded__``
    envelope; dicts and lists are encoded recursively; everything else is
    returned unchanged.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, bytes):
        return {
            "__relpy_encoded__": True,
            "type": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, _dt.datetime):
        return {"__relpy_encoded__": True, "type": "datetime", "value": value.isoformat()}
    if isinstance(value, _dt.date):
        return {"__relpy_encoded__": True, "type": "date", "value": value.isoformat()}
    if isinstance(value, _dt.time):
        return {"__relpy_encoded__": True, "type": "time", "value": value.isoformat()}
    if isinstance(value, dict):
        return {key: encode_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def decode_value(value: Any) -> Any:
    """Inverse of :func:`encode_value`."""
    if isinstance(value, dict):
        if value.get("__relpy_encoded__") is True:
            kind = value.get("type")
            if kind == "bytes":
                return base64.b64decode(value["value"].encode("ascii"))
            if kind == "datetime":
                return _dt.datetime.fromisoformat(value["value"])
            if kind == "date":
                return _dt.date.fromisoformat(value["value"])
            if kind == "time":
                return _dt.time.fromisoformat(value["value"])
            raise ValueError(f"Unknown encoded RelPy value type: {kind!r}")
        return {key: decode_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def encode_row(row: dict[str, Any]) -> dict[str, Any]:
    """Encode every value in a row dictionary."""
    return {key: encode_value(value) for key, value in row.items()}


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    """Decode every value in a row dictionary."""
    return {key: decode_value(value) for key, value in row.items()}


# ---------------------------------------------------------------------------
# SQLAlchemy column types (imported lazily)
# ---------------------------------------------------------------------------

def sqlalchemy_column_type(
    column_def: Any,
    *,
    key_like: bool = False,
):
    """
    Return the SQLAlchemy type object for a RelPy :class:`ColumnDef`.

    Args:
        column_def:
            The RelPy ColumnDef describing the column.
        key_like:
            True when the column participates in a primary key, foreign key,
            unique constraint, or index. String columns that are key-like get a
            bounded VARCHAR (portable and indexable across every dialect,
            including Oracle and MySQL); non-key string columns get TEXT so
            long free text is not truncated.

    Raises:
        BackendNotAvailableError: if SQLAlchemy is not installed.
    """
    try:
        import sqlalchemy as sa
    except ImportError as exc:  # pragma: no cover - exercised only without sqlalchemy
        from .exceptions import BackendNotAvailableError

        raise BackendNotAvailableError(
            "SQLAlchemy is required for SQL backends. Install it with "
            "`pip install sqlalchemy` (plus the driver for your database)."
        ) from exc

    storage_type = column_def.storage_type

    if storage_type is int:
        return sa.BigInteger()
    if storage_type is float:
        return sa.Float()
    if storage_type is bool:
        return sa.Boolean()
    if storage_type is bytes:
        return sa.LargeBinary()
    if storage_type is _dt.datetime:
        return sa.DateTime()
    if storage_type is _dt.date:
        return sa.Date()
    if storage_type is _dt.time:
        return sa.Time()
    if storage_type in (dict, list):
        return _json_text_type()
    if storage_type is str:
        return sa.String(255) if key_like else sa.Text()

    # Fallback: store anything else as portable JSON text.
    return _json_text_type()


def _json_text_type():
    """
    A portable "JSON stored as TEXT" SQLAlchemy type.

    Using TEXT rather than a native JSON column keeps dict/list columns working
    identically on every supported dialect, including ones with limited or
    version-dependent JSON support (notably Oracle). Values are encoded with
    the same envelope format used everywhere else in RelPy.
    """
    import json

    import sqlalchemy as sa

    class _RelPyJSONText(sa.types.TypeDecorator):
        impl = sa.Text
        cache_ok = True

        def process_bind_param(self, value, dialect):
            if value is None:
                return None
            return json.dumps(encode_value(value), ensure_ascii=False)

        def process_result_value(self, value, dialect):
            if value is None:
                return None
            return decode_value(json.loads(value))

    return _RelPyJSONText()


def python_type_from_sqlalchemy(sa_type) -> type:
    """
    Best-effort mapping from a reflected SQLAlchemy column type back to a
    Python/RelPy logical type. Used when reflecting an existing database.
    """
    import sqlalchemy as sa

    try:
        python_type = sa_type.python_type
    except (NotImplementedError, AttributeError):
        python_type = None

    if isinstance(sa_type, sa.Boolean):
        return bool
    if isinstance(sa_type, (sa.Integer, sa.BigInteger, sa.SmallInteger)):
        return int
    if isinstance(sa_type, (sa.Float, sa.Numeric)):
        return float
    if isinstance(sa_type, sa.LargeBinary):
        return bytes
    if isinstance(sa_type, sa.DateTime):
        return _dt.datetime
    if isinstance(sa_type, sa.Date):
        return _dt.date
    if isinstance(sa_type, sa.Time):
        return _dt.time
    if isinstance(sa_type, sa.JSON):
        return dict
    if isinstance(sa_type, (sa.String, sa.Text)):
        return str

    if python_type in (int, float, str, bool, bytes, _dt.datetime, _dt.date, _dt.time):
        return python_type

    # Unknown types are treated as strings so reflection never hard-fails.
    return str
