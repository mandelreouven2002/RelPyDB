"""
CoreDB — the thin Python shell over RelPyDB's native C engine.

The data and the work live in C (see engine_src/_relpyengine.c): tables are
native columnar arrays, and scan / filter / aggregate / group-by run in a C
loop. This module is only the friendly API: it builds a query plan and hands it
to the C engine, then reads back the result. It reuses RelPyDB's own `col()` /
`Condition` expression objects, so the query syntax is identical to RelPy.

Supported column types: int, float, bool, str (with NULLs). Ordering, DISTINCT
and LIMIT are applied as a thin Python step over the C-produced rows; filtering,
aggregation and grouping — the data-heavy work — happen entirely in C.

Example
-------
    from relpy.core_engine import CoreDB
    from relpy import col, count, avg

    db = CoreDB()
    db.create_table("users")
    db.add_column("users", "id", int)
    db.add_column("users", "name", str)
    db.add_column("users", "age", int, nullable=True)

    db.insert_many("users", [{"name": "Ada", "age": 36}, {"name": "Bob", "age": None}])
    db.query("users").where(col("age") > 30).to_list()
    db.query("users").group_by("age").aggregate(n=count()).to_list()
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

try:
    from . import _relpyengine  # the native C columnar engine (required)
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "RelPyDB's C engine (relpy._relpyengine) is not built. RelPyDB requires "
        "its native engine — build it with `pip install .` (or, from a source "
        "checkout, `python setup.py build_ext --inplace`). A C compiler and the "
        "Python development headers are required."
    ) from exc

# Reuse RelPy's own expression + aggregate objects so the query surface is
# identical to the pure-Python engine: col(), Condition and count()/sum_()/
# avg()/min_()/max_() all work unchanged against CoreDB.
from .queries import Condition  # noqa: F401
from .grouping import AggregationSpec


_TYPECODE = {int: "i", float: "f", bool: "b", str: "s"}


def _spec_tuple(alias: str, spec: "AggregationSpec") -> tuple:
    if not isinstance(spec, AggregationSpec):
        raise TypeError(
            "aggregate() expects RelPy aggregate specs "
            "(count(), sum_(...), avg(...), min_(...), max_(...))."
        )
    return (alias, spec.function_name, spec.column_name)


class CoreDB:
    """A relational database whose storage and engine are native C."""

    def __init__(self) -> None:
        self._engine = _relpyengine.Database()
        self._columns: dict[str, list[tuple[str, type]]] = {}

    # -- schema -----------------------------------------------------------
    def create_table(self, name: str) -> None:
        self._engine.create_table(name)
        self._columns[name] = []

    def add_column(self, table: str, name: str, data_type: type,
                   *, nullable: bool = True) -> None:
        if data_type not in _TYPECODE:
            raise TypeError(
                f"CoreDB supports int, float, bool, str columns; got {data_type!r}"
            )
        self._engine.add_column(table, name, _TYPECODE[data_type])
        self._columns[table].append((name, data_type))

    # -- data -------------------------------------------------------------
    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        cols = self._columns[table]
        packed = [tuple(r.get(cn) for cn, _ in cols) for r in rows]
        self._engine.insert_many(table, packed)

    def insert(self, table: str, row: dict[str, Any]) -> None:
        self.insert_many(table, [row])

    def row_count(self, table: str) -> int:
        return self._engine.row_count(table)

    # -- query ------------------------------------------------------------
    def query(self, table: str) -> "CoreQuery":
        return CoreQuery(self, table)


class CoreQuery:
    """A fluent query that compiles to a plan executed by the C engine."""

    def __init__(self, db: CoreDB, table: str) -> None:
        self._db = db
        self._table = table
        self._condition: Condition | None = None
        self._select: list[str] | None = None
        self._order: list[tuple[str, bool]] = []
        self._limit_n: int | None = None
        self._offset_n: int = 0
        self._distinct = False

    # builder methods (return self for chaining) ------------------------
    def where(self, condition: Condition) -> "CoreQuery":
        if not isinstance(condition, Condition):
            raise TypeError("CoreDB.where expects a Condition (use col(...)).")
        if condition.plan is None:
            raise TypeError(
                "This predicate cannot run on the C engine "
                "(only structured col() comparisons are supported)."
            )
        self._condition = condition if self._condition is None else (self._condition & condition)
        return self

    def select(self, *columns: str) -> "CoreQuery":
        self._select = list(columns)
        return self

    def order_by(self, column: str, descending: bool = False) -> "CoreQuery":
        self._order.append((column, descending))
        return self

    def limit(self, n: int | None) -> "CoreQuery":
        self._limit_n = n
        return self

    def offset(self, n: int) -> "CoreQuery":
        self._offset_n = n
        return self

    def distinct(self, enabled: bool = True) -> "CoreQuery":
        self._distinct = enabled
        return self

    # terminal methods --------------------------------------------------
    @property
    def _plan(self):
        return self._condition.plan if self._condition is not None else None

    def to_list(self) -> list[dict[str, Any]]:
        rows = self._db._engine.materialize(self._table, self._plan, self._select)
        # ordering / distinct / limit are a thin Python post-step
        if self._distinct:
            seen, out = set(), []
            for r in rows:
                key = tuple(sorted(r.items()))
                if key not in seen:
                    seen.add(key)
                    out.append(r)
            rows = out
        for column, desc in reversed(self._order):
            non_null = [r for r in rows if r.get(column) is not None]
            nulls = [r for r in rows if r.get(column) is None]
            non_null.sort(key=lambda r: r[column], reverse=desc)
            rows = non_null + nulls
        if self._offset_n or self._limit_n is not None:
            end = None if self._limit_n is None else self._offset_n + self._limit_n
            rows = rows[self._offset_n:end]
        return rows

    def count(self, column: str | None = None) -> int:
        if column is None:
            return self._db._engine.filter_count(self._table, self._plan)
        agg = self._db._engine.aggregate(self._table, self._plan,
                                         [("n", "count", column)])
        return agg["n"]

    def _agg1(self, func: str, column: str):
        return self._db._engine.aggregate(self._table, self._plan,
                                          [("v", func, column)])["v"]

    def sum(self, column: str):
        return self._agg1("sum", column)

    def average(self, column: str):
        return self._agg1("avg", column)

    avg = average

    def min(self, column: str):
        return self._agg1("min", column)

    def max(self, column: str):
        return self._agg1("max", column)

    def first(self):
        rows = self.limit(1).to_list()
        return rows[0] if rows else None

    # group by ----------------------------------------------------------
    def group_by(self, *columns: str) -> "CoreGroupedQuery":
        return CoreGroupedQuery(self, list(columns))


class CoreGroupedQuery:
    def __init__(self, source: CoreQuery, group_columns: list[str]) -> None:
        self._source = source
        self._group_columns = group_columns

    def aggregate(self, **specs: "AggregationSpec") -> "CoreGroupedQuery":
        self._specs = [_spec_tuple(alias, s) for alias, s in specs.items()]
        return self

    def to_list(self) -> list[dict[str, Any]]:
        src = self._source
        return src._db._engine.group(src._table, src._plan,
                                     self._group_columns, self._specs)
