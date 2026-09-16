from __future__ import annotations

import base64
import datetime as dt
from typing import Any


# =============================================================================
# Hashable conversion
# =============================================================================
#
# RelPy rows may contain values that are not natively hashable, such as dict,
# list, and set (for example, JSON-style columns). However, several RelPy
# features need to treat full values as hashable:
#
#   - DISTINCT (Query, GroupedQuery)
#   - GROUP BY group keys (GroupedQuery)
#   - JOIN keys (joins)
#   - Index keys (IndexMixin)
#
# make_hashable() recursively converts a value into an equivalent, hashable
# representation:
#
#   dict  -> ("dict",  sorted (key, make_hashable(value)) pairs)
#   list  -> ("list",  tuple of make_hashable(item))
#   tuple -> ("tuple", tuple of make_hashable(item))
#   set   -> ("set",   sorted tuple of make_hashable(item))
#
# Any other value is wrapped as ("value", value) if it is already hashable, or
# as ("repr", repr(value)) as a last resort.
#
# This single implementation replaces four nearly identical copies that
# previously lived in queries.py, grouping.py, joins.py, and indexes.py.


def make_hashable(value: Any) -> Any:
    """
    Converts a value into a hashable representation.

    Dict keys and set members are sorted by repr() so that the resulting
    representation is stable and comparable regardless of original ordering.
    """

    # Fast path: the overwhelming majority of values are immutable scalars that
    # are already hashable. Handle them first with a cheap exact-type check,
    # before the isinstance ladder below. This is equivalent to the general
    # "try: hash(value); return ('value', value)" path but much cheaper, and it
    # accelerates every JOIN key, GROUP BY key, DISTINCT key and index key.
    value_type = type(value)
    if (
        value_type is int
        or value_type is str
        or value_type is float
        or value_type is bool
        or value_type is bytes
        or value is None
        or value_type is dt.datetime
        or value_type is dt.date
        or value_type is dt.time
    ):
        return ("value", value)

    if isinstance(value, dict):
        return (
            "dict",
            tuple(
                (key, make_hashable(value[key]))
                for key in sorted(value.keys(), key=repr)
            ),
        )

    if isinstance(value, list):
        return ("list", tuple(make_hashable(item) for item in value))

    if isinstance(value, tuple):
        return ("tuple", tuple(make_hashable(item) for item in value))

    if isinstance(value, set):
        return (
            "set",
            tuple(
                sorted(
                    (make_hashable(item) for item in value),
                    key=repr,
                )
            ),
        )

    try:
        hash(value)
        return ("value", value)
    except TypeError:
        return ("repr", repr(value))


# =============================================================================
# JSON serialization fallback
# =============================================================================
#
# Used as the `default=` callback for json.dumps() so that values which are
# not natively JSON-serializable can still be exported.
#
# This single implementation replaces three nearly identical copies that
# previously lived in tables.py, queries.py, and grouping.py.


def default_json_encoder(value: Any) -> Any:
    """
    Converts a value that json.dumps() cannot serialize by default into a
    JSON-compatible representation.

    Supported:
        bytes               -> {"__type__": "bytes", "encoding": "base64", "value": ...}
        datetime/date/time  -> ISO 8601 string

    Raises:
        TypeError: if the value has no known JSON representation.
    """

    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "encoding": "base64",
            "value": base64.b64encode(value).decode("ascii"),
        }

    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()

    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable."
    )
